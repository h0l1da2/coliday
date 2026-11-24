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

from coliday import auth, guard, jobs, members, remote, team
from coliday.jobs import organize as jobs_organize
from coliday.config import Config
from coliday.events import LEAD, CodexTranslator, Translator
from coliday.store import RESULT, Store


def build_command(
    cfg: Config, agents_file: str, model: str | None, job_id: int, resume: str | None = None,
    lead_prompt: str | None = None, *, artifacts: str | None = None, claude_bin: str | None = None,
    allow: list[str] | None = None, add_dirs: list[str] | None = None, settings: str | None = None,
) -> list[str]:
    """Claude 팀장 명령. lead_prompt 를 주지 않으면 기본 팀원으로 만든 지시문을 쓴다.

    artifacts, claude_bin 은 서버에서 실행할 때 그 서버의 경로를 준다 (없으면 이 컴퓨터의 설정).
    allow 는 권한 검사 없이 실행할 도구 규칙(Codex 팀원 실행 파일), add_dirs 는 더 열어 둘 폴더다 (그 실행 파일에 줄 명세를 쓰는 곳).
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
    for directory in add_dirs or []:
        cmd += ["--add-dir", directory]
    if settings:  # .env 읽기를 막는 규칙과 샌드박스 (guard.py)
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

    # 사람이 이 작업에 허용하지 않았으면 에이전트가 .env 파일을 읽지 못하게 막는다.
    env_guard: guard.EnvGuard | None = None
    if store.allow_env(job_id):
        store.add_event(job_id, LEAD, "phase", "사람이 허용한 작업입니다: 에이전트가 .env 파일을 읽을 수 있습니다")
    elif host is not None:
        env_guard = guard.for_job(remote_workdir, remote.split(job.repo)[1], None, local=False)
    else:
        env_guard = guard.for_job(job.workdir, job.repo, str(Path.home()), local=True)
    if env_guard is not None:
        store.add_event(
            job_id, LEAD, "phase",
            f".env 파일은 읽지 못하게 막고 실행합니다 (도중에 허용하려면 TUI 에서 Ctrl+L, 또는 coliday env {job_id} allow)",
        )

    # 후속 요청이 있으면 이어서 시킨 실행이다: 같은 세션을 resume 하고 로그는 덧붙인다.
    follow_up = store.last_follow_up(job_id)
    prompt = follow_up.message if follow_up else job.prompt
    resume = job.session_id if follow_up else None

    codex_home, codex_login = "", None  # 서버에 잠깐 둔 Codex 로그인 (끝나면 지운다)
    server: members.Server | None = None
    translator: Translator | CodexTranslator
    if team.is_codex(job.model):
        # Codex 팀장: 지시문을 요청 앞에 붙이고, 팀원 실행 요청은 members.Server 가 받는다.
        server = members.Server(cfg, job, resolved, env_guard)
        cmd = build_codex_command(cfg, job.model, resume, server.directory, artifacts, env_guard)
        lead = team.codex_lead_prompt(
            resolved, members.agent_command(job_id), cfg.max_attempts, artifacts, custom.lead_prompt
        ) + (guard.LEAD_RULE if env_guard is not None else "")
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
        where = f"{host} 에" if host is not None else "이 컴퓨터에"
        reason = f"{where} codex 가 없어"
        if has_codex and env_guard is not None and any(team.is_codex(s.get("model")) for s in resolved.values()):
            # 낮은 버전의 Codex 는 .env 를 막는 설정을 모른다: 막지 못한 채 돌리지 않는다.
            try:
                version = jobs.cli_version(host, codex_bin)
            except remote.RemoteError as exc:
                store.add_event(job_id, LEAD, "error", str(exc))
                return 1 if store.finish_or_continue(job_id, "failed", phase="서버 접속 실패") else None
            if version is None or version < guard.MIN_CODEX:
                has_codex = False
                reason = f"{where} 있는 codex({guard.show(version)})가 .env 읽기를 막지 못해"
        if not has_codex:
            # Codex CLI 가 없어도 작업은 된다: Codex 로 두었던 팀원을 Claude 로 돌린다 (없는 명령을 부르다 실패하지 않게).
            resolved, switched = team.without_codex(resolved)
            if switched:
                store.add_event(job_id, LEAD, "phase", f"{reason} {', '.join(switched)} 은(는) Claude 로 실행합니다")
        # Codex 팀원은 실행 파일 하나씩으로 만들어 두고 그 파일만 허용한다 (auto 모드 분류기가 codex 실행을 막지 않게).
        runner_dir = f"{remote_dir}/codex" if host is not None else str(job_dir / "codex")
        made = team.codex_runners(resolved, codex_bin, artifacts, local=host is None, guard=env_guard)
        runners = {role: f"{runner_dir}/{file_name}" for role, (file_name, _) in made.items()}
        scripts = {runners[role]: script for role, (_, script) in made.items()}
        allow = [team.runner_rule(path) for path in scripts]
        # 명세 파일은 실행 파일과 다른 폴더에 둔다: 에이전트가 마음대로 쓸 수 있는 곳에 허용된 실행 파일을 두지 않는다.
        tasks_dir = f"{runner_dir}-tasks"
        add_dirs = [tasks_dir] if scripts else []
        settings = None
        if env_guard is not None:
            settings = env_guard.claude_settings(
                writable=[artifacts, *add_dirs, *env_guard.roots[:2]],  # 홈 밖에 있는 레포에서도 커밋할 수 있게
                protected=cfg.protected_paths if host is None else (),
                # Codex 는 자기 샌드박스를 따로 쓴다 (샌드박스 안에서 또 샌드박스를 켤 수 없다). .env 는 Codex 쪽 설정이 막는다.
                excluded=[f"{team.runner_command(path)} *" for path in scripts],
            )
        agents_json = json.dumps(team.claude_agents(resolved, runners, tasks_dir), ensure_ascii=False, indent=2)
        agents_file.write_text(agents_json, encoding="utf-8")  # 서버에서 실행해도 무엇을 보냈는지 여기 남긴다
        lead_prompt = team.lead_prompt(cfg.max_attempts, artifacts, resolved, custom.lead_prompt)
        if env_guard is not None:
            lead_prompt += guard.LEAD_RULE
        if host is not None:
            try:
                remote.check(
                    host,
                    f"mkdir -p {shlex.quote(artifacts)} {shlex.quote(runner_dir)} {shlex.quote(tasks_dir)}"
                    f" && cat > {shlex.quote(remote_dir + '/agents.json')}",
                    stdin=agents_json,
                )
                for path, script in scripts.items():
                    remote.check(host, f"cat > {shlex.quote(path)} && chmod 700 {shlex.quote(path)}", stdin=script)
            except remote.RemoteError as exc:
                store.add_event(job_id, LEAD, "error", str(exc))
                return 1 if store.finish_or_continue(job_id, "failed", phase="서버 접속 실패") else None
            inner = build_command(
                cfg, f"{remote_dir}/agents.json", job.model, job_id, resume, lead_prompt,
                artifacts=artifacts, claude_bin=jobs.remote_claude_bin(), allow=allow, add_dirs=add_dirs,
                settings=settings,
            )
            # 서버에서는 로그인하지 않는다: 이 컴퓨터에 등록한 인증을 서버의 claude 에 넘긴다 (codex 팀원도 물려받는다).
            # 표준 입력의 첫 줄들로 보낸다: 명령 인자에 넣으면 `ps` 에 보이고, 서버의 디스크에는 쓰지 않는다.
            secrets = auth.load(cfg)
            if not auth.get(cfg, auth.CLAUDE):
                store.add_event(job_id, LEAD, "error", f"서버에서 쓸 Claude 토큰이 등록돼 있지 않습니다. {auth.HOW_TO}.")
                return 1 if store.finish_or_continue(job_id, "failed", phase="인증 없음") else None
            receive = "".join(f"IFS= read -r {name} && export {name} && " for name in secrets)
            prompt = "".join(f"{value}\n" for value in secrets.values()) + prompt
            if scripts and not auth.get(cfg, auth.CODEX):
                # Codex 팀원: 등록한 API 키가 없으면 이 컴퓨터의 Codex 로그인을 쓴다. 넘길 토큰이 따로 없어 로그인 파일을
                # 서버의 작업 폴더에 잠깐 두고(본인만 읽게) 실행이 끝나면 지운다.
                codex_login = auth.codex_login()
                if codex_login:
                    codex_home = f"{remote_dir}/{jobs.REMOTE_CODEX_HOME}"
                    try:
                        remote.check(
                            host,
                            f"umask 077; mkdir -p {shlex.quote(codex_home)} && cat > {shlex.quote(codex_home + '/auth.json')}",
                            stdin=codex_login,
                        )
                    except remote.RemoteError as exc:
                        store.add_event(job_id, LEAD, "error", str(exc))
                        return 1 if store.finish_or_continue(job_id, "failed", phase="서버 접속 실패") else None
                    receive += f"export CODEX_HOME={shlex.quote(codex_home)} && "
                    store.add_event(job_id, LEAD, "phase", f"Codex 팀원은 이 컴퓨터의 Codex 로그인으로 {host} 에서 실행합니다")
                else:
                    store.add_event(
                        job_id, LEAD, "phase",
                        f"이 컴퓨터에 Codex 로그인이 없어 Codex 팀원은 {host} 의 로그인으로 실행합니다 (없으면 실패합니다)",
                    )
            # 프로세스 번호를 남겨 두고(취소할 때 쓴다) 그 프로세스가 그대로 에이전트가 된다.
            cmd = remote.command(
                host,
                f"{receive}cd {shlex.quote(remote_workdir)} && echo $$ > {shlex.quote(remote_dir + '/pid')}"
                f" && exec {shlex.join(inner)}",
            )
        else:
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
    try:
        with (job_dir / "stream.jsonl").open(log_mode, encoding="utf-8") as raw_log, \
                stderr_path.open(log_mode, encoding="utf-8") as err_log:
            if server:
                server.start()
            proc = subprocess.Popen(
                cmd, cwd=None if host is not None else job.workdir, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=err_log, text=True, bufsize=1,
                env=members.agent_env(env_guard) if host is None else None,
            )
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(prompt)
            proc.stdin.close()
            told_blocked = env_guard is None  # 막힌 읽기는 실행마다 한 번만 알린다
            for line in proc.stdout:
                raw_log.write(line)
                raw_log.flush()
                _handle_line(store, job_id, translator, line)
                if not told_blocked and ".env" in line and _blocked_read(line):
                    told_blocked = True
                    store.add_event(
                        job_id, LEAD, "error",
                        f"에이전트가 .env 파일을 읽으려다 막혔습니다. 읽게 하려면 이 작업에 허용하세요: "
                        f"TUI 에서 Ctrl+L, 또는 coliday env {job_id} allow",
                    )
            returncode = proc.wait()
    except OSError as exc:  # 실행 파일 없음 등
        store.add_event(job_id, LEAD, "error", f"{cmd[0]} 실행 실패: {exc}")
        if host is not None and codex_home and codex_login:
            _return_codex_login(store, job_id, host, codex_home, codex_login)
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
        if codex_home and codex_login:
            _return_codex_login(store, job_id, host, codex_home, codex_login)
        if not ok and auth.is_failure(translator.result):
            store.add_event(job_id, LEAD, "error", _auth_hint(host))
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


def _auth_hint(host: str) -> str:
    """서버의 claude 가 넘겨받은 인증을 거부했을 때 무엇을 하면 되는지."""
    return (
        f"{host} 의 claude 가 이 컴퓨터에 등록한 인증을 받아들이지 않았습니다 (만료됐거나 잘못된 값). "
        "`claude setup-token` 으로 새로 받아 다시 등록하세요 (프로젝트 추가 창의 토큰 칸, 또는 `coliday auth set`)."
    )


def _return_codex_login(store: Store, job_id: int, host: str, codex_home: str, sent: str) -> None:
    """서버에 둔 Codex 로그인을 지운다. 서버에서 쓰는 동안 갱신됐으면 이 컴퓨터의 로그인에 반영한다 (아니면 여기서 로그인이 풀린다)."""
    login = shlex.quote(codex_home + "/auth.json")
    try:
        proc = remote.run(host, f"cat {login} 2>/dev/null; rm -rf {shlex.quote(codex_home)}")
    except remote.RemoteError as exc:
        store.add_event(
            job_id, LEAD, "error",
            f"{host} 에 둔 Codex 로그인 파일을 지우지 못했습니다 ({codex_home}). 작업을 삭제하면 함께 지워집니다: {exc}",
        )
        return
    if auth.restore_codex_login(sent, proc.stdout):
        store.add_event(job_id, LEAD, "phase", "서버에서 갱신된 Codex 로그인을 이 컴퓨터에 반영했습니다")


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


def build_codex_command(
    cfg: Config, model: str, resume: str | None, rpc: Path, artifacts: str, env_guard: guard.EnvGuard | None = None,
) -> list[str]:
    """Codex 팀장 명령. 샌드박스(workspace-write)는 유지하고 팀원 요청 폴더와 결과물 폴더만 쓸 수 있게 연다."""
    cmd = [cfg.codex_bin, "exec"]
    if resume:
        cmd += ["resume", resume]
    cmd += [
        "--json", "--skip-git-repo-check",
        # `exec resume` 에는 -s 옵션이 없어 샌드박스도 설정 값(-c)으로 준다
        *team.codex_access("workspace-write", rpc, artifacts, guard=env_guard),
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


def _tail(path, limit: int = 300) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return " ".join(text[-limit:].split())
