"""작업 목록을 구획과 트리로 정리한다."""

from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict
from typing import TYPE_CHECKING, AbstractSet, Literal, Mapping, Sequence, TypeAlias

if TYPE_CHECKING:
    from .store import Job


GroupBy = Literal["none", "status"]  # 프로젝트별로는 목록 자체를 거른다 (tui 가 보는 프로젝트의 작업만 넘긴다)
STATUS_ORDER = ("running", "queued", "failed", "succeeded", "cancelled")


@dataclass(frozen=True)
class JobMeta:
    parent_id: int | None = None
    group_id: int | None = None
    pinned: bool = False


@dataclass(frozen=True)
class JobGroup:
    id: int
    name: str
    created_at: float


@dataclass(frozen=True)
class Header:
    key: str
    label: str
    count: int
    collapsed: bool
    job_ids: tuple[int, ...]


@dataclass(frozen=True)
class JobLine:
    job: Job
    depth: int
    section: str
    child_count: int
    collapsed: bool
    detached_parent: int | None


Line: TypeAlias = Header | JobLine

_STATUS_LABELS = {
    "running": "실행 중",
    "queued": "대기",
    "failed": "실패",
    "succeeded": "완료",
    "cancelled": "취소",
}


def build(
    jobs: Sequence[Job],
    meta: Mapping[int, JobMeta],
    groups: Sequence[JobGroup],
    *,
    group_by: GroupBy,
    collapsed: AbstractSet[str],
    reveal: int | None = None,
) -> list[Line]:
    """작업을 우선순위에 따라 구획으로 나누고 구획 안에서 트리로 만든다.

    meta 에 없는 작업은 JobMeta() (부모·그룹·고정 없음)로 본다. store.job_meta() 를 그대로 넘기면 된다.
    """
    if group_by not in ("none", "status"):
        raise ValueError(group_by)

    rows = list(jobs)
    if not groups and group_by == "none" and not any(
        (item := meta.get(job.id, JobMeta())).pinned or item.group_id is not None or item.parent_id is not None
        for job in rows
    ):
        return [JobLine(job, 0, "", 0, False, None) for job in rows]

    sections: list[tuple[str, str, list[Job]]] = []
    taken: set[int] = set()

    pinned = [job for job in rows if meta.get(job.id, JobMeta()).pinned]
    if pinned:
        sections.append(("pinned", "📌 고정", pinned))
        taken.update(job.id for job in pinned)

    for group in groups:
        members = [
            job for job in rows
            if job.id not in taken and meta.get(job.id, JobMeta()).group_id == group.id
        ]
        sections.append((f"group:{group.id}", group.name, members))
        taken.update(job.id for job in members)

    remaining = [job for job in rows if job.id not in taken]
    if group_by == "none":
        sections.append(("", "", remaining))
    else:
        by_status: dict[str, list[Job]] = defaultdict(list)
        for job in remaining:
            by_status[job.status].append(job)
        statuses = [*STATUS_ORDER, *(status for status in by_status if status not in STATUS_ORDER)]
        sections.extend(
            (f"status:{status}", _STATUS_LABELS.get(status, status), by_status[status])
            for status in statuses if by_status[status]
        )

    result: list[Line] = []
    for key, label, members in sections:
        member_lines = _tree_lines(members, meta, key, collapsed, reveal)
        job_ids = tuple(line.job.id for line in _tree_lines(members, meta, key, frozenset(), None))
        if key:
            is_collapsed = key in collapsed and reveal not in job_ids
            result.append(Header(key, label, len(members), is_collapsed, job_ids))
            if is_collapsed:
                continue
        result.extend(member_lines)
    return result


def row_key(line: Line) -> str:
    """목록 행의 겹치지 않는 키."""
    return f"h:{line.key}" if isinstance(line, Header) else str(line.job.id)


def section_of(lines: Sequence[Line], job_id: int) -> str | None:
    """작업이 속한 구획 키. 접힌 구획도 찾는다."""
    for line in lines:
        if isinstance(line, JobLine) and line.job.id == job_id:
            return line.section
    for line in lines:
        if isinstance(line, Header) and job_id in line.job_ids:
            return line.key
    return None


def subtree(job_id: int, meta: Mapping[int, JobMeta]) -> set[int]:
    """자신과 모든 하위 작업 번호. 잘못된 순환도 한 번씩만 센다."""
    children: dict[int, list[int]] = defaultdict(list)
    for child_id, item in meta.items():
        if item.parent_id is not None:
            children[item.parent_id].append(child_id)
    found: set[int] = set()
    pending = [job_id]
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        pending.extend(children.get(current, ()))
    return found


def tree_key(job_id: int) -> str:
    return f"job:{job_id}"


def toggle(collapsed: frozenset[str], key: str) -> frozenset[str]:
    """접힘 키 하나를 뒤집은 새 집합."""
    return collapsed - {key} if key in collapsed else collapsed | {key}


def _tree_lines(
    jobs: Sequence[Job],
    meta: Mapping[int, JobMeta],
    section: str,
    collapsed: AbstractSet[str],
    reveal: int | None,
) -> list[JobLine]:
    by_id = {job.id: job for job in jobs}
    parent: dict[int, int] = {}
    detached: dict[int, int] = {}
    for job in jobs:
        parent_id = meta.get(job.id, JobMeta()).parent_id
        if parent_id is None:
            continue
        # 부모는 같은 구획(jobs)에 있으면 인정한다. meta 에는 기록이 있는 작업만 있으므로 meta 에 있는지는 보지 않는다.
        if parent_id in by_id:
            parent[job.id] = parent_id
        else:
            detached[job.id] = parent_id

    children: dict[int, list[int]] = defaultdict(list)
    for child_id, parent_id in parent.items():
        children[parent_id].append(child_id)

    descendants: dict[int, set[int]] = {}

    def below(job_id: int) -> set[int]:
        if job_id not in descendants:
            found: set[int] = set()
            pending = list(children.get(job_id, ()))
            while pending:
                child_id = pending.pop()
                if child_id == job_id or child_id in found:
                    continue
                found.add(child_id)
                pending.extend(children.get(child_id, ()))
            descendants[job_id] = found
        return descendants[job_id]

    def order_key(job_id: int) -> tuple[float, int]:
        ids = below(job_id) | {job_id}
        return max((by_id[item].last_asked, item) for item in ids)

    for child_ids in children.values():
        child_ids.sort(key=order_key, reverse=True)

    roots = [job.id for job in jobs if job.id not in parent]
    roots.sort(key=order_key, reverse=True)

    reveal_ancestors: set[int] = set()
    current = reveal
    seen_chain: set[int] = set()
    while current is not None and current not in seen_chain:
        seen_chain.add(current)
        parent_id = parent.get(current)
        if parent_id is None:
            break
        reveal_ancestors.add(parent_id)
        current = parent_id

    lines: list[JobLine] = []
    visited: set[int] = set()

    def emit(job_id: int, depth: int) -> None:
        if job_id in visited:
            return
        visited.add(job_id)
        child_ids = children.get(job_id, ())
        is_collapsed = bool(child_ids) and tree_key(job_id) in collapsed and job_id not in reveal_ancestors
        lines.append(JobLine(by_id[job_id], depth, section, len(below(job_id)), is_collapsed, detached.get(job_id)))
        if not is_collapsed:
            for child_id in child_ids:
                emit(child_id, depth + 1)
        else:
            visited.update(below(job_id))

    for root_id in roots:
        emit(root_id, 0)
    for job_id in sorted(by_id, key=order_key, reverse=True):
        emit(job_id, 0)
    return lines
