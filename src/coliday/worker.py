"""작업 하나를 실행하는 백그라운드 프로세스.

`coliday run` 이 별도 세션(start_new_session)으로 띄운다. 그래서 터미널을 닫거나 Ctrl+C 로
따라보기를 멈춰도 작업은 계속되고, `coliday cancel` 은 프로세스 그룹 전체를 종료한다.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from coliday import jobs, members, remote, team
from coliday.jobs import organize as jobs_organize
from coliday.config import Config
from coliday.events import LEAD, CodexTranslator, Translator
from coliday.store import RESULT, Store


def build_command(
    cfg: Config, agents_file: str, model: str | None, job_id: int, resume: str | None = None,
    lead_prompt: str | None = None, *, artifacts: str | None = None, claude_bin: str | None = None,
) -> list[str]:
    """Claude 팀장 명령. lead_prompt 를 주지 않으면 기본 팀원으로 만든 지시문을 쓴다.

    artifacts, claude_bin 은 서버에서 실행할 때 그 서버의 경로를 준다 (없으면 이 컴퓨터의 설정).
    """
    artifacts = artifacts or str(cfg.artifacts_dir(job_id))
    # 요청 본문은 stdin 으로 넘긴다 ('-' 로 시작하는 요청이 옵션으로 해석되지 않게).
    cmd = [
        claude_bin or cfg.claude_bin, "-p",
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", cfg.permission_mode,
        "--permission-prompts", "none",
        "--agents", agents_file,
        "--append-system-prompt", lead_prompt or team.lead_prompt(cfg.max_attempts, artifacts),
        "-n", f"coliday#{job_id}",
        "--add-dir", artifacts,  # 작업 디렉토리 밖이지만 결과물은 쓸 수 있게 한다
    ]
    if model:
        cmd += ["--model", model]
    if resume:
        cmd += ["--resume", resume]
    return cmd


def run_job(cfg: Config, store: Store, job_id: int) -> int:
    if not store.start_job(job_id, os.getpid()):
        return 0  # 시작 전에 취소됨
    while True:
        # 실행 중에 사용자가 보낸 요청이 대기하고 있으면 끝내지 않고 같은 세션으로 한 번 더 실행한다.
        code = _run_turn(cfg, store, job_id)
        if code is not None:
            return code


def _run_turn(cfg: Config, store: Store, job_id: int) -> int | None:
    """팀장을 한 번 실행한다. 작업이 끝났으면 종료 코드, 대기 요청이 있어 이어서 실행해야 하면 None."""
    job = store.get_job(job_id)
    job_dir = cfg.job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    cfg.artifacts_dir(job_id).mkdir(exist_ok=True)  # 스크린샷·영상을 남길 곳. 이어서 시켜도 같은 폴더에 쌓인다
    artifacts = str(cfg.artifacts_dir(job_id))
    host, remote_workdir = remote.split(job.workdir)
    remote_dir = ""
    if host is not None:
        # 서버의 프로젝트: 에이전트는 서버에서 돌고 결과물도 서버에 쌓인다 (끝나면 이 컴퓨터로 가져온다).
        try:
            if team.is_codex(job.model):
                raise remote.RemoteError("SSH 프로젝트에서는 Codex 팀장을 쓸 수 없습니다. 팀장을 Claude 로 두세요.")
            remote_dir = jobs.remote_job_dir(host, job_id)
            artifacts = f"{remote_dir}/artifacts"
        except remote.RemoteError as exc:
            store.add_event(job_id, LEAD, "error", str(exc))
            return 1 if store.finish_or_continue(job_id, "failed", phase="서버 접속 실패") else None

    try:
        custom = team.load_custom(cfg.team_path)
        resolved = team.resolve_agents(
            team.load_agents(cfg.agents_override, custom), team.load_models(cfg.models_path), cfg.max_attempts, artifacts
        )
    except (OSError, ValueError) as exc:  # 잘못된 사용자 설정
        store.add_event(job_id, LEAD, "error", f"팀·모델 설정 오류: {exc}")
        return 1 if store.finish_or_continue(job_id, "failed", phase="팀·모델 설정 오류") else None

    # 후속 요청이 있으면 이어서 시킨 실행이다: 같은 세션을 resume 하고 로그는 덧붙인다.
    follow_up = store.last_follow_up(job_id)
    prompt = follow_up.message if follow_up else job.prompt
    resume = job.session_id if follow_up else None

    server: members.Server | None = None
    translator: Translator | CodexTranslator
    if team.is_codex(job.model):
        # Codex 팀장: 지시문을 요청 앞에 붙이고, 팀원 실행 요청은 members.Server 가 받는다.
        server = members.Server(cfg, job, resolved)
        cmd = build_codex_command(cfg, job.model, resume, server.directory, artifacts)
        lead = team.codex_lead_prompt(
            resolved, members.agent_command(job_id), cfg.max_attempts, artifacts, custom.lead_prompt
        )
        prompt = f"{lead}\n# 사용자 요청\n{prompt}"
        translator = CodexTranslator(hide=members.MARKER)
    else:
        agents_file = job_dir / "agents.json"
        codex_bin = jobs.remote_codex_bin() if host is not None else cfg.codex_bin
        try:
            has_codex = not remote.missing_commands(host, codex_bin) if host is not None else bool(shutil.which(codex_bin))
        except remote.RemoteError as exc:
            store.add_event(job_id, LEAD, "error", str(exc))
            return 1 if store.finish_or_continue(job_id, "failed", phase="서버 접속 실패") else None
        if not has_codex:
            # Codex CLI 가 없어도 작업은 된다: Codex 로 두었던 팀원을 Claude 로 돌린다 (없는 명령을 부르다 실패하지 않게).
            resolved, switched = team.without_codex(resolved)
            if switched:
                where = f"{host} 에" if host is not None else "이 컴퓨터에"
                store.add_event(
                    job_id, LEAD, "phase",
                    f"{where} codex 가 없어 {', '.join(switched)} 은(는) Claude 로 실행합니다",
                )
        agents_json = json.dumps(
            team.claude_agents(resolved, codex_bin, artifacts, local=host is None), ensure_ascii=False, indent=2
        )
        agents_file.write_text(agents_json, encoding="utf-8")  # 서버에서 실행해도 무엇을 보냈는지 여기 남긴다
        lead_prompt = team.lead_prompt(cfg.max_attempts, artifacts, resolved, custom.lead_prompt)
        if host is not None:
            try:
                remote.check(
                    host,
                    f"mkdir -p {shlex.quote(artifacts)} && cat > {shlex.quote(remote_dir + '/agents.json')}",
                    stdin=agents_json,
                )
            except remote.RemoteError as exc:
                store.add_event(job_id, LEAD, "error", str(exc))
                return 1 if store.finish_or_continue(job_id, "failed", phase="서버 접속 실패") else None
            inner = build_command(
                cfg, f"{remote_dir}/agents.json", job.model, job_id, resume, lead_prompt,
                artifacts=artifacts, claude_bin=jobs.remote_claude_bin(),
            )
            # 프로세스 번호를 남겨 두고(취소할 때 쓴다) 그 프로세스가 그대로 에이전트가 된다.
            cmd = remote.command(
                host,
                f"cd {shlex.quote(remote_workdir)} && echo $$ > {shlex.quote(remote_dir + '/pid')} && exec {shlex.join(inner)}",
            )
        else:
            cmd = build_command(cfg, str(agents_file), job.model, job_id, resume, lead_prompt)
        translator = Translator()
    log_mode = "a" if follow_up else "w"

    stderr_path = job_dir / "stderr.log"
    try:
        with (job_dir / "stream.jsonl").open(log_mode, encoding="utf-8") as raw_log, \
                stderr_path.open(log_mode, encoding="utf-8") as err_log:
            if server:
                server.start()
            proc = subprocess.Popen(
                cmd, cwd=None if host is not None else job.workdir, stdin=subprocess.PIPE,
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
        return 1 if store.finish_or_continue(job_id, "failed", phase="팀장 실행 실패") else None
    finally:
        if server:
            server.stop()

    ok = returncode == 0 and not translator.is_error
    if not ok:
        tail = _tail(stderr_path)
        store.add_event(job_id, LEAD, "error", f"종료 코드 {returncode}" + (f": {tail}" if tail else ""))
    if translator.result:  # 상태를 끝으로 바꾸기 전에 남긴다 (끝난 작업을 본 쪽은 보고도 이미 볼 수 있게)
        store.add_event(job_id, LEAD, RESULT, translator.result)
    if host is not None:
        if returncode == 255 and not translator.result:  # ssh 가 끊겼다: 서버의 에이전트가 남아 돌지 않게 한다
            store.add_event(job_id, LEAD, "error", f"{host} 와의 SSH 연결이 끊겼습니다. 서버의 에이전트를 멈춥니다.")
            remote.spawn(host, jobs.stop_script(job_id))
        _fetch_artifacts(store, job_id, host, artifacts, cfg.artifacts_dir(job_id))
    jobs_organize(cfg.artifacts_dir(job_id))  # 팀장이 멈춘 지금, 사진·영상을 한 폴더에 늘어놓는다
    cost = translator.cost_usd
    if server and server.cost_usd:  # Codex 팀장은 비용을 알려 주지 않는다. Claude 팀원이 쓴 비용만 더한다
        cost = (cost or 0.0) + server.cost_usd
    finished = store.finish_or_continue(
        job_id,
        "succeeded" if ok else "failed",
        phase="완료" if ok else "실패",
        result=translator.result,
        cost_usd=_total_cost(job.cost_usd if follow_up else None, cost),
        # 이번 실행이 세션을 만들기 전에 실패해도 이전 세션 id 는 지우지 않는다 (다시 이어갈 수 있게).
        session_id=translator.session_id or job.session_id,
    )
    return (0 if ok else 1) if finished else None


def _fetch_artifacts(store: Store, job_id: int, host: str, source: str, target: Path) -> None:
    """서버에 쌓인 결과물을 이 컴퓨터의 결과물 폴더로 가져온다 (화면과 `coliday open` 은 이 폴더를 본다).

    서버의 폴더가 원본이라 매번 통째로 다시 받는다 (받은 뒤 정리하면서 이름이 바뀌어 겹쳐 받을 수 없다).
    받지 못하면 이전에 받은 것을 그대로 둔다.
    """
    fresh = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    shutil.rmtree(fresh, ignore_errors=True)
    try:
        fresh.mkdir(parents=True)
        remote.fetch_dir(host, source, fresh)
        shutil.rmtree(target, ignore_errors=True)
        fresh.rename(target)
    except (remote.RemoteError, OSError) as exc:
        shutil.rmtree(fresh, ignore_errors=True)
        store.add_event(job_id, LEAD, "error", f"결과물을 가져오지 못했습니다 (서버의 {source} 에 남아 있습니다): {exc}")


def build_codex_command(cfg: Config, model: str, resume: str | None, rpc: Path, artifacts: str) -> list[str]:
    """Codex 팀장 명령. 샌드박스(workspace-write)는 유지하고 팀원 요청 폴더와 결과물 폴더만 쓸 수 있게 연다."""
    cmd = [cfg.codex_bin, "exec"]
    if resume:
        cmd += ["resume", resume]
    cmd += [
        "--json", "--skip-git-repo-check",
        "-c", 'sandbox_mode="workspace-write"',  # `exec resume` 에는 -s 옵션이 없어 설정 값으로 준다
        *team.codex_writable_roots(rpc, artifacts),
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
