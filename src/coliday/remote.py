"""SSH 로 접속하는 원격 프로젝트.

원격 위치는 `host:/절대/경로` 문자열이다 (`user@host:/srv/app`, `~/.ssh/config` 의 별칭 `myserver:/srv/app`).
포트가 22 가 아니면 호스트 뒤에 붙인다: `user@host:2222:/srv/app`. 이때 "호스트"는 `user@host:2222` 전체다.
로컬 경로는 항상 `/` 로 시작하므로 저장된 값만 보고 구분할 수 있다.

서버에는 coliday 를 설치하지 않는다: git 과 에이전트 CLI(claude, codex)만 있으면 되고, 이 컴퓨터의 coliday 가
ssh 로 명령을 실행하고 출력을 받아 온다. 접속 설정(포트·키·점프 호스트)은 `~/.ssh/config` 를 그대로 따른다.
백그라운드에서 실행하므로 비밀번호를 물을 수 없다 (키 인증이어야 한다). 키 인증이 안 돼 있으면
프로젝트를 추가할 때 비밀번호를 한 번 받아 이 컴퓨터의 공개키를 서버에 등록한다 (`install_key`). 비밀번호는 저장하지 않는다.
"""

from __future__ import annotations

import functools
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path


class RemoteError(RuntimeError):
    pass


_BARE_HOST = r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9_][A-Za-z0-9_.-]*"  # '-' 로 시작하면 ssh 옵션으로 읽힌다
_HOST = rf"{_BARE_HOST}(?::[0-9]{{1,5}})?"  # 뒤의 숫자는 포트
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


def with_port(host: str, port: str | int | None) -> str:
    """`user@host` 에 포트를 붙인 호스트. 포트가 비었으면 그대로다 (22, 또는 ~/.ssh/config 의 설정)."""
    text = str(port or "").strip()
    if not text:
        return host
    if not text.isdigit() or not 1 <= int(text) <= 65535:
        raise RemoteError(f"포트는 1~65535 의 숫자여야 합니다: {text}")
    if not re.fullmatch(_BARE_HOST, host):
        raise RemoteError(f"호스트 이름이 올바르지 않습니다: {host!r}")
    return f"{host}:{int(text)}"


def target(host: str) -> list[str]:
    """ssh 명령에 넣을 접속 대상 인자: `user@host`, 포트가 있으면 `-p 포트 user@host`."""
    if not re.fullmatch(_HOST, host):
        raise RemoteError(f"호스트 이름이 올바르지 않습니다: {host!r}")
    bare, _, port = host.partition(":")
    if not port:
        return [bare]
    if not 1 <= int(port) <= 65535:
        raise RemoteError(f"포트는 1~65535 의 숫자여야 합니다: {port}")
    return ["-p", str(int(port)), bare]


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
    quoted = shlex.quote(_PATH + script)
    wrapped = f'case "$SHELL" in */bash|*/zsh) exec "$SHELL" -lc {quoted};; *) exec /bin/sh -c {quoted};; esac'
    return [ssh_bin(), *_options(), *target(host), wrapped]


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


_PASSWORD_ENV = "COLIDAY_SSH_PASSWORD"
_KEY_NAMES = ("id_ed25519", "id_ecdsa", "id_rsa")
# 받은 공개키(stdin)가 없을 때만 한 줄 덧붙인다. 파일과 폴더는 본인만 읽게 만든다 (아니면 sshd 가 키를 무시한다).
_ADD_KEY = (
    'umask 077; mkdir -p "$HOME/.ssh" && key=$(cat) && [ -n "$key" ] && '
    '{ grep -qxF "$key" "$HOME/.ssh/authorized_keys" 2>/dev/null || printf \'%s\\n\' "$key" >> "$HOME/.ssh/authorized_keys"; }'
)


def key_works(host: str) -> bool:
    """비밀번호 없이(키로) 접속되는지."""
    try:
        return run(host, "true").returncode == 0
    except RemoteError:
        return False


def install_key(host: str, password: str) -> Path:
    """비밀번호로 한 번 접속해 이 컴퓨터의 공개키를 서버의 authorized_keys 에 등록한다. 등록한 공개키 파일을 돌려준다.

    비밀번호는 이 접속에만 쓰고 어디에도 저장하지 않는다 (명령 인자나 로그에도 남기지 않고 자식 프로세스의 환경으로만 넘긴다).
    처음 보는 서버의 호스트 키는 묻지 않고 받아들인다 (터미널에서 yes 를 누르는 것과 같다. 이미 아는 서버의 키가 바뀌었으면 실패한다).
    """
    destination = target(host)
    public = _public_key()
    work = Path(tempfile.mkdtemp(prefix="coliday-askpass-"))
    try:
        askpass = work / "askpass"
        askpass.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "${_PASSWORD_ENV}"\n', encoding="utf-8")
        askpass.chmod(0o700)
        env = dict(os.environ, SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE="force")
        env.setdefault("DISPLAY", ":0")  # 오래된 ssh 는 DISPLAY 가 있어야 SSH_ASKPASS 를 쓴다
        env[_PASSWORD_ENV] = password
        cmd = [
            ssh_bin(),
            "-o", "PreferredAuthentications=password,keyboard-interactive", "-o", "PubkeyAuthentication=no",
            "-o", "NumberOfPasswordPrompts=1", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ControlPath=none", "-o", f"ConnectTimeout={_CONNECT_TIMEOUT}",
            *destination, _ADD_KEY,
        ]
        try:
            # 터미널에서 떼어 낸다: ssh 가 터미널에 직접 비밀번호를 묻지 않고 askpass 를 쓰게.
            proc = subprocess.run(
                cmd, input=public.read_text(encoding="utf-8").strip() + "\n", capture_output=True, text=True,
                timeout=30, check=False, env=env, start_new_session=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise RemoteError(f"{host}: 응답이 없습니다 (30초 초과)") from exc
        except OSError as exc:
            raise RemoteError(f"ssh 를 실행할 수 없습니다 ({ssh_bin()}): {exc}") from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if proc.returncode != 0:
        reason = (proc.stderr.strip().splitlines() or ["접속 실패"])[-1]
        hint = "비밀번호가 맞는지, 서버가 비밀번호 로그인을 허용하는지 확인하세요" if proc.returncode == 255 else "서버에 키를 쓰지 못했습니다"
        raise RemoteError(f"{host}: SSH 키를 등록하지 못했습니다 ({reason}). {hint}.")
    if not key_works(host):
        raise RemoteError(
            f"{host}: 키({public})를 등록했지만 키로 접속되지 않습니다. 키에 암호가 걸려 있으면 ssh-add 로 등록하고, "
            f"터미널에서 ssh {' '.join(destination)} 가 비밀번호 없이 되는지 확인하세요."
        )
    return public


def _public_key() -> Path:
    """이 컴퓨터의 공개키 파일. 없으면 암호 없는 ed25519 키를 새로 만든다."""
    directory = Path.home() / ".ssh"
    for key_name in _KEY_NAMES:
        if (directory / key_name).is_file() and (directory / f"{key_name}.pub").is_file():
            return directory / f"{key_name}.pub"
    target = directory / _KEY_NAMES[0]
    try:
        directory.mkdir(mode=0o700, exist_ok=True)
        if target.exists():  # 개인키만 있다: 덮어쓰지 않고 공개키만 다시 뽑는다
            made = subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", str(target)], capture_output=True, text=True, check=False)
            if made.returncode == 0:
                target.with_name(target.name + ".pub").write_text(made.stdout, encoding="utf-8")
        else:
            made = subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "coliday", "-f", str(target)],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
            )
    except OSError as exc:
        raise RemoteError(f"SSH 키를 만들 수 없습니다: {exc}") from exc
    if made.returncode != 0:
        raise RemoteError(f"SSH 키를 만들 수 없습니다: {made.stderr.strip() or 'ssh-keygen 실패'}")
    return target.with_name(target.name + ".pub")


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
