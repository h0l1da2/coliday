"""coliday CLI 진입점. 인자 없이 터미널에서 실행하면 TUI 를 연다."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

from coliday import config, gitops, jobs, members, projects, remote, team, worker
from coliday.jobs import STATUS_ICON, JobError
from coliday.store import QUEUED, RESULT, EventRow, Job, Store


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    cfg = config.load()
    if args.cmd is None:
        args = parser.parse_args(["ui" if sys.stdin.isatty() and sys.stdout.isatty() else "ls"])
    if args.cmd == "ui":
        return _cmd_ui(cfg)
    if args.cmd == "update":
        return _cmd_update(args)  # DB 는 쓰지 않는다
    if args.cmd == "_agent":
        # Codex 샌드박스 안에서 실행된다. DB 를 열면(쓰기) 막히므로 Store 를 만들기 전에 처리한다.
        return members.request(Path(args.dir), args.role, sys.stdin.read())
    store = Store(cfg.db_path)
    try:
        code = args.func(cfg, store, args)
        _update_notice(cfg, args)
        return code
    except KeyError as exc:
        print(f"작업 #{exc.args[0]} 을(를) 찾을 수 없습니다.", file=sys.stderr)
        return 2
    except (JobError, gitops.GitError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        store.close()


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="coliday",
        description="팀장 에이전트에게 일을 시키고 진행 상황을 본다. 인자 없이 실행하면 TUI 를 연다.",
    )
    sub = p.add_subparsers(dest="cmd", metavar="{ui,run,continue,env,project,model,team,prompt,ls,show,diff,open,cancel,rm,update}")

    sub.add_parser("ui", help="TUI 열기 (인자 없이 실행한 것과 같다)")

    run = sub.add_parser("run", help="새 작업 실행")
    run.add_argument("prompt", help="팀장에게 시킬 일")
    run.add_argument("-C", "--dir",
                     help="프로젝트 디렉토리 (기본: 현재 디렉토리). 서버의 디렉토리는 user@host:/경로 (SSH)")
    run.add_argument("--from", dest="parent", type=int, metavar="ID",
                     help="작업 #ID 의 지금 상태(커밋하지 않은 변경 포함)에서 파생해 새 작업을 시작 (-C, --no-worktree 와 함께 쓰지 않는다)")
    run.add_argument("--no-worktree", action="store_true", help="worktree 없이 프로젝트 디렉토리에서 직접 작업")
    run.add_argument("--allow-env", action="store_true",
                     help="이 작업에서 에이전트가 .env 파일을 읽어도 된다 (기본: 읽지 못하게 막는다)")
    run.add_argument("-d", "--detach", action="store_true", help="시작만 하고 바로 돌아오기")
    run.add_argument("--model", help="이 작업만 쓸 팀장 에이전트·모델 (예: codex, claude:opus). 기본: coliday model 설정")
    run.set_defaults(func=_cmd_run)

    cont = sub.add_parser("continue", help="작업에 이어서 시키기 (같은 세션·같은 작업 디렉토리). 실행 중이면 끝난 뒤 전달")
    cont.add_argument("id", type=int)
    cont.add_argument("prompt", help="이어서 시킬 일")
    cont.add_argument("-d", "--detach", action="store_true", help="시작만 하고 바로 돌아오기")
    cont.add_argument("--model", help="이번 실행부터 쓸 팀장 모델 (기본: coliday model 설정, 없으면 이전과 같음)")
    env = cont.add_mutually_exclusive_group()
    env.add_argument("--allow-env", dest="allow_env", action="store_true", default=None,
                     help="이번 실행부터 .env 파일 읽기를 허용한다 (기본: 작업을 시작할 때 정한 그대로)")
    env.add_argument("--deny-env", dest="allow_env", action="store_false", help="이번 실행부터 .env 파일 읽기를 다시 막는다")
    cont.set_defaults(func=_cmd_continue)

    proj = sub.add_parser("project", help="연결된 프로젝트 디렉토리 보기/추가/빼기 (서버는 user@host:/경로 로 SSH 접속)")
    proj.add_argument("action", nargs="?", choices=["add", "rm"], help="생략하면 목록 보기")
    proj.add_argument("dir", nargs="?",
                      help="디렉토리 (add 는 기본: 현재 디렉토리). 서버는 user@host:/경로 또는 ~/.ssh/config 별칭:/경로")
    proj.add_argument("--name", help="add: 목록에 보일 이름 (기본: 폴더 이름). 이미 연결된 디렉토리면 이름만 바꾼다")
    proj.add_argument("--port", help="add: 서버의 SSH 포트 (기본 22). user@host:2222:/경로 로 써도 된다")
    proj.add_argument("--password", action="store_true",
                      help="add: 서버 비밀번호를 물어 이 컴퓨터의 SSH 키를 서버에 등록한다 (키 인증이 안 돼 있을 때. 비밀번호는 저장하지 않는다)")
    proj.set_defaults(func=_cmd_project)

    model = sub.add_parser("model", help="기본 에이전트와 팀장·팀원의 에이전트·모델 보기/바꾸기 (다음 실행부터 적용)")
    model.add_argument("role", nargs="?", help="default(기본 에이전트), lead(팀장) 또는 팀원 이름 (예: coder)")
    model.add_argument("name", nargs="?", help="claude, codex, claude:<모델>, codex:<모델> (예: claude:opus, codex:gpt-6.1-sol). 모델 이름은 모두 소문자, 띄어쓰기 없이 하이픈")
    model.add_argument("--reset", action="store_true", help="해당 역할을 기본값으로 되돌리기")
    model.set_defaults(func=_cmd_model)

    tm = sub.add_parser(
        "team", help="팀원 구성과 역할별 프롬프트 보기/바꾸기 (다음 실행부터 적용)",
        description="팀원을 고치거나 넣고 뺀다. 바꾼 것만 ~/.coliday/team.json 에 저장되고 나머지는 기본값을 따른다. "
                    "lead 는 팀장 지시문이다 ({team} 자리에 팀원 목록, {max_attempts} 자리에 최대 시도 횟수가 들어간다).",
    )
    tm.add_argument("action", nargs="?", choices=["show", "set", "rm", "reset"],
                    help="show: 고칠 수 있는 원문(JSON) / set: 고치거나 새로 넣기 / rm: 팀에서 빼기 / reset: 기본값으로. 생략하면 목록")
    tm.add_argument("role", nargs="?", help="lead(팀장 지시문) 또는 팀원 이름. 없는 이름으로 set 하면 새 팀원이 된다")
    tm.add_argument("--description", help="set: 팀장이 보고 언제 맡길지 정하는 한 줄 설명")
    tm.add_argument("--prompt", help="set: 프롬프트 (팀장은 지시문)")
    tm.add_argument("--prompt-file", help="set: 프롬프트를 읽을 파일 ('-' 는 stdin)")
    tm.add_argument("--tools", help="set: 쓸 도구를 쉼표로 (예: Read,Grep,Bash). all 은 모든 도구")
    tm.add_argument("--all", action="store_true", help="reset: 팀 전체를 기본값으로")
    tm.set_defaults(func=_cmd_team)

    prompt = sub.add_parser("prompt", help="팀장·팀원에게 주는 프롬프트 보기 (실행에 쓰이는 내용 그대로)")
    prompt.add_argument("role", nargs="?", help="lead(팀장) 또는 팀원 이름. 생략하면 전부")
    prompt.set_defaults(func=_cmd_prompt)

    ls = sub.add_parser("ls", help="작업 목록")
    ls.add_argument("-n", "--limit", type=int, default=20)
    ls.set_defaults(func=_cmd_ls)

    show = sub.add_parser("show", help="작업 타임라인과 결과")
    show.add_argument("id", type=int)
    show.add_argument("-f", "--follow", action="store_true", help="끝날 때까지 실시간으로 보기")
    show.set_defaults(func=_cmd_show)

    diff = sub.add_parser("diff", help="작업이 만든 코드 변경 보기")
    diff.add_argument("id", type=int)
    diff.set_defaults(func=_cmd_diff)

    opn = sub.add_parser("open", help="작업이 남긴 결과물(스크린샷·영상) 폴더 열기")
    opn.add_argument("id", type=int)
    opn.set_defaults(func=_cmd_open)

    envp = sub.add_parser("env", help="작업 도중에 그 작업의 .env 파일 읽기를 허용하거나 다시 막기 (그 작업에만 해당)")
    envp.add_argument("id", type=int)
    envp.add_argument("action", nargs="?", choices=["allow", "deny"], help="생략하면 지금 상태 보기")
    envp.set_defaults(func=_cmd_env)

    cancel = sub.add_parser("cancel", help="실행 중인 작업 취소")
    cancel.add_argument("id", type=int)
    cancel.set_defaults(func=_cmd_cancel)

    rm = sub.add_parser("rm", help="끝난 작업 삭제 (목록·기록과 worktree·브랜치)")
    rm.add_argument("id", type=int)
    rm.add_argument("--force", action="store_true", help="검토하지 않은 변경이 있어도 삭제")
    rm.set_defaults(func=_cmd_rm)

    upd = sub.add_parser("update", help="coliday 를 최신 버전으로 업데이트 (clone 한 폴더에서 git pull, 필요하면 재설치)")
    upd.add_argument("--check", action="store_true", help="받을 업데이트가 있는지만 확인")

    agent = sub.add_parser("_agent")  # 내부용: Codex 팀장이 팀원에게 맡길 때 실행한다
    agent.add_argument("dir")
    agent.add_argument("role")

    w = sub.add_parser("_worker")  # 내부용: help 를 주지 않으면 목록에 나오지 않는다
    w.add_argument("id", type=int)
    w.set_defaults(func=lambda cfg, store, a: worker.run_job(cfg, store, a.id))
    return p


def _cmd_ui(cfg: config.Config) -> int:
    try:
        from coliday.tui import run_tui
    except ImportError:
        print("TUI 에 필요한 textual 이 설치되지 않았습니다: pip install textual", file=sys.stderr)
        return 2
    return run_tui(cfg)


def _cmd_run(cfg: config.Config, store: Store, args: argparse.Namespace) -> int:
    if args.parent is not None:
        if args.dir is not None or args.no_worktree:
            raise JobError("--from 은 -C, --no-worktree 와 함께 쓸 수 없습니다 (파생 작업은 부모 작업의 위치와 방식을 따른다).")
        launched = jobs.derive(cfg, store, args.parent, args.prompt, model=args.model, allow_env=args.allow_env)
    else:
        launched = jobs.launch(
            cfg, store, args.prompt, "." if args.dir is None else args.dir, no_worktree=args.no_worktree, model=args.model,
            allow_env=args.allow_env,
        )
    for w in launched.warnings:
        print(f"주의: {w}", file=sys.stderr)
    job = launched.job
    print(f"작업 #{job.id} 시작 — {job.workdir}")
    if job.branch:
        print(f"브랜치: {job.branch}")
    if args.detach:
        print(f"진행 상황: coliday show {job.id} -f")
        return 0
    code = _follow(store, job.id)
    _print_artifacts(cfg, job.id)
    return code


def _cmd_continue(cfg: config.Config, store: Store, args: argparse.Namespace) -> int:
    last = max((ev.id for ev in store.events(args.id)), default=0)  # 이번 실행의 이벤트만 따라본다
    launched = jobs.continue_job(cfg, store, args.id, args.prompt, model=args.model, allow_env=args.allow_env)
    for w in launched.warnings:
        print(f"주의: {w}", file=sys.stderr)
    job = launched.job
    if launched.queued:
        print(f"작업 #{job.id} 이(가) 실행 중이라 요청을 대기시켰습니다. 지금 실행이 끝나면 이어서 전달됩니다.")
    else:
        print(f"작업 #{job.id} 이어서 시작 — {job.workdir}")
    if args.detach:
        print(f"진행 상황: coliday show {job.id} -f")
        return 0
    code = _follow(store, job.id, after_id=last)
    _print_artifacts(cfg, job.id)
    return code


def _update_notice(cfg: config.Config, args: argparse.Namespace) -> None:
    """새 버전이 있으면 명령 출력 끝에 한 줄 알린다. 사람이 터미널에서 직접 실행했을 때만 (스크립트 출력은 건드리지 않는다)."""
    if args.cmd.startswith("_") or not (sys.stdout.isatty() and sys.stderr.isatty()):
        return
    from coliday import updater

    try:
        text = updater.notice(cfg.update_stamp)
    except Exception:  # 알림 때문에 명령이 실패하면 안 된다
        return
    if text:
        print(f"\n💡 {text}", file=sys.stderr)


def _cmd_update(args: argparse.Namespace) -> int:
    from coliday import updater

    try:
        result = updater.update(updater.source_root(), check=args.check)
    except updater.UpdateError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not result.commits:
        print(f"이미 최신입니다 ({result.before[:7]}).")
        return 0
    if args.check:
        print(f"받을 업데이트가 {len(result.commits)}개 있습니다. 받으려면: coliday update")
    else:
        print(f"업데이트했습니다: {result.before[:7]} → {result.after[:7]} ({len(result.commits)}개)")
    for line in result.commits[:20]:
        print(f"  {line}")
    if len(result.commits) > 20:
        print(f"  … 외 {len(result.commits) - 20}개")
    if result.reinstalled:
        print("의존성이 바뀌어 다시 설치했습니다.")
    if result.changed:
        print("열려 있는 coliday 화면은 닫았다 다시 여세요. 실행 중인 작업은 이전 버전으로 끝까지 돕니다.")
    return 0


def _cmd_project(cfg: config.Config, store: Store, args: argparse.Namespace) -> int:
    if args.action == "add":
        directory, password = args.dir or ".", None
        host, path = remote.split(directory)
        if args.port:
            if host is None:
                raise JobError("--port 는 서버의 프로젝트(user@host:/경로)에만 씁니다.")
            try:
                directory = f"{remote.with_port(host, args.port)}:{path}"
            except remote.RemoteError as exc:
                raise JobError(str(exc)) from exc
        if args.password and host is not None:
            password = getpass.getpass(f"{remote.split(directory)[0]} 의 비밀번호: ")
        path = jobs.add_project(cfg, store, directory, args.name, password)
        print(f"연결했습니다: {jobs.project_names(cfg, store)[path]}  {path}")
        return 0
    if args.action == "rm":
        if not args.dir:
            raise JobError("목록에서 뺄 디렉토리를 주세요. 예: coliday project rm ~/work_dir/some-project")
        if not jobs.remove_project(cfg, store, args.dir):
            raise JobError(f"목록에 없는 디렉토리입니다: {args.dir}")
        print(f"목록에서 뺐습니다: {args.dir}")
        return 0
    items = jobs.project_names(cfg, store)
    if not items:
        print("연결된 프로젝트가 없습니다. 예: coliday project add ~/work_dir/some-project --name 쇼핑몰")
    for path, name in items.items():
        print(f"{_pad(name, 28)}  {path}" + ("" if projects.exists(path) else "  (없음)"))
    return 0


def _cmd_model(cfg: config.Config, store: Store, args: argparse.Namespace) -> int:
    if args.role and bool(args.name) == args.reset:
        raise JobError("모델과 --reset 중 하나만 주세요. 예: coliday model coder codex / coliday model coder --reset")
    if args.reset and not args.role:
        raise JobError("되돌릴 역할을 주세요. 예: coliday model coder --reset")
    if args.role:
        jobs.set_model(cfg, args.role, None if args.reset else args.name)
    for role, name, changed in jobs.model_rows(cfg):
        print(f"{_pad(role, 14)}  {_pad(name, 28)}  {'설정함' if changed else '기본값'}")
        if role == team.DEFAULT_ROLE:
            print("-" * 52)
    return 0


def _cmd_team(cfg: config.Config, store: Store, args: argparse.Namespace) -> int:
    setting = args.description is not None or args.prompt is not None or args.prompt_file or args.tools is not None
    if setting and args.action != "set":
        raise JobError("--description, --prompt, --prompt-file, --tools 는 set 에만 씁니다. 예: coliday team set qa --prompt-file qa.md")
    if args.all and (args.action != "reset" or args.role):
        raise JobError("--all 은 역할 없이 reset 에만 씁니다. 예: coliday team reset --all")
    if args.action in ("show", "set", "rm") and not args.role:
        raise JobError(f"역할을 주세요. 예: coliday team {args.action} reviewer")
    if args.action == "show":
        print(json.dumps(_as_lines(jobs.role_definition(cfg, args.role)), ensure_ascii=False, indent=2))
        return 0
    if args.action == "set":
        if not setting:
            raise JobError('바꿀 내용을 주세요. 예: coliday team set qa --description "검수 담당" --prompt-file qa.md --tools Read,Bash')
        if args.prompt is not None and args.prompt_file:
            raise JobError("--prompt 와 --prompt-file 중 하나만 주세요.")
        prompt = args.prompt
        if args.prompt_file:
            try:
                prompt = sys.stdin.read() if args.prompt_file == "-" else Path(args.prompt_file).read_text(encoding="utf-8")
            except OSError as exc:
                raise JobError(f"프롬프트 파일을 읽을 수 없습니다: {exc}") from exc
        tools: object = team.KEEP
        if args.tools is not None:
            tools = None if args.tools.strip().lower() == "all" else [t.strip() for t in args.tools.split(",") if t.strip()]
        jobs.set_role(cfg, args.role, description=args.description, prompt=prompt, tools=tools)
    elif args.action == "rm":
        jobs.remove_role(cfg, args.role)
    elif args.action == "reset":
        if not args.role and not args.all:
            raise JobError("되돌릴 역할을 주세요. 전부 되돌리려면: coliday team reset --all")
        jobs.reset_role(cfg, args.role)
    elif args.role:
        raise JobError(f"무엇을 할지 주세요. 예: coliday team show {args.role} / coliday team set {args.role} --prompt-file p.md")
    for role, state, description in jobs.team_roles(cfg):
        print(f"{_pad(role, 14)}  {_pad(state, 8)}  {description}")
    if args.action:
        print("다음 실행부터 적용됩니다.")
    return 0


def _as_lines(definition: dict) -> dict:
    """team.json 에 그대로 붙여 넣을 수 있는 모양: 프롬프트는 줄 단위 배열."""
    return {k: v.split("\n") if k == "prompt" else v for k, v in definition.items() if v is not None}


def _cmd_prompt(cfg: config.Config, store: Store, args: argparse.Namespace) -> int:
    print("\n\n".join(f"── {title} ──\n{body}" for title, body in jobs.role_prompts(cfg, args.role)))
    return 0


def _cmd_ls(cfg: config.Config, store: Store, args: argparse.Namespace) -> int:
    rows = [jobs.reconcile(store, j) for j in store.list_jobs(args.limit)]
    if not rows:
        print('아직 작업이 없습니다. 예: coliday run "README 오타 수정"')
        return 0
    now = time.time()
    meta = store.job_meta()
    for j in rows:
        icon = STATUS_ICON.get(j.status, "?")
        repo = remote.name(j.repo)
        parent = meta[j.id].parent_id if j.id in meta else None
        prompt = (f"↳#{parent} " if parent is not None else "") + j.prompt  # 파생 작업이면 부모 번호
        print(
            f"{icon} #{j.id:<4} {jobs.ago(now - j.last_asked):>6}  {repo:<20.20}  "
            f"{_pad(jobs.one_line(prompt, 40), 40)}  {jobs.one_line(j.phase, 50)}"
        )
    return 0


def _cmd_show(cfg: config.Config, store: Store, args: argparse.Namespace) -> int:
    job = jobs.reconcile(store, store.get_job(args.id))
    _print_header(job)
    if args.follow and not job.is_terminal:
        return _follow(store, job.id)
    for ev in store.events(job.id):
        _print_event(ev)
    _print_result(store.get_job(job.id), reported=store.has_result_event(job.id))
    _print_artifacts(cfg, job.id)
    return 0


def _cmd_diff(cfg: config.Config, store: Store, args: argparse.Namespace) -> int:
    job = store.get_job(args.id)
    text = gitops.diff(job.workdir, job.base_commit)
    if not text.strip():
        print("변경 사항이 없습니다.")
        return 0
    pager = os.environ.get("PAGER", "less -R")
    if sys.stdout.isatty():
        subprocess.run(pager, shell=True, input=text, text=True, check=False)
    else:
        sys.stdout.write(text)
    return 0


def _cmd_open(cfg: config.Config, store: Store, args: argparse.Namespace) -> int:
    root = jobs.open_artifacts(cfg, store, args.id)
    files = jobs.artifacts(cfg, args.id)
    print(f"결과물 {jobs.artifacts_summary(files)}: {root}" if files else f"아직 결과물이 없습니다: {root}")
    for path in files:
        print(f"  {path.relative_to(root)}")
    return 0


def _cmd_env(cfg: config.Config, store: Store, args: argparse.Namespace) -> int:
    job = store.get_job(args.id)
    if args.action is None:
        state = "허용 (에이전트가 읽을 수 있음)" if store.allow_env(job.id) else "차단 (에이전트가 읽지 못함)"
        print(f"작업 #{job.id} 의 .env 읽기: {state}")
        return 0
    print(jobs.set_env_access(cfg, store, job.id, args.action == "allow"))
    return 0


def _cmd_cancel(cfg: config.Config, store: Store, args: argparse.Namespace) -> int:
    job = store.get_job(args.id)
    if jobs.cancel(store, job):
        print(f"작업 #{job.id} 취소했습니다.")
    else:
        print(f"작업 #{job.id} 은(는) 이미 종료됐습니다 ({store.get_job(job.id).status}).")
    return 0


def _cmd_rm(cfg: config.Config, store: Store, args: argparse.Namespace) -> int:
    print(jobs.remove(cfg, store, store.get_job(args.id), force=args.force))
    return 0


def _follow(store: Store, job_id: int, after_id: int = 0) -> int:
    last = after_id
    try:
        while True:
            for ev in store.events(job_id, after_id=last):
                _print_event(ev)
                last = ev.id
            job = jobs.reconcile(store, store.get_job(job_id))
            if job.is_terminal:
                for ev in store.events(job_id, after_id=last):
                    _print_event(ev)
                _print_result(job, reported=store.has_result_event(job_id))
                return 0 if job.status == "succeeded" else 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        print(f"\n따라보기를 멈췄습니다. 작업은 계속됩니다: coliday show {job_id} -f / 취소: coliday cancel {job_id}")
        return 130


def _print_artifacts(cfg: config.Config, job_id: int) -> None:
    files = jobs.artifacts(cfg, job_id)
    if files:
        print(f"결과물 {jobs.artifacts_summary(files)}: {cfg.artifacts_dir(job_id)}  (열기: coliday open {job_id})")


def _print_header(job: Job) -> None:
    print(f"{STATUS_ICON.get(job.status, '?')} 작업 #{job.id} [{job.status}]  {job.repo}")
    print(f"  요청: {job.prompt}")
    print(f"  작업 위치: {job.workdir}" + (f"  (브랜치 {job.branch})" if job.branch else ""))


def _print_event(ev: EventRow) -> None:
    ts = time.strftime("%H:%M:%S", time.localtime(ev.ts))
    if ev.kind == QUEUED:
        print(f"{ts}  {_pad(ev.actor, 12)}  (대기: 지금 실행이 끝나면 전달) {ev.message}")
        return
    if ev.kind == RESULT:
        print(f"\n── 팀장 보고 ({ts}) ──\n{ev.message}\n")
        return
    print(f"{ts}  {_pad(ev.actor, 12)}  {ev.message}")


def _print_result(job: Job, reported: bool = False) -> None:
    """reported: 보고가 이미 타임라인(result 이벤트)에 출력됐으면 다시 찍지 않는다."""
    print(f"\n{STATUS_ICON.get(job.status, '?')} {job.status}  {job.phase}")
    if job.cost_usd is not None:
        print(f"비용: ${job.cost_usd:.4f}")
    if job.result and not reported:
        print("\n── 팀장 보고 ──")
        print(job.result)
    if job.branch:
        print(f"\n변경 확인: coliday diff {job.id}")


def _pad(text: str, width: int) -> str:
    """한글처럼 두 칸을 차지하는 문자를 고려해 오른쪽을 공백으로 채운다."""
    used = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - used)
