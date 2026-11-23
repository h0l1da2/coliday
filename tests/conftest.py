import pytest


@pytest.fixture(autouse=True)
def _no_update_check(monkeypatch):
    """테스트가 이 레포의 원격(GitHub)을 받아 오지 않게 새 버전 확인을 끈다. 확인 자체를 보는 테스트만 다시 켠다."""
    monkeypatch.setenv("COLIDAY_NO_UPDATE_CHECK", "1")
