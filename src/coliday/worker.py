"""작업 하나를 실행하는 백그라운드 프로세스.

`coliday run` 이 별도 세션(start_new_session)으로 띄운다. 그래서 터미널을 닫거나 Ctrl+C 로
따라보기를 멈춰도 작업은 계속되고, `coliday cancel` 은 프로세스 그룹 전체를 종료한다.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

from coliday import guard, hooks, members, remote, review_gate, team, topics
from coliday.jobs import one_line, organize as jobs_organize
from coliday.config import Config
from coliday.events import LEAD, CodexTranslator, Translator
from coliday.store import RESULT, Store


_CLAUDE_BG_WAIT_ENV = "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"
_CLAUDE_BG_WAIT_DEFAULT_MS = "3600000"
_CLAUDE_BG_WAIT_WARNING = re.compile(
    rb"Background tasks still running .+? after the last turn \(.*\); stopping them\. "
    rb"Set CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 to wait indefinitely\."
)


def build_command(
    cfg: Config, agents_file: str, model: str | None, job_id: int, resume: str | None = None,
    lead_prompt: str | None = None, *,
    allow: list[str] | None = None, add_dirs: list[str] | None = None, settings: str | None = None,
) -> list[str]:
    """Claude 팀장 명령. lead_prompt 를 주지 않으면 기본 팀원으로 만든 지시문을 쓴다.

    allow 는 권한 검사 없이 실행할 도구 규칙(Codex 팀원 실행 파일, 서버를 다루는 실행 파일),
    add_dirs 는 더 열어 둘 폴더다 (Codex 팀원 실행 파일에 줄 명세를 쓰는 곳).
    """
    artifacts = str(cfg.artifacts_dir(job_id))
    # 요청 본문은 stdin 으로 넘긴다 ('-' 로 시작하는 요청이 옵션으로 해석되지 않게).
    cmd = [
        cfg.claude_bin, "-p",
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
    for directory in add_dirs or []:
        cmd += ["--add-dir", directory]
    if settings:  # .env 읽기를 막는 규칙과 샌드박스 (guard.py), PreToolUse 훅 (hooks.py)
        cmd += ["--settings", settings]
    if allow:
        cmd += ["--allowedTools", *allow]
    return cmd


def run_job(cfg: Config, store: Store, job_id: int) -> int:
    if not store.start_job(job_id, os.getpid()):
        return 0  # 시작 전에 취소됨
    while True:
        # 실행 중에 사용자가 보낸 요청이 대기하고 있으면 끝내지 않고 같은 세션으로 한 번 더 실행한다.
        code = _run_turn(cfg, store, job_id)
        if code is not None:
            # 작업은 이미 끝난 상태다 (사용자는 결과를 본다). 그 뒤에 비슷한 작업끼리 자동 묶음으로 묶는다.
            topics.classify_quietly(cfg, store, job_id)
            return code


def _run_turn(cfg: Config, store: Store, job_id: int) -> int | None:
    """팀장을 한 번 실행한다. 작업이 끝났으면 종료 코드, 대기 요청이 있어 이어서 실행해야 하면 None."""
    job = store.get_job(job_id)
    job_dir = cfg.job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    cfg.artifacts_dir(job_id).mkdir(exist_ok=True)  # 스크린샷·영상을 남길 곳. 이어서 시켜도 같은 폴더에 쌓인다
    artifacts = str(cfg.artifacts_dir(job_id))
    host, remote_workdir = remote.split(job.workdir)
    workdir = job.workdir  # 에이전트가 실행되는 이 컴퓨터의 폴더
    helpers: list[str] = []  # 서버의 프로젝트를 다루는 실행 파일 (권한 검사와 샌드박스를 거치지 않게 연다)
    server_note = ""
    if host is not None:
        # 서버의 프로젝트: 에이전트는 이 컴퓨터에서 돌고, 아래 실행 파일로 ssh 를 거쳐 서버의 파일과 명령을 다룬다.
        workdir = str(job_dir / "work")
        try:
            scripts = remote.helper_scripts(host, remote_workdir, workdir)
            Path(workdir).mkdir(exist_ok=True)
            (job_dir / "server").mkdir(exist_ok=True)
            paths = {name: str(job_dir / "server" / name) for name in scripts}
            for name, script in scripts.items():
                Path(paths[name]).write_text(script, encoding="utf-8")
                Path(paths[name]).chmod(0o700)
        except (remote.RemoteError, OSError) as exc:
            store.add_event(job_id, LEAD, "error", f"서버 작업을 준비하지 못했습니다: {exc}")
            return 1 if store.finish_or_continue(job_id, "failed", phase="서버 작업 준비 실패", **_env_note_drop(None)) else None
        helpers = list(paths.values())
        server_note = remote.agent_note(host, remote_workdir, workdir, paths)
        store.add_event(job_id, LEAD, "phase", f"이 컴퓨터의 에이전트가 SSH 로 {host} 의 {remote_workdir} 에서 작업합니다")

    try:
        custom = team.load_custom(cfg.team_path)
        resolved = team.resolve_agents(
            team.load_agents(cfg.agents_override, custom), team.load_models(cfg.models_path), cfg.max_attempts, artifacts
        )
    except (OSError, ValueError) as exc:  # 잘못된 사용자 설정
        store.add_event(job_id, LEAD, "error", f"팀·모델 설정 오류: {exc}")
        return 1 if store.finish_or_continue(job_id, "failed", phase="팀·모델 설정 오류", **_env_note_drop(None)) else None
    if host is not None:
        resolved = team.for_server(resolved, server_note)

    # 사람이 이 작업에 허용하지 않았으면 에이전트가 .env 파일을 읽지 못하게 막는다.
    env_guard: guard.EnvGuard | None = None
    if store.allow_env(job_id):
        store.add_event(job_id, LEAD, "phase", "사람이 허용한 작업입니다: 에이전트가 .env 파일을 읽을 수 있습니다")
    else:
        env_guard = guard.for_job(workdir, workdir if host is not None else job.repo, str(Path.home()), local=True)
        where = "이 컴퓨터의 .env 파일은" if host is not None else ".env 파일은"
        limit = " 서버의 파일은 막지 못합니다." if host is not None else ""
        store.add_event(
            job_id, LEAD, "phase",
            f"{where} 읽지 못하게 막고 실행합니다 (도중에 허용하려면 TUI 에서 Ctrl+L, 또는 coliday env {job_id} allow).{limit}",
        )
    env_rule = ""
    if env_guard is not None:
        env_rule = guard.LEAD_RULE if host is None else guard.LOCAL_ONLY_RULE
        sandbox_note = guard.sandbox_note()  # 샌드박스 안에서 달라지는 점 (JDK 찾기)
        env_rule += sandbox_note
        resolved = team.with_note(resolved, sandbox_note)

    # 리뷰 담당 호출 수는 실행(요청)마다 새로 센다. 상한을 넘으면 그 호출을 막는다 (review_gate).
    review_counter = job_dir / "review-count.json"
    review_gate.reset(review_counter)

    # 후속 요청이 있으면 이어서 시킨 실행이다: 같은 세션을 resume 하고 로그는 덧붙인다.
    follow_up = store.last_follow_up(job_id)
    prompt = follow_up.message if follow_up else _derived_note(store, job_id) + job.prompt
    resume = job.session_id if follow_up else None

    server: members.Server | None = None
    translator: Translator | CodexTranslator
    is_codex = team.is_codex(job.model)
    if is_codex:
        # Codex 팀장: 지시문을 요청 앞에 붙이고, 팀원 실행 요청은 members.Server 가 받는다.
        server = members.Server(
            cfg, job, resolved, env_guard, cwd=workdir, helpers=helpers, review_counter=review_counter,
        )
        cmd = build_codex_command(cfg, job.model, resume, server.directory, artifacts, env_guard, network=host is not None)
        lead = team.codex_lead_prompt(
            resolved, members.agent_command(job_id), cfg.max_attempts, artifacts, custom.lead_prompt
        ) + env_rule + server_note
        prompt = f"{lead}\n# 사용자 요청\n{prompt}"
        translator = CodexTranslator(hide=members.MARKER)
    else:
        agents_file = job_dir / "agents.json"
        has_codex = bool(shutil.which(cfg.codex_bin))
        reason = "이 컴퓨터에 codex 가 없어"
        if has_codex and env_guard is not None and any(team.is_codex(s.get("model")) for s in resolved.values()):
            # 낮은 버전의 Codex 는 .env 를 막는 설정을 모른다: 막지 못한 채 돌리지 않는다.
            version = guard.local_version(cfg.codex_bin)
            if version is None or version < guard.MIN_CODEX:
                has_codex = False
                reason = f"이 컴퓨터에 있는 codex({guard.show(version)})가 .env 읽기를 막지 못해"
        if not has_codex:
            # Codex CLI 가 없어도 작업은 된다: Codex 로 두었던 팀원을 Claude 로 돌린다 (없는 명령을 부르다 실패하지 않게).
            resolved, switched = team.without_codex(resolved)
            if switched:
                store.add_event(job_id, LEAD, "phase", f"{reason} {', '.join(switched)} 은(는) Claude 로 실행합니다")
        # Codex 팀원은 실행 파일 하나씩으로 만들어 두고 그 파일만 허용한다 (auto 모드 분류기가 codex 실행을 막지 않게).
        runner_dir = str(job_dir / "codex")
        made = team.codex_runners(resolved, cfg.codex_bin, artifacts, guard=env_guard, network=host is not None)
        runners = {role: f"{runner_dir}/{file_name}" for role, (file_name, _) in made.items()}
        scripts = {runners[role]: script for role, (_, script) in made.items()}
        allow = [team.runner_rule(path) for path in [*scripts, *helpers]]
        # 명세 파일은 실행 파일과 다른 폴더에 둔다: 에이전트가 마음대로 쓸 수 있는 곳에 허용된 실행 파일을 두지 않는다.
        tasks_dir = f"{runner_dir}-tasks"
        add_dirs = [tasks_dir] if scripts else []
        settings_obj: dict = {}
        if env_guard is not None:
            settings_obj = json.loads(env_guard.claude_settings(
                writable=[artifacts, *add_dirs, *env_guard.roots[:2]],  # 홈 밖에 있는 레포에서도 커밋할 수 있게
                protected=cfg.protected_paths,
                # Codex 는 자기 샌드박스를 따로 쓴다 (샌드박스 안에서 또 샌드박스를 켤 수 없다). .env 는 Codex 쪽 설정이 막는다.
                # 서버를 다루는 실행 파일은 ssh 연결(네트워크, ~/.ssh)이 필요하다.
                excluded=[f"{team.runner_command(path)} *" for path in [*scripts, *helpers]],
            ))
        # 리뷰 상한과 Codex 래퍼의 명령 제한은 .env 를 허용한 작업에도 건다 (hooks.py)
        hooks_config = job_dir / "hooks.json"
        hooks.write_config(hooks_config, review_counter=review_counter, wrappers=runners, tasks_dir=tasks_dir)
        settings_obj |= hooks.claude_hooks(hooks_config)
        settings = json.dumps(settings_obj, ensure_ascii=False)
        agents_json = json.dumps(team.claude_agents(resolved, runners, tasks_dir), ensure_ascii=False, indent=2)
        agents_file.write_text(agents_json, encoding="utf-8")
        lead_prompt = team.lead_prompt(cfg.max_attempts, artifacts, resolved, custom.lead_prompt) + env_rule + server_note
        for path, script in scripts.items():
            Path(path).parent.mkdir(exist_ok=True)
            Path(tasks_dir).mkdir(exist_ok=True)
            Path(path).write_text(script, encoding="utf-8")
            Path(path).chmod(0o700)
        cmd = build_command(
            cfg, str(agents_file), job.model, job_id, resume, lead_prompt, allow=allow, add_dirs=add_dirs,
            settings=settings,
        )
        translator = Translator()
    log_mode = "a" if follow_up else "w"

    stderr_path = job_dir / "stderr.log"
    keeper = remote.keep_alive(host) if host is not None else None  # 에이전트의 명령이 매번 새로 접속하지 않게
    try:
        with (job_dir / "stream.jsonl").open(log_mode, encoding="utf-8") as raw_log, \
                stderr_path.open(log_mode + "b") as err_log:
            stderr_start = err_log.tell()  # resume 로그에서는 이번 실행이 추가한 바이트만 검사한다
            if server:
                server.start()
            env = members.agent_env(env_guard)
            if not is_codex:
                env = dict(os.environ if env is None else env)
                env.setdefault(_CLAUDE_BG_WAIT_ENV, _CLAUDE_BG_WAIT_DEFAULT_MS)
            proc = subprocess.Popen(
                cmd, cwd=workdir, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=err_log, text=True, bufsize=1,
                env=env,
            )
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(prompt)
            proc.stdin.close()
            told_blocked = env_guard is None  # 막힌 읽기는 실행마다 한 번만 알린다
            blocked = False
            for line in proc.stdout:
                raw_log.write(line)
                raw_log.flush()
                _handle_line(store, job_id, translator, line)
                if not told_blocked and ".env" in line and _blocked_read(line):
                    told_blocked = blocked = True
                    store.add_event(
                        job_id, LEAD, "error",
                        f"에이전트가 .env 파일을 읽으려다 막혔습니다. 읽게 하려면 이 작업에 허용하세요: "
                        f"TUI 에서 Ctrl+L, 또는 coliday env {job_id} allow",
                    )
            returncode = proc.wait()
    except OSError as exc:  # 실행 파일 없음 등
        store.add_event(job_id, LEAD, "error", f"{cmd[0]} 실행 실패: {exc}")
        return 1 if store.finish_or_continue(job_id, "failed", phase="팀장 실행 실패", **_env_note_drop(None)) else None
    finally:
        if server:
            server.stop()
        if keeper is not None:
            keeper.terminate()

    warning = None if is_codex else _claude_bg_wait_warning(stderr_path, stderr_start)
    ok = returncode == 0 and not translator.is_error and warning is None
    phase = "완료" if ok else "실패"
    if warning is not None:
        phase = "Claude 백그라운드 대기 한도 초과"
        store.add_event(job_id, LEAD, "error", f"{phase} (종료 코드 {returncode}): {warning}")
    elif not ok:
        tail = _tail(stderr_path)
        store.add_event(job_id, LEAD, "error", f"종료 코드 {returncode}" + (f": {tail}" if tail else ""))
    if refused := review_gate.refused(review_counter):
        store.add_event(
            job_id, LEAD, "phase",
            f"리뷰 반복 상한({team.MAX_REVIEW_CALLS}번)에 도달해 리뷰 담당 호출 {refused}번을 막았습니다",
        )
    if translator.result:  # 상태를 끝으로 바꾸기 전에 남긴다 (끝난 작업을 본 쪽은 보고도 이미 볼 수 있게)
        store.add_event(job_id, LEAD, RESULT, translator.result)
    jobs_organize(cfg.artifacts_dir(job_id))  # 팀장이 멈춘 지금, 사진·영상을 한 폴더에 늘어놓는다
    cost = translator.cost_usd
    if server and server.cost_usd:  # Codex 팀장은 비용을 알려 주지 않는다. Claude 팀원이 쓴 비용만 더한다
        cost = (cost or 0.0) + server.cost_usd
    finished = store.finish_or_continue(
        job_id,
        "succeeded" if ok else "failed",
        phase=phase,
        result=translator.result,
        cost_usd=_total_cost(job.cost_usd if follow_up else None, cost),
        # 이번 실행이 세션을 만들기 전에 실패해도 이전 세션 id 는 지우지 않는다 (다시 이어갈 수 있게).
        session_id=translator.session_id or job.session_id,
        **_env_note_drop(translator.result, blocked=blocked),
    )
    return (0 if ok else 1) if finished else None


def _env_note_drop(result: str | None, *, blocked: bool = False) -> dict:
    """실행 도중에 .env 를 허용해 대기시킨 '이어서 진행' 요청을 이번 실행이 끝날 때 쓸지 정한다.

    에이전트가 .env 가 필요하다고 멈췄을 때(보고에 NEED_MARKER)만 이어서 실행한다. 그 밖에는 이미 끝난 작업을
    다시 열어 범위를 넓히므로 버리고, 허용은 유지된 채 사용자가 이어서 시킬지 정하게 타임라인에 알린다.
    """
    if guard.NEED_MARKER in (result or ""):
        return {}
    note = guard.ALLOWED_NOT_RESUMED + (guard.BLOCKED_HINT if blocked else "")
    return {"drop": (guard.ALLOWED_NOTE,), "dropped_note": note}


_PARENT_REPORT_LIMIT = 2000  # 파생 작업 안내에 붙이는 부모 보고의 최대 글자 수


def _derived_note(store: Store, job_id: int) -> str:
    """파생 작업의 첫 실행에서 요청 앞에 붙이는 안내. 파생 작업이 아니거나 출처 작업이 지워졌으면 빈 문자열.

    jobs.prompt 에는 사용자가 쓴 요청만 남기고, 에이전트에게 줄 때만 붙인다.
    목록의 부모(parent_id)는 부모를 지우면 조부모로 옮겨지므로 스냅샷을 뜬 출처(origin)를 쓴다.
    """
    try:
        parent_id = store.origin(job_id)
        if parent_id is None:
            return ""
        parent = store.get_job(parent_id)
        report = (store.last_result(parent_id) or "").strip()
    except (KeyError, sqlite3.Error):  # 안내 때문에 작업이 실패하면 안 된다
        return ""
    note = (
        f"[파생 작업 안내] 이 작업은 #{parent.id}(요청: {one_line(parent.prompt, 200)})에서 파생됐다. "
        "작업 디렉토리는 그 작업의 변경 위에서 시작한다 (그 변경은 이 작업 시작 시점의 기준 커밋에 들어 있다).\n"
    )
    if report:
        if len(report) > _PARENT_REPORT_LIMIT:
            report = report[:_PARENT_REPORT_LIMIT].rstrip() + "\n…(뒷부분 생략)"
        note += f"\n## #{parent.id} 의 마지막 보고\n{report}\n"
    return note + "\n## 이번 요청\n"


def build_codex_command(
    cfg: Config, model: str, resume: str | None, rpc: Path, artifacts: str, env_guard: guard.EnvGuard | None = None,
    *, network: bool = False,
) -> list[str]:
    """Codex 팀장 명령. 샌드박스(workspace-write)는 유지하고 팀원 요청 폴더와 결과물 폴더만 쓸 수 있게 연다."""
    cmd = [cfg.codex_bin, "exec"]
    if resume:
        cmd += ["resume", resume]
    cmd += [
        "--json", "--skip-git-repo-check",
        # `exec resume` 에는 -s 옵션이 없어 샌드박스도 설정 값(-c)으로 준다
        *team.codex_access("workspace-write", rpc, artifacts, guard=env_guard, network=network),
    ]
    if team.codex_model(model):
        cmd += ["-m", team.codex_model(model)]
    cmd.append("-")  # 요청 본문은 stdin
    return cmd


def _blocked_read(line: str) -> bool:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return False
    return isinstance(raw, dict) and guard.blocked_read(raw)


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


def _claude_bg_wait_warning(path: Path, start: int) -> str | None:
    try:
        with path.open("rb") as log:
            log.seek(start)
            for line in log:
                match = _CLAUDE_BG_WAIT_WARNING.fullmatch(line.rstrip(b"\r\n"))
                if match:
                    return match[0].decode("utf-8", errors="replace")
    except OSError:
        pass
    return None


def _tail(path, limit: int = 300) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return " ".join(text[-limit:].split())
