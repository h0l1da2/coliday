"""테스트가 띄운 백그라운드 coliday 프로세스가 테스트보다 오래 살아남지 않게 한다.

worker 와 자동 묶기는 `start_new_session` 으로 떠서, 띄운 쪽(pytest)이 끝나거나 Ctrl+C·강제 종료로 죽어도 남는다.
남은 worker 는 가짜 claude·codex 를 붙잡은 채 계속 돌았다. 그래서 테스트는 환경 변수 `COLIDAY_LIFELINE` 에
생명줄 파일(pytest 의 PID 가 적힌 파일)을 주고, 그 환경을 물려받은 백그라운드 프로세스는 파일이 사라지거나
그 PID 가 죽으면 자기 프로세스 그룹(자기와 그 아래 claude·codex)을 끝낸다. 실사용에서는 이 변수가 없어 아무 일도 하지 않는다.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path

ENV = "COLIDAY_LIFELINE"


def watch(interval: float = 0.5) -> bool:
    """생명줄을 지켜보기 시작했으면 True.

    자기 세션의 리더(따로 떠 있는 백그라운드 프로세스)일 때만 지켜본다. 테스트가 같은 프로세스 안에서
    `cli.main` 을 부를 때 pytest 자신의 프로세스 그룹을 끝내지 않도록, 생명줄의 주인이 자기 자신이어도 지켜보지 않는다.
    """
    value = os.environ.get(ENV)
    if not value or os.getsid(0) != os.getpid():
        return False
    path = Path(value)
    if _owner(path) == os.getpid():
        return False
    threading.Thread(target=_loop, args=(path, interval), name="coliday-lifeline", daemon=True).start()
    return True


def _owner(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _alive(path: Path) -> bool:
    pid = _owner(path)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _loop(path: Path, interval: float) -> None:
    while _alive(path):
        time.sleep(interval)
    os.killpg(os.getpgrp(), signal.SIGTERM)
