"""SSH 프로젝트. 가짜 ssh 가 받은 명령을 이 컴퓨터에서 실행한다 (서버의 홈은 따로 둔 폴더)."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from coliday import auth, cli, config, gitops, jobs, remote
from coliday.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
SRC = Path(__file__).parents[1] / "src"
HOST = "dev@server"
TOKEN = "sk-ant-oat01-" + "t0ken" * 8


@pytest.fixture
def env(tmp_path, monkeypatch):
    tmp_path = tmp_path.resolve()
    for name in ("claude", "codex", "ssh"):
        script = tmp_path / name
        script.write_text(f"#!/bin/sh\nexec {sys.executable} {FIXTURES / f'fake_{name}.py'} \"$@\"\n")
        script.chmod(0o755)
    (tmp_path / "server-home").mkdir()
    monkeypatch.setenv("COLIDAY_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "local-codex"))  # 이 컴퓨터의 진짜 Codex 로그인을 건드리지 않는다
    monkeypatch.setenv("COLIDAY_CLAUDE_BIN", str(tmp_path / "no-local-claude"))  # 이 컴퓨터의 claude 는 쓰지 않는다
    monkeypatch.setenv("COLIDAY_REMOTE_CLAUDE_BIN", str(tmp_path / "claude"))
    monkeypatch.setenv("COLIDAY_REMOTE_CODEX_BIN", str(tmp_path / "codex"))
    monkeypatch.setenv("COLIDAY_SSH_BIN", str(tmp_path / "ssh"))
    monkeypatch.setenv("FAKE_SSH_HOME", str(tmp_path / "server-home"))
    monkeypatch.setenv("FAKE_SSH_LOG", str(tmp_path / "ssh.jsonl"))
    monkeypatch.setenv("FAKE_CLAUDE_ARGS", str(tmp_path / "args.json"))
    monkeypatch.setenv("PYTHONPATH", str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    remote.home.cache_clear()
    auth.save(config.load(), auth.CLAUDE, TOKEN)  # 서버 작업에는 넘겨 줄 토큰이 있어야 한다
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
    runner = env / "server-home" / ".coliday" / "jobs" / "1" / "codex" / "1-coder"   # 실행 파일도 서버에 둔다
    assert f"{shlex.quote(str(runner))} <1에서 쓴 파일의 경로>" in agents["coder"]["prompt"] and os.access(runner, os.X_OK)
    assert f"{shlex.quote(str(env / 'codex'))} exec -C" in runner.read_text()
    assert str(env / "server-home" / ".coliday" / "jobs" / "1" / "artifacts") in runner.read_text()
    argv = json.loads((env / "args.json").read_text())["argv"]
    assert argv[argv.index("--allowedTools") + 1] == f"Bash({shlex.quote(str(runner))}:*)"
    tasks = runner.parent.parent / "codex-tasks"                          # 명세를 쓸 폴더도 서버에 만들고 열어 둔다
    assert tasks.is_dir() and str(tasks) in [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]


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
    _, script = team.codex_runners(resolved, "codex", path, local=False)["coder"]
    assert shlex.quote(team.codex_writable_roots(path, local=False)[1]) in script


def test_서버에_codex가_없으면_codex_팀원을_claude로_돌린다(env, app, monkeypatch, capsys):
    monkeypatch.setenv("COLIDAY_REMOTE_CODEX_BIN", "no-such-codex")
    cli.main(["model", "reviewer", "codex:gpt-x"])
    assert cli.main(["run", "x", "-C", app]) == 0                         # codex 가 없어도 시작되고 끝난다
    agents = json.loads((env / "server-home" / ".coliday" / "jobs" / "1" / "agents.json").read_text())
    for name in ("codex-coder", "reviewer"):                              # 기본 Codex 팀원과 직접 고른 팀원 모두
        assert "model" not in agents[name] and "Codex CLI" not in agents[name]["prompt"], name
    assert agents["codex-coder"]["prompt"].startswith("너는 시니어 개발자다")   # 역할 지침과 도구는 그대로
    assert agents["codex-coder"]["tools"] == ["Read", "Grep", "Glob", "Edit", "Write", "Bash"]
    assert agents["architect"]["model"] == "opus"                         # Claude 팀원은 건드리지 않는다
    notes = [e.message for e in _store().events(1) if "codex 가 없어" in e.message]
    assert notes == [f"{HOST} 에 codex 가 없어 codex-coder, reviewer 은(는) Claude 로 실행합니다"]


@pytest.fixture
def password_server(env, monkeypatch):
    """키 인증이 안 돼 있어 비밀번호가 있어야 접속되는 서버. 이 컴퓨터의 홈도 비어 있다 (SSH 키 없음)."""
    local_home = env / "local-home"
    local_home.mkdir()
    monkeypatch.setenv("HOME", str(local_home))
    monkeypatch.setenv("FAKE_SSH_PASSWORD", "s3cret 'pw' $HOME")
    return local_home


def test_비밀번호로_한_번_접속해_SSH_키를_등록하고_프로젝트를_추가한다(env, app, password_server):
    cfg, store = config.load(), _store()
    authorized = env / "server-home" / ".ssh" / "authorized_keys"
    with pytest.raises(jobs.JobError, match="Permission denied"):
        jobs.add_project(cfg, store, app)                                  # 키도 비밀번호도 없으면 접속하지 못한다
    with pytest.raises(jobs.JobError, match="SSH 키를 등록하지 못했습니다.*비밀번호가 맞는지"):
        jobs.add_project(cfg, store, app, None, "wrong")
    assert not authorized.exists() and jobs.project_names(cfg, store) == {}

    assert jobs.add_project(cfg, store, app, "서버 앱", "s3cret 'pw' $HOME") == app
    public = (password_server / ".ssh" / "id_ed25519.pub").read_text().strip()   # 키가 없어서 새로 만들었다
    assert authorized.read_text() == public + "\n"
    assert authorized.stat().st_mode & 0o077 == 0 and (password_server / ".ssh" / "id_ed25519").stat().st_mode & 0o077 == 0
    assert jobs.add_project(cfg, store, app) == app                        # 이제 비밀번호 없이 된다

    # 비밀번호는 어디에도 남지 않는다: ssh 명령 인자, 프로젝트 목록, 임시 파일
    assert "s3cret" not in (env / "ssh.jsonl").read_text() and "s3cret" not in cfg.projects_path.read_text()
    assert not list(Path(tempfile.gettempdir()).glob("coliday-askpass-*"))


def test_이미_키로_접속되면_비밀번호를_받아도_서버를_건드리지_않는다(env, app, password_server):
    authorized = env / "server-home" / ".ssh" / "authorized_keys"
    authorized.parent.mkdir()
    authorized.write_text("ssh-ed25519 AAAA other\n")
    assert jobs.add_project(config.load(), _store(), app, None, "아무거나") == app
    assert authorized.read_text() == "ssh-ed25519 AAAA other\n" and not (password_server / ".ssh" / "id_ed25519").exists()
    assert not any("PubkeyAuthentication=no" in c["options"] for c in _ssh_calls(env))


def test_있는_키는_다시_만들지_않고_두_번_등록해도_한_줄만_남는다(env, password_server):
    ssh_dir = password_server / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_rsa").write_text("private")
    (ssh_dir / "id_rsa.pub").write_text("ssh-rsa AAAA mine\n")
    authorized = env / "server-home" / ".ssh" / "authorized_keys"
    authorized.parent.mkdir()
    authorized.write_text("ssh-ed25519 AAAA other\n")
    for _ in range(2):
        assert remote.install_key(HOST, "s3cret 'pw' $HOME") == ssh_dir / "id_rsa.pub"
    assert authorized.read_text() == "ssh-ed25519 AAAA other\nssh-rsa AAAA mine\n"
    assert (ssh_dir / "id_rsa").read_text() == "private" and not (ssh_dir / "id_ed25519").exists()
    with pytest.raises(remote.RemoteError, match="호스트 이름이 올바르지 않습니다"):
        remote.install_key("-oProxyCommand=x", "pw")


def test_project_add_password는_비밀번호를_물어_키를_등록한다(env, app, password_server, monkeypatch, capsys):
    asked = []
    monkeypatch.setattr("getpass.getpass", lambda prompt: asked.append(prompt) or "s3cret 'pw' $HOME")
    assert cli.main(["project", "add", app, "--password"]) == 0
    assert asked == [f"{HOST} 의 비밀번호: "] and "연결했습니다" in capsys.readouterr().out
    assert (env / "server-home" / ".ssh" / "authorized_keys").exists()
    assert cli.main(["project", "add", str(env / "srv" / "app"), "--password"]) == 0   # 이 컴퓨터의 폴더면 묻지 않는다
    assert len(asked) == 1


def test_포트는_호스트_뒤에_붙이고_ssh에는_p_옵션으로_넘긴다(env, app, capsys):
    host, path = app.split(":", 1)
    spec = f"{host}:2222:{path}"
    assert remote.split(spec) == (f"{host}:2222", path) and remote.is_remote(spec)
    assert remote.split(f"{host}:{path}") == (host, path)                 # 포트가 없으면 종전대로
    assert remote.target(f"{host}:2222") == ["-p", "2222", host] and remote.target(host) == [host]
    assert remote.with_port(host, " 2222 ") == f"{host}:2222" and remote.with_port(host, "") == host
    for bad in ("0", "65536", "22a", "-1"):
        with pytest.raises(remote.RemoteError, match="포트는 1~65535"):
            remote.with_port(host, bad)
    with pytest.raises(remote.RemoteError):
        remote.target(f"{host}:99999")

    assert cli.main(["project", "add", f"{host}:{path}", "--port", "2222", "--name", "포트 서버"]) == 0
    assert jobs.project_names(config.load(), _store()) == {spec: "포트 서버"}
    assert all(c["options"][-2:] == ["-p", "2222"] and c["host"] == host for c in _ssh_calls(env))
    assert cli.main(["run", "x", "-C", spec]) == 0                         # 작업도 그 포트로 접속해 실행한다
    job = _store().get_job(1)
    assert job.status == "succeeded" and job.workdir.startswith(f"{host}:2222:/")
    assert all(c["options"][-2:] == ["-p", "2222"] for c in _ssh_calls(env))
    assert cli.main(["project", "add", str(env / "srv" / "app"), "--port", "2222"]) == 2
    assert "--port 는 서버의 프로젝트" in capsys.readouterr().err


def test_포트가_있는_서버에도_비밀번호로_키를_등록한다(env, app, password_server):
    host, path = app.split(":", 1)
    assert jobs.add_project(config.load(), _store(), f"{host}:2222:{path}", None, "s3cret 'pw' $HOME") == f"{host}:2222:{path}"
    first = _ssh_calls(env)
    assert any("PubkeyAuthentication=no" in c["options"] and c["options"][-2:] == ["-p", "2222"] for c in first)


def test_서버에서도_기본으로_env_읽기를_막고_막을_수_없으면_알린다(env, app, monkeypatch, capsys):
    monkeypatch.setattr(jobs, "_remote_is_linux", lambda host: False)     # 테스트의 "서버"는 이 컴퓨터다
    assert cli.main(["run", "x", "-C", app]) == 0
    argv = json.loads((env / "args.json").read_text())["argv"]
    settings = json.loads(argv[argv.index("--settings") + 1])
    workdir = remote.split(_store().get_job(1).workdir)[1]
    # 서버에서는 디스크 전체가 아니라 작업 디렉토리와 원래 레포 아래만 막는다. 이 컴퓨터의 DB 경로는 넘기지 않는다
    assert settings["sandbox"]["filesystem"]["denyRead"] == [f"{workdir}/**/.env*", f"{env / 'srv' / 'app'}/**/.env*"]
    assert settings["sandbox"]["filesystem"]["denyWrite"] == [] and settings["sandbox"]["failIfUnavailable"] is True

    monkeypatch.setenv("FAKE_CLAUDE_VERSION", "1.0.58 (Claude Code)")     # 서버의 claude 가 낮다
    assert cli.main(["run", "y", "-C", app]) == 2
    assert f"{HOST} 의 claude(1.0.58)는 .env 읽기를 막지 못합니다" in capsys.readouterr().err
    assert cli.main(["run", "y", "-C", app, "--allow-env"]) == 0
    argv = json.loads((env / "args.json").read_text())["argv"]
    assert "--settings" not in argv

    monkeypatch.delenv("FAKE_CLAUDE_VERSION")
    monkeypatch.setattr(jobs, "_remote_is_linux", lambda host: True)      # 리눅스 서버에 bubblewrap 이 없다
    monkeypatch.setattr(remote, "missing_commands", lambda host, *names: [n for n in names if n == "bwrap"])
    assert cli.main(["run", "z", "-C", app]) == 2
    assert "bwrap 이(가) 없어 .env 읽기를 막을 수 없습니다" in capsys.readouterr().err


def test_등록한_인증을_서버의_claude에_넘기되_명령에는_드러내지_않는다(env, app, monkeypatch, capsys):
    saved = config.load().auth_path
    assert cli.main(["auth", "rm"]) == 0 and not saved.exists()
    assert cli.main(["run", "로그인 고쳐줘", "-C", app]) == 2                  # 토큰이 없으면 서버 작업을 만들지 않는다
    err = capsys.readouterr().err
    assert "Claude 토큰이 등록돼 있지 않습니다" in err and "claude setup-token" in err and _store().list_jobs() == []

    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(TOKEN + "\n"))
    assert cli.main(["auth", "set"]) == 0
    out = capsys.readouterr().out
    assert TOKEN not in out and "Claude 토큰" in out                       # 값은 화면에 다시 보여 주지 않는다
    assert saved.read_text() == f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n" and saved.stat().st_mode & 0o777 == 0o600

    assert cli.main(["run", "로그인 고쳐줘", "-C", app]) == 0
    recorded = json.loads((env / "args.json").read_text())
    assert recorded["auth"] == {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN}           # 서버의 claude 가 환경변수로 받는다
    assert recorded["stdin"] == "로그인 고쳐줘"                              # 요청에는 섞이지 않는다
    assert TOKEN not in (env / "ssh.jsonl").read_text()                     # ssh 명령 인자에는 없다 (ps 에 보인다)
    home = env / "server-home"
    leaked = [p for p in home.rglob("*") if p.is_file() and TOKEN in p.read_text(errors="replace")]
    assert leaked == []                                                     # 서버의 디스크에도 쓰지 않는다
    job_dir = config.load().job_dir(1)
    assert all(TOKEN not in f.read_text(errors="replace") for f in job_dir.rglob("*") if f.is_file())

    assert cli.main(["continue", "1", "테스트도 돌려줘"]) == 0                # 이어서 시킬 때도 넘긴다
    recorded = json.loads((env / "args.json").read_text())
    assert recorded["auth"] == {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN} and recorded["stdin"] == "테스트도 돌려줘"

    assert cli.main(["auth", "rm", "claude"]) == 0
    assert cli.main(["continue", "1", "한 번 더"]) == 2                       # 지운 뒤에는 이어서 시키지도 못한다
    assert "Claude 토큰이 등록돼 있지 않습니다" in capsys.readouterr().err


def test_codex_키와_API_키도_넘기고_이상한_값은_등록하지_않는다(env, app, monkeypatch, capsys):
    cfg = config.load()
    auth.save(cfg, auth.CLAUDE, "  sk-ant-api03-abcdefghijklmnop  ")       # API 키는 다른 환경변수로 넘긴다
    auth.save(cfg, auth.CODEX, "sk-proj-codexkey1234567890")
    monkeypatch.setenv("FAKE_CLAUDE_ENV", "CODEX_API_KEY")
    assert cli.main(["run", "x", "-C", app]) == 0
    recorded = json.loads((env / "args.json").read_text())
    assert recorded["auth"] == {"ANTHROPIC_API_KEY": "sk-ant-api03-abcdefghijklmnop"} and recorded["stdin"] == "x"
    assert recorded["env"] == {"CODEX_API_KEY": "sk-proj-codexkey1234567890"}   # codex 팀원은 claude 의 환경을 물려받는다
    assert "codexkey" not in (env / "ssh.jsonl").read_text()
    assert cli.main(["auth"]) == 0
    out = capsys.readouterr().out
    assert "Anthropic API 키" in out and "abcdefghijkl" not in out and "ANTHROPIC_API_KEY" in out
    assert "codex: 등록됨" in out and "codexkey" not in out

    for bad in ("", "two words", "line1\nline2"):                          # 줄이 나뉘면 뒤쪽이 요청으로 흘러 들어간다
        with pytest.raises(auth.AuthError):
            auth.save(cfg, auth.CLAUDE, bad)
    assert auth.get(cfg, auth.CLAUDE) == "sk-ant-api03-abcdefghijklmnop"    # 실패한 등록은 기존 값을 건드리지 않는다
    assert auth.clear(cfg, auth.CODEX) and auth.get(cfg, auth.CODEX) is None and auth.get(cfg, auth.CLAUDE)
    cfg.auth_path.write_text("CLAUDE_CODE_OAUTH_TOKEN=broken value\nEVIL=1\n")
    assert auth.load(cfg) == {}                                             # 손으로 망가뜨린 줄·모르는 이름은 쓰지 않는다


def test_서버의_claude가_인증을_거부하면_어떻게_할지_알린다(env, app, monkeypatch, capsys):
    script = env / "claude"
    script.write_text(
        "#!/bin/sh\n[ \"$1\" = --version ] && { echo '2.1.331 (Claude Code)'; exit 0; }\ncat > /dev/null\n"
        "echo '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":true,"
        "\"result\":\"Failed to authenticate. API Error: 401 OAuth access token is invalid.\"}'\nexit 1\n"
    )
    assert cli.main(["run", "x", "-C", app]) == 1
    messages = [e.message for e in _store().events(1)]
    assert any("받아들이지 않았습니다" in m and "claude setup-token" in m for m in messages)
    assert all("sk-ant-oat01" not in m for m in messages)


def test_project_add_token은_토큰을_물어_등록한다(env, app, monkeypatch, capsys):
    auth.clear(config.load())
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("sk-ant-oat01-from-project-add\n"))
    assert cli.main(["project", "add", app, "--token"]) == 0
    assert auth.get(config.load(), auth.CLAUDE) == "sk-ant-oat01-from-project-add"
    assert "sk-ant-oat01" not in capsys.readouterr().out


def _login(refresh: str) -> str:
    return json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "acc-" + refresh, "refresh_token": refresh}})


def test_codex_팀원은_이_컴퓨터의_codex_로그인으로_실행하고_끝나면_서버에서_지운다(env, app, monkeypatch, capsys):
    local = env / "local-codex" / "auth.json"
    local.parent.mkdir()
    local.write_text(_login("r1"))
    monkeypatch.setenv("FAKE_CLAUDE_ENV", "CODEX_HOME,CODEX_API_KEY")
    monkeypatch.setenv("FAKE_CLAUDE_CODEX_LOGIN", "")
    assert cli.main(["run", "x", "-C", app]) == 0
    recorded = json.loads((env / "args.json").read_text())
    codex_home = env / "server-home" / ".coliday" / "jobs" / "1" / "codex-home"
    assert recorded["env"] == {"CODEX_HOME": str(codex_home), "CODEX_API_KEY": None}
    assert recorded["codex_login"] == {"text": _login("r1"), "mode": "0o600"}   # 실행하는 동안만, 본인만 읽게 둔다
    assert not codex_home.exists() and local.read_text() == _login("r1")        # 끝나면 지우고, 갱신이 없었으면 그대로
    assert "r1" not in (env / "ssh.jsonl").read_text()                          # 명령 인자에는 넣지 않는다
    assert any("이 컴퓨터의 Codex 로그인으로" in e.message for e in _store().events(1))

    monkeypatch.setenv("FAKE_CLAUDE_CODEX_LOGIN", _login("r2"))                 # 서버의 codex 가 로그인을 갱신했다
    assert cli.main(["continue", "1", "한 번 더"]) == 0
    assert local.read_text() == _login("r2") and local.stat().st_mode & 0o777 == 0o600   # 이 컴퓨터에 반영한다
    assert not codex_home.exists()
    assert any("갱신된 Codex 로그인" in e.message for e in _store().events(1))

    # 그 사이 이 컴퓨터에서도 갱신됐으면 (보낸 것과 달라졌으면) 덮어쓰지 않는다
    local.write_text(_login("r9"))
    assert not auth.restore_codex_login(_login("r2"), _login("r3")) and local.read_text() == _login("r9")
    assert not auth.restore_codex_login(_login("r9"), "") and local.read_text() == _login("r9")   # 서버에서 못 읽어 왔다


def test_API_키가_있거나_로그인이_없으면_codex_로그인을_서버에_두지_않는다(env, app, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_ENV", "CODEX_HOME")
    local_home = os.environ["CODEX_HOME"]
    assert cli.main(["run", "x", "-C", app]) == 0                               # 이 컴퓨터에 Codex 로그인이 없다
    assert json.loads((env / "args.json").read_text())["env"] == {"CODEX_HOME": local_home}   # 서버 쪽 값을 바꾸지 않는다
    assert any("이 컴퓨터에 Codex 로그인이 없어" in e.message for e in _store().events(1))

    local = env / "local-codex" / "auth.json"
    local.parent.mkdir()
    local.write_text(_login("r1"))
    auth.save(config.load(), auth.CODEX, "sk-proj-codexkey1234567890")          # API 키를 등록했으면 그쪽을 쓴다
    assert cli.main(["run", "x", "-C", app]) == 0
    assert json.loads((env / "args.json").read_text())["env"] == {"CODEX_HOME": local_home}
    assert not (env / "server-home" / ".coliday" / "jobs" / "2" / "codex-home").exists()

    local.write_text("{broken")                                                 # 깨진 파일은 넘기지 않는다
    assert auth.codex_login() is None
    assert not auth.restore_codex_login(_login("r1"), "not json") and local.read_text() == "{broken"
