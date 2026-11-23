"""SSH 프로젝트. 가짜 ssh 가 받은 명령을 이 컴퓨터에서 실행한다 (서버의 홈은 따로 둔 폴더)."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coliday import cli, config, gitops, jobs, remote
from coliday.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
SRC = Path(__file__).parents[1] / "src"
HOST = "dev@server"


@pytest.fixture
def env(tmp_path, monkeypatch):
    tmp_path = tmp_path.resolve()
    for name in ("claude", "codex", "ssh"):
        script = tmp_path / name
        script.write_text(f"#!/bin/sh\nexec {sys.executable} {FIXTURES / f'fake_{name}.py'} \"$@\"\n")
        script.chmod(0o755)
    (tmp_path / "server-home").mkdir()
    monkeypatch.setenv("COLIDAY_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COLIDAY_CLAUDE_BIN", str(tmp_path / "no-local-claude"))  # 이 컴퓨터의 claude 는 쓰지 않는다
    monkeypatch.setenv("COLIDAY_REMOTE_CLAUDE_BIN", str(tmp_path / "claude"))
    monkeypatch.setenv("COLIDAY_REMOTE_CODEX_BIN", str(tmp_path / "codex"))
    monkeypatch.setenv("COLIDAY_SSH_BIN", str(tmp_path / "ssh"))
    monkeypatch.setenv("FAKE_SSH_HOME", str(tmp_path / "server-home"))
    monkeypatch.setenv("FAKE_SSH_LOG", str(tmp_path / "ssh.jsonl"))
    monkeypatch.setenv("FAKE_CLAUDE_ARGS", str(tmp_path / "args.json"))
    monkeypatch.setenv("PYTHONPATH", str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    remote.home.cache_clear()
    return tmp_path


@pytest.fixture
def app(env):
    """서버에 있는 레포의 위치 (`dev@server:/…/srv/app`)."""
    r = env / "srv" / "app"
    (r / "sub").mkdir(parents=True)
    (r / "sub" / "a.txt").write_text("a\n")
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(r), *args], check=True)
    return f"{HOST}:{r}"


def _store():
    return Store(config.load().db_path)


def _ssh_calls(env):
    path = env / "ssh.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _wait(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.1)
    raise AssertionError("시간 초과")


def test_위치_문자열로_로컬과_서버를_구분한다():
    assert remote.split("/srv/app") == (None, "/srv/app")
    assert remote.split("~/app") == (None, "~/app") and remote.split("./a:b") == (None, "./a:b")
    assert remote.split("dev@server:/srv/app") == ("dev@server", "/srv/app")
    assert remote.split("myserver:~/app") == ("myserver", "~/app")
    assert remote.split("-oProxyCommand=x:/srv") == (None, "-oProxyCommand=x:/srv")   # ssh 옵션으로 읽힐 값은 호스트가 아니다
    assert remote.join("dev@server:/a", "b/c") == "dev@server:/a/b/c" and remote.join("/a", ".") == "/a"
    assert remote.name("dev@server:/srv/app/") == "app"
    with pytest.raises(remote.RemoteError):
        remote.command("-oProxyCommand=x", "true")
    with pytest.raises(remote.RemoteError):
        remote.normalize("server:relative/path")


def test_서버의_프로젝트에서_실행하면_에이전트가_서버에서_돈다(env, app, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_ARTIFACT", "login.png,notes/run.log")
    monkeypatch.setenv("FAKE_CLAUDE_TOUCH", "created.txt")
    assert cli.main(["run", "로그인 고쳐줘", "-C", app + "/sub"]) == 0

    home = env / "server-home"
    job = _store().get_job(1)
    assert job.status == "succeeded" and job.repo == app
    assert job.workdir == f"{HOST}:{home}/.coliday/worktrees/app-1/sub" and job.branch == "coliday/1"
    worktree = home / ".coliday" / "worktrees" / "app-1"
    assert (worktree / "sub" / "a.txt").is_file()                      # worktree 는 서버에 만든다
    assert not (config.load().worktrees_dir / "app-1").exists()

    recorded = json.loads((env / "args.json").read_text())
    argv = recorded["argv"]
    assert recorded["cwd"] == str(worktree / "sub") and recorded["stdin"] == "로그인 고쳐줘"
    remote_job = home / ".coliday" / "jobs" / "1"
    assert argv[argv.index("--agents") + 1] == str(remote_job / "agents.json")
    assert "coder" in json.loads((remote_job / "agents.json").read_text())
    assert argv[argv.index("--add-dir") + 1] == str(remote_job / "artifacts")
    assert f"- 결과물 폴더: {remote_job / 'artifacts'}" in argv[argv.index("--append-system-prompt") + 1]
    assert (remote_job / "pid").read_text().strip().isdigit()           # 취소할 때 쓸 프로세스 번호

    calls = _ssh_calls(env)
    assert calls and all(c["host"] == HOST for c in calls)
    assert all("BatchMode=yes" in c["options"] for c in calls)           # 비밀번호를 묻느라 멈추지 않는다

    # 결과물은 서버에서 가져와 이 컴퓨터의 폴더에서 본다 (정리까지 된다)
    local = config.load().artifacts_dir(1)
    assert (local / "login.png").read_bytes() == b"fake" and (local / "_other" / "notes" / "run.log").is_file()

    capsys.readouterr()
    assert cli.main(["diff", "1"]) == 0                                   # diff 도 서버에서 계산한다
    assert "created.txt" in capsys.readouterr().out
    cli.main(["ls"])
    assert "app" in capsys.readouterr().out
    assert jobs.list_projects(config.load(), _store()) == [app + "/sub"]  # 시작한 위치가 연결된다


def test_서버_작업을_이어서_시키고_삭제하면_서버의_worktree도_지운다(env, app, monkeypatch, capsys):
    cli.main(["run", "x", "-C", app])
    monkeypatch.setenv("FAKE_CLAUDE_ARTIFACT", "second.png")
    assert cli.main(["continue", "1", "더 해줘"]) == 0
    argv = json.loads((env / "args.json").read_text())["argv"]
    assert "--resume" in argv and _store().get_job(1).status == "succeeded"
    assert (config.load().artifacts_dir(1) / "second.png").is_file()

    home = env / "server-home"
    assert cli.main(["rm", "1", "--force"]) == 0
    assert not (home / ".coliday" / "worktrees" / "app-1").exists()
    branches = subprocess.run(["git", "-C", remote.split(app)[1], "branch"], capture_output=True, text=True).stdout
    assert "coliday/1" not in branches
    _wait(lambda: not (home / ".coliday" / "jobs" / "1").exists())      # 서버에 둔 정의·결과물 원본도 치운다


def test_프로젝트를_서버_위치로_연결한다(env, app, capsys):
    assert cli.main(["project", "add", app, "--name", "운영 서버"]) == 0
    link = env / "server-home" / "app"
    link.symlink_to(remote.split(app)[1])
    assert cli.main(["project", "add", f"{HOST}:~/app/../app"]) == 0      # ~ 는 서버의 홈
    cfg = config.load()
    assert jobs.project_names(cfg, _store()) == {app: "운영 서버", f"{HOST}:{env / 'server-home' / 'app'}": "app"}

    capsys.readouterr()
    assert cli.main(["project", "add", f"{HOST}:/no/such/dir"]) == 2
    assert "디렉토리가 없습니다" in capsys.readouterr().err
    assert cli.main(["project", "add", f"{HOST}:relative"]) == 2
    assert "절대 경로나 ~/" in capsys.readouterr().err

    cli.main(["project"])
    out = capsys.readouterr().out
    assert app in out and "(없음)" not in out                             # 목록을 볼 때는 접속하지 않는다


def test_서버에_접속하지_못하면_이유를_알리고_작업을_만들지_않는다(env, app, monkeypatch, capsys):
    cli.main(["project", "add", app])
    monkeypatch.setenv("FAKE_SSH_FAIL", "1")
    capsys.readouterr()
    assert cli.main(["run", "x", "-C", app]) == 2
    err = capsys.readouterr().err
    assert "SSH 로 접속할 수 없습니다" in err and "Permission denied (publickey)" in err and f"ssh {HOST}" in err
    assert _store().list_jobs() == []
    assert cli.main(["project", "add", f"{HOST}:/other"]) == 2
    assert cli.main(["project", "add", app, "--name", "새 이름"]) == 0     # 이미 연결된 항목의 이름은 바꿀 수 있다
    assert cli.main(["project", "rm", app]) == 0                          # 목록에서 빼는 것도 된다
    with pytest.raises(gitops.GitError):
        gitops.diff(app, None)


def test_서버에서는_codex_팀장을_막고_claude가_없으면_알린다(env, app, monkeypatch, capsys):
    assert cli.main(["run", "x", "-C", app, "--model", "codex"]) == 2
    assert "SSH 프로젝트에서는 Codex 팀장을 쓸 수 없습니다" in capsys.readouterr().err
    monkeypatch.setenv("COLIDAY_REMOTE_CLAUDE_BIN", "no-such-claude")
    assert cli.main(["run", "x", "-C", app]) == 2
    assert "no-such-claude 을(를) 찾을 수 없습니다" in capsys.readouterr().err
    assert _store().list_jobs() == []

    monkeypatch.setenv("COLIDAY_REMOTE_CLAUDE_BIN", str(env / "claude"))
    cli.main(["model", "coder", "codex"])                                 # 팀원은 Codex 로 둘 수 있다: 서버의 codex 를 부른다
    assert cli.main(["run", "x", "-C", app]) == 0
    assert not any("codex 가 없어" in e.message for e in _store().events(1))
    agents = json.loads((env / "server-home" / ".coliday" / "jobs" / "1" / "agents.json").read_text())
    assert f"{shlex.quote(str(env / 'codex'))} exec -C" in agents["coder"]["prompt"]
    assert str(env / "server-home" / ".coliday" / "jobs" / "1" / "artifacts") in agents["coder"]["prompt"]


def test_취소하면_서버의_에이전트도_끝낸다(env, app):
    job_dir = env / "server-home" / ".coliday" / "jobs" / "7"
    job_dir.mkdir(parents=True)
    agent = subprocess.Popen(["sleep", "60"])
    try:
        (job_dir / "pid").write_text(f"{agent.pid}\n")
        remote.spawn(HOST, jobs.stop_script(7))
        assert agent.wait(timeout=10) != 0
        _wait(lambda: not (job_dir / "pid").exists())
    finally:
        agent.kill()


def test_실행_중인_서버_작업을_취소한다(env, app, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    assert cli.main(["run", "x", "-C", app, "-d"]) == 0
    _wait(lambda: (env / "args.json").exists())
    pid = json.loads((env / "args.json").read_text())["pid"]
    assert cli.main(["cancel", "1"]) == 0
    _wait(lambda: not jobs.alive(pid))
    assert _store().get_job(1).status == "cancelled"
    _wait(lambda: any("kill -TERM" in c["command"] for c in _ssh_calls(env)))   # 서버의 프로세스도 직접 끝낸다


def test_결과물을_가져올_때_링크는_따라가지_않는다(env, tmp_path):
    source = env / "server-home" / "art"
    source.mkdir()
    (source / "ok.png").write_bytes(b"img")
    (source / "secret").symlink_to("/etc/passwd")
    target = tmp_path / "fetched"
    target.mkdir()
    assert remote.fetch_dir(HOST, str(source), target) == 1
    assert sorted(p.name for p in target.iterdir()) == ["ok.png"]
    empty = tmp_path / "empty"
    empty.mkdir()
    assert remote.fetch_dir(HOST, "/no/such/dir", empty) == 0 and list(empty.iterdir()) == []


def test_서버의_결과물_경로는_이_컴퓨터에서_풀지_않는다(tmp_path):
    from coliday import team
    link = tmp_path / "link"
    link.symlink_to(tmp_path)   # 이 컴퓨터에서는 링크인 경로가 서버에서는 진짜 폴더일 수 있다 (macOS 의 /var 처럼)
    path = f"{link}/jobs/1/artifacts"

    def roots(**kw):
        return json.loads(team.codex_writable_roots(path, **kw)[1].split("=", 1)[1])

    assert roots(local=False) == [path]
    assert roots() == [str(tmp_path.resolve() / "jobs" / "1" / "artifacts")]
    resolved = team.resolve_agents(team.DEFAULT_AGENTS, team.Models(agents={"coder": "codex"}))
    wrapper = team.claude_agents(resolved, "codex", path, local=False)["coder"]["prompt"]
    assert shlex.quote(team.codex_writable_roots(path, local=False)[1]) in wrapper


def test_서버에_codex가_없으면_codex_팀원을_claude로_돌린다(env, app, monkeypatch, capsys):
    monkeypatch.setenv("COLIDAY_REMOTE_CODEX_BIN", "no-such-codex")
    cli.main(["model", "reviewer", "codex:gpt-x"])
    assert cli.main(["run", "x", "-C", app]) == 0                         # codex 가 없어도 시작되고 끝난다
    agents = json.loads((env / "server-home" / ".coliday" / "jobs" / "1" / "agents.json").read_text())
    for name in ("codex-coder", "reviewer"):                              # 기본 Codex 팀원과 직접 고른 팀원 모두
        assert "model" not in agents[name] and "exec -C" not in agents[name]["prompt"], name
    assert agents["codex-coder"]["prompt"].startswith("너는 시니어 개발자다")   # 역할 지침과 도구는 그대로
    assert agents["codex-coder"]["tools"] == ["Read", "Grep", "Glob", "Edit", "Write", "Bash"]
    assert agents["architect"]["model"] == "opus"                         # Claude 팀원은 건드리지 않는다
    notes = [e.message for e in _store().events(1) if "codex 가 없어" in e.message]
    assert notes == [f"{HOST} 에 codex 가 없어 codex-coder, reviewer 은(는) Claude 로 실행합니다"]
