import os
import subprocess
import sys
import time
from pathlib import Path

from coliday import lifeline

SRC = str(Path(lifeline.__file__).resolve().parents[1])
# 따로 뜬 백그라운드 프로세스 흉내: 생명줄을 지켜보며 자식(sleep)을 하나 둔다. 자식 PID 를 먼저 알린다.
CHILD = (
    "import subprocess, sys, time\n"
    "from coliday import lifeline\n"
    "child = subprocess.Popen(['sleep', '60'])\n"
    "print(child.pid, lifeline.watch(0.05), flush=True)\n"
    "time.sleep(60)\n"
)


def _spawn(path: Path) -> tuple[subprocess.Popen, int, str]:
    env = {**os.environ, lifeline.ENV: str(path), "PYTHONPATH": SRC}
    proc = subprocess.Popen([sys.executable, "-c", CHILD], stdout=subprocess.PIPE, text=True, env=env,
                            start_new_session=True)
    pid, watching = proc.stdout.readline().split()
    return proc, int(pid), watching


def _gone(pid: int, deadline: float = 5.0) -> bool:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _cleanup(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        os.killpg(proc.pid, 9)
        proc.wait()


def test_생명줄_파일이_사라지면_백그라운드_프로세스와_그_자식이_함께_끝난다(tmp_path):
    path = tmp_path / "pid"
    path.write_text(str(os.getpid()))
    proc, child, watching = _spawn(path)
    try:
        assert watching == "True"
        time.sleep(0.3)
        assert proc.poll() is None                              # 생명줄이 있는 동안은 계속 돈다
        path.unlink()
        assert proc.wait(timeout=5) != 0 and _gone(child)       # 자기 프로세스 그룹(자식 포함)을 끝낸다
    finally:
        _cleanup(proc)


def test_생명줄의_주인이_죽으면_백그라운드_프로세스도_끝난다(tmp_path):
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    path = tmp_path / "pid"
    path.write_text(str(owner.pid))
    proc, child, _ = _spawn(path)
    try:
        time.sleep(0.3)
        assert proc.poll() is None
        owner.kill()                                            # pytest 가 Ctrl+C·강제 종료로 죽은 경우
        owner.wait()
        assert proc.wait(timeout=5) != 0 and _gone(child)
    finally:
        owner.kill()
        _cleanup(proc)


def test_같은_프로세스_안이나_세션_리더가_아니면_지켜보지_않는다(tmp_path, monkeypatch):
    path = tmp_path / "pid"
    path.write_text(str(os.getpid()))
    monkeypatch.setenv(lifeline.ENV, str(path))
    assert lifeline.watch() is False                            # pytest 안에서 cli.main 을 불러도 pytest 를 끝내지 않는다
    monkeypatch.delenv(lifeline.ENV)
    assert lifeline.watch() is False                            # 실사용: 변수가 없으면 아무 일도 하지 않는다


def test_테스트가_띄운_worker는_테스트가_끝나면_남지_않는다(_lifeline, tmp_path):
    env = {**os.environ, "PYTHONPATH": SRC}
    assert env[lifeline.ENV] == str(_lifeline)                  # conftest 가 테스트마다 생명줄을 준다
    proc, child, watching = _spawn(_lifeline)
    try:
        assert watching == "True"
        _lifeline.unlink()                                      # conftest 의 정리 단계와 같다
        assert proc.wait(timeout=5) != 0 and _gone(child)
    finally:
        _cleanup(proc)
