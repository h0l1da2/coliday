"""작업 목록 화면 설정을 읽고 저장한다."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from .joblist import GroupBy


@dataclass(frozen=True)
class Prefs:
    group_by: GroupBy = "none"
    collapsed: frozenset[str] = frozenset()


def load(path: str | os.PathLike[str]) -> Prefs:
    """설정이 없거나 올바르지 않으면 기본값을 돌려준다."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        group_by = value.get("group_by", "none")
        collapsed = value.get("collapsed", [])
        if group_by == "project":  # 프로젝트별 묶기는 없어졌다. 접어 둔 곳은 그대로 살린다
            group_by = "none"
        if group_by not in ("none", "status") or not isinstance(collapsed, list):
            return Prefs()
        if not all(isinstance(key, str) for key in collapsed):
            return Prefs()
        return Prefs(group_by=group_by, collapsed=frozenset(collapsed))
    except (OSError, ValueError, TypeError, AttributeError):
        return Prefs()


def update(
    path: str | os.PathLike[str],
    *,
    group_by: GroupBy | None = None,
    add: Iterable[str] = (),
    remove: Iterable[str] = (),
) -> Prefs:
    """파일의 지금 내용에 이번에 바꾼 것만 적용해 저장하고, 저장한 설정을 돌려준다.

    여러 화면이 같은 파일을 쓰므로 들고 있던 설정 전체를 쓰면 다른 화면의 변경을 되돌린다.
    읽기부터 바꾸기까지 `ui.json.lock` 을 잠가 동시에 저장해도 서로의 변경을 지우지 않는다.
    """
    if group_by is not None and group_by not in ("none", "status"):
        raise ValueError(group_by)
    remove = frozenset(remove)
    with _locked(Path(path)):
        current = load(path)
        new = Prefs(
            group_by=current.group_by if group_by is None else group_by,
            collapsed=(current.collapsed - remove) | (frozenset(add) - remove),
        )
        save(path, new)
    return new


@contextlib.contextmanager
def _locked(target: Path) -> Iterator[None]:
    """다른 프로세스·스레드와 겹치지 않게 잠근다. 잠금 파일을 만들 수 없으면(읽기 전용 폴더 등) 잠그지 않고 진행한다."""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target.with_name(target.name + ".lock"), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError:
        yield
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)  # 같은 프로세스의 다른 스레드도 열린 파일이 다르면 서로 기다린다
        except OSError:
            pass  # 잠금을 지원하지 않는 파일 시스템: 잠그지 않고 진행한다
        yield
    finally:
        os.close(fd)  # 닫으면 잠금도 풀린다


def save(path: str | os.PathLike[str], prefs: Prefs) -> None:
    """같은 폴더의 임시 파일을 완성한 뒤 원자적으로 바꾼다."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent, prefix=f".{target.name}.", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(
                {"group_by": prefs.group_by, "collapsed": sorted(prefs.collapsed)},
                stream,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
