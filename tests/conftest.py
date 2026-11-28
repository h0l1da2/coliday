import os

import pytest

from coliday import lifeline


@pytest.fixture(autouse=True)
def _no_update_check(monkeypatch):
    """테스트가 이 레포의 원격(GitHub)을 받아 오지 않게 새 버전 확인을 끈다. 확인 자체를 보는 테스트만 다시 켠다."""
    monkeypatch.setenv("COLIDAY_NO_UPDATE_CHECK", "1")


@pytest.fixture(autouse=True)
def _lifeline(tmp_path_factory, monkeypatch):
    """테스트가 띄운 worker·자동 묶기가 테스트 뒤에 남지 않게 한다 (`coliday.lifeline`).

    테스트가 끝나면 생명줄 파일을 지워 그 테스트의 백그라운드 프로세스를 끝낸다.
    Ctrl+C 나 강제 종료로 pytest 가 죽어도 파일에 적힌 PID 가 사라지므로 함께 끝난다.
    """
    path = tmp_path_factory.mktemp("lifeline") / "pid"
    path.write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setenv(lifeline.ENV, str(path))
    yield path
    path.unlink(missing_ok=True)
