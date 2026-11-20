"""가짜 claude 로 run → worker → 저장 → 조회 전체 흐름을 검증한다."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coliday import cli, config, jobs
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
    fake_bin.write_text("#!/bin/sh\necho 'No conversation found' >&2\nexit 1\n")

    assert cli.main(["continue", "1", "더 해줘"]) == 1

    job = _store().get_job(1)
    assert (job.status, job.session_id, job.cost_usd) == ("failed", "s-1", 0.05)


def _recorded(env):
    recorded = json.loads((env / "args.json").read_text())
    argv = recorded["argv"]
    agents = json.loads(Path(argv[argv.index("--agents") + 1]).read_text())
    return argv, agents


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
        assert "최대 5번까지만 시도한다" in agents[name]["prompt"], name
        assert '"최대 시도 횟수 도달"' in agents[name]["prompt"] and "{max_attempts}" not in agents[name]["prompt"]


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
        assert f"이 폴더에 저장한다: {folder}" in agents[name]["prompt"], name
        assert "하위 폴더를 만들지 말고" in agents[name]["prompt"] and "작업 공간으로 쓰지 않는다" in agents[name]["prompt"]
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
    assert "login-page.png" in out and ".hidden" not in out and "notes.txt" not in out
    # 하위 폴더에 저장한 사진·영상은 맨 위로 꺼내 한 폴더에서 다 보이게 하고, 나머지는 _other 로 모은다
    assert "flows-order-cancel.webm" in out and "flows/" not in out
    assert sorted(p.name for p in folder.iterdir() if not p.name.startswith(".")) == [
        "_other", "flows-order-cancel.webm", "login-page.png"]
    assert (folder / "_other" / "notes.txt").is_file()
    _wait(lambda: (env / "opened.txt").exists())
    assert (env / "opened.txt").read_text().strip() == str(folder)

    cli.main(["show", "1"])
    assert "결과물 2개" in capsys.readouterr().out

    monkeypatch.delenv("FAKE_CLAUDE_ARTIFACT")
    cli.main(["continue", "1", "더"])   # 이어서 시켜도 이전 결과물은 남는다
    assert len(jobs.artifacts(config.load(), 1)) == 3
    cli.main(["rm", "1"])               # 작업을 지우면 결과물도 지워진다
    assert not folder.exists()


def test_결과물_정리는_사진_영상만_맨_위에_늘어놓고_나머지는_other로_모은다(tmp_path):
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
        ".tmp", "01-ok.png", "_other", "verify-01-ok-2.png", "verify-01-ok.png", "verify-deep-flow.WEBM"]
    assert (root / "verify-01-ok.png").read_text() == "already" and (root / "verify-01-ok-2.png").read_text() == "nested"
    assert sorted(str(p.relative_to(root / "_other")) for p in (root / "_other").rglob("*") if p.is_file()) == [
        "dom-ok.html", "verify/check.js"]


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
    assert "아직 결과물(사진·영상)이 없습니다" in capsys.readouterr().out
    _wait(lambda: (env / "opened.txt").exists())
    assert (env / "opened.txt").read_text().strip() == str(config.load().artifacts_dir(1))
    assert cli.main(["open", "99"]) == 2
    cli.main(["show", "1"])
    assert "coliday open 1" not in capsys.readouterr().out  # 결과물 줄을 찍지 않는다


def test_codex로_도는_역할도_결과물_폴더에_쓸_수_있게_샌드박스를_연다(env, repo, monkeypatch):
    cli.main(["model", "default", "codex"])
    monkeypatch.setenv("FAKE_CODEX_DELEGATE", "coder")
    cli.main(["run", "x", "-C", str(repo)])

    folder = str(config.load().artifacts_dir(1))
    lead, member = sorted(_codex_calls(env), key=lambda c: "# 사용자 요청" not in c["stdin"])
    lead_roots = next(a for a in lead["argv"] if a.startswith("sandbox_workspace_write.writable_roots="))
    assert folder in json.loads(lead_roots.split("=", 1)[1]) and f"- 결과물 폴더: {folder}" in lead["stdin"]
    assert f"sandbox_workspace_write.writable_roots={json.dumps([folder])}" in member["argv"]
    assert f"이 폴더에 저장한다: {folder}" in member["stdin"]

    monkeypatch.setenv("FAKE_CODEX_DELEGATE", "reviewer")   # 읽기 전용 역할은 열지 않는다
    cli.main(["run", "y", "-C", str(repo)])
    member = [c for c in _codex_calls(env) if "# 사용자 요청" not in c["stdin"]][-1]
    assert "read-only" in member["argv"] and not any("writable_roots" in a for a in member["argv"])


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

    cli.main(["run", "x", "-C", str(repo)])

    argv, agents = _recorded(env)
    assert "--agents" in argv and _codex_calls(env) == []  # 팀장은 Claude
    import shlex
    codex = shlex.quote(os.environ["COLIDAY_CODEX_BIN"])  # 경로에 한글·공백이 있어도 명령이 깨지지 않게 인용된다
    roots = shlex.quote(f'sandbox_workspace_write.writable_roots={json.dumps([str(config.load().artifacts_dir(1))])}')
    assert (f'{codex} exec -C "$PWD" -s workspace-write --skip-git-repo-check -c {roots} -o "$OUT" - < "$PROMPT"'
            in agents["coder"]["prompt"])
    assert "너는 시니어 개발자다" in agents["coder"]["prompt"]  # 역할 지침은 그대로 Codex 에 전달
    assert (agents["coder"]["model"], agents["coder"]["tools"]) == ("haiku", ["Bash", "Read"])
    assert "-s read-only --skip-git-repo-check -m gpt-x" in agents["reviewer"]["prompt"]  # 수정 도구가 없는 역할
    assert "-s workspace-write" in agents["verifier"]["prompt"]  # 빌드 산출물을 써야 한다
    assert agents["codex-coder"]["model"] == "sonnet" and "codex exec" not in agents["codex-coder"]["prompt"]
    assert all("codex_sandbox" not in spec for spec in agents.values())
    assert "codex exec" not in agents["architect"]["prompt"]


def test_기본_codex_coder는_codex_래퍼다(env, repo, capsys):
    cli.main(["run", "x", "-C", str(repo)])
    _, agents = _recorded(env)
    assert " exec -C " in agents["codex-coder"]["prompt"] and agents["codex-coder"]["model"] == "haiku"

    capsys.readouterr()
    cli.main(["model"])
    lines = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()}
    assert "codex" in lines["codex-coder"] and "기본값" in lines["codex-coder"]


def test_팀장을_codex로_바꾸면_codex가_실행되고_타임라인과_보고가_남는다(env, repo, capsys):
    assert cli.main(["model", "lead", "codex:gpt-x"]) == 0

    assert cli.main(["run", "주문 취소 API 추가", "-C", str(repo)]) == 0

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

    assert cli.main(["run", "x", "-C", str(repo)]) == 0

    lead, member = sorted(_codex_calls(env), key=lambda c: "# 사용자 요청" not in c["stdin"])
    assert member["argv"] == ["exec", "--json", "--skip-git-repo-check", "-s", "read-only", "-m", "gpt-r", "-"]
    assert member["stdin"].startswith("너는 코드 리뷰어다") and member["stdin"].endswith("# 작업 명세\n로그인 구현\n세부 명세")
    events = [(e.actor, e.kind, e.message) for e in _store().events(1)]
    assert ("reviewer", "tool", "Bash ls -la") in events
    assert ("팀장", "text", "팀원 보고(0): 코덱스 팀원 보고") in events

    monkeypatch.setenv("FAKE_CODEX_DELEGATE", "nobody")
    cli.main(["run", "y", "-C", str(repo)])
    texts = [e.message for e in _store().events(2) if e.kind == "text"]
    assert any(t.startswith("팀원 보고(1): 알 수 없는 역할: nobody") for t in texts)


def test_codex_팀장을_이어서_시키면_resume하고_claude와_바꾸면_새_세션으로_시작한다(env, repo, capsys):
    cli.main(["model", "lead", "codex"])
    cli.main(["run", "x", "-C", str(repo)])

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
