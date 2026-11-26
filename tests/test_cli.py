"""가짜 claude 로 run → worker → 저장 → 조회 전체 흐름을 검증한다."""

import dataclasses
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import pytest

from coliday import cli, config, jobs, members, team, worker
from coliday.store import Store

FAKE = Path(__file__).parent / "fixtures" / "fake_claude.py"
FAKE_CODEX = Path(__file__).parent / "fixtures" / "fake_codex.py"
SRC = Path(__file__).parents[1] / "src"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    fake_bin = tmp_path / "claude"
    fake_bin.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("COLIDAY_HOME", str(home))
    monkeypatch.setenv("COLIDAY_CLAUDE_BIN", str(fake_bin))
    monkeypatch.setenv("FAKE_CLAUDE_ARGS", str(tmp_path / "args.json"))
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_CODEX} \"$@\"\n")
    fake_codex.chmod(0o755)
    monkeypatch.setenv("COLIDAY_CODEX_BIN", str(fake_codex))
    monkeypatch.setenv("FAKE_CODEX_ARGS", str(tmp_path / "codex.jsonl"))
    fake_open = tmp_path / "open"
    fake_open.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> {tmp_path / 'opened.txt'}\n")
    fake_open.chmod(0o755)
    monkeypatch.setenv("COLIDAY_OPEN_BIN", str(fake_open))
    monkeypatch.setenv("PYTHONPATH", str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return tmp_path


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "myrepo"
    (r / "sub").mkdir(parents=True)
    (r / "sub" / "a.txt").write_text("a\n")
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(r), *args], check=True)
    return r


def _store():
    return Store(config.load().db_path)


def test_run은_worktree에서_팀장을_실행하고_이벤트와_결과를_저장한다(env, repo, capsys):
    code = cli.main(["run", "주문 취소 API 추가", "-C", str(repo / "sub")])

    assert code == 0
    job = _store().get_job(1)
    assert job.status == "succeeded"
    assert job.result == "최종 보고: 완료"
    assert job.cost_usd == 0.05
    assert job.branch == "coliday/1"
    assert Path(job.workdir) == config.load().worktrees_dir / "myrepo-1" / "sub"

    recorded = json.loads((env / "args.json").read_text())
    assert Path(recorded["cwd"]).resolve() == Path(job.workdir).resolve()
    argv = recorded["argv"]
    assert recorded["stdin"] == "주문 취소 API 추가"
    agents = json.loads(Path(argv[argv.index("--agents") + 1]).read_text())
    assert {"architect", "coder", "codex-coder", "reviewer", "verifier"} <= set(agents)
    lead_prompt = argv[argv.index("--append-system-prompt") + 1]
    assert "코드를 바꾸지 않는 일은 팀원에게 맡기지 말고" in lead_prompt  # 찾기·질문은 팀장이 직접

    out = capsys.readouterr().out
    assert "architect에게 분배: 주문 취소 설계" in out
    assert "최종 보고: 완료" in out


def test_claude가_실패하면_failed와_stderr_요약을_남긴다(env, repo, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_EXIT", "3")

    code = cli.main(["run", "x", "-C", str(repo)])

    assert code == 1
    store = _store()
    assert store.get_job(1).status == "failed"
    errors = [e.message for e in store.events(1) if e.kind == "error"]
    assert errors and "종료 코드 3" in errors[0] and "boom" in errors[0]


def test_git_저장소가_아니면_no_worktree_없이는_거부한다(env, tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()

    assert cli.main(["run", "x", "-C", str(plain)]) == 2
    assert _store().list_jobs() == []

    assert cli.main(["run", "x", "-C", str(plain), "--no-worktree"]) == 0
    job = _store().get_job(1)
    assert job.branch is None and Path(job.workdir) == plain.resolve()


def test_diff는_새_파일과_수정을_base_커밋_기준으로_보여준다(env, repo, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_TOUCH", "새파일.txt")
    cli.main(["run", "x", "-C", str(repo)])
    wt = Path(_store().get_job(1).workdir)
    (wt / "sub" / "a.txt").write_text("changed\n")
    capsys.readouterr()

    cli.main(["diff", "1"])

    out = capsys.readouterr().out
    assert "+changed" in out
    assert "+new file" in out


def test_cancel은_실행중인_작업을_종료하고_상태를_덮어쓰지_않는다(env, repo, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    assert cli.main(["run", "x", "-C", str(repo), "-d"]) == 0
    store = _store()
    _wait(lambda: store.get_job(1).status == "running")
    pid = store.get_job(1).pid
    _wait(lambda: (env / "args.json").exists())  # claude 가 실제로 뜬 뒤 취소한다

    assert cli.main(["cancel", "1"]) == 0

    # 테스트 프로세스가 worker 의 부모라 직접 회수한다 (실제로는 run 이 먼저 끝나 init 이 회수).
    _wait(lambda: os.waitpid(pid, os.WNOHANG)[0] == pid)
    claude_pid = json.loads((env / "args.json").read_text())["pid"]
    _wait(lambda: not jobs.alive(claude_pid))  # 같은 프로세스 그룹의 claude 도 종료돼야 한다
    assert store.get_job(1).status == "cancelled"


def test_대기중에_취소된_작업은_worker가_running으로_덮어쓰지_않는다(env, repo):
    from coliday import worker
    store = _store()
    job = store.create_job(prompt="p", repo=str(repo), workdir=str(repo), model=None)
    store.finish_job(job.id, "cancelled", phase="사용자가 취소함")

    assert worker.run_job(config.load(), store, job.id) == 0

    assert store.get_job(job.id).status == "cancelled"
    assert not (env / "args.json").exists()  # claude 를 실행하지 않았다


def test_dash로_시작하는_요청도_그대로_전달된다(env, repo):
    assert cli.main(["run", "-C", str(repo), "--", "-v 옵션 설명 추가"]) == 0

    assert json.loads((env / "args.json").read_text())["stdin"] == "-v 옵션 설명 추가"


def test_worker가_죽어_running으로_남은_작업은_ls에서_failed로_정리된다(env, repo, capsys):
    store = _store()
    job = store.create_job(prompt="p", repo=str(repo), workdir=str(repo), model=None)
    dead = subprocess.Popen(["true"])
    dead.wait()
    store.update_job(job.id, status="running", pid=dead.pid)

    cli.main(["ls"])

    assert store.get_job(job.id).status == "failed"
    assert "worker 비정상 종료" in capsys.readouterr().out


def test_rm은_실행중_작업을_거부하고_끝난_작업의_worktree를_지운다(env, repo):
    cli.main(["run", "x", "-C", str(repo)])
    wt = config.load().worktrees_dir / "myrepo-1"
    assert wt.is_dir()

    job_dir = config.load().job_dir(1)
    assert job_dir.is_dir()

    assert cli.main(["rm", "1"]) == 0

    assert not wt.exists()
    branches = subprocess.run(["git", "-C", str(repo), "branch"], capture_output=True, text=True).stdout
    assert "coliday/1" not in branches
    # 작업 자체도 목록·기록에서 사라진다
    store = _store()
    assert store.list_jobs() == [] and store.events(1) == [] and not job_dir.exists()
    assert cli.main(["rm", "1"]) == 2
    assert cli.main(["show", "1"]) == 2

    cli.main(["run", "y", "-C", str(repo)])
    assert store.list_jobs()[0].id == 2  # 지운 번호를 다시 쓰지 않는다 (worktree 이름이 겹치지 않게)


def test_rm은_변경이_남은_worktree면_작업을_남기고_force면_지운다(env, repo, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_TOUCH", "created.txt")
    cli.main(["run", "x", "-C", str(repo)])
    wt = config.load().worktrees_dir / "myrepo-1"

    assert cli.main(["rm", "1"]) == 2
    assert wt.is_dir() and _store().get_job(1).status == "succeeded"

    assert cli.main(["rm", "1", "--force"]) == 0
    assert not wt.exists() and _store().list_jobs() == []


def test_rm은_worktree_없이_실행한_작업의_기록만_지운다(env, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "keep.txt").write_text("keep\n")
    cli.main(["run", "x", "-C", str(plain), "--no-worktree"])

    assert cli.main(["rm", "1"]) == 0

    assert _store().list_jobs() == [] and (plain / "keep.txt").read_text() == "keep\n"


def test_rm은_worktree가_이미_없는_작업도_지우고_실행중이면_거부한다(env, repo, monkeypatch):
    import shutil
    cli.main(["run", "x", "-C", str(repo)])
    shutil.rmtree(config.load().worktrees_dir / "myrepo-1")
    assert cli.main(["rm", "1"]) == 0
    assert _store().list_jobs() == []

    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    cli.main(["run", "x", "-C", str(repo), "-d"])
    store = _store()
    _wait(lambda: store.get_job(2).status == "running")
    pid = store.get_job(2).pid
    assert cli.main(["rm", "2"]) == 2
    assert store.get_job(2).status == "running"
    cli.main(["cancel", "2"])
    _wait(lambda: os.waitpid(pid, os.WNOHANG)[0] == pid)


def test_continue는_같은_worktree에서_이전_세션을_resume한다(env, repo, capsys):
    cli.main(["run", "주문 취소 API 추가", "-C", str(repo)])
    first = _store().get_job(1)
    capsys.readouterr()

    assert cli.main(["continue", "1", "테스트도 추가해줘"]) == 0

    store = _store()
    assert [j.id for j in store.list_jobs()] == [1]  # 새 작업을 만들지 않는다
    job = store.get_job(1)
    assert job.status == "succeeded"
    assert (job.workdir, job.branch, job.session_id) == (first.workdir, "coliday/1", "s-1")
    assert job.cost_usd == pytest.approx(0.10)  # 두 번 실행한 비용의 합
    recorded = json.loads((env / "args.json").read_text())
    assert recorded["stdin"] == "테스트도 추가해줘"
    assert recorded["argv"][recorded["argv"].index("--resume") + 1] == "s-1"
    assert Path(recorded["cwd"]).resolve() == Path(first.workdir).resolve()
    # 원본 이벤트 로그는 덮어쓰지 않고 덧붙인다
    stream = (config.load().job_dir(1) / "stream.jsonl").read_text()
    assert stream.count('"subtype":"success"') == 6
    out = capsys.readouterr().out
    assert out.count("architect에게 분배") == 1  # 이전 실행의 타임라인을 다시 출력하지 않는다
    assert "사용자" in out and "테스트도 추가해줘" in out


def test_처음_실행은_resume_없이_시작한다(env, repo):
    cli.main(["run", "x", "-C", str(repo)])

    assert "--resume" not in json.loads((env / "args.json").read_text())["argv"]


def test_실행중인_작업에_보낸_요청은_지금_실행이_끝나면_같은_세션으로_이어서_전달된다(env, repo, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "2")
    cli.main(["run", "처음 요청", "-C", str(repo), "-d"])
    store = _store()
    _wait(lambda: store.get_job(1).status == "running")
    pid = store.get_job(1).pid
    capsys.readouterr()

    assert cli.main(["continue", "1", "추가 요청 1", "-d"]) == 0
    assert cli.main(["continue", "1", "추가 요청 2", "-d"]) == 0
    assert "실행 중이라 요청을 대기시켰습니다" in capsys.readouterr().out
    assert [e.message for e in store.events(1) if e.kind == "queued"] == ["추가 요청 1", "추가 요청 2"]
    assert store.get_job(1).status == "running" and store.last_follow_up(1) is None

    _wait(lambda: os.waitpid(pid, os.WNOHANG)[0] == pid, timeout=30)  # 같은 worker 가 두 번째 실행까지 마친다
    job = store.get_job(1)
    assert job.status == "succeeded" and job.cost_usd == pytest.approx(0.10)
    recorded = json.loads((env / "args.json").read_text())
    assert recorded["stdin"] == "추가 요청 1\n\n추가 요청 2"  # 대기한 요청을 한 번에 전달
    assert recorded["argv"][recorded["argv"].index("--resume") + 1] == "s-1"
    kinds = [e.kind for e in store.events(1) if e.kind in ("queued", "prompt", "result")]
    assert kinds == ["result", "prompt", "result"]  # 대기 표시는 후속 요청으로 바뀐다


def test_취소한_작업에_대기시킨_요청은_전달하지_않는다(env, repo, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    cli.main(["run", "x", "-C", str(repo), "-d"])
    store = _store()
    _wait(lambda: store.get_job(1).status == "running")
    pid = store.get_job(1).pid
    cli.main(["continue", "1", "나중에", "-d"])

    cli.main(["cancel", "1"])
    _wait(lambda: os.waitpid(pid, os.WNOHANG)[0] == pid)

    assert store.get_job(1).status == "cancelled"
    assert not any(e.kind in ("queued", "prompt") for e in store.events(1))


def test_worker가_죽어_남은_대기_요청은_다음에_이어서_시킬_때_함께_전달된다(env, repo):
    cli.main(["run", "x", "-C", str(repo)])
    store = _store()
    store.add_event(1, "사용자", "queued", "전달 못 한 요청")  # 대기시킨 뒤 worker 가 죽은 상황

    assert cli.main(["continue", "1", "새 요청"]) == 0

    assert json.loads((env / "args.json").read_text())["stdin"] == "전달 못 한 요청\n\n새 요청"
    assert not any(e.kind == "queued" for e in store.events(1))


def test_continue는_worktree가_없거나_삭제된_작업을_거부한다(env, repo, capsys):
    cli.main(["run", "x", "-C", str(repo)])
    store = _store()
    cli.main(["cancel", "1"])
    store._conn.execute("UPDATE jobs SET status = 'cancelled' WHERE id = 1")
    import shutil
    shutil.rmtree(config.load().worktrees_dir / "myrepo-1")  # worktree 만 직접 지운 경우
    assert cli.main(["continue", "1", "더 해줘"]) == 2
    assert "작업 디렉토리가 없습니다" in capsys.readouterr().err
    assert store.get_job(1).status == "cancelled"
    assert store.last_follow_up(1) is None

    cli.main(["rm", "1"])  # 삭제한 작업
    assert cli.main(["continue", "1", "더 해줘"]) == 2
    assert "찾을 수 없습니다" in capsys.readouterr().err


def test_오래된_작업을_이어서_시켜도_대기_시간초과로_정리되지_않는다(env, repo):
    store = _store()
    job = store.create_job(prompt="p", repo=str(repo), workdir=str(repo), model=None)
    store.finish_job(job.id, "succeeded", phase="완료")
    store._conn.execute("UPDATE jobs SET created_at = created_at - 3600 WHERE id = ?", (job.id,))

    assert store.requeue_job(job.id, "이어서") is True
    assert store.requeue_job(job.id, "중복 요청") is False  # 이미 대기 중이면 다시 이어가지 않는다

    assert jobs.reconcile(store, store.get_job(job.id)).status == "queued"
    assert [e.message for e in store.events(job.id)] == ["이어서"]


def test_이어서_시킨_실행이_세션_없이_실패해도_이전_세션은_남는다(env, repo, monkeypatch):
    cli.main(["run", "x", "-C", str(repo)])
    fake_bin = env / "claude"
    fake_bin.write_text(
        "#!/bin/sh\n[ \"$1\" = --version ] && { echo '2.1.331 (Claude Code)'; exit 0; }\n"
        "echo 'No conversation found' >&2\nexit 1\n"
    )

    assert cli.main(["continue", "1", "더 해줘"]) == 1

    job = _store().get_job(1)
    assert (job.status, job.session_id, job.cost_usd) == ("failed", "s-1", 0.05)


def _recorded(env):
    recorded = json.loads((env / "args.json").read_text())
    argv = recorded["argv"]
    agents = json.loads(Path(argv[argv.index("--agents") + 1]).read_text())
    return argv, agents


def _role_prompt(agents, name, job_id=1):
    """팀원이 실제로 받는 역할 지침: Codex 팀원이면 실행 파일에 들어 있다 (래퍼 프롬프트가 아니라)."""
    runners = [p for p in (config.load().job_dir(job_id) / "codex").glob(f"*-{name}")]
    return runners[0].read_text() if runners else agents[name]["prompt"]


def test_model로_바꾼_팀장과_팀원_모델이_다음_실행에_적용된다(env, repo, capsys):
    assert cli.main(["model", "lead", "sonnet"]) == 0
    assert cli.main(["model", "coder", "haiku"]) == 0
    capsys.readouterr()

    assert cli.main(["model"]) == 0
    lines = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()}
    assert "sonnet" in lines["lead"] and "설정함" in lines["lead"]
    assert "haiku" in lines["coder"] and "설정함" in lines["coder"]
    assert "opus" in lines["architect"] and "기본값" in lines["architect"]

    cli.main(["run", "x", "-C", str(repo)])
    argv, agents = _recorded(env)
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert agents["coder"]["model"] == "haiku"
    assert agents["architect"]["model"] == "opus"  # 바꾸지 않은 팀원은 정의대로

    cli.main(["run", "x", "-C", str(repo), "--model", "opus"])  # 작업별 지정이 설정보다 우선
    argv, _ = _recorded(env)
    assert argv[argv.index("--model") + 1] == "opus"


def test_model_reset은_기본값으로_되돌린다(env, repo):
    from coliday import team
    cli.main(["model", "lead", "sonnet"])
    cli.main(["model", "coder", "haiku"])

    assert cli.main(["model", "lead", "--reset"]) == 0
    assert cli.main(["model", "coder", "--reset"]) == 0

    assert team.load_models(config.load().models_path) == team.Models()
    cli.main(["run", "x", "-C", str(repo)])
    argv, agents = _recorded(env)
    assert "--model" not in argv
    assert "model" not in agents["coder"]
    assert "model" not in team.DEFAULT_AGENTS["coder"]  # 기본 정의를 오염시키지 않는다


def test_model은_없는_역할과_잘못된_입력을_거부한다(env, capsys):
    assert cli.main(["model", "nobody", "opus"]) == 2
    assert "알 수 없는 역할" in capsys.readouterr().err
    assert cli.main(["model", "coder"]) == 2
    assert cli.main(["model", "coder", "opus", "--reset"]) == 2
    assert not config.load().models_path.exists()


def test_깨진_models_json은_작업을_만들지_않고_오류로_알린다(env, repo, capsys):
    config.load().home.mkdir(parents=True)
    config.load().models_path.write_text('{"lead": 3}')

    assert cli.main(["run", "x", "-C", str(repo)]) == 2

    assert "모델 설정을 읽을 수 없습니다" in capsys.readouterr().err
    assert _store().list_jobs() == []


def test_continue는_바뀐_팀장_모델을_쓰고_설정이_없으면_이전_모델을_유지한다(env, repo):
    cli.main(["run", "x", "-C", str(repo)])

    cli.main(["continue", "1", "더"])
    argv, _ = _recorded(env)
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"  # 첫 실행에서 보고된 모델

    cli.main(["model", "lead", "sonnet"])
    cli.main(["continue", "1", "더"])
    argv, _ = _recorded(env)
    assert argv[argv.index("--model") + 1] == "sonnet"


def test_prompt는_실행에_쓰이는_역할별_프롬프트를_보여준다(env, capsys):
    from coliday import team
    cli.main(["model", "coder", "haiku"])
    capsys.readouterr()

    assert cli.main(["prompt"]) == 0
    out = capsys.readouterr().out
    assert team.LEAD_PROMPT.strip() in out
    for name, spec in team.DEFAULT_AGENTS.items():
        assert spec["prompt"].strip() in out and spec["description"] in out
    assert "── coder · 모델: claude:haiku · 도구: Read, Grep, Glob, Edit, Write, Bash ──" in out

    assert cli.main(["prompt", "reviewer"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("── reviewer · 모델: claude:opus")
    assert "coliday 팀장 모드" not in out and "시니어 개발자" not in out

    assert cli.main(["prompt", "nobody"]) == 2
    assert "알 수 없는 역할" in capsys.readouterr().err


def test_prompt는_agents_json으로_바꾼_팀원_정의를_보여준다(env, capsys):
    config.load().home.mkdir(parents=True)
    config.load().agents_override.write_text(json.dumps({"qa": {"description": "검수", "prompt": "너는 QA 다."}}))

    assert cli.main(["prompt"]) == 0

    out = capsys.readouterr().out
    assert "── qa · 모델: claude (팀장과 같은 모델) ──" in out and "너는 QA 다." in out
    assert "시니어 개발자" not in out


def test_project로_디렉토리를_연결하고_뺀다(env, repo, tmp_path, capsys):
    other = tmp_path / "other"
    other.mkdir()

    assert cli.main(["project", "add", str(repo)]) == 0
    assert cli.main(["project", "add", str(other)]) == 0
    assert cli.main(["project", "add", str(repo / ".")]) == 0  # 같은 디렉토리는 한 번만
    assert jobs.list_projects(config.load(), _store()) == [str(repo.resolve()), str(other.resolve())]

    capsys.readouterr()
    assert cli.main(["project"]) == 0
    out = capsys.readouterr().out
    assert str(repo.resolve()) in out and str(other.resolve()) in out

    assert cli.main(["project", "rm", str(repo)]) == 0
    assert jobs.list_projects(config.load(), _store()) == [str(other.resolve())]
    assert repo.is_dir()  # 목록에서만 뺀다

    assert cli.main(["project", "rm", str(repo)]) == 2
    assert cli.main(["project", "add", str(tmp_path / "nope")]) == 2
    assert cli.main(["project", "rm"]) == 2


def test_run한_디렉토리는_프로젝트_목록에_연결된다(env, repo, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    cli.main(["run", "x", "-C", str(repo / "sub")])
    cli.main(["run", "x", "-C", str(plain)])  # 거부된 실행은 연결하지 않는다

    assert jobs.list_projects(config.load(), _store()) == [str((repo / "sub").resolve())]


def test_목록_파일이_없으면_기존_작업의_레포로_채운다(env, repo, tmp_path):
    store = _store()
    gone = tmp_path / "gone"
    for r in (repo, gone, repo):
        store.create_job(prompt="p", repo=str(r), workdir=str(r), model=None)

    assert jobs.list_projects(config.load(), store) == [str(repo)]

    jobs.remove_project(config.load(), store, repo)
    assert jobs.list_projects(config.load(), store) == []  # 뺀 뒤에는 다시 채우지 않는다


def test_깨진_projects_json이어도_run은_실행되고_경고만_남긴다(env, repo, capsys):
    config.load().home.mkdir(parents=True)
    config.load().projects_path.write_text("{not json")

    assert cli.main(["run", "x", "-C", str(repo)]) == 0

    assert "프로젝트 목록에 추가하지 못했습니다" in capsys.readouterr().err
    assert cli.main(["project"]) == 2


def test_project_add_name으로_이름을_붙이고_바꾼다(env, repo, capsys):
    assert cli.main(["project", "add", str(repo), "--name", "쇼핑몰 백엔드"]) == 0
    assert cli.main(["project", "add", str(repo)]) == 0  # 이름 없이 다시 추가해도 이름은 남는다
    store = _store()
    assert jobs.project_names(config.load(), store) == {str(repo.resolve()): "쇼핑몰 백엔드"}

    capsys.readouterr()
    cli.main(["project"])
    assert "쇼핑몰 백엔드" in capsys.readouterr().out

    cli.main(["project", "add", str(repo), "--name", "shop"])
    assert jobs.project_names(config.load(), store) == {str(repo.resolve()): "shop"}
    cli.main(["project", "add", str(repo), "--name", ""])  # 빈 이름은 폴더 이름으로 되돌린다
    assert jobs.project_names(config.load(), store) == {str(repo.resolve()): "myrepo"}

    cli.main(["run", "x", "-C", str(repo)])  # 작업을 시작해도 목록과 이름은 그대로
    assert jobs.list_projects(config.load(), store) == [str(repo.resolve())]


def test_경로만_적힌_이전_형식의_projects_json도_읽는다(env, repo):
    config.load().home.mkdir(parents=True)
    config.load().projects_path.write_text(json.dumps([str(repo), {"path": "/x/y", "name": "와이"}]))

    assert jobs.project_names(config.load(), _store()) == {str(repo): "myrepo", "/x/y": "와이"}


def test_이어서_시켜도_이전_팀장_보고가_타임라인에_남는다(env, repo, capsys):
    cli.main(["run", "x", "-C", str(repo)])
    store = _store()
    # 보고를 이벤트로 남기기 전에 끝난 작업처럼 만든다
    store._conn.execute("DELETE FROM events WHERE kind = 'result'")
    store.update_job(1, result="첫 번째 보고")
    capsys.readouterr()

    assert cli.main(["continue", "1", "더 해줘"]) == 0

    results = [e.message for e in store.events(1) if e.kind == "result"]
    assert results == ["첫 번째 보고", "최종 보고: 완료"]
    order = [e.kind for e in store.events(1) if e.kind in ("result", "prompt")]
    assert order == ["result", "prompt", "result"]  # 이전 보고 → 후속 요청 → 새 보고

    capsys.readouterr()
    cli.main(["show", "1"])
    out = capsys.readouterr().out
    assert out.count("첫 번째 보고") == 1 and out.count("최종 보고: 완료") == 1  # 마지막 보고를 두 번 찍지 않는다
    assert out.index("첫 번째 보고") < out.index("더 해줘") < out.index("최종 보고: 완료")


def test_한_번_실행한_작업의_보고는_show에_한_번만_나온다(env, repo, capsys):
    cli.main(["run", "x", "-C", str(repo)])
    assert capsys.readouterr().out.count("최종 보고: 완료") == 1

    cli.main(["show", "1"])
    assert capsys.readouterr().out.count("최종 보고: 완료") == 1


def test_기본_에이전트를_codex로_바꾸면_따로_고르지_않은_역할이_모두_codex로_일한다(env, repo, capsys):
    assert cli.main(["model", "default", "codex"]) == 0
    assert cli.main(["model", "reviewer", "claude:opus"]) == 0   # 이 역할만 Claude 의 특정 모델
    assert cli.main(["model", "verifier", "codex:gpt-6.1-sol"]) == 0
    capsys.readouterr()

    cli.main(["model"])
    lines = {line.split()[0]: line for line in capsys.readouterr().out.splitlines() if line.strip()}
    assert "codex" in lines["default"] and "설정함" in lines["default"]
    assert lines["lead"].split()[1:] == ["codex", "기본값"]
    assert lines["architect"].split()[1:] == ["codex", "기본값"]      # 정의의 opus 대신 기본 에이전트
    assert lines["coder"].split()[1:] == ["codex", "기본값"]
    assert lines["reviewer"].split()[1:] == ["claude:opus", "설정함"]
    assert lines["verifier"].split()[1:] == ["codex:gpt-6.1-sol", "설정함"]

    assert cli.main(["run", "x", "-C", str(repo)]) == 0
    assert not (env / "args.json").exists()  # 팀장이 Codex 로 실행됐다
    assert _store().get_job(1).model == "codex"


def test_기본_에이전트가_claude면_claude_접두사와_모델만_쓴_값이_같은_뜻이다(env, repo, capsys):
    cli.main(["model", "lead", "claude:sonnet"])
    cli.main(["model", "coder", "haiku"])          # 에이전트 없이 모델만: Claude 모델
    cli.main(["model", "codex-coder", "claude"])   # Codex 로 정의된 팀원을 Claude 기본 모델로
    capsys.readouterr()

    cli.main(["model"])
    lines = {line.split()[0]: line for line in capsys.readouterr().out.splitlines() if line.strip()}
    assert lines["default"].split()[1:] == ["claude", "기본값"]
    assert "claude:sonnet" in lines["lead"] and "claude:haiku" in lines["coder"]
    assert lines["codex-coder"].split()[1] == "claude" and "claude:opus" in lines["architect"]

    cli.main(["run", "x", "-C", str(repo)])
    argv, agents = _recorded(env)
    assert argv[argv.index("--model") + 1] == "sonnet"  # claude 에는 모델 이름만 넘긴다
    assert agents["coder"]["model"] == "haiku" and agents["architect"]["model"] == "opus"
    assert "model" not in agents["codex-coder"] and "codex exec" not in agents["codex-coder"]["prompt"]

    assert cli.main(["model", "default", "--reset"]) == 0
    assert cli.main(["model", "lead", "claude:"]) == 2  # 모델 이름이 빠짐


def test_이어서_시킬_때_기본_에이전트를_바꾸면_팀장도_바뀐다(env, repo):
    cli.main(["run", "x", "-C", str(repo)])          # Claude 팀장
    cli.main(["model", "default", "codex"])

    assert cli.main(["continue", "1", "더"]) == 0     # Codex 로 넘어간다 (새 세션)
    assert _codex_calls(env) and "resume" not in _codex_calls(env)[-1]["argv"]
    assert _store().get_job(1).model == "codex"

    cli.main(["model", "default", "claude"])
    (env / "args.json").unlink()
    assert cli.main(["continue", "1", "다시"]) == 0   # 다시 Claude 기본 모델
    argv, _ = _recorded(env)
    assert "--model" not in argv and "--resume" not in argv


def test_최대_시도_횟수는_기본_5번이고_팀장과_팀원_지침에_들어간다(env, repo):
    cli.main(["run", "x", "-C", str(repo)])

    argv, agents = _recorded(env)
    lead = argv[argv.index("--append-system-prompt") + 1]
    assert "최대 5번까지만 시도한다" in lead and "## 실패한 작업 정리" in lead
    assert "5번 시도해도 실패하거나" in lead and "{max_attempts}" not in lead
    for name in ("architect", "coder", "reviewer", "verifier", "codex-coder"):  # Codex 래퍼도 역할 지침째로 넘긴다
        prompt = _role_prompt(agents, name)
        assert "최대 5번까지만 시도한다" in prompt, name
        assert '"최대 시도 횟수 도달"' in prompt and "{max_attempts}" not in prompt


def test_COLIDAY_MAX_ATTEMPTS로_최대_시도_횟수를_바꾼다(env, repo, monkeypatch, capsys):
    monkeypatch.setenv("COLIDAY_MAX_ATTEMPTS", "3")
    config.load().home.mkdir(parents=True)
    config.load().agents_override.write_text(json.dumps({"qa": {"description": "검수", "prompt": "너는 QA 다."}}))

    cli.main(["run", "x", "-C", str(repo)])
    argv, agents = _recorded(env)
    lead = argv[argv.index("--append-system-prompt") + 1]
    assert "최대 3번까지만 시도한다" in lead and "5번" not in lead
    assert agents["qa"]["prompt"].startswith("너는 QA 다.") and "최대 3번까지만 시도한다" in agents["qa"]["prompt"]

    cli.main(["run", "y", "-C", str(repo), "--model", "codex"])   # Codex 팀장도 같은 값을 쓴다
    assert "최대 3번까지만 시도한다" in _codex_calls(env)[-1]["stdin"]

    capsys.readouterr()
    cli.main(["prompt"])
    out = capsys.readouterr().out
    assert out.count("최대 3번까지만 시도한다") == 2 and "5번" not in out  # 팀장 + qa


@pytest.mark.parametrize("value", ["0", "-1", "많이", "2.5"])
def test_COLIDAY_MAX_ATTEMPTS가_잘못되면_실행하지_않고_알린다(env, repo, monkeypatch, value):
    monkeypatch.setenv("COLIDAY_MAX_ATTEMPTS", value)

    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "x", "-C", str(repo)])

    assert "COLIDAY_MAX_ATTEMPTS" in str(exc.value)


def test_화면_작업의_결과물_폴더가_팀장과_쓰기_가능한_팀원_지침에_들어간다(env, repo):
    cli.main(["run", "x", "-C", str(repo)])

    folder = str(config.load().artifacts_dir(1))
    argv, agents = _recorded(env)
    lead = argv[argv.index("--append-system-prompt") + 1]
    assert "## 화면 작업의 결과물" in lead and f"- 결과물 폴더: {folder}" in lead
    assert argv[argv.index("--add-dir") + 1] == folder and Path(folder).is_dir()
    for name in ("coder", "codex-coder", "verifier"):
        prompt = _role_prompt(agents, name)
        assert f"이 폴더에 저장한다: {folder}" in prompt, name
        assert "하위 폴더를 만들지 말고" in prompt and "작업 공간으로 쓰지 않는다" in prompt
    for name in ("architect", "reviewer"):  # 읽기만 하는 역할
        assert "화면 작업의 결과물" not in agents[name]["prompt"], name


def test_결과물이_있으면_경로를_알려주고_open으로_폴더를_연다(env, repo, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_ARTIFACT", "login-page.png,flows/order-cancel.webm,notes.txt,.hidden")
    cli.main(["run", "x", "-C", str(repo)])
    folder = config.load().artifacts_dir(1)

    out = capsys.readouterr().out
    assert f"결과물 2개 (사진 1, 영상 1) + 기타 파일 1개: {folder}" in out and "coliday open 1" in out

    assert cli.main(["open", "1"]) == 0
    out = capsys.readouterr().out
    assert "login-page.png" in out and ".hidden" not in out
    assert "notes.txt" in out  # 맨 위에 둔 사진·영상 아닌 파일은 사용자가 확인할 파일이다: 그대로 두고 함께 보여 준다
    # 하위 폴더에 저장한 사진·영상은 맨 위로 꺼내 한 폴더에서 다 보이게 한다
    assert "flows-order-cancel.webm" in out and "flows/" not in out
    assert sorted(p.name for p in folder.iterdir() if not p.name.startswith(".")) == [
        "flows-order-cancel.webm", "login-page.png", "notes.txt"]
    _wait(lambda: (env / "opened.txt").exists())
    assert (env / "opened.txt").read_text().strip() == str(folder)

    cli.main(["show", "1"])
    assert "결과물 2개" in capsys.readouterr().out

    monkeypatch.delenv("FAKE_CLAUDE_ARTIFACT")
    cli.main(["continue", "1", "더"])   # 이어서 시켜도 이전 결과물은 남는다
    assert len(jobs.artifacts(config.load(), 1)) == 3
    cli.main(["rm", "1"])               # 작업을 지우면 결과물도 지워진다
    assert not folder.exists()


def test_결과물_정리는_사진_영상을_맨_위로_꺼내고_하위_폴더의_나머지는_other로_모은다(tmp_path):
    root = tmp_path / "artifacts"
    (root / "verify" / "deep").mkdir(parents=True)
    (root / "01-ok.png").write_text("top")
    (root / "verify-01-ok.png").write_text("already")
    (root / "verify" / "01-ok.png").write_text("nested")          # 이름이 겹친다
    (root / "verify" / "deep" / "flow.WEBM").write_text("video")
    (root / "verify" / "check.js").write_text("script")
    (root / "dom-ok.html").write_text("html")
    (root / ".tmp").mkdir()
    (root / ".tmp" / "work.png").write_text("hidden")

    jobs.organize(root)
    jobs.organize(root)  # 여러 번 불러도 같다

    assert sorted(p.name for p in root.iterdir()) == [
        ".tmp", "01-ok.png", "_other", "dom-ok.html", "verify-01-ok-2.png", "verify-01-ok.png", "verify-deep-flow.WEBM"]
    assert (root / "verify-01-ok.png").read_text() == "already" and (root / "verify-01-ok-2.png").read_text() == "nested"
    assert sorted(str(p.relative_to(root / "_other")) for p in (root / "_other").rglob("*") if p.is_file()) == [
        "verify/check.js"]
    assert jobs.artifacts_summary([root / "report.md"]) == "파일 1개"   # 사진·영상 없이 확인할 파일만 있을 때


def test_실행중에는_결과물_폴더를_정리하지_않는다(env, repo, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    cli.main(["run", "x", "-C", str(repo), "-d"])
    store = _store()
    _wait(lambda: store.get_job(1).status == "running")
    pid = store.get_job(1).pid
    work = config.load().artifacts_dir(1) / "verify"
    _wait(lambda: work.parent.is_dir())
    work.mkdir()
    (work / "shot.png").write_text("x")      # 팀원이 작업 공간으로 쓰는 중
    (work / "check.js").write_text("x")

    assert cli.main(["open", "1"]) == 0
    assert len(jobs.artifacts(config.load(), 1)) == 2
    assert (work / "shot.png").is_file() and (work / "check.js").is_file()  # 옮기지 않았다

    cli.main(["cancel", "1"])
    _wait(lambda: os.waitpid(pid, os.WNOHANG)[0] == pid)
    assert cli.main(["open", "1"]) == 0      # 끝난 뒤에 열면 정리된다
    root = config.load().artifacts_dir(1)
    assert (root / "verify-shot.png").is_file() and (root / "_other" / "verify" / "check.js").is_file()


def test_결과물이_아직_없어도_open은_폴더를_열고_없다고_알린다(env, repo, capsys):
    cli.main(["run", "x", "-C", str(repo)])
    capsys.readouterr()

    assert cli.main(["open", "1"]) == 0
    assert "아직 결과물이 없습니다" in capsys.readouterr().out
    _wait(lambda: (env / "opened.txt").exists())
    assert (env / "opened.txt").read_text().strip() == str(config.load().artifacts_dir(1))
    assert cli.main(["open", "99"]) == 2
    cli.main(["show", "1"])
    assert "coliday open 1" not in capsys.readouterr().out  # 결과물 줄을 찍지 않는다


def test_codex로_도는_역할도_결과물_폴더에_쓸_수_있게_샌드박스를_연다(env, repo, monkeypatch):
    cli.main(["model", "default", "codex"])
    monkeypatch.setenv("FAKE_CODEX_DELEGATE", "coder")
    cli.main(["run", "x", "-C", str(repo), "--allow-env"])

    folder = str(config.load().artifacts_dir(1))
    lead, member = sorted(_codex_calls(env), key=lambda c: "# 사용자 요청" not in c["stdin"])
    lead_roots = next(a for a in lead["argv"] if a.startswith("sandbox_workspace_write.writable_roots="))
    assert folder in json.loads(lead_roots.split("=", 1)[1]) and f"- 결과물 폴더: {folder}" in lead["stdin"]
    assert f"sandbox_workspace_write.writable_roots={json.dumps([folder])}" in member["argv"]
    assert f"이 폴더에 저장한다: {folder}" in member["stdin"]

    monkeypatch.setenv("FAKE_CODEX_DELEGATE", "reviewer")   # 읽기 전용 역할은 열지 않는다
    cli.main(["run", "y", "-C", str(repo), "--allow-env"])
    member = [c for c in _codex_calls(env) if "# 사용자 요청" not in c["stdin"]][-1]
    assert 'sandbox_mode="read-only"' in member["argv"] and not any("writable_roots" in a for a in member["argv"])


def test_홈이_심볼릭_링크여도_codex에는_링크를_푼_경로만_넘긴다(env, repo, monkeypatch):
    # Codex 는 쓰기 폴더 경로에 심볼릭 링크가 끼면 모든 명령 실행을 거부한다 (~/.coliday → 다른 폴더).
    real = env / "real-home"
    real.mkdir()
    link = env / "link-home"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("COLIDAY_HOME", str(link))
    cli.main(["model", "default", "codex"])
    monkeypatch.setenv("FAKE_CODEX_DELEGATE", "coder")
    cli.main(["run", "x", "-C", str(repo), "--allow-env"])

    calls = _codex_calls(env)
    assert len(calls) == 2  # 팀장, 팀원
    for call in calls:
        roots = json.loads(next(
            a for a in call["argv"] if a.startswith("sandbox_workspace_write.writable_roots=")
        ).split("=", 1)[1])
        assert roots and all(os.path.realpath(r) == r for r in roots)
        assert str(link) not in call["stdin"]  # 지침에 적는 결과물 폴더도 같은 경로여야 한다
    assert str(real.resolve() / "jobs" / "1" / "artifacts") in calls[0]["stdin"]
    assert Path(_store().get_job(1).workdir).is_relative_to(real.resolve())


def test_에이전트와_모델이_안_맞으면_설정과_실행_전에_거부한다(env, repo, capsys):
    assert cli.main(["model", "lead", "codex:fable"]) == 2
    assert "Claude 모델이라 Codex 에서 쓸 수 없습니다" in capsys.readouterr().err
    assert cli.main(["model", "coder", "claude:gpt-6.1-sol"]) == 2
    assert "codex:gpt-6.1-sol 로 쓰거나" in capsys.readouterr().err
    assert cli.main(["model", "coder", "gpt-6.1-sol"]) == 2      # 에이전트 없이 쓰면 Claude 모델로 본다
    assert cli.main(["model", "default", "codex:opus"]) == 2
    assert not config.load().models_path.exists()
    assert cli.main(["run", "x", "-C", str(repo), "--model", "codex:sonnet"]) == 2
    assert _store().list_jobs() == []

    # 이미 저장된 잘못된 값도 실행 전에 알린다 (작업을 만들지 않는다)
    config.load().models_path.write_text(json.dumps({"lead": "codex:fable"}))
    capsys.readouterr()
    assert cli.main(["run", "x", "-C", str(repo)]) == 2
    assert "fable 은(는) Claude 모델" in capsys.readouterr().err and _store().list_jobs() == []
    assert cli.main(["model", "lead", "codex"]) == 0           # 고칠 수 있다
    assert cli.main(["run", "x", "-C", str(repo)]) == 0


def _codex_calls(env):
    path = env / "codex.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_claude_팀장_아래_팀원을_codex로_바꾸면_codex를_돌리는_래퍼가_된다(env, repo):
    cli.main(["model", "coder", "codex"])
    cli.main(["model", "reviewer", "codex:gpt-x"])
    cli.main(["model", "verifier", "codex"])
    cli.main(["model", "codex-coder", "sonnet"])  # 반대로 Codex 기본 팀원을 Claude 로

    cli.main(["run", "x", "-C", str(repo), "--allow-env"])

    argv, agents = _recorded(env)
    assert "--agents" in argv and _codex_calls(env) == []  # 팀장은 Claude
    import shlex
    codex = shlex.quote(os.environ["COLIDAY_CODEX_BIN"])  # 경로에 한글·공백이 있어도 명령이 깨지지 않게 인용된다
    roots = shlex.quote(f'sandbox_workspace_write.writable_roots={json.dumps([str(config.load().artifacts_dir(1))])}')
    # Codex 팀원마다 실행 파일을 하나씩 만들고, 래퍼는 그 파일에 작업 명세만 넘긴다
    runners = {p.name.split("-", 1)[1]: p for p in (config.load().job_dir(1) / "codex").iterdir()}
    assert sorted(runners) == ["coder", "reviewer", "verifier"]
    coder = runners["coder"].read_text()
    mode = shlex.quote('sandbox_mode="workspace-write"')
    assert f'{codex} exec -C "$(pwd -P)" --skip-git-repo-check -c {mode} -c {roots} -o "$OUT" -' in coder
    assert "너는 시니어 개발자다" in coder  # 역할 지침은 그대로 Codex 에 전달
    tasks = config.load().job_dir(1) / "codex-tasks"   # 명세는 파일로 써서 경로만 넘긴다 (heredoc 은 허용 규칙이 어긋난다)
    assert f"{shlex.quote(str(runners['coder']))} <1에서 쓴 파일의 경로>" in agents["coder"]["prompt"]
    assert f"{tasks}/{runners['coder'].name}-<임의의 숫자 6자리>.md" in agents["coder"]["prompt"] and tasks.is_dir()
    assert "<<" not in agents["coder"]["prompt"] and str(tasks) in [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
    assert (agents["coder"]["model"], agents["coder"]["tools"]) == ("haiku", ["Bash", "Read", "Write"])
    read_only = shlex.quote('sandbox_mode="read-only"')
    assert f"--skip-git-repo-check -c {read_only} -m gpt-x" in runners["reviewer"].read_text()  # 수정 도구가 없는 역할
    assert mode in runners["verifier"].read_text()  # 빌드 산출물을 써야 한다
    assert agents["codex-coder"]["model"] == "sonnet" and "Codex CLI" not in agents["codex-coder"]["prompt"]
    assert all("codex_sandbox" not in spec for spec in agents.values())
    assert "Codex CLI" not in agents["architect"]["prompt"]
    # auto 권한 모드의 분류기가 codex 실행을 막지 않게, 그 실행 파일만 허용 규칙으로 연다
    start = argv.index("--allowedTools") + 1
    assert sorted(argv[start:start + 3]) == sorted(f"Bash({shlex.quote(str(p))}:*)" for p in runners.values())


def test_codex_실행_파일은_역할_지침과_작업_명세를_넘기고_답을_출력한다(env, repo, monkeypatch):
    import subprocess
    cli.main(["model", "coder", "codex"])
    cli.main(["run", "x", "-C", str(repo)])
    runner = next(p for p in (config.load().job_dir(1) / "codex").iterdir() if p.name.endswith("-coder"))
    assert os.access(runner, os.X_OK)

    done = subprocess.run([str(runner)], input="로그인 구현\n'따옴표' $HOME", cwd=repo, capture_output=True, text=True)
    assert (done.returncode, done.stdout) == (0, "코덱스 팀원 보고\n")  # 진행 로그 없이 Codex 의 마지막 답만
    call = _codex_calls(env)[-1]
    assert call["stdin"].startswith("너는 시니어 개발자다")
    assert call["stdin"].endswith("\n\n# 작업 명세\n로그인 구현\n'따옴표' $HOME")  # 명세는 고치지 않고 그대로
    assert Path(call["cwd"]).resolve() == repo.resolve()

    spec = repo / "spec.md"                            # 래퍼는 명세를 파일로 넘긴다
    spec.write_text("심각도 [P0]~[P3] `git diff` $(pwd)")
    assert subprocess.run([str(runner), str(spec)], cwd=repo, capture_output=True, text=True).stdout == "코덱스 팀원 보고\n"
    assert _codex_calls(env)[-1]["stdin"].endswith("\n\n# 작업 명세\n심각도 [P0]~[P3] `git diff` $(pwd)")
    missing = subprocess.run([str(runner), str(repo / "none.md")], cwd=repo, capture_output=True, text=True)
    assert missing.returncode == 2 and "작업 명세 파일이 없거나 비어 있습니다" in missing.stderr
    assert len(_codex_calls(env)) == 2                 # 명세가 없으면 Codex 를 부르지 않는다

    monkeypatch.setenv("FAKE_CODEX_EXIT", "3")
    failed = subprocess.run([str(runner)], input="x", cwd=repo, capture_output=True, text=True)
    assert failed.returncode == 3 and failed.stdout == ""
    assert "codex 실행 실패 (종료 코드 3)" in failed.stderr and "codex stderr tail" in failed.stderr


def test_역할_지침에_heredoc_표시가_있어도_실행_파일이_깨지지_않는다():
    resolved = {"qa": {"prompt": "첫 줄\nCOLIDAY_ROLE_PROMPT\n끝 줄", "model": "codex"}}
    _, script = team.codex_runners(resolved)["qa"]
    assert "cat <<'COLIDAY_ROLE_PROMPT_'\n첫 줄\nCOLIDAY_ROLE_PROMPT\n끝 줄\nCOLIDAY_ROLE_PROMPT_\n" in script
    assert team.codex_runners({"이상한 이름/..": {"prompt": "p", "model": "codex"}})["이상한 이름/.."][0] == "1-_________"


def test_기본_codex_coder는_codex_래퍼다(env, repo, capsys):
    cli.main(["run", "x", "-C", str(repo)])
    _, agents = _recorded(env)
    assert "Codex CLI" in agents["codex-coder"]["prompt"] and agents["codex-coder"]["model"] == "haiku"

    capsys.readouterr()
    cli.main(["model"])
    lines = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()}
    assert "codex" in lines["codex-coder"] and "기본값" in lines["codex-coder"]


def test_팀장을_codex로_바꾸면_codex가_실행되고_타임라인과_보고가_남는다(env, repo, capsys):
    assert cli.main(["model", "lead", "codex:gpt-x"]) == 0

    assert cli.main(["run", "주문 취소 API 추가", "-C", str(repo), "--allow-env"]) == 0

    assert not (env / "args.json").exists()  # claude 는 실행하지 않았다
    (call,) = _codex_calls(env)
    job = _store().get_job(1)
    assert call["argv"][0] == "exec" and "resume" not in call["argv"]
    assert 'sandbox_mode="workspace-write"' in call["argv"] and call["argv"][-3:] == ["-m", "gpt-x", "-"]
    assert any(a.startswith("sandbox_workspace_write.writable_roots=[") for a in call["argv"])
    assert Path(call["cwd"]).resolve() == Path(job.workdir).resolve()
    assert "# coliday 팀장 모드" in call["stdin"] and call["stdin"].endswith("# 사용자 요청\n주문 취소 API 추가")
    assert " -m coliday _agent " in call["stdin"] and "- reviewer: " in call["stdin"]
    assert "Agent 도구" not in call["stdin"]

    assert (job.status, job.result, job.session_id, job.model) == ("succeeded", "코덱스 최종 보고\n둘째 줄", "t-1", "codex:gpt-x")
    assert job.cost_usd is None
    events = [(e.actor, e.kind, e.message) for e in _store().events(1)]
    assert ("팀장", "start", "작업 시작 (codex)") in events
    assert ("팀장", "tool", "Bash ls -la") in events and ("팀장", "tool", "Edit a.py") in events
    assert ("팀장", "text", "코덱스 최종 보고") in events
    assert "코덱스 최종 보고" in capsys.readouterr().out


def test_codex_팀장이_claude_팀원에게_맡기면_팀원_과정과_보고가_돌아온다(env, repo, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_DELEGATE", "coder")
    cli.main(["model", "lead", "codex"])
    cli.main(["model", "coder", "sonnet"])

    assert cli.main(["run", "x", "-C", str(repo)]) == 0

    job = _store().get_job(1)
    recorded = json.loads((env / "args.json").read_text())  # 팀원으로 실행된 claude
    argv = recorded["argv"]
    assert recorded["stdin"] == "로그인 구현\n세부 명세"
    assert Path(recorded["cwd"]).resolve() == Path(job.workdir).resolve()
    assert "너는 시니어 개발자다" in argv[argv.index("--append-system-prompt") + 1]
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob,Edit,Write,Bash"
    assert "--agents" not in argv

    events = [(e.actor, e.kind, e.message) for e in _store().events(1)]
    order = [events.index(x) for x in (
        ("팀장", "dispatch", "coder에게 분배: 로그인 구현"),
        ("coder", "start", "작업 시작 (model=claude-opus-5-5)"),
        ("coder", "done", "종료 (completed)"),
        ("팀장", "phase", "coder 결과 검토 중"),
        ("팀장", "text", "팀원 보고(0): 최종 보고: 완료"),
    )]
    assert order == sorted(order)
    assert not any("_agent" in message for _, _, message in events)  # 분배 명령 자체는 타임라인에 넣지 않는다
    assert job.status == "succeeded" and job.cost_usd == 0.05  # Claude 팀원이 쓴 비용


def test_codex_팀장이_codex_팀원과_없는_역할에_맡기는_경우(env, repo, monkeypatch):
    cli.main(["model", "lead", "codex"])
    monkeypatch.setenv("FAKE_CODEX_DELEGATE", "reviewer")
    cli.main(["model", "reviewer", "codex:gpt-r"])

    assert cli.main(["run", "x", "-C", str(repo), "--allow-env"]) == 0

    lead, member = sorted(_codex_calls(env), key=lambda c: "# 사용자 요청" not in c["stdin"])
    assert member["argv"] == ["exec", "--json", "--skip-git-repo-check", "-c", 'sandbox_mode="read-only"', "-m", "gpt-r", "-"]
    assert member["stdin"].startswith("너는 코드 리뷰어다") and member["stdin"].endswith("# 작업 명세\n로그인 구현\n세부 명세")
    events = [(e.actor, e.kind, e.message) for e in _store().events(1)]
    assert ("reviewer", "tool", "Bash ls -la") in events
    assert ("팀장", "text", "팀원 보고(0): 코덱스 팀원 보고") in events

    monkeypatch.setenv("FAKE_CODEX_DELEGATE", "nobody")
    cli.main(["run", "y", "-C", str(repo), "--allow-env"])
    texts = [e.message for e in _store().events(2) if e.kind == "text"]
    assert any(t.startswith("팀원 보고(1): 알 수 없는 역할: nobody") for t in texts)


def test_codex_팀장을_이어서_시키면_resume하고_claude와_바꾸면_새_세션으로_시작한다(env, repo, capsys):
    cli.main(["model", "lead", "codex"])
    cli.main(["run", "x", "-C", str(repo), "--allow-env"])

    assert cli.main(["continue", "1", "더 해줘"]) == 0
    call = _codex_calls(env)[-1]
    assert call["argv"][:3] == ["exec", "resume", "t-1"] and 'sandbox_mode="workspace-write"' in call["argv"]
    assert call["stdin"].endswith("# 사용자 요청\n더 해줘")

    capsys.readouterr()
    assert cli.main(["continue", "1", "claude 로", "--model", "sonnet"]) == 0  # Codex → Claude
    assert "새 세션으로 시작합니다" in capsys.readouterr().err
    argv, _ = _recorded(env)
    assert "--resume" not in argv and argv[argv.index("--model") + 1] == "sonnet"
    assert _store().get_job(1).session_id == "s-1"

    assert cli.main(["continue", "1", "다시 codex", "--model", "codex"]) == 0  # Claude → Codex
    assert "resume" not in _codex_calls(env)[-1]["argv"]
    assert len({e.kind for e in _store().events(1)} & {"result"}) == 1 and _store().get_job(1).status == "succeeded"


def test_codex가_실패하면_failed와_오류를_남기고_없으면_미리_거부한다(env, repo, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CODEX_EXIT", "2")
    assert cli.main(["run", "x", "-C", str(repo), "--model", "codex"]) == 1
    errors = [e.message for e in _store().events(1) if e.kind == "error"]
    assert errors[0] == "codex boom" and "종료 코드 2" in errors[1] and "codex stderr tail" in errors[1]
    assert _store().get_job(1).status == "failed"

    monkeypatch.setenv("COLIDAY_CODEX_BIN", str(env / "no-such-codex"))
    capsys.readouterr()
    assert cli.main(["model", "coder", "codex"]) == 2
    assert "Codex CLI 를 찾을 수 없습니다" in capsys.readouterr().err
    assert cli.main(["run", "x", "-C", str(repo), "--model", "codex"]) == 2
    assert len(_store().list_jobs()) == 1
    assert cli.main(["model", "coder", "codex:bad;rm"]) == 2  # 셸 명령에 들어가는 모델 이름


def test_prompt는_codex_팀장이면_셸로_맡기는_지시문을_보여준다(env, capsys):
    cli.main(["model", "lead", "codex"])
    capsys.readouterr()
    cli.main(["prompt", "lead"])
    out = capsys.readouterr().out
    assert "모델: codex" in out and "## 팀원에게 맡기는 방법" in out and "## 직접 처리하는 일" in out


def _wait(cond, timeout=10.0):
    import time
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.1)
    raise AssertionError("시간 초과")


def _team_file():
    return json.loads(config.load().team_path.read_text())


def test_team으로_바꾼_역할만_저장되고_다음_실행에_적용된다(env, repo, capsys):
    assert cli.main(["team", "set", "reviewer", "--prompt", "너는 보안 리뷰어다."]) == 0
    assert cli.main(["team", "set", "qa", "--description", "구현 뒤 사용자 시나리오 검수",
                     "--prompt", "너는 QA 다.\n둘째 줄", "--tools", "Read, Bash"]) == 0
    assert cli.main(["team", "rm", "codex-coder"]) == 0
    assert _team_file() == {"agents": {   # 바꾼 것만 적힌다. 프롬프트는 줄 단위 배열
        "reviewer": {"prompt": ["너는 보안 리뷰어다."]},
        "qa": {"description": "구현 뒤 사용자 시나리오 검수", "prompt": ["너는 QA 다.", "둘째 줄"], "tools": ["Read", "Bash"]},
        "codex-coder": None,
    }}
    capsys.readouterr()
    assert cli.main(["team"]) == 0
    rows = {line.split()[0]: line.split()[1] for line in capsys.readouterr().out.splitlines()}
    assert rows == {"lead": "기본값", "architect": "기본값", "coder": "기본값", "codex-coder": "뺌",
                    "reviewer": "고침", "verifier": "기본값", "qa": "추가함"}

    cli.main(["run", "x", "-C", str(repo)])
    argv, agents = _recorded(env)
    assert list(agents) == ["architect", "coder", "reviewer", "verifier", "qa"]
    assert agents["reviewer"]["prompt"].startswith("너는 보안 리뷰어다.") and "실패했을 때" in agents["reviewer"]["prompt"]
    assert agents["reviewer"]["tools"] == ["Read", "Grep", "Glob", "Bash"]   # 안 바꾼 항목은 기본값
    assert agents["qa"]["tools"] == ["Read", "Bash"] and agents["qa"]["prompt"].startswith("너는 QA 다.\n둘째 줄")
    lead = argv[argv.index("--append-system-prompt") + 1]
    assert "- qa: 구현 뒤 사용자 시나리오 검수" in lead and "codex-coder" not in lead   # 팀장도 바뀐 팀을 안다

    cli.main(["run", "y", "-C", str(repo), "--model", "codex"])   # Codex 팀장도 같은 팀을 본다
    stdin = _codex_calls(env)[-1]["stdin"]
    assert "- qa: 구현 뒤 사용자 시나리오 검수" in stdin and "codex-coder" not in stdin

    assert cli.main(["team", "reset", "reviewer"]) == 0 and cli.main(["team", "reset", "codex-coder"]) == 0
    assert list(_team_file()["agents"]) == ["qa"]
    assert cli.main(["team", "reset", "--all"]) == 0
    assert not config.load().team_path.exists()   # 바꾼 것이 없으면 파일도 없다


def test_team_set_lead로_팀장_지시문을_바꾼다(env, repo, tmp_path, capsys, monkeypatch):
    capsys.readouterr()
    assert cli.main(["team", "show", "lead"]) == 0
    template = "\n".join(json.loads(capsys.readouterr().out)["prompt"])
    assert "{team}" in template and "{max_attempts}" in template and "## 팀원 (Agent" not in template

    f = tmp_path / "lead.md"
    f.write_text("너는 꼼꼼한 팀장이다.\n{team}\n같은 일은 {max_attempts}번까지만.\n")
    assert cli.main(["team", "set", "lead", "--prompt-file", str(f)]) == 0
    cli.main(["run", "x", "-C", str(repo)])
    argv, _ = _recorded(env)
    lead = argv[argv.index("--append-system-prompt") + 1]
    assert lead.startswith("너는 꼼꼼한 팀장이다.\n## 팀원 (Agent 도구의 subagent_type)\n- architect:")
    assert "같은 일은 5번까지만." in lead and "coliday 팀장 모드" not in lead
    assert "## 화면 작업의 결과물" in lead   # 결과물 규칙은 그대로 붙는다

    # {team} 자리를 지워도 팀원 목록은 들어간다. Codex 팀장은 그 자리에 맡기는 방법이 들어가 위임이 된다
    assert cli.main(["team", "set", "lead", "--prompt", "자리 없는 지시문"]) == 0
    assert _team_file() == {"lead": {"prompt": ["자리 없는 지시문"]}}
    monkeypatch.setenv("FAKE_CODEX_DELEGATE", "coder")
    cli.main(["run", "y", "-C", str(repo), "--model", "codex"])
    lead_call = next(c for c in _codex_calls(env) if "# 사용자 요청" in c["stdin"])
    assert lead_call["stdin"].startswith("자리 없는 지시문\n\n## 팀원에게 맡기는 방법")
    assert len(_codex_calls(env)) == 1 and _store().get_job(2).status == "succeeded"   # coder 는 Claude 로 돌았다

    assert cli.main(["team", "set", "lead", "--prompt", template]) == 0   # 기본과 같으면 저장하지 않는다
    assert not config.load().team_path.exists()
    assert cli.main(["team", "set", "lead", "--description", "x"]) == 2
    assert cli.main(["team", "set", "lead", "--prompt", "  "]) == 2


def test_team은_잘못된_입력을_저장하지_않는다(env, capsys):
    assert cli.main(["team", "set", "qa", "--prompt", "설명이 없다"]) == 2
    assert "설명(description)" in capsys.readouterr().err
    assert cli.main(["team", "set", "bad name", "--description", "d", "--prompt", "p"]) == 2
    assert cli.main(["team", "set", "lead2;rm", "--description", "d", "--prompt", "p"]) == 2
    assert cli.main(["team", "set", "default", "--description", "d", "--prompt", "p"]) == 2
    assert cli.main(["team", "set", "coder", "--tools", "Read; rm -rf"]) == 2
    assert cli.main(["team", "set", "coder", "--prompt", ""]) == 2
    assert cli.main(["team", "set", "coder"]) == 2
    assert cli.main(["team", "rm", "nobody"]) == 2 and cli.main(["team", "rm", "lead"]) == 2
    assert cli.main(["team", "reset", "coder"]) == 2      # 바꾼 적 없는 역할
    assert cli.main(["team", "reset"]) == 2 and cli.main(["team", "rm", "coder", "--prompt", "x"]) == 2
    assert not config.load().team_path.exists()

    for name in ("architect", "coder", "codex-coder", "reviewer"):
        assert cli.main(["team", "rm", name]) == 0
    capsys.readouterr()
    assert cli.main(["team", "rm", "verifier"]) == 2     # 마지막 팀원은 뺄 수 없다
    assert "팀원이 하나는 있어야 합니다" in capsys.readouterr().err
    assert cli.main(["team", "set", "coder", "--tools", "all"]) == 0   # 뺀 기본 역할을 다시 넣으면 기본 정의에서 시작한다
    assert _team_file()["agents"]["coder"] == {"tools": None}
    agents = team.load_agents(config.load().agents_override, team.load_custom(config.load().team_path))
    assert "tools" not in agents["coder"] and agents["coder"]["prompt"].startswith("너는 시니어 개발자다")


def test_손으로_쓴_team_json이_잘못되면_실행_전에_알린다(env, repo, capsys):
    cfg = config.load()
    cfg.home.mkdir(parents=True)
    cfg.team_path.write_text(json.dumps({"agents": {"qa": {"prompt": ["줄1", "줄2"]}}}))   # 설명이 없다
    assert cli.main(["team"]) == 2 and "설명(description)" in capsys.readouterr().err
    assert cli.main(["prompt"]) == 2

    assert cli.main(["run", "x", "-C", str(repo)]) == 2 and _store().list_jobs() == []   # 작업을 만들지 않는다
    assert "팀 설정이 잘못됐습니다" in capsys.readouterr().err

    # 작업을 만든 뒤에 파일이 깨졌으면 그 작업이 실패하고 이유가 타임라인에 남는다
    store = _store()
    job = store.create_job(prompt="p", repo=str(repo), workdir=str(repo), model=None)
    assert worker.run_job(cfg, store, job.id) == 1
    assert store.get_job(job.id).status == "failed"
    assert any("설명(description)" in e.message for e in store.events(job.id))

    cfg.team_path.write_text(json.dumps({"agent": {}}))   # 오타 난 키
    assert cli.main(["team"]) == 2 and "형식이어야 합니다" in capsys.readouterr().err
    cfg.team_path.write_text("{")
    assert cli.main(["team"]) == 2

    cfg.team_path.write_text(json.dumps({"agents": {"qa": {"description": "검수", "prompt": ["줄1", "줄2"]}}}))
    assert jobs.role_definition(cfg, "qa") == {"description": "검수", "prompt": "줄1\n줄2", "tools": None}
    assert cli.main(["model", "qa", "codex"]) == 0   # 새 역할도 모델을 고를 수 있다


def test_agents_json_위에_역할별_변경을_얹는다(env, capsys):
    cfg = config.load()
    cfg.home.mkdir(parents=True)
    cfg.agents_override.write_text(json.dumps({"qa": {"prompt": "너는 QA 다."}, "dev": {"description": "구현", "prompt": "구현한다."}}))
    assert cli.main(["team", "set", "qa", "--prompt", "너는 깐깐한 QA 다."]) == 0   # 바탕에 설명이 없어도 고칠 수 있다
    assert cli.main(["team", "rm", "dev"]) == 0
    agents = team.load_agents(cfg.agents_override, team.load_custom(cfg.team_path))
    assert agents == {"qa": {"prompt": "너는 깐깐한 QA 다."}}


def test_이_컴퓨터에_codex가_없으면_codex_팀원을_claude로_돌린다(env, repo, monkeypatch):
    monkeypatch.setenv("COLIDAY_CODEX_BIN", "no-such-codex")
    assert cli.main(["run", "x", "-C", str(repo)]) == 0
    _, agents = _recorded(env)
    assert "model" not in agents["codex-coder"] and "Codex CLI" not in agents["codex-coder"]["prompt"]
    argv = _recorded(env)[0]
    assert "--allowedTools" not in argv and argv.count("--add-dir") == 1 and not (config.load().job_dir(1) / "codex").exists()
    assert any("이 컴퓨터에 codex 가 없어 codex-coder 은(는) Claude 로 실행합니다" == e.message for e in _store().events(1))
    assert cli.main(["run", "y", "-C", str(repo), "--model", "codex"]) == 2   # Codex 팀장은 대신할 수 없다: 종전대로 알린다


# ── .env 읽기 차단 (guard.py) ──────────────────────────────────────────
def _settings(argv):
    return json.loads(argv[argv.index("--settings") + 1]) if "--settings" in argv else None


def test_기본으로는_env_파일을_읽지_못하게_막고_실행한다(env, repo):
    assert cli.main(["run", "x", "-C", str(repo)]) == 0
    argv, agents = _recorded(env)
    cfg = config.load()
    settings = _settings(argv)
    assert settings["permissions"]["deny"] == ["Read(**/.env*)", "Read(//**/.env*)"]   # 작업 디렉토리 안과 밖
    sandbox = settings["sandbox"]
    # 규칙만으로는 grep -r 이나 python open() 으로 샌다: OS 샌드박스를 켜고, 밖에서 다시 실행하는 길도 닫는다
    assert (sandbox["enabled"], sandbox["allowUnsandboxedCommands"], sandbox["failIfUnavailable"]) == (True, False, True)
    assert all(g.endswith("/**/.env*") for g in sandbox["filesystem"]["denyRead"]) and sandbox["filesystem"]["denyRead"]
    assert str(cfg.db_path) in sandbox["filesystem"]["denyWrite"]         # 에이전트가 허용 여부를 스스로 바꾸지 못하게
    assert str(cfg.artifacts_dir(1)) in sandbox["filesystem"]["allowWrite"]
    assert sandbox["network"]["allowedDomains"] == ["*"]                   # 빌드가 의존성을 받을 수 있게 네트워크는 막지 않는다

    runner = next((cfg.job_dir(1) / "codex").glob("*-codex-coder"))        # Codex 팀원은 Codex 의 권한 프로필로 막는다
    script = runner.read_text()
    assert 'default_permissions="coliday"' in script and '"**/.env*"="none"' in script and "sandbox_mode" not in script
    assert f'"{cfg.artifacts_dir(1)}"="write"' in script
    import shlex
    assert sandbox["excludedCommands"] == [f"{shlex.quote(str(runner))} *"]  # Codex 는 Claude 의 샌드박스 밖에서 돈다
    assert not _store().allow_env(1)
    assert any(".env 파일은 읽지 못하게 막고 실행합니다" in e.message for e in _store().events(1))


def test_사람이_허용한_작업에서만_env_읽기를_막지_않는다(env, repo, capsys):
    assert cli.main(["run", "x", "-C", str(repo), "--allow-env"]) == 0
    argv, _ = _recorded(env)
    assert _settings(argv) is None and _store().allow_env(1)
    assert any("사람이 허용한 작업입니다" in e.message for e in _store().events(1))
    assert "sandbox_mode" in next((config.load().job_dir(1) / "codex").glob("*-codex-coder")).read_text()

    assert cli.main(["continue", "1", "더 해줘"]) == 0                      # 이어서 시키면 처음 정한 대로다
    assert _settings(_recorded(env)[0]) is None
    assert cli.main(["continue", "1", "더 해줘", "--deny-env"]) == 0         # 사람이 다시 막을 수 있다
    assert _settings(_recorded(env)[0]) is not None and not _store().allow_env(1)
    assert cli.main(["continue", "1", "더 해줘", "--allow-env"]) == 0
    assert _settings(_recorded(env)[0]) is None and _store().allow_env(1)

    assert cli.main(["run", "y", "-C", str(repo)]) == 0                    # 허용은 그 작업에만 해당한다
    assert _settings(_recorded(env)[0]) is not None and not _store().allow_env(2)


def test_env를_막지_못하는_버전이면_막지_않은_채_돌리지_않고_알린다(env, repo, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_VERSION", "1.0.58 (Claude Code)")
    assert cli.main(["run", "x", "-C", str(repo)]) == 2
    err = capsys.readouterr().err
    assert "claude(1.0.58)는 .env 읽기를 막지 못합니다" in err and "2.1.325 이상" in err and "--allow-env" in err
    assert _store().list_jobs() == []
    assert cli.main(["run", "x", "-C", str(repo), "--allow-env"]) == 0     # 사람이 허용하면 시작한다
    assert cli.main(["continue", "1", "y", "--deny-env"]) == 2             # 다시 막으려 해도 막을 수 없으면 거부한다
    assert _store().allow_env(1)

    monkeypatch.delenv("FAKE_CLAUDE_VERSION")
    monkeypatch.setenv("FAKE_CODEX_VERSION", "codex-cli 0.120.0")          # 낮은 Codex: 팀원은 Claude 로 돌리고, 팀장은 거부
    assert cli.main(["run", "z", "-C", str(repo)]) == 0
    _, agents = _recorded(env)
    assert "Codex CLI" not in agents["codex-coder"]["prompt"] and "model" not in agents["codex-coder"]
    assert any("codex(0.120.0)가 .env 읽기를 막지 못해 codex-coder 은(는) Claude 로 실행합니다" in e.message
               for e in _store().events(2))
    assert cli.main(["run", "w", "-C", str(repo), "--model", "codex"]) == 2
    assert "codex(0.120.0)는 .env 읽기를 막지 못합니다" in capsys.readouterr().err


def test_codex_팀장과_그_팀원도_env를_읽지_못하게_막는다(env, repo, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_DELEGATE", "coder")
    assert cli.main(["run", "x", "-C", str(repo), "--model", "codex"]) == 0
    lead = next(c for c in _codex_calls(env) if "# 사용자 요청" in c["stdin"])
    profile = next(a for a in lead["argv"] if a.startswith("permissions.coliday.filesystem="))
    assert 'default_permissions="coliday"' in lead["argv"] and not any("sandbox_mode" in a for a in lead["argv"])
    assert '":project_roots"={"."="write","**/.env*"="none"}' in profile
    assert f'"{members.rpc_dir(1).resolve()}"="write"' in profile             # 팀원에게 맡기는 요청 폴더는 쓸 수 있어야 한다
    argv, _ = json.loads((env / "args.json").read_text())["argv"], None      # Claude 팀원(coder)
    assert _settings(argv)["sandbox"]["enabled"] is True


def test_codex_권한_프로필은_읽기_전용_역할에_쓰기를_주지_않는다():
    from coliday import guard
    g = guard.EnvGuard(("/w", "/repo"))
    read_only = g.codex_args("read-only", ["/art"])[3]
    assert '":project_roots"={"."="read","**/.env*"="none"}' in read_only and '"/art"' not in read_only
    assert '"/w/**/.env*"="none","/repo/**/.env*"="none"' in read_only and '":root"="read"' in read_only
    assert '"/art"="write"' in g.codex_args("workspace-write", ["/art"])[3]
    assert json.loads(g.claude_settings())["sandbox"]["filesystem"]["denyRead"] == ["/w/**/.env*", "/repo/**/.env*"]
    assert json.loads(guard.EnvGuard(("/w",), everywhere=True).claude_settings())["sandbox"]["filesystem"]["denyRead"] == ["/**/.env*"]
    assert guard.parse_version("codex-cli 0.171.0") == (0, 171, 0) and guard.parse_version("이상한 출력") is None


def test_env_명령으로_작업_도중에_허용하고_막힌_읽기를_타임라인에_알린다(env, repo, monkeypatch, capsys):
    from coliday import guard
    assert cli.main(["run", "x", "-C", str(repo)]) == 0
    lead = _recorded(env)[0]
    rule = lead[lead.index("--append-system-prompt") + 1]
    assert "## .env 파일" in rule and guard.NEED_MARKER in rule and "우회하지 않는다" in rule   # 막힌 작업의 팀장만 받는 규칙
    capsys.readouterr()
    assert cli.main(["env", "1"]) == 0 and "차단" in capsys.readouterr().out

    assert cli.main(["env", "1", "allow"]) == 0                            # 끝난 작업: 다음에 이어서 시킬 때부터
    assert "다음에 이어서 시킬 때부터 적용" in capsys.readouterr().out and _store().allow_env(1)
    assert cli.main(["env", "1", "allow"]) == 0 and "이미" in capsys.readouterr().out
    assert cli.main(["continue", "1", "더"]) == 0
    lead = _recorded(env)[0]
    assert "--settings" not in lead and "## .env 파일" not in lead[lead.index("--append-system-prompt") + 1]
    assert cli.main(["env", "1", "deny"]) == 0 and not _store().allow_env(1)

    # 에이전트가 허용을 기다리며 멈춘 작업은 허용하면 같은 세션으로 바로 이어서 실행한다
    _store().update_job(1, result=f"{guard.NEED_MARKER}: .env 의 DB_URL")
    capsys.readouterr()
    assert cli.main(["env", "1", "allow"]) == 0 and "이어서 시작했습니다" in capsys.readouterr().out
    _wait(lambda: _store().get_job(1).is_terminal and "--resume" in json.loads((env / "args.json").read_text())["argv"])
    recorded = json.loads((env / "args.json").read_text())
    assert recorded["stdin"] == guard.ALLOWED_NOTE and "--resume" in recorded["argv"] and "--settings" not in recorded["argv"]

    assert guard.blocked_read({"type": "user", "message": {"content": [{"type": "tool_result", "content": "cat: .env: Operation not permitted"}]}})
    assert guard.blocked_read({"type": "user", "x": "/w/.env.prod File is in a directory that is denied by your permission settings."})
    assert not guard.blocked_read({"type": "user", "x": "cat: a.txt: Operation not permitted"})
    assert not guard.blocked_read({"type": "system", "x": ".env Operation not permitted"})


def test_보고에_적힌_경로_가운데_실제로_있는_것만_링크가_된다(tmp_path):
    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    (work / "src" / "a.py").write_text("x")
    (work / "한글 폴더").mkdir()
    (work / "한글 폴더" / "보고서.md").write_text("x")
    href = lambda path: quote(str(path), safe="/")

    text = jobs.link_paths(
        f"`src/a.py:12` 수정, `git status`, `없는.py`. 보고서는 {work}/src/a.py에 있고 폴더는 {work}/src.\n"
        f"`{work}/한글 폴더/보고서.md` https://example.com/a/b and/or ~/ / [기존](/tmp)\n"
        f"```\n{work}/src/a.py\n```",
        str(work),
    )
    lines = text.split("\n")
    assert f"[`src/a.py:12`]({href(work / 'src' / 'a.py')})" in lines[0]            # 줄 번호가 붙어도 파일을 찾는다
    assert "`git status`, `없는.py`." in lines[0]                                   # 없는 경로는 그대로
    assert f"[{work}/src/a.py]({href(work / 'src' / 'a.py')})에 있고" in lines[0]    # 붙여 쓴 조사는 링크 밖
    assert f"[{work}/src]({href(work / 'src')})." in lines[0]
    assert f"({href(work / '한글 폴더' / '보고서.md')})" in lines[1] and "%20" in lines[1]
    assert "https://example.com/a/b and/or ~/ / [기존](/tmp)" in lines[1]            # URL·단어 사이의 /·기존 링크는 그대로
    assert lines[3] == f"{work}/src/a.py"                                           # 코드 블록 안은 건드리지 않는다

    # 서버의 프로젝트: 지정한 폴더 밖의 경로는 이 컴퓨터에 있어도 링크하지 않는다
    assert jobs.link_paths(f"{tmp_path} `src/a.py`", str(work), within=str(work)) == (
        f"{tmp_path} [`src/a.py`]({href(work / 'src' / 'a.py')})")


def test_폴더_열기는_파일을_실행하지_않고_그_파일이_든_폴더를_연다(env, tmp_path, monkeypatch):
    cfg = config.load()   # env 의 가짜 open 은 받은 인자를 opened.txt 에 적는다
    folder = tmp_path / "out"
    folder.mkdir()
    (folder / "report.md").write_text("x")
    (folder / "Evil.app").mkdir()

    monkeypatch.setattr(sys, "platform", "linux")
    assert jobs.open_folder(cfg, folder) == folder
    assert jobs.open_folder(cfg, folder / "report.md") == folder
    log = env / "opened.txt"
    _wait(lambda: log.exists() and len(log.read_text().split()) == 2)
    assert log.read_text().split() == [str(folder), str(folder)]
    with pytest.raises(jobs.JobError, match="없는 경로"):
        jobs.open_folder(cfg, folder / "none")

    # macOS 의 open 은 .app 같은 묶음 폴더를 실행하므로 위치만 보여 준다
    monkeypatch.setattr(sys, "platform", "darwin")
    opened = []
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda cmd, **_: opened.append(cmd))
    cfg = dataclasses.replace(cfg, open_bin="open")
    assert jobs.open_folder(cfg, folder / "Evil.app") == folder
    assert jobs.open_folder(cfg, folder / "report.md") == folder
    assert jobs.open_folder(cfg, folder) == folder
    assert opened == [["open", "-R", str(folder / "Evil.app")], ["open", "-R", str(folder / "report.md")], ["open", str(folder)]]


def test_보고에_중간_폴더를_빼먹은_경로는_실제_위치를_찾아_옆에_붙인다(tmp_path):
    work = tmp_path / "work"
    (work / "out" / "_output").mkdir(parents=True)
    (work / "out" / "_output" / "result.csv").write_text("x")
    (work / "a").mkdir()
    (work / "b").mkdir()
    (work / "a" / "same.txt").write_text("x")
    (work / "b" / "same.txt").write_text("x")
    actual = quote(str(work / "out" / "_output" / "result.csv"), safe="/")

    text = jobs.link_paths(f"결과는 {work}/out/result.csv 와 `out/result.csv`", str(work))
    assert text.count(f"(실제 위치: [`out/_output/result.csv`]({actual}))") == 2
    assert f"{work}/out/result.csv (실제 위치" in text          # 적힌 글자는 그대로 둔다
    # 폴더도 찾는다. 이름이 같은 것이 둘 이상이거나 경로처럼 생기지 않은 것은 건드리지 않는다
    assert "(실제 위치: [`out/_output`]" in jobs.link_paths("`reports/_output`", str(work))
    for plain in ("`x/same.txt`", "`result.csv`", "`git status`", "`x/none.md`", "`https://a.example/out/result.csv`"):
        assert jobs.link_paths(plain, str(work)) == plain


def test_이어서_시킨_작업은_목록_맨_위로_올라오고_경과도_그때부터_센다(env, repo, capsys):
    cli.main(["run", "첫 작업", "-C", str(repo)])
    cli.main(["run", "둘째 작업", "-C", str(repo)])
    store = _store()
    old = time.time() - 7300   # 둘 다 두 시간쯤 전에 시킨 작업으로 만든다
    store._conn.execute("UPDATE jobs SET created_at = ? + id", (old,))
    assert [j.id for j in store.list_jobs()] == [2, 1]
    assert store.get_job(1).last_asked == store.get_job(1).created_at

    cli.main(["continue", "1", "하나 더"])   # 끝난 작업에 이어서 시킨다
    assert [j.id for j in store.list_jobs()] == [1, 2]
    assert time.time() - store.get_job(1).last_asked < 60 and store.get_job(1).created_at < time.time() - 7000
    capsys.readouterr()
    cli.main(["ls"])
    first, second = capsys.readouterr().out.strip().splitlines()
    assert "#1" in first and " 2h " not in first and "#2" in second and " 2h " in second

    # 실행 중에 보낸 요청(대기)도 요청한 시각으로 친다
    store._conn.execute("UPDATE events SET ts = ? WHERE job_id = 1", (old,))
    store._conn.execute("UPDATE jobs SET status = 'running' WHERE id = 2")
    assert [j.id for j in store.list_jobs()] == [2, 1]
    store._conn.execute("UPDATE jobs SET status = 'running' WHERE id = 1")
    assert store.queue_message(1, "대기 요청")
    assert [j.id for j in store.list_jobs()] == [1, 2]
