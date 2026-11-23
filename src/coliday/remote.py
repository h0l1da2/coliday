"""SSH 로 접속하는 원격 프로젝트.

원격 위치는 `host:/절대/경로` 문자열이다 (`user@host:/srv/app`, `~/.ssh/config` 의 별칭 `myserver:/srv/app`).
로컬 경로는 항상 `/` 로 시작하므로 저장된 값만 보고 구분할 수 있다.

서버에는 coliday 를 설치하지 않는다: git 과 에이전트 CLI(claude, codex)만 있으면 되고, 이 컴퓨터의 coliday 가
ssh 로 명령을 실행하고 출력을 받아 온다. 접속 설정(포트·키·점프 호스트)은 `~/.ssh/config` 를 그대로 따른다.
백그라운드에서 실행하므로 비밀번호를 물을 수 없다 (키 인증이어야 한다).
"""

from __future__ import annotations

import functools
import os
import posixpath
import re
import shlex
import subprocess
import tarfile
from pathlib import Path


class RemoteError(RuntimeError):
    pass


_HOST = r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9_][A-Za-z0-9_.-]*"  # '-' 로 시작하면 ssh 옵션으로 읽힌다
_SPEC_RE = re.compile(rf"(?P<host>{_HOST}):(?P<path>[^\0\n]*)")
_CONNECT_TIMEOUT = 10
# 서버의 로그인 셸에서 실행한다 (~/.local/bin 처럼 프로필이 넣는 PATH 가 있어야 claude·codex 를 찾는다).
_PATH = 'PATH="$HOME/.local/bin:$HOME/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"; export PATH; '


def split(location: str | os.PathLike) -> tuple[str | None, str]:
    """(호스트, 경로). 로컬이면 호스트가 None 이다."""
    text = str(location)
    if text.startswith(("/", "~", ".")):
        return None, text
    match = _SPEC_RE.fullmatch(text)
    if not match or not match["path"]:
        return None, text
    return match["host"], match["path"]


def is_remote(location: str | os.PathLike) -> bool:
    return split(location)[0] is not None


def join(location: str | os.PathLike, *parts: str) -> str:
    host, path = split(location)
    parts = tuple(p for p in parts if p and p != ".")
    if host is None:
        return str(Path(path, *parts))
    return f"{host}:{posixpath.join(path, *parts)}"


def name(location: str | os.PathLike) -> str:
    """마지막 폴더 이름."""
    return posixpath.basename(split(location)[1].rstrip("/")) or str(location)


def ssh_bin() -> str:
    return os.environ.get("COLIDAY_SSH_BIN") or "ssh"


def _options() -> list[str]:
    control = Path.home() / ".ssh"
    try:
        control.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        pass
    return [
        "-o", "BatchMode=yes",  # 비밀번호·확인 질문으로 멈추지 않고 바로 실패한다
        "-o", f"ConnectTimeout={_CONNECT_TIMEOUT}",
        "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4",
        # 연결을 재사용한다: diff 갱신처럼 자주 실행하는 명령이 매번 새로 접속하지 않게.
        "-o", "ControlMaster=auto", "-o", f"ControlPath={control}/coliday-%C", "-o", "ControlPersist=120",
    ]


def command(host: str, script: str) -> list[str]:
    """host 에서 셸 스크립트를 실행하는 ssh 명령."""
    if not re.fullmatch(_HOST, host):
        raise RemoteError(f"호스트 이름이 올바르지 않습니다: {host!r}")
    quoted = shlex.quote(_PATH + script)
    wrapped = f'case "$SHELL" in */bash|*/zsh) exec "$SHELL" -lc {quoted};; *) exec /bin/sh -c {quoted};; esac'
    return [ssh_bin(), *_options(), host, wrapped]


def run(host: str, script: str, *, stdin: str | None = None, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    """스크립트를 실행하고 결과를 돌려준다. 접속하지 못했으면 RemoteError."""
    try:
        proc = subprocess.run(
            command(host, script), input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RemoteError(f"{host}: 응답이 없습니다 ({int(timeout)}초 초과)") from exc
    except OSError as exc:
        raise RemoteError(f"ssh 를 실행할 수 없습니다 ({ssh_bin()}): {exc}") from exc
    if proc.returncode == 255:  # ssh 자체의 실패 (접속·인증)
        reason = (proc.stderr.strip().splitlines() or ["접속 실패"])[-1]
        raise RemoteError(
            f"{host} 에 SSH 로 접속할 수 없습니다: {reason}\n"
            f"터미널에서 `ssh {host}` 가 비밀번호 없이(키 인증) 되는지 확인하세요."
        )
    return proc


def check(host: str, script: str, *, stdin: str | None = None, timeout: float = 30) -> str:
    """실행해서 표준 출력을 돌려준다. 명령이 실패해도 RemoteError."""
    proc = run(host, script, stdin=stdin, timeout=timeout)
    if proc.returncode != 0:
        raise RemoteError(f"{host}: {proc.stderr.strip() or proc.stdout.strip() or f'명령 실패 ({proc.returncode})'}")
    return proc.stdout


@functools.lru_cache(maxsize=32)
def home(host: str) -> str:
    """서버의 홈 디렉토리 (절대 경로)."""
    path = check(host, 'cd "$HOME" && pwd -P').strip()
    if not path.startswith("/"):
        raise RemoteError(f"{host}: 홈 디렉토리를 알 수 없습니다")
    return path


def normalize(location: str) -> str:
    """`host:경로` 를 `host:/절대경로` 로. `~` 는 서버의 홈으로 바꾼다 (이때만 접속한다)."""
    host, path = split(location)
    if host is None:
        raise RemoteError(f"SSH 위치가 아닙니다: {location}")
    if path == "~" or path.startswith("~/"):
        path = home(host) + path[1:]
    if not path.startswith("/"):
        raise RemoteError(f"서버의 경로는 절대 경로나 ~/ 로 시작해야 합니다: {location} (예: {host}:/srv/app, {host}:~/app)")
    return f"{host}:{posixpath.normpath(path)}"


def is_dir(location: str) -> bool:
    host, path = split(location)
    assert host is not None
    return run(host, f"test -d {shlex.quote(path)}").returncode == 0


def missing_commands(host: str, *names: str) -> list[str]:
    """서버의 PATH 에서 찾지 못한 명령."""
    script = "; ".join(f"command -v {shlex.quote(n)} >/dev/null 2>&1 || echo {shlex.quote(n)}" for n in names)
    return check(host, script).split()


def fetch_dir(host: str, path: str, target: Path, *, timeout: float = 300) -> int:
    """서버의 폴더 내용을 target 으로 가져온다 (target 은 비어 있어야 한다). 가져온 파일 수를 돌려준다.

    서버가 보낸 묶음을 그대로 풀지 않는다: 폴더 밖으로 나가는 경로·링크는 거부한다.
    """
    # COPYFILE_DISABLE: macOS 서버의 tar 가 확장 속성을 `._이름` 파일로 끼워 넣지 않게 한다.
    script = f"cd {shlex.quote(path)} 2>/dev/null && COPYFILE_DISABLE=1 tar -cf - . || true"
    try:
        proc = subprocess.Popen(
            command(host, script), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise RemoteError(f"ssh 를 실행할 수 없습니다 ({ssh_bin()}): {exc}") from exc
    count = 0
    try:
        assert proc.stdout is not None
        with tarfile.open(fileobj=proc.stdout, mode="r|") as archive:
            for member in archive:
                if not (member.isfile() or member.isdir()):
                    continue  # 링크·장치 파일은 가져오지 않는다
                archive.extract(member, target, filter="data")
                count += member.isfile()
    except tarfile.ReadError:
        pass  # 폴더가 없거나 비어 있다
    except (tarfile.TarError, OSError) as exc:
        raise RemoteError(f"{host}: 결과물을 가져오지 못했습니다: {exc}") from exc
    finally:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
    return count


def spawn(host: str, script: str) -> None:
    """결과를 기다리지 않고 실행한다 (취소·정리처럼 화면을 멈추면 안 되는 일)."""
    try:
        subprocess.Popen(
            command(host, script), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except (OSError, RemoteError):
        pass
