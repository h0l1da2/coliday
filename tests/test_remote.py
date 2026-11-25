"""SSH 프로젝트. 가짜 ssh 가 받은 명령을 이 컴퓨터에서 실행한다 (서버의 홈은 따로 둔 폴더).

에이전트(가짜 claude·codex)는 이 컴퓨터에서 돌고, 서버는 작업마다 만들어 주는 실행 파일로 다룬다.
"""

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

from coliday import cli, config, gitops, jobs, remote, team
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
    monkeypatch.setenv("COLIDAY_CLAUDE_BIN", str(tmp_path / "claude"))   # 에이전트는 이 컴퓨터의 claude·codex 다
    monkeypatch.setenv("COLIDAY_CODEX_BIN", str(tmp_path / "codex"))
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


def _helper(job_id: int, name: str, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    """에이전트가 하듯이 그 작업의 서버용 실행 파일을 실행한다."""
    path = config.load().job_dir(job_id) / "server" / name
    return subprocess.run([str(path), *args], input=stdin, capture_output=True, text=True, timeout=30)


def test_서버의_프로젝트도_에이전트는_이_컴퓨터에서_돌고_실행_파일로_서버를_다룬다(env, app, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_ARTIFACT", "login.png,notes/run.log")
    assert cli.main(["run", "로그인 고쳐줘", "-C", app + "/sub"]) == 0

    home = env / "server-home"
    cfg = config.load()
    job = _store().get_job(1)
    assert job.status == "succeeded" and job.repo == app
    assert job.workdir == f"{HOST}:{home}/.coliday/worktrees/app-1/sub" and job.branch == "coliday/1"
    worktree = home / ".coliday" / "worktrees" / "app-1"
    assert (worktree / "sub" / "a.txt").is_file()                      # worktree 는 서버에 만든다
    assert not (cfg.worktrees_dir / "app-1").exists()
    assert not (home / ".coliday" / "jobs").exists()                    # 서버에 에이전트용 파일을 두지 않는다

    work = cfg.job_dir(1) / "work"
    recorded = json.loads((env / "args.json").read_text())
    argv = recorded["argv"]
    assert recorded["cwd"] == str(work) and recorded["stdin"] == "로그인 고쳐줘"    # 이 컴퓨터의 작업 폴더에서 돈다
    paths = {name: str(cfg.job_dir(1) / "server" / name) for name in ("server", "server-pull", "server-push")}
    allowed = argv[argv.index("--allowedTools") + 1:]
    for path in paths.values():                                         # 그 실행 파일만 권한 검사·샌드박스 없이 실행된다
        assert os.access(path, os.X_OK) and f"Bash({shlex.quote(path)}:*)" in allowed
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert {f"{shlex.quote(p)} *" for p in paths.values()} <= set(settings["sandbox"]["excludedCommands"])
    lead = argv[argv.index("--append-system-prompt") + 1]
    assert "## 서버의 프로젝트" in lead and f"서버 {HOST} 의 `{worktree / 'sub'}`" in lead and paths["server-pull"] in lead
    assert "이 컴퓨터에 있는 이름이 .env 로 시작하는 파일" in lead         # 서버의 .env 는 막는다고 말하지 않는다
    agents = json.loads((cfg.job_dir(1) / "agents.json").read_text())
    # 팀원도 같은 방법을 안다 (Codex 팀원은 그 실행 파일 안의 역할 지침에 들어 있다)
    assert "## 서버의 프로젝트" in agents["coder"]["prompt"] and "## 서버의 프로젝트" in agents["reviewer"]["prompt"]
    assert "## 서버의 프로젝트" in (cfg.job_dir(1) / "codex" / "1-codex-coder").read_text()
    events = [e.message for e in _store().events(1)]
    assert any("이 컴퓨터의 에이전트가 SSH 로" in m for m in events) and any("서버의 파일은 막지 못합니다" in m for m in events)

    # 실행 파일: 서버의 프로젝트 폴더에서 명령을 돌리고, 파일을 가져와 고쳐서 올린다
    done = _helper(1, "server", "pwd; cat a.txt; echo \"it's $HOME\"")
    assert done.returncode == 0 and done.stdout == f"{worktree / 'sub'}\na\nit's {home}\n"
    assert _helper(1, "server", "exit 7").returncode == 7                # 서버 명령의 종료 코드가 그대로 온다
    assert _helper(1, "server", "cat", stdin="표준 입력\n").stdout == "표준 입력\n"
    assert _helper(1, "server-pull", "a.txt").returncode == 0 and (work / "a.txt").read_text() == "a\n"
    (work / "a.txt").write_text("고쳤다\n")
    (work / "새 폴더").mkdir()
    (work / "새 폴더" / "it's new.txt").write_text("new\n")
    assert _helper(1, "server-push", "a.txt", "새 폴더").returncode == 0
    assert (worktree / "sub" / "a.txt").read_text() == "고쳤다\n"
    assert (worktree / "sub" / "새 폴더" / "it's new.txt").read_text() == "new\n"
    assert _helper(1, "server-pull", "새 폴더/it's new.txt").returncode == 0
    for bad in ("/etc/passwd", "../x", "a/../../x", "-rf"):               # 프로젝트 폴더 밖은 받지 않는다
        assert _helper(1, "server-pull", bad).returncode == 2 and _helper(1, "server-push", bad).returncode == 2
    missing = _helper(1, "server-pull", "없는파일")
    assert missing.returncode == 1 and "가져오지 못했습니다" in missing.stderr
    assert _helper(1, "server").returncode == 2

    calls = _ssh_calls(env)
    assert calls and all(c["host"] == HOST for c in calls)
    assert all("BatchMode=yes" in c["options"] for c in calls)           # 비밀번호를 묻느라 멈추지 않는다
    assert any("-N" in c["options"] for c in calls)                      # 작업이 도는 동안 연결을 열어 둔다
    assert (cfg.artifacts_dir(1) / "login.png").read_bytes() == b"fake"   # 결과물은 이 컴퓨터에 바로 쌓인다

    capsys.readouterr()
    assert cli.main(["diff", "1"]) == 0                                   # diff 는 서버에서 계산한다
    assert "고쳤다" in capsys.readouterr().out
    assert jobs.list_projects(cfg, _store()) == [app + "/sub"]           # 시작한 위치가 연결된다


def test_서버_작업을_이어서_시키고_삭제하면_서버의_worktree도_지운다(env, app, monkeypatch, capsys):
    cli.main(["run", "x", "-C", app])
    assert cli.main(["continue", "1", "더 해줘"]) == 0
    recorded = json.loads((env / "args.json").read_text())
    assert "--resume" in recorded["argv"] and recorded["cwd"] == str(config.load().job_dir(1) / "work")
    assert _store().get_job(1).status == "succeeded"

    home = env / "server-home"
    assert cli.main(["rm", "1", "--force"]) == 0
    assert not (home / ".coliday" / "worktrees" / "app-1").exists()
    branches = subprocess.run(["git", "-C", remote.split(app)[1], "branch"], capture_output=True, text=True).stdout
    assert "coliday/1" not in branches


def test_서버의_프로젝트에서도_codex_팀장과_팀원이_네트워크를_열고_이_컴퓨터에서_돈다(env, app, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CODEX_ARGS", str(env / "codex.jsonl"))
    cli.main(["model", "coder", "codex"])
    assert cli.main(["run", "x", "-C", app]) == 0                         # Claude 팀장 + Codex 팀원
    cfg = config.load()
    runner = (cfg.job_dir(1) / "codex" / "1-coder").read_text()
    assert 'permissions.coliday.network={"enabled"=true}' in runner       # ssh 로 서버에 닿아야 한다
    assert "## 서버의 프로젝트" in runner
    argv = json.loads((env / "args.json").read_text())["argv"]
    assert f"Bash({shlex.quote(str(cfg.job_dir(1) / 'codex' / '1-coder'))}:*)" in argv

    assert cli.main(["run", "y", "-C", app, "--model", "codex"]) == 0     # Codex 팀장도 된다 (전부 이 컴퓨터에서 돈다)
    lead = json.loads((env / "codex.jsonl").read_text().splitlines()[-1])
    assert lead["cwd"] == str(cfg.job_dir(2) / "work")
    assert 'permissions.coliday.network={"enabled"=true}' in lead["argv"]
    assert "## 서버의 프로젝트" in lead["stdin"] and str(cfg.job_dir(2) / "server" / "server") in lead["stdin"]
    assert _store().get_job(2).status == "succeeded"

    # 읽기 전용 역할도 서버 작업에서는 작업 폴더(서버 파일을 가져다 보는 곳)에 쓸 수 있고, Bash 가 없던 역할은 Bash 를 받는다
    resolved = team.for_server({"viewer": {"prompt": "본다", "tools": ["Read"]}, "any": {"prompt": "한다"}}, "\n## 서버의 프로젝트\n")
    assert resolved["viewer"]["tools"] == ["Read", "Bash"] and "tools" not in resolved["any"]
    assert team.codex_sandbox(resolved["viewer"]) == "read-only" and team.codex_sandbox(resolved["viewer"], True) == "workspace-write"
    plain = team.codex_access("read-only", network=True)                  # .env 를 막지 않는 작업도 네트워크는 연다
    assert 'permissions.coliday.network={"enabled"=true}' in plain and not any(".env" in part for part in plain)
    assert team.codex_access("read-only") == ["-c", 'sandbox_mode="read-only"']


def test_실행_중인_서버_작업을_취소한다(env, app, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    assert cli.main(["run", "x", "-C", app, "-d"]) == 0
    _wait(lambda: (env / "args.json").exists())
    pid = json.loads((env / "args.json").read_text())["pid"]
    assert cli.main(["cancel", "1"]) == 0
    _wait(lambda: not jobs.alive(pid))
    assert _store().get_job(1).status == "cancelled"


def test_서버_작업도_이_컴퓨터의_env는_막고_허용하면_푼다(env, app, monkeypatch, capsys):
    assert cli.main(["run", "x", "-C", app]) == 0
    argv = json.loads((env / "args.json").read_text())["argv"]
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["sandbox"]["enabled"] and settings["sandbox"]["filesystem"]["denyRead"]   # 이 컴퓨터의 .env
    assert settings["sandbox"]["filesystem"]["denyWrite"] == config.load().protected_paths

    monkeypatch.setenv("FAKE_CLAUDE_VERSION", "1.0.58 (Claude Code)")     # 이 컴퓨터의 claude 가 낮다
    assert cli.main(["run", "y", "-C", app]) == 2
    assert "이 컴퓨터의 claude(1.0.58)는 .env 읽기를 막지 못합니다" in capsys.readouterr().err
    assert cli.main(["run", "y", "-C", app, "--allow-env"]) == 0
    argv = json.loads((env / "args.json").read_text())["argv"]
    assert "--settings" not in argv and "Bash(" in " ".join(argv)         # 허용해도 서버용 실행 파일은 열려 있다
    assert ".env 로 시작하는 파일" not in argv[argv.index("--append-system-prompt") + 1]

