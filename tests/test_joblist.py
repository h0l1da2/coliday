from __future__ import annotations

import random

from coliday.joblist import JobGroup, JobMeta, bundle, ordered
from coliday.store import Job


def _job(
    job_id: int,
    *,
    status: str = "queued",
    repo: str = "/work/shop",
    asked: float | None = None,
) -> Job:
    return Job(
        id=job_id,
        prompt=f"작업 {job_id}",
        repo=repo,
        workdir=f"/work/job-{job_id}",
        branch=None,
        base_commit=None,
        status=status,
        phase="",
        pid=None,
        session_id=None,
        model=None,
        result=None,
        cost_usd=None,
        created_at=float(job_id),
        started_at=None,
        finished_at=None,
        asked_at=asked,
    )


def test_ordered는_고정한_작업을_맨_위로_올리고_각각의_입력_순서를_유지한다():
    jobs = [_job(5), _job(4), _job(3), _job(2), _job(1)]
    meta = {2: JobMeta(pinned=True), 4: JobMeta(pinned=True), 3: JobMeta(group_id=7)}
    assert [job.id for job in ordered(jobs, meta)] == [4, 2, 5, 3, 1]


def test_ordered는_meta에_없는_작업을_고정으로_보지_않는다():
    jobs = [_job(3), _job(2), _job(1)]
    assert [job.id for job in ordered(jobs, {})] == [3, 2, 1]
    assert [job.id for job in ordered(jobs, {9: JobMeta(pinned=True)})] == [3, 2, 1]


def test_ordered는_무작위_입력에서도_모든_작업이_한번씩_나온다():
    rng = random.Random(7)
    for _ in range(50):
        ids = rng.sample(range(1, 40), rng.randint(0, 20))
        jobs = [_job(job_id) for job_id in ids]
        meta = {job_id: JobMeta(pinned=rng.random() < 0.3) for job_id in rng.sample(range(1, 40), 15)}
        result = [job.id for job in ordered(jobs, meta)]
        assert sorted(result) == sorted(ids)
        flags = [meta.get(job_id, JobMeta()).pinned for job_id in result]
        assert flags == sorted(flags, reverse=True)       # 고정이 모두 앞에 온다


def test_bundle은_그룹_구성원에_자신을_넣고_번호_순으로_둔다():
    group = JobGroup(7, "결제", 1.0)
    meta = {9: JobMeta(group_id=7), 3: JobMeta(group_id=7), 5: JobMeta(group_id=7, pinned=True), 4: JobMeta(group_id=8)}
    b = bundle(5, meta, [group])
    assert b.group == group and b.members == (3, 5, 9) and b.pinned
    assert b.parent is None and b.children == () and not b.empty


def test_bundle은_보이지_않는_그룹이면_그룹이_없다():
    meta = {1: JobMeta(group_id=8), 2: JobMeta(group_id=8)}
    b = bundle(1, meta, [JobGroup(7, "결제", 1.0)])
    assert b.group is None and b.members == () and b.empty


def test_bundle은_부모와_자식을_찾고_자기_참조는_뺀다():
    meta = {2: JobMeta(parent_id=1), 3: JobMeta(parent_id=2), 4: JobMeta(parent_id=2), 5: JobMeta(parent_id=3)}
    b = bundle(2, meta, [])
    assert b.parent == 1 and b.children == (3, 4) and not b.empty
    broken = bundle(6, {6: JobMeta(parent_id=6)}, [])
    assert broken.parent is None and broken.children == () and broken.empty


def test_bundle은_아무것도_없으면_비고_고정만으로는_묶음이_아니다():
    assert bundle(1, {}, []).empty
    pinned = bundle(1, {1: JobMeta(pinned=True)}, [])
    assert pinned.empty and pinned.pinned
