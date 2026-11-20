"""작업 하나를 실행하는 백그라운드 프로세스.

`coliday run` 이 별도 세션(start_new_session)으로 띄운다. 그래서 터미널을 닫거나 Ctrl+C 로
따라보기를 멈춰도 작업은 계속되고, `coliday cancel` 은 프로세스 그룹 전체를 종료한다.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from coliday import members, team
from coliday.config import Config
from coliday.events import LEAD, CodexTranslator, Translator
from coliday.store import RESULT, Store


def build_command(
    cfg: Config, agents_file: str, model: str | None, job_id: int, resume: str | None = None,
) -> list[str]:
    # 요청 본문은 stdin 으로 넘긴다 ('-' 로 시작하는 요청이 옵션으로 해석되지 않게).
    cmd = [
        cfg.claude_bin, "-p",
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", cfg.permission_mode,
        "--permission-prompts", "none",
        "--agents", agents_file,
        "--append-system-prompt", team.LEAD_PROMPT,
        "-n", f"coliday#{job_id}",
    ]
    if model:
        cmd += ["--model", model]
    if resume:
        cmd += ["--resume", resume]
    return cmd


def run_job(cfg: Config, store: Store, job_id: int) -> int:
    if not store.start_job(job_id, os.getpid()):
        return 0  # 시작 전에 취소됨
    job = store.get_job(job_id)
    job_dir = cfg.job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        resolved = team.resolve_agents(team.load_agents(cfg.agents_override), team.load_models(cfg.models_path))
    except (OSError, ValueError) as exc:  # 잘못된 사용자 설정
        store.add_event(job_id, LEAD, "error", f"에이전트·모델 설정 오류: {exc}")
        store.finish_job(job_id, "failed", phase="에이전트·모델 설정 오류")
        return 1

    # 후속 요청이 있으면 이어서 시킨 실행이다: 같은 세션을 resume 하고 로그는 덧붙인다.
    follow_up = store.last_follow_up(job_id)
    prompt = follow_up.message if follow_up else job.prompt
    resume = job.session_id if follow_up else None

    server: members.Server | None = None
    translator: Translator | CodexTranslator
    if team.is_codex(job.model):
        # Codex 팀장: 지시문을 요청 앞에 붙이고, 팀원 실행 요청은 members.Server 가 받는다.
        server = members.Server(cfg, job, resolved)
        cmd = build_codex_command(cfg, job.model, resume, server.directory)
        prompt = f"{team.codex_lead_prompt(resolved, members.agent_command(job_id))}\n# 사용자 요청\n{prompt}"
        translator = CodexTranslator(hide=members.MARKER)
    else:
        agents_file = job_dir / "agents.json"
        agents_file.write_text(
            json.dumps(team.claude_agents(resolved, cfg.codex_bin), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        cmd = build_command(cfg, str(agents_file), job.model, job_id, resume)
        translator = Translator()
    log_mode = "a" if follow_up else "w"

    stderr_path = job_dir / "stderr.log"
    try:
        with (job_dir / "stream.jsonl").open(log_mode, encoding="utf-8") as raw_log, \
                stderr_path.open(log_mode, encoding="utf-8") as err_log:
            if server:
                server.start()
            proc = subprocess.Popen(
                cmd, cwd=job.workdir, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=err_log, text=True, bufsize=1,
            )
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(prompt)
            proc.stdin.close()
            for line in proc.stdout:
                raw_log.write(line)
                raw_log.flush()
                _handle_line(store, job_id, translator, line)
            returncode = proc.wait()
    except OSError as exc:  # 실행 파일 없음 등
        store.add_event(job_id, LEAD, "error", f"{cmd[0]} 실행 실패: {exc}")
        store.finish_job(job_id, "failed", phase="팀장 실행 실패")
        return 1
    finally:
        if server:
            server.stop()

    ok = returncode == 0 and not translator.is_error
    if not ok:
        tail = _tail(stderr_path)
        store.add_event(job_id, LEAD, "error", f"종료 코드 {returncode}" + (f": {tail}" if tail else ""))
    if translator.result:  # 상태를 끝으로 바꾸기 전에 남긴다 (끝난 작업을 본 쪽은 보고도 이미 볼 수 있게)
        store.add_event(job_id, LEAD, RESULT, translator.result)
    cost = translator.cost_usd
    if server and server.cost_usd:  # Codex 팀장은 비용을 알려 주지 않는다. Claude 팀원이 쓴 비용만 더한다
        cost = (cost or 0.0) + server.cost_usd
    store.finish_job(
        job_id,
        "succeeded" if ok else "failed",
        phase="완료" if ok else "실패",
        result=translator.result,
        cost_usd=_total_cost(job.cost_usd if follow_up else None, cost),
        # 이번 실행이 세션을 만들기 전에 실패해도 이전 세션 id 는 지우지 않는다 (다시 이어갈 수 있게).
        session_id=translator.session_id or job.session_id,
    )
    return 0 if ok else 1


def build_codex_command(cfg: Config, model: str, resume: str | None, rpc: Path) -> list[str]:
    """Codex 팀장 명령. 샌드박스(workspace-write)는 유지하고 팀원 요청 폴더만 쓸 수 있게 연다."""
    cmd = [cfg.codex_bin, "exec"]
    if resume:
        cmd += ["resume", resume]
    cmd += [
        "--json", "--skip-git-repo-check",
        "-c", 'sandbox_mode="workspace-write"',  # `exec resume` 에는 -s 옵션이 없어 설정 값으로 준다
        "-c", f"sandbox_workspace_write.writable_roots=[{json.dumps(str(rpc))}]",
    ]
    if team.codex_model(model):
        cmd += ["-m", team.codex_model(model)]
    cmd.append("-")  # 요청 본문은 stdin
    return cmd


def _handle_line(store: Store, job_id: int, translator: Translator | CodexTranslator, line: str) -> None:
    line = line.strip()
    if not line:
        return
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return
    if not isinstance(raw, dict):
        return
    had_session = translator.session_id
    for ev in translator.feed(raw):
        store.add_event(job_id, ev.actor, ev.kind, ev.message)
        if ev.kind != "text":
            store.update_job(job_id, phase=f"{ev.actor}: {ev.message}")
    if translator.session_id and translator.session_id != had_session:
        store.update_job(job_id, session_id=translator.session_id)
        if translator.model:  # Codex 는 모델을 알려 주지 않는다: 설정한 값(codex[:모델])을 그대로 둔다
            store.update_job(job_id, model=translator.model)


def _total_cost(previous: float | None, current: float | None) -> float | None:
    """claude 는 실행 한 번의 비용만 보고하므로 이어서 시킨 실행은 이전 비용에 더한다."""
    if previous is None or current is None:
        return current if previous is None else previous
    return previous + current


def _tail(path, limit: int = 300) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return " ".join(text[-limit:].split())
