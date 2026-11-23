"""`coliday update`: clone 한 폴더를 최신으로 받는다.

coliday 는 clone 한 폴더의 코드를 그대로 쓰도록(editable) 설치하므로 `git pull` 이 곧 업데이트다.
의존성(pyproject.toml)이 바뀐 업데이트면 설치도 다시 한다.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class Result:
    root: Path
    before: str
    after: str
    commits: list[str]         # 새로 받은 커밋 한 줄 요약 (최신이 먼저). check 면 받을 커밋
    reinstalled: bool = False  # 의존성이 바뀌어 설치를 다시 했다

    @property
    def changed(self) -> bool:
        return self.before != self.after


def source_root() -> Path:
    """지금 실행 중인 coliday 가 들어 있는 clone 폴더."""
    root = Path(__file__).resolve().parents[2]
    if not (root / ".git").exists() or not (root / "pyproject.toml").is_file():
        raise UpdateError(
            f"git 으로 받은 폴더에서 설치한 것이 아니라 자동으로 업데이트할 수 없습니다: {Path(__file__).resolve().parent}\n"
            "README 의 설치 방법대로 clone 해서 다시 설치하세요."
        )
    return root


def _git(root: Path, *args: str, timeout: float = 120) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, check=False, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise UpdateError(f"git {args[0]} 이(가) {int(timeout)}초 안에 끝나지 않았습니다 (네트워크를 확인하세요).") from exc
    except OSError as exc:
        raise UpdateError(f"git 을 실행할 수 없습니다: {exc}") from exc
    if proc.returncode != 0:
        raise UpdateError(proc.stderr.strip() or f"git {' '.join(args)} 실패")
    return proc.stdout


def update(root: Path, *, check: bool = False, install: Callable[[Path], None] | None = None) -> Result:
    """최신 커밋을 받는다. check 면 받을 것이 있는지만 확인하고 아무것도 바꾸지 않는다.

    받은 커밋이 지금 위치의 뒤를 잇는 경우에만 받는다 (fast-forward). 직접 고친 내용은 건드리지 않는다.
    """
    before = _git(root, "rev-parse", "HEAD").strip()
    try:
        upstream = _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}").strip()
    except UpdateError as exc:
        raise UpdateError(
            f"받아 올 원격 브랜치가 정해져 있지 않습니다 ({root}). 브랜치를 main 으로 바꾼 뒤 다시 실행하세요: git -C {root} switch main"
        ) from exc
    _git(root, "fetch", "--quiet")
    incoming = _git(root, "log", "--oneline", f"HEAD..{upstream}").splitlines()
    if check or not incoming:
        return Result(root, before, before, incoming)
    try:
        _git(root, "merge", "--ff-only", "--quiet", upstream)
    except UpdateError as exc:
        raise UpdateError(
            f"자동으로 받을 수 없습니다: {exc}\n"
            f"clone 한 폴더({root})에 직접 고친 내용이나 따로 만든 커밋이 있으면 정리한 뒤 다시 실행하세요."
        ) from exc
    after = _git(root, "rev-parse", "HEAD").strip()
    reinstalled = False
    if _git(root, "diff", "--name-only", before, after, "--", "pyproject.toml").strip():
        (install or reinstall)(root)
        reinstalled = True
    return Result(root, before, after, incoming, reinstalled)


def reinstall(root: Path) -> None:
    """의존성이 바뀌었을 때 지금 쓰는 파이썬 환경에 다시 설치한다 (pip 이 없는 uv 환경은 uv 로)."""
    if importlib.util.find_spec("pip") is not None:
        cmd = [sys.executable, "-m", "pip", "install", "--quiet", "-e", str(root)]
    elif shutil.which("uv"):
        cmd = ["uv", "pip", "install", "--quiet", "--python", sys.executable, "-e", str(root)]
    else:
        raise UpdateError(
            f"코드는 받았지만 의존성을 다시 설치할 방법(pip, uv)을 찾지 못했습니다. README 의 설치 명령을 한 번 더 실행하세요 ({root})."
        )
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL)
    except OSError as exc:
        raise UpdateError(f"코드는 받았지만 다시 설치하지 못했습니다: {exc}") from exc
    if proc.returncode != 0:
        raise UpdateError(
            "코드는 받았지만 의존성 설치에 실패했습니다. README 의 설치 명령을 한 번 더 실행하세요:\n"
            + (proc.stderr.strip() or proc.stdout.strip())[-2000:]
        )


# ── 새 버전 알림 ─────────────────────────────────────────────────────────
# 실행할 때 원격을 가끔(하루 한 번) 백그라운드로 받아 두고, 받을 커밋이 있으면 한 줄로 알린다.
# 알림은 이미 받아 둔 정보로 만든다: 실행을 기다리게 하지 않고, 네트워크가 없으면 조용히 넘어간다.
CHECK_EVERY_SEC = 24 * 3600
DISABLE_ENV = "COLIDAY_NO_UPDATE_CHECK"


def pending(root: Path) -> int:
    """받아 둔 원격 기준으로 아직 받지 않은 커밋 수. 네트워크는 쓰지 않는다. 알 수 없으면 0."""
    try:
        return int(_git(root, "rev-list", "--count", "HEAD..@{upstream}", timeout=10).strip() or 0)
    except (UpdateError, ValueError):
        return 0


def refresh(root: Path, stamp: Path, *, now: float | None = None) -> bool:
    """마지막 확인에서 하루가 지났으면 백그라운드로 원격을 받아 온다. 시작했으면 True.

    끝나기를 기다리지 않는다. 결과는 다음 `pending` 부터 보인다.
    """
    now = time.time() if now is None else now
    try:
        if now - stamp.stat().st_mtime < CHECK_EVERY_SEC:
            return False
    except OSError:
        pass  # 아직 확인한 적이 없다
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(f"{int(now)}\n")  # 먼저 적는다: 받는 데 실패해도 실행할 때마다 다시 시도하지 않는다
        os.utime(stamp, (now, now))
        subprocess.Popen(
            ["git", "-C", str(root), "fetch", "--quiet"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},  # 인증을 묻느라 멈추지 않는다
        )
    except OSError:
        return False
    return True


def notice(stamp: Path) -> str | None:
    """새 버전이 있으면 알릴 한 줄, 없거나 알 수 없으면 None. 필요하면 백그라운드 확인도 시작한다."""
    if os.environ.get(DISABLE_ENV):
        return None
    try:
        root = source_root()
    except UpdateError:
        return None  # clone 해서 설치한 것이 아니면 확인할 곳이 없다
    refresh(root, stamp)
    count = pending(root)
    return f"새 버전이 있습니다 (커밋 {count}개). 받으려면: coliday update" if count else None
