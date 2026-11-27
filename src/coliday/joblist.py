"""작업 목록의 고정 순서와 작업 하나의 묶음(그룹·부모·자식)을 계산한다 (화면 없이 순수 계산)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping, Sequence

if TYPE_CHECKING:
    from .store import Job


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


def ordered(jobs: Sequence[Job], meta: Mapping[int, JobMeta]) -> list[Job]:
    """고정한 작업을 맨 위로 (고정끼리·나머지끼리는 입력 순서 유지). meta 에 없으면 고정 아님."""
    rows = list(jobs)
    pinned = [job for job in rows if meta.get(job.id, JobMeta()).pinned]
    rest = [job for job in rows if not meta.get(job.id, JobMeta()).pinned]
    return pinned + rest


@dataclass(frozen=True)
class Bundle:
    """한 작업과 묶인 것: 같은 그룹 작업, 부모, 바로 아래 자식."""

    job_id: int
    pinned: bool
    group: JobGroup | None          # groups(보이는 그룹)에 있는 그룹만. 아니면 None
    members: tuple[int, ...]        # 같은 그룹 작업 번호 (자신 포함, 번호 오름차순). group 이 None 이면 ()
    parent: int | None
    children: tuple[int, ...]       # parent_id == job_id 인 작업 (번호 오름차순, 자신 제외)

    @property
    def empty(self) -> bool:
        """그룹도 부모도 자식도 없다 (고정만은 묶음이 아니다)."""
        return self.group is None and self.parent is None and not self.children


def bundle(job_id: int, meta: Mapping[int, JobMeta], groups: Sequence[JobGroup]) -> Bundle:
    """job_id 의 묶음. meta 에 없는 작업은 JobMeta() 로 본다."""
    own = meta.get(job_id, JobMeta())
    group = next((item for item in groups if item.id == own.group_id), None) if own.group_id is not None else None
    members: tuple[int, ...] = ()
    if group is not None:
        found = {other for other, item in meta.items() if item.group_id == group.id}
        found.add(job_id)
        members = tuple(sorted(found))
    parent = own.parent_id if own.parent_id != job_id else None
    children = tuple(sorted(other for other, item in meta.items() if item.parent_id == job_id and other != job_id))
    return Bundle(job_id, own.pinned, group, members, parent, children)
