from __future__ import annotations

import random

import pytest

from coliday.joblist import (
    Header,
    JobGroup,
    JobLine,
    JobMeta,
    build,
    row_key,
    section_of,
    subtree,
    toggle,
    tree_key,
)
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


def _ids(lines):
    return [line.job.id for line in lines if isinstance(line, JobLine)]


def _headers(lines):
    return [line for line in lines if isinstance(line, Header)]


def test_구획_우선순위와_빈_구획_표시():
    jobs = [
        _job(1, status="failed", asked=10),
        _job(2, status="running", asked=20),
        _job(3, status="succeeded", asked=30),
        _job(4, status="queued", asked=40),
    ]
    groups = [JobGroup(7, "첫 그룹", 1), JobGroup(8, "빈 그룹", 2)]
    meta = {1: JobMeta(group_id=7), 2: JobMeta(group_id=7, pinned=True), 3: JobMeta(pinned=True)}

    lines = build(jobs, meta, groups, group_by="status", collapsed=set())

    headers = _headers(lines)
    assert [(h.key, h.count, h.job_ids) for h in headers] == [
        ("pinned", 2, (3, 2)),
        ("group:7", 1, (1,)),
        ("group:8", 0, ()),
        ("status:queued", 1, (4,)),
    ]
    assert _ids(lines) == [3, 2, 1, 4]


def test_프로젝트_기준은_없다():
    # 프로젝트별로는 화면이 목록 자체를 거른다. 예전 기준 값은 받지 않는다
    with pytest.raises(ValueError):
        build([_job(1)], {}, [], group_by="project", collapsed=set())


def test_접힌_개수와_job_ids는_전체이고_reveal은_입력을_바꾸지_않는다():
    jobs = [_job(1), _job(2), _job(3)]
    meta = {1: JobMeta(), 2: JobMeta(parent_id=1), 3: JobMeta(parent_id=2)}
    collapsed = frozenset({"status:queued", "job:1", "job:2"})

    hidden = build(jobs, meta, [], group_by="status", collapsed=collapsed)
    assert hidden == [Header("status:queued", "대기", 3, True, (1, 2, 3))]

    shown = build(jobs, meta, [], group_by="status", collapsed=collapsed, reveal=3)
    assert collapsed == frozenset({"status:queued", "job:1", "job:2"})
    assert _ids(shown) == [1, 2, 3]
    assert [line.depth for line in shown if isinstance(line, JobLine)] == [0, 1, 2]
    assert [line.collapsed for line in shown if isinstance(line, JobLine)] == [False, False, False]


def test_트리_접기_child_count와_하위_트리_최신순():
    jobs = [_job(1, asked=1), _job(2, asked=2), _job(3, asked=30), _job(4, asked=40)]
    meta = {
        1: JobMeta(),
        2: JobMeta(parent_id=1),
        3: JobMeta(parent_id=2),
        4: JobMeta(),
    }
    lines = build(jobs, meta, [], group_by="none", collapsed={"job:1"})
    job_lines = [line for line in lines if isinstance(line, JobLine)]
    assert [(line.job.id, line.depth, line.child_count, line.collapsed) for line in job_lines] == [
        (4, 0, 0, False),
        (1, 0, 2, True),
    ]


def test_다른_구획이나_목록_밖의_부모는_detached_root다():
    jobs = [
        _job(1, status="running"),
        _job(2, status="failed"),
        _job(3, status="queued"),
        _job(4, status="queued"),
        _job(6, status="queued"),
    ]
    meta = {
        1: JobMeta(),
        2: JobMeta(parent_id=1),
        3: JobMeta(parent_id=99),
        4: JobMeta(parent_id=6),
    }
    lines = build(jobs, meta, [], group_by="status", collapsed=set())
    details = {line.job.id: line for line in lines if isinstance(line, JobLine)}
    assert (details[2].depth, details[2].detached_parent) == (0, 1)
    assert (details[3].depth, details[3].detached_parent) == (0, 99)
    # 부모 #6 은 meta 에 줄이 없지만 같은 구획에 있으므로 그 밑에 들어간다
    assert (details[4].depth, details[4].detached_parent) == (1, None)
    assert (details[6].depth, details[6].child_count) == (0, 1)


def test_meta에_부모_줄이_없어도_목록에_있으면_트리가_만들어진다():
    # store.job_meta() 처럼 파생·그룹·고정 기록이 있는 작업(자식)만 담은 meta 를 그대로 넘긴다
    jobs = [_job(1, asked=1), _job(2, asked=2), _job(3, asked=3)]
    meta = {2: JobMeta(parent_id=1), 3: JobMeta(parent_id=2)}

    for group_by in ("none", "status"):
        lines = build(jobs, meta, [], group_by=group_by, collapsed=set())
        job_lines = [line for line in lines if isinstance(line, JobLine)]
        assert [(line.job.id, line.depth, line.child_count, line.detached_parent) for line in job_lines] == [
            (1, 0, 2, None),
            (2, 1, 1, None),
            (3, 2, 0, None),
        ]

    collapsed = build(jobs, meta, [], group_by="none", collapsed={"job:1"})
    assert _ids(collapsed) == [1] and collapsed[0].collapsed
    assert subtree(1, meta) == {1, 2, 3}


def test_none에_장식이_없으면_입력_순서를_그대로_둔다():
    jobs = [_job(3), _job(1), _job(2)]
    lines = build(jobs, {}, [], group_by="none", collapsed={"job:1"})
    assert _ids(lines) == [3, 1, 2]
    assert _headers(lines) == []


def test_사이클도_끝나고_모든_작업이_한번씩_나온다():
    jobs = [_job(1), _job(2), _job(3)]
    meta = {1: JobMeta(parent_id=2), 2: JobMeta(parent_id=3), 3: JobMeta(parent_id=1)}
    lines = build(jobs, meta, [], group_by="status", collapsed=set())
    assert sorted(_ids(lines)) == [1, 2, 3]
    assert len(_ids(lines)) == len(set(_ids(lines)))


def test_무작위_입력에서도_작업과_row_key가_정확히_한번씩이다():
    for seed in range(30):
        rng = random.Random(seed)
        jobs = [_job(i, status=rng.choice(["queued", "running", "failed"]), asked=rng.random()) for i in range(1, 31)]
        rng.shuffle(jobs)
        meta = {
            i: JobMeta(
                parent_id=rng.choice([None, *range(1, 31)]),
                group_id=rng.choice([None, 1, 2, 99]),
                pinned=rng.random() < 0.15,
            )
            for i in range(1, 31)
        }
        lines = build(
            jobs, meta, [JobGroup(1, "가", 1), JobGroup(2, "나", 2)],
            group_by=rng.choice(["none", "status"]), collapsed=set(),
        )
        assert sorted(_ids(lines)) == list(range(1, 31))
        keys = [row_key(line) for line in lines]
        assert len(keys) == len(set(keys))


def test_subtree_toggle_tree_key_section_of():
    meta = {
        1: JobMeta(),
        2: JobMeta(parent_id=1),
        3: JobMeta(parent_id=2),
        4: JobMeta(parent_id=9),
    }
    assert subtree(1, meta) == {1, 2, 3}
    assert subtree(9, meta) == {9, 4}
    assert tree_key(12) == "job:12"
    assert toggle(frozenset({"a"}), "a") == frozenset()
    assert toggle(frozenset(), "a") == frozenset({"a"})

    lines = build([_job(1), _job(2)], meta, [], group_by="status", collapsed={"status:queued"})
    assert section_of(lines, 2) == "status:queued"
    assert section_of(lines, 99) is None
