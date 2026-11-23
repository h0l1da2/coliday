from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coliday import cli, updater


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
    ).stdout


def _commit(repo: Path, name: str, text: str, message: str) -> None:
    (repo / name).write_text(text)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", message)


@pytest.fixture
def clone(tmp_path):
    """원격(origin)과 그것을 clone 한 폴더."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _commit(origin, "pyproject.toml", "v1\n", "init")
    _git(tmp_path, "clone", "-q", str(origin), "clone")
    return origin, tmp_path / "clone"


def test_update는_새_커밋을_받고_의존성이_바뀌었을_때만_다시_설치한다(clone):
    origin, local = clone
    installed: list[Path] = []
    assert updater.update(local, install=installed.append).commits == []          # 이미 최신

    _commit(origin, "a.py", "x\n", "기능 추가")
    _commit(origin, "b.py", "y\n", "버그 수정")
    checked = updater.update(local, check=True, install=installed.append)
    assert [c.split(" ", 1)[1] for c in checked.commits] == ["버그 수정", "기능 추가"] and not checked.changed
    assert not (local / "a.py").exists()                                          # 확인만 하고 받지 않는다

    result = updater.update(local, install=installed.append)
    assert result.changed and len(result.commits) == 2 and (local / "b.py").is_file()
    assert not result.reinstalled and installed == []                             # 코드만 바뀌면 받기만 한다

    _commit(origin, "pyproject.toml", "v2\n", "의존성 변경")
    result = updater.update(local, install=installed.append)
    assert result.reinstalled and installed == [local]


def test_update는_직접_고친_내용을_덮어쓰지_않는다(clone):
    origin, local = clone
    _commit(origin, "a.py", "원격\n", "원격 변경")
    (local / "a.py").write_text("내가 쓴 파일\n")                                   # 받을 파일과 겹치는 로컬 파일
    with pytest.raises(updater.UpdateError, match="직접 고친 내용"):
        updater.update(local, install=lambda _: None)
    assert (local / "a.py").read_text() == "내가 쓴 파일\n"

    (local / "a.py").unlink()
    _commit(local, "mine.py", "z\n", "로컬 커밋")                                   # 갈라진 기록도 합치지 않는다
    with pytest.raises(updater.UpdateError):
        updater.update(local, install=lambda _: None)
    assert "로컬 커밋" in _git(local, "log", "--oneline", "-1")


def test_update는_원격을_알_수_없으면_이유를_알린다(clone, tmp_path):
    _, local = clone
    _git(local, "switch", "-q", "-c", "side")
    with pytest.raises(updater.UpdateError, match="원격 브랜치"):
        updater.update(local)
    _git(local, "switch", "-q", "main")
    _git(local, "remote", "set-url", "origin", str(tmp_path / "gone"))
    with pytest.raises(updater.UpdateError):
        updater.update(local)


def test_update_명령은_결과를_알린다(clone, monkeypatch, capsys):
    origin, local = clone
    monkeypatch.setattr(updater, "source_root", lambda: local)
    assert cli.main(["update"]) == 0 and "이미 최신입니다" in capsys.readouterr().out
    _commit(origin, "a.py", "x\n", "기능 추가")
    assert cli.main(["update", "--check"]) == 0
    assert "받을 업데이트가 1개" in capsys.readouterr().out and not (local / "a.py").exists()
    assert cli.main(["update"]) == 0
    out = capsys.readouterr().out
    assert "업데이트했습니다" in out and "기능 추가" in out and "다시 여세요" in out

    def not_a_clone():
        raise updater.UpdateError("git 으로 받은 폴더에서 설치한 것이 아니라")
    monkeypatch.setattr(updater, "source_root", not_a_clone)
    assert cli.main(["update"]) == 2 and "git 으로 받은 폴더" in capsys.readouterr().err


def test_설치된_coliday의_clone_폴더를_찾는다():
    assert updater.source_root() == Path(__file__).resolve().parents[1]


def test_새_버전_알림은_받아_둔_정보로_만들고_하루에_한_번만_확인한다(clone, tmp_path, monkeypatch):
    import time

    origin, local = clone
    stamp = tmp_path / "home" / "update-check"
    monkeypatch.setattr(updater, "source_root", lambda: local)
    assert updater.notice(stamp) is None                                # 끈 상태 (테스트 기본값)
    assert not stamp.exists()
    monkeypatch.delenv(updater.DISABLE_ENV)

    _commit(origin, "a.py", "x\n", "기능 추가")
    assert updater.notice(stamp) is None and stamp.exists()             # 아직 받아 두지 않았다: 백그라운드 확인만 시작
    deadline = time.time() + 10
    while updater.pending(local) == 0 and time.time() < deadline:
        time.sleep(0.1)
    assert updater.notice(stamp) == "새 버전이 있습니다 (커밋 1개). 받으려면: coliday update"
    assert not (local / "a.py").exists()                                # 알리기만 하고 받지 않는다

    _commit(origin, "b.py", "y\n", "또 추가")
    assert updater.refresh(local, stamp) is False                       # 하루 안에는 다시 확인하지 않는다
    assert updater.refresh(local, stamp, now=time.time() + updater.CHECK_EVERY_SEC + 1) is True

    updater.update(local, install=lambda _: None)
    assert updater.notice(stamp) is None                                # 받고 나면 알림이 사라진다


def test_새_버전_확인이_실패해도_조용히_넘어간다(clone, tmp_path, monkeypatch):
    _, local = clone
    monkeypatch.delenv(updater.DISABLE_ENV)
    monkeypatch.setattr(updater, "source_root", lambda: local)
    subprocess.run(["git", "-C", str(local), "remote", "set-url", "origin", str(tmp_path / "gone")], check=True)
    assert updater.notice(tmp_path / "home" / "update-check") is None   # 원격에 닿지 못한다
    assert updater.pending(tmp_path) == 0                               # git 저장소가 아니다

    def not_a_clone():
        raise updater.UpdateError("clone 이 아니다")
    monkeypatch.setattr(updater, "source_root", not_a_clone)
    assert updater.notice(tmp_path / "x" / "update-check") is None and not (tmp_path / "x").exists()


def test_명령은_터미널에서_직접_실행했을_때만_새_버전을_알린다(tmp_path, monkeypatch, capsys):
    import sys

    monkeypatch.setenv("COLIDAY_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(updater, "notice", lambda stamp: "새 버전이 있습니다 (커밋 2개). 받으려면: coliday update")
    assert cli.main(["ls"]) == 0
    assert "새 버전" not in capsys.readouterr().err                      # 출력을 다른 프로그램이 받는 중이다

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    assert cli.main(["ls"]) == 0
    captured = capsys.readouterr()
    assert "새 버전이 있습니다 (커밋 2개)" in captured.err and "새 버전" not in captured.out

    def boom(stamp):
        raise RuntimeError("확인 실패")
    monkeypatch.setattr(updater, "notice", boom)
    assert cli.main(["ls"]) == 0                                        # 알림이 실패해도 명령은 된다
