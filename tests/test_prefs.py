from __future__ import annotations

import json
import multiprocessing
import os
import threading
from pathlib import Path

import pytest

from coliday import prefs as prefs_module
from coliday.prefs import Prefs, load, save, update


@pytest.mark.parametrize(
    "contents",
    [
        "{깨짐",
        json.dumps({"group_by": "날짜", "collapsed": []}),
        json.dumps({"group_by": "status", "collapsed": "job:1"}),
        json.dumps({"group_by": "status", "collapsed": ["job:1", 2]}),
    ],
)
def test_깨졌거나_모르는_설정은_기본값(tmp_path, contents):
    path = tmp_path / "ui.json"
    path.write_text(contents, encoding="utf-8")
    assert load(path) == Prefs()


def test_파일이_없으면_기본값(tmp_path):
    assert load(tmp_path / "missing.json") == Prefs()


def test_부모_폴더를_만들어_저장하고_그대로_읽는다(tmp_path):
    path = tmp_path / "없는" / "폴더" / "ui.json"
    prefs = Prefs(group_by="status", collapsed=frozenset({"job:12", "group:3"}))

    save(path, prefs)

    assert load(path) == prefs
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "group_by": "status",
        "collapsed": ["group:3", "job:12"],
    }


def test_저장한_뒤_임시_파일이_남지_않는다(tmp_path):
    path = tmp_path / "ui.json"
    save(path, Prefs(group_by="status", collapsed=frozenset({"status:failed"})))
    assert list(tmp_path.iterdir()) == [path]


def test_update는_다른_화면이_먼저_저장한_묶기_기준을_보존한다(tmp_path):
    path = tmp_path / "ui.json"
    # A·B 둘 다 기본값으로 열었다. A 가 묶기 기준을 바꾸고, B 는 그 뒤 작업 하나를 접는다.
    a = update(path, group_by="status")
    b = update(path, add={"job:3"})

    assert a == Prefs("status")
    assert b == load(path) == Prefs("status", frozenset({"job:3"}))


def test_update는_다른_화면이_먼저_저장한_접힘을_보존한다(tmp_path):
    path = tmp_path / "ui.json"
    save(path, Prefs("status", frozenset({"group:1", "job:2"})))   # 다른 화면이 저장해 둔 것

    assert update(path, group_by="none") == Prefs("none", frozenset({"group:1", "job:2"}))
    assert update(path, add={"job:9"}, remove={"job:2"}) == Prefs("none", frozenset({"group:1", "job:9"}))
    assert update(path, add={"job:5"}, remove={"job:5"}) == Prefs("none", frozenset({"group:1", "job:9"}))
    assert load(path) == Prefs("none", frozenset({"group:1", "job:9"}))


def test_update는_파일이_없거나_깨졌으면_기본값에_적용하고_모르는_기준은_거부한다(tmp_path):
    path = tmp_path / "없는" / "ui.json"
    assert update(path, add={"job:1"}) == Prefs("none", frozenset({"job:1"}))
    path.write_text("{깨짐", encoding="utf-8")
    assert update(path, group_by="status") == Prefs("status")
    with pytest.raises(ValueError):
        update(path, group_by="날짜")
    assert load(path) == Prefs("status")


def test_없어진_프로젝트_묶기로_저장된_파일은_없음으로_읽고_접힘은_남긴다(tmp_path):
    path = tmp_path / "ui.json"
    path.write_text(json.dumps({"group_by": "project", "collapsed": ["status:failed", "job:3"]}), encoding="utf-8")
    assert load(path) == Prefs("none", frozenset({"status:failed", "job:3"}))
    with pytest.raises(ValueError):
        update(path, group_by="project")
    assert update(path, add={"job:4"}) == Prefs("none", frozenset({"status:failed", "job:3", "job:4"}))
    assert json.loads(path.read_text(encoding="utf-8"))["group_by"] == "none"


def test_여러_스레드가_동시에_update해도_모든_변경이_남는다(tmp_path):
    path = tmp_path / "ui.json"
    workers = 8
    for round_ in range(20):  # 경합은 매번 나지 않는다: 여러 번 되풀이한다
        barrier = threading.Barrier(workers)

        def change(i: int) -> None:
            barrier.wait()  # 모두 같은 기본 설정을 읽게 동시에 시작한다
            if i == 0:
                update(path, group_by="status" if round_ % 2 == 0 else "none")
            else:
                update(path, add={f"job:{round_}-{i}"})

        threads = [threading.Thread(target=change, args=(i,)) for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        prefs = load(path)
        assert prefs.group_by == ("status" if round_ % 2 == 0 else "none")
        assert {f"job:{round_}-{i}" for i in range(1, workers)} <= prefs.collapsed
    assert len(load(path).collapsed) == 20 * (workers - 1)


def _update_in_process(path: str, key: str, barrier) -> None:
    barrier.wait()
    for n in range(10):
        update(path, add={f"{key}:{n}"})


def test_여러_프로세스가_동시에_update해도_모든_변경이_남는다(tmp_path):
    path = tmp_path / "ui.json"
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(4)
    procs = [context.Process(target=_update_in_process, args=(str(path), f"p{i}", barrier)) for i in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
        assert p.exitcode == 0
    assert load(path).collapsed == {f"p{i}:{n}" for i in range(4) for n in range(10)}


def test_잠금_파일을_만들_수_없어도_update는_예전처럼_저장한다(tmp_path, monkeypatch):
    path = tmp_path / "ui.json"
    real_open = os.open

    def deny(file, *args, **kwargs):
        if str(file).endswith(".lock"):
            raise PermissionError(13, "Permission denied", str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(prefs_module.os, "open", deny)
    assert update(path, add={"job:1"}) == load(path) == Prefs("none", frozenset({"job:1"}))


def test_update는_잠금_파일_디스크립터를_남기지_않는다(tmp_path):
    path = tmp_path / "ui.json"
    fd_dir = Path("/dev/fd")
    before = len(list(fd_dir.iterdir()))
    for n in range(50):
        update(path, add={f"job:{n}"})
    assert len(list(fd_dir.iterdir())) <= before
