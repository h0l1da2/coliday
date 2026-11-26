"""작업 묶기의 데이터와 실행: job_meta·job_groups 표, 파생 작업(스냅샷에서 시작), 첫 실행 안내문."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coliday import cli, config, gitops, jobs, remote, worker
from coliday.joblist import JobMeta
from coliday.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
SRC = Path(__file__).parents[1] / "src"

# 이번 변경 전의 jobs 표 열. 이 목록이 바뀌면 실행 중인 이전 버전 worker/TUI 가 `Job(**row)` 에서 죽는다.
JOBS_COLUMNS = [
    "id", "prompt", "repo", "workdir", "branch", "base_commit", "status", "phase", "pid", "session_id", "model",
    "result", "cost_usd", "created_at", "started_at", "finished_at",
]

# job_meta 가 생기기 전 버전의 스키마
_OLD_SCHEMA = """
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, prompt TEXT NOT NULL, repo TEXT NOT NULL, workdir TEXT NOT NULL,
    branch TEXT, base_commit TEXT, status TEXT NOT NULL, phase TEXT NOT NULL DEFAULT '', pid INTEGER,
    session_id TEXT, model TEXT, result TEXT, cost_usd REAL, created_at REAL NOT NULL, started_at REAL, finished_at REAL
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL REFERENCES jobs(id), ts REAL NOT NULL,
    actor TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL
);
CREATE INDEX idx_events_job ON events(job_id, id);
CREATE TABLE job_options (job_id INTEGER PRIMARY KEY REFERENCES jobs(id), allow_env INTEGER NOT NULL DEFAULT 0);
"""


P = "/r"  # 그룹이 속한 프로젝트


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "jobs.db")
    yield s
    s.close()


def _job(store: Store, parent_id: int | None = None, status: str = "succeeded") -> int:
    job = store.create_job(prompt="p", repo="/r", workdir="/r", model=None, parent_id=parent_id)
    if status != "queued":
        store.finish_job(job.id, status)
    return job.id


# ── 스키마 ────────────────────────────────────────────────────────────────


def test_옛_스키마_DB를_열면_새_표가_생기고_기존_데이터는_그대로다(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO jobs(prompt, repo, workdir, status, created_at) VALUES ('옛 작업', '/r', '/r', 'succeeded', 1.0)"
    )
    conn.execute("INSERT INTO events(job_id, ts, actor, kind, message) VALUES (1, 1.0, '팀장', 'text', '안녕')")
    conn.commit()
    conn.close()

    s = Store(path)
    tables = {r[0] for r in s._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"job_meta", "job_groups"} <= tables
    job = s.get_job(1)
    assert (job.prompt, job.status) == ("옛 작업", "succeeded")
    assert [e.message for e in s.events(1)] == ["안녕"]
    assert s.job_meta() == {} and s.children(1) == [] and s.list_groups() == []
    s.close()
    Store(path).close()  # 두 번 열어도 된다


def test_jobs_표의_열은_늘리지_않는다(store):
    assert [r["name"] for r in store._conn.execute("PRAGMA table_info(jobs)")] == JOBS_COLUMNS


# ── 파생 관계 ────────────────────────────────────────────────────────────


def test_파생_작업을_만들면_부모와_자식이_기록된다(store):
    a = _job(store)
    b = _job(store, parent_id=a)
    c = _job(store, parent_id=a)

    assert store.children(a) == [b, c]
    assert store.job_meta()[b] == JobMeta(parent_id=a)
    assert a not in store.job_meta()  # 파생·그룹·고정이 없는 작업은 기록이 없다


def test_지우면_자식은_할아버지_밑으로_없으면_최상위로(store):
    a = _job(store)
    b = _job(store, parent_id=a)
    c = _job(store, parent_id=b)
    d = _job(store, parent_id=b)

    assert store.delete_job(b)
    meta = store.job_meta()
    assert meta[c].parent_id == a and meta[d].parent_id == a and b not in meta
    assert store.children(a) == [c, d]

    assert store.delete_job(a)
    meta = store.job_meta()
    assert meta[c].parent_id is None and meta[d].parent_id is None


@pytest.mark.parametrize("order", [(0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0)])
def test_3단_사슬을_어떤_순서로_지워도_남은_작업의_부모는_남은_조상이다(store, order):
    chain = [_job(store)]
    for _ in range(2):
        chain.append(_job(store, parent_id=chain[-1]))
    left = list(chain)
    for index in order:
        assert store.delete_job(chain[index])
        left.remove(chain[index])
        meta = store.job_meta()
        for position, job_id in enumerate(left):
            expected = left[position - 1] if position else None
            assert meta.get(job_id, JobMeta()).parent_id == expected
    assert store.job_meta() == {}


def test_출처는_만들_때_기록되고_부모를_지워도_바뀌지_않는다(store):
    a = _job(store)
    b = _job(store, parent_id=a)
    c = _job(store, parent_id=b)
    store.set_pinned([a], True)  # 고정·그룹만 있는 작업은 출처가 없다

    assert (store.origin(a), store.origin(b), store.origin(c)) == (None, a, b)
    assert store.delete_job(b)
    assert store.job_meta()[c].parent_id == a and store.origin(c) == b
    assert store.origin(999) is None


def test_출처_열이_없는_개발_중_job_meta에도_열을_더한다(tmp_path):
    path = tmp_path / "dev.db"
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_SCHEMA)
    conn.execute("CREATE TABLE job_meta (job_id INTEGER PRIMARY KEY, parent_id INTEGER, group_id INTEGER, pinned_at REAL)")
    conn.execute("INSERT INTO jobs(prompt, repo, workdir, status, created_at) VALUES ('p', '/r', '/r', 'succeeded', 1)")
    conn.execute("INSERT INTO job_meta(job_id, parent_id) VALUES (1, 7)")
    conn.commit()
    conn.close()

    s = Store(path)
    assert s.origin(1) is None and s.job_meta()[1].parent_id is None   # 옛 행은 출처를 모른다 (안내 생략)
    child = s.create_job(prompt="c", repo="/r", workdir="/r", model=None, parent_id=1)
    assert s.origin(child.id) == 1
    s.close()
    Store(path).close()  # 두 번 열어도 된다


def test_실행_중이라_지우지_않은_작업의_자식은_그대로다(store):
    a = _job(store, status="queued")
    b = _job(store, parent_id=a)

    assert not store.delete_job(a)
    assert store.job_meta()[b].parent_id == a


def test_부모나_그룹이_이전_버전에서_지워졌으면_없는_것으로_본다(store):
    a = _job(store)
    b = _job(store, parent_id=a)
    group = store.create_group("배포", project=P)
    store.set_group([b], group.id)
    # job_meta 를 모르는 이전 버전이 지운 것처럼 jobs·job_groups 에서만 지운다
    store._conn.execute("DELETE FROM jobs WHERE id = ?", (a,))
    store._conn.execute("DELETE FROM job_groups WHERE id = ?", (group.id,))

    assert store.job_meta()[b] == JobMeta(parent_id=None, group_id=None)
    assert store.children(a) == [b]  # 기록은 남아 있다 (목록에서는 부모 없이 보인다)


def test_지워진_작업의_기록은_job_meta에_나오지_않는다(store):
    a = _job(store)
    store.set_pinned([a], True)
    store._conn.execute("DELETE FROM jobs WHERE id = ?", (a,))  # 이전 버전이 지움

    assert store.job_meta() == {}


def test_부모_기록이_실패하면_작업도_만들지_않는다(store, monkeypatch):
    real = store._conn

    class Failing:
        def execute(self, sql, *args):
            if sql.startswith("INSERT INTO job_meta"):
                raise sqlite3.OperationalError("디스크 오류")
            return real.execute(sql, *args)

    monkeypatch.setattr(store, "_conn", Failing())
    with pytest.raises(sqlite3.OperationalError):
        store.create_job(prompt="p", repo="/r", workdir="/r", model=None, parent_id=1)
    monkeypatch.undo()
    assert store.list_jobs() == []


# ── 그룹·고정 ─────────────────────────────────────────────────────────────


def test_그룹을_만들고_이름을_바꾸고_지운다(store):
    a, b, c = _job(store), _job(store), _job(store)
    first = store.create_group("  배포   준비 ", project=P)
    second = store.create_group("리뷰", project=P)
    assert first.name == "배포 준비"
    assert [g.name for g in store.list_groups()] == ["배포 준비", "리뷰"]  # 만든 순서

    store.set_group([a, b], first.id)
    store.set_group([c], second.id)
    meta = store.job_meta()
    assert (meta[a].group_id, meta[b].group_id, meta[c].group_id) == (first.id, first.id, second.id)

    store.set_group([b], None)
    assert store.job_meta()[b].group_id is None

    store.rename_group(first.id, "배포")
    assert [g.name for g in store.list_groups()] == ["배포", "리뷰"]

    assert store.delete_group(first.id) is True
    assert store.delete_group(first.id) is False
    assert [g.name for g in store.list_groups()] == ["리뷰"]
    assert store.job_meta()[a].group_id is None
    assert {j.id for j in store.list_jobs()} == {a, b, c}  # 작업은 남는다


def test_그룹_이름이_비었거나_겹치면_거부한다(store):
    group = store.create_group("배포", project=P)
    other = store.create_group("리뷰", project=P)
    for bad in ("", "   "):
        with pytest.raises(ValueError):
            store.create_group(bad, project=P)
        with pytest.raises(ValueError):
            store.rename_group(group.id, bad)
    with pytest.raises(ValueError):
        store.create_group(" 배포 ", project=P)
    with pytest.raises(ValueError):
        store.rename_group(other.id, "배포")
    with pytest.raises(ValueError):
        store.rename_group(999, "새 이름")
    assert [g.name for g in store.list_groups()] == ["배포", "리뷰"]


def test_없는_그룹에는_넣지_않는다(store):
    a = _job(store)
    with pytest.raises(ValueError):
        store.set_group([a], 42)
    assert store.job_meta() == {}


def test_jobs의_그룹_함수는_오류를_JobError로_알린다(store):
    group = jobs.create_group(store, "배포", P)
    with pytest.raises(jobs.JobError):
        jobs.create_group(store, "배포", P)
    with pytest.raises(jobs.JobError):
        jobs.rename_group(store, group.id, "")
    with pytest.raises(jobs.JobError):
        jobs.set_group(store, [1], 999)
    jobs.rename_group(store, group.id, "출시")
    jobs.delete_group(store, group.id)
    jobs.delete_group(store, group.id)  # 이미 없어도 오류가 아니다
    assert store.list_groups() == []


def test_고정과_해제는_여러_작업에_한_번에_적용된다(store):
    a, b, c = _job(store), _job(store), _job(store)
    store.set_pinned([a, b], True)
    pinned_at = store._conn.execute("SELECT pinned_at FROM job_meta WHERE job_id = ?", (a,)).fetchone()[0]
    store.set_pinned([a], True)  # 다시 고정해도 고정한 시각은 그대로
    assert store._conn.execute("SELECT pinned_at FROM job_meta WHERE job_id = ?", (a,)).fetchone()[0] == pinned_at
    meta = store.job_meta()
    assert meta[a].pinned and meta[b].pinned and c not in meta

    store.set_pinned([a, c], False)
    meta = store.job_meta()
    assert not meta[a].pinned and meta[b].pinned and not meta[c].pinned
    store.set_pinned([999], True)  # 없는 작업은 건너뛴다
    assert 999 not in store.job_meta()


def test_그룹과_고정과_부모는_서로_덮어쓰지_않는다(store):
    a = _job(store)
    b = _job(store, parent_id=a)
    group = store.create_group("g", project=P)
    store.set_group([b], group.id)
    store.set_pinned([b], True)
    assert store.job_meta()[b] == JobMeta(parent_id=a, group_id=group.id, pinned=True)


# ── 프로젝트별 보기: 레포로 거르기·실행 중 작업·프로젝트별 그룹 ─────────────────────


def test_list_jobs는_레포로_거르고_순서는_그대로다(store):
    a = store.create_job(prompt="a", repo="/r", workdir="/r", model=None).id
    b = store.create_job(prompt="b", repo="/x", workdir="/x", model=None).id
    c = store.create_job(prompt="c", repo="dev@h:/r", workdir="dev@h:/r", model=None).id
    d = store.create_job(prompt="d", repo="/r", workdir="/r", model=None).id
    store.add_event(a, "사용자", "prompt", "이어서")         # 이어서 시킨 작업은 맨 위로

    assert store.list_jobs(repos=[]) == []                  # IN () 문법 오류 없이 빈 목록
    assert [j.id for j in store.list_jobs(repos=["/r"])] == [a, d]
    assert [j.id for j in store.list_jobs(repos=("/r", "dev@h:/r"))] == [a, d, c]
    assert [j.id for j in store.list_jobs(1, repos=["/r", "/x"])] == [a]
    assert [j.id for j in store.list_jobs(repos=["/없음"])] == []
    assert [j.id for j in store.list_jobs()] == [a, d, c, b]   # repos 없이 부르면 전체 (CLI)
    assert sorted(store.repos()) == ["/r", "/x", "dev@h:/r"]


def test_active_jobs는_실행_중과_대기_중만(store):
    queued = _job(store, status="queued")
    running = _job(store, status="queued")
    assert store.start_job(running, pid=os.getpid())
    _job(store)
    _job(store, status="failed")
    assert [j.id for j in store.active_jobs()] == [queued, running]


def test_그룹_이름은_프로젝트_안에서만_겹치지_않으면_된다(store):
    a = store.create_group("결제", project="/r/a")
    b = store.create_group("결제", project="/r/b")         # 다른 프로젝트는 같은 이름 허용
    store.create_group("리뷰", project="/r/a")
    with pytest.raises(ValueError):
        store.create_group(" 결제", project="/r/a")
    with pytest.raises(ValueError):
        store.rename_group(a.id, "리뷰")                      # 같은 프로젝트의 이름으로는 못 바꾼다
    store.rename_group(b.id, "리뷰")                          # 다른 프로젝트의 이름은 된다
    with pytest.raises(ValueError):
        store.create_group("x", project="")
    assert [g.name for g in store.list_groups(project="/r/a")] == ["결제", "리뷰"]
    assert [(g.id, g.name) for g in store.list_groups(project="/r/b")] == [(b.id, "리뷰")]
    assert store.list_groups(project="/없음") == []
    assert len(store.list_groups()) == 3                     # 프로젝트 없이 부르면 전체 (있는지 확인하는 용도)


def _old_groups_db(path):
    """project 열이 없던 개발 중의 job_groups (name 만 UNIQUE) 에 그룹·고정·파생이 든 DB."""
    s = Store(path)
    a = _job(s)
    b = _job(s, parent_id=a)
    s.set_pinned([b], True)
    s._conn.executescript(
        "DROP TABLE job_groups;"
        "CREATE TABLE job_groups (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, created_at REAL NOT NULL);"
        "INSERT INTO job_groups(name, created_at) VALUES ('옛 그룹', 1);"
    )
    s._conn.execute("UPDATE job_meta SET group_id = 1 WHERE job_id = ?", (b,))
    s.close()
    return a, b


def test_옛_형태의_그룹_표는_다시_만들고_고정과_부모는_남긴다(tmp_path):
    path = tmp_path / "dev.db"
    a, b = _old_groups_db(path)

    s = Store(path)
    columns = [r["name"] for r in s._conn.execute("PRAGMA table_info(job_groups)")]
    assert "project" in columns and s.list_groups() == []
    row = s._conn.execute("SELECT group_id, parent_id, pinned_at FROM job_meta WHERE job_id = ?", (b,)).fetchone()
    assert row["group_id"] is None and row["parent_id"] == a and row["pinned_at"] is not None
    first = s.create_group("새 그룹", project="/r")
    s.set_group([b], first.id)
    s.close()

    again = Store(path)                                      # 다시 열어도 새로 만든 그룹은 그대로다
    assert [g.name for g in again.list_groups(project="/r")] == ["새 그룹"]
    assert again.job_meta()[b] == JobMeta(parent_id=a, group_id=first.id, pinned=True)
    again.close()


def test_옛_형태의_그룹_표를_두_프로세스가_함께_열어도_한_번만_바꾼다(tmp_path):
    path = tmp_path / "dev.db"
    _old_groups_db(path)
    script = (
        "import sys; from pathlib import Path; from coliday.store import Store; "
        "s = Store(Path(sys.argv[1])); s.close()"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    procs = [subprocess.Popen([sys.executable, "-c", script, str(path)], env=env, stderr=subprocess.PIPE) for _ in range(4)]
    errors = [p.communicate(timeout=60)[1] for p in procs]
    assert all(p.returncode == 0 for p in procs), errors
    s = Store(path)
    assert "project" in {r["name"] for r in s._conn.execute("PRAGMA table_info(job_groups)")}
    s.close()


# ── 파생 작업 실행 ─────────────────────────────────────────────────────────


@pytest.fixture
def env(tmp_path, monkeypatch):
    tmp_path = tmp_path.resolve()
    for name in ("claude", "codex"):
        script = tmp_path / name
        script.write_text(f"#!/bin/sh\nexec {sys.executable} {FIXTURES / f'fake_{name}.py'} \"$@\"\n")
        script.chmod(0o755)
    monkeypatch.setenv("COLIDAY_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COLIDAY_CLAUDE_BIN", str(tmp_path / "claude"))
    monkeypatch.setenv("COLIDAY_CODEX_BIN", str(tmp_path / "codex"))
    monkeypatch.setenv("FAKE_CLAUDE_ARGS", str(tmp_path / "args.json"))
    monkeypatch.setenv("PYTHONPATH", str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return tmp_path


@pytest.fixture
def repo(env):
    r = env / "myrepo"
    (r / "sub").mkdir(parents=True)
    (r / "sub" / "a.txt").write_text("a\n")
    (r / "keep.txt").write_text("keep\n")
    (r / ".gitignore").write_text("*.log\n")
    _git(r, "init", "-q")
    _git(r, "add", ".")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return r


def _git(path, *args) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout


def _store():
    return Store(config.load().db_path)


def _wait_done(store, job_id, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if store.get_job(job_id).is_terminal:
            return store.get_job(job_id)
        time.sleep(0.1)
    raise AssertionError("시간 초과")


def _parent_with_changes(repo) -> Path:
    """worktree 에서 실행한 부모 작업 #1 을 만들고 그 worktree 에 여러 종류의 변경을 남긴다."""
    assert cli.main(["run", "부모 요청", "-C", str(repo / "sub")]) == 0
    wt = config.load().worktrees_dir / "myrepo-1"
    (wt / "sub" / "a.txt").write_text("부모가 고침\n")          # 고쳤지만 스테이징 안 함
    (wt / "keep.txt").write_text("스테이징함\n")
    _git(wt, "add", "keep.txt")                                  # 스테이징함
    (wt / "새파일.txt").write_text("new\n")                      # 추적하지 않는 새 파일
    (wt / "debug.log").write_text("무시\n")                      # .gitignore 된 파일
    return wt


def test_파생_작업은_부모의_커밋하지_않은_변경_위에서_시작하고_부모는_그대로다(env, repo):
    parent_wt = _parent_with_changes(repo)
    before = (_git(parent_wt, "status", "--porcelain"), _git(parent_wt, "rev-parse", "HEAD"),
              _git(parent_wt, "branch", "--show-current"))
    parent = _store().get_job(1)

    launched = jobs.derive(config.load(), _store(), 1, "  자식 요청  ")

    child = launched.job
    assert child.prompt == "자식 요청" and child.branch == "coliday/2" and child.base_commit != parent.base_commit
    child_wt = config.load().worktrees_dir / "myrepo-2"
    assert Path(child.workdir) == child_wt / "sub"                   # 부모와 같은 하위 폴더에서 일한다
    assert (child_wt / "sub" / "a.txt").read_text() == "부모가 고침\n"
    assert (child_wt / "keep.txt").read_text() == "스테이징함\n"
    assert (child_wt / "새파일.txt").read_text() == "new\n"
    assert not (child_wt / "debug.log").exists()
    assert gitops.diff(child.workdir, child.base_commit) == ""       # 자식 diff 에는 자식 변경만 나온다
    assert _git(child_wt, "status", "--porcelain") == ""
    # 부모의 파일·인덱스(스테이징 여부)·HEAD·브랜치는 그대로
    assert (_git(parent_wt, "status", "--porcelain"), _git(parent_wt, "rev-parse", "HEAD"),
            _git(parent_wt, "branch", "--show-current")) == before
    assert _git(child_wt, "rev-parse", "HEAD^").strip() == parent.base_commit
    store = _store()
    assert store.job_meta()[2].parent_id == 1 and not store.allow_env(2)
    assert _wait_done(store, 2).status == "succeeded"


def test_부모가_깨끗하면_스냅샷_없이_HEAD에서_시작한다(env, repo):
    assert cli.main(["run", "부모", "-C", str(repo)]) == 0
    parent = _store().get_job(1)

    child = jobs.derive(config.load(), _store(), 1, "자식").job

    assert child.base_commit == parent.base_commit
    assert Path(child.workdir) == config.load().worktrees_dir / "myrepo-2"
    _wait_done(_store(), 2)


def test_env_읽기_허용은_물려받지_않는다(env, repo):
    assert cli.main(["run", "부모", "-C", str(repo), "--allow-env"]) == 0
    assert _store().allow_env(1)

    jobs.derive(config.load(), _store(), 1, "자식")
    jobs.derive(config.load(), _store(), 1, "허용한 자식", allow_env=True)

    store = _store()
    assert not store.allow_env(2) and store.allow_env(3)
    _wait_done(store, 2)
    _wait_done(store, 3)


def test_worktree_없이_실행한_부모에서_파생하면_같은_디렉토리에서_worktree_없이_실행한다(env, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert cli.main(["run", "부모", "-C", str(plain), "--no-worktree"]) == 0

    child = jobs.derive(config.load(), _store(), 1, "자식").job

    assert child.branch is None and child.base_commit is None and Path(child.workdir) == plain.resolve()
    assert _store().job_meta()[2].parent_id == 1
    assert _wait_done(_store(), 2).status == "succeeded"


def test_부모가_없거나_작업_디렉토리가_지워졌으면_파생하지_않는다(env, repo):
    with pytest.raises(jobs.JobError, match="찾을 수 없습니다"):
        jobs.derive(config.load(), _store(), 7, "자식")
    with pytest.raises(jobs.JobError, match="비어 있습니다"):
        jobs.derive(config.load(), _store(), 7, "   ")

    assert cli.main(["run", "부모", "-C", str(repo)]) == 0
    import shutil
    shutil.rmtree(config.load().worktrees_dir / "myrepo-1")
    with pytest.raises(jobs.JobError, match="작업 디렉토리가 없습니다"):
        jobs.derive(config.load(), _store(), 1, "자식")
    assert [j.id for j in _store().list_jobs()] == [1]


def test_git_사용자_설정이_없어도_스냅샷을_만든다(env, repo, monkeypatch, tmp_path):
    parent_wt = _parent_with_changes(repo)
    home = tmp_path / "empty-home"
    home.mkdir()
    global_config = tmp_path / "gitconfig"
    # 사용자 이름을 추측하지 않고, 커밋 서명을 켜 둔 환경에서도 된다
    global_config.write_text("[user]\n\tuseConfigOnly = true\n[commit]\n\tgpgsign = true\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(name, raising=False)
    bare = subprocess.run(["git", "-C", str(parent_wt), "commit-tree", "HEAD^{tree}", "-m", "x"],
                          capture_output=True, text=True)
    assert bare.returncode != 0  # 이 환경에서는 그냥 커밋할 수 없다

    commit = gitops.snapshot(parent_wt, "스냅샷")

    assert _git(parent_wt, "log", "-1", "--format=%an <%ae>|%s", commit).strip() == "coliday <coliday@localhost>|스냅샷"
    assert "새파일.txt" in _git(parent_wt, "ls-tree", "-r", "-z", "--name-only", commit).split("\0")


def test_파생_작업의_첫_실행에만_부모_안내를_붙이고_요청_기록은_그대로다(env, repo):
    assert cli.main(["run", "결제 모듈 리팩터링", "-C", str(repo)]) == 0

    assert cli.main(["run", "--from", "1", "테스트 추가"]) == 0

    recorded = json.loads((env / "args.json").read_text())
    stdin = recorded["stdin"]
    assert "#1" in stdin and "결제 모듈 리팩터링" in stdin and "최종 보고: 완료" in stdin  # 부모 요청과 마지막 보고
    assert stdin.endswith("테스트 추가") and stdin.index("최종 보고") < stdin.index("테스트 추가")
    assert _store().get_job(2).prompt == "테스트 추가"

    assert cli.main(["continue", "2", "하나 더"]) == 0  # 이어서 시킨 실행에는 붙이지 않는다
    assert json.loads((env / "args.json").read_text())["stdin"] == "하나 더"


def test_부모_보고가_길면_잘라서_붙인다(env, repo):
    store = _store()
    parent = store.create_job(prompt="부모", repo=str(repo), workdir=str(repo), model=None)
    store.add_event(parent.id, "팀장", "result", "가" * 5000)
    store.finish_job(parent.id, "succeeded")
    child = store.create_job(prompt="자식", repo=str(repo), workdir=str(repo), model=None, parent_id=parent.id)

    assert worker.run_job(config.load(), store, child.id) == 0

    stdin = json.loads((env / "args.json").read_text())["stdin"]
    assert "가" * 2000 in stdin and "가" * 2001 not in stdin and "생략" in stdin


def test_부모가_그사이_지워졌어도_파생_작업은_요청만으로_실행된다(env, repo):
    store = _store()
    parent = store.create_job(prompt="부모", repo=str(repo), workdir=str(repo), model=None)
    store.finish_job(parent.id, "succeeded")
    child = store.create_job(prompt="자식", repo=str(repo), workdir=str(repo), model=None, parent_id=parent.id)
    assert store.delete_job(parent.id)

    assert worker.run_job(config.load(), store, child.id) == 0

    assert json.loads((env / "args.json").read_text())["stdin"] == "자식"
    assert store.get_job(child.id).status == "succeeded"


def test_중간_부모를_지워도_첫_실행_안내는_조부모를_말하지_않는다(env, repo):
    # #1 → #2 → #3 파생. #3 의 첫 실행 전에 #2 를 지우면 목록의 부모는 #1 로 옮겨지지만 스냅샷 출처는 #2 였다.
    store = _store()
    grand = store.create_job(prompt="조부모 요청", repo=str(repo), workdir=str(repo), model=None)
    store.add_event(grand.id, "팀장", "result", "조부모의 보고")
    store.finish_job(grand.id, "succeeded")
    middle = store.create_job(prompt="부모 요청", repo=str(repo), workdir=str(repo), model=None, parent_id=grand.id)
    store.finish_job(middle.id, "succeeded")
    child = store.create_job(prompt="자식", repo=str(repo), workdir=str(repo), model=None, parent_id=middle.id)
    assert store.delete_job(middle.id)
    assert store.job_meta()[child.id].parent_id == grand.id      # 목록에서는 조부모 밑으로 간다
    assert store.origin(child.id) == middle.id                    # 출처 기록은 그대로

    assert worker.run_job(config.load(), store, child.id) == 0

    stdin = json.loads((env / "args.json").read_text())["stdin"]
    assert stdin == "자식"                                         # 안내를 생략한다
    assert f"#{grand.id}" not in stdin and "조부모" not in stdin


def test_부모가_살아_있으면_안내에_부모_번호와_보고가_들어간다(env, repo):
    store = _store()
    grand = store.create_job(prompt="조부모 요청", repo=str(repo), workdir=str(repo), model=None)
    store.finish_job(grand.id, "succeeded")
    middle = store.create_job(prompt="부모 요청", repo=str(repo), workdir=str(repo), model=None, parent_id=grand.id)
    store.add_event(middle.id, "팀장", "result", "부모의 보고")
    store.finish_job(middle.id, "succeeded")
    child = store.create_job(prompt="자식", repo=str(repo), workdir=str(repo), model=None, parent_id=middle.id)

    assert worker.run_job(config.load(), store, child.id) == 0

    stdin = json.loads((env / "args.json").read_text())["stdin"]
    assert f"#{middle.id}(요청: 부모 요청)" in stdin and "부모의 보고" in stdin and "조부모" not in stdin
    assert stdin.endswith("자식")


def test_파생_작업이_아니면_요청을_그대로_넘긴다(env, repo):
    assert cli.main(["run", "그냥 요청", "-C", str(repo)]) == 0
    assert json.loads((env / "args.json").read_text())["stdin"] == "그냥 요청"


# ── 서버의 프로젝트 ────────────────────────────────────────────────────────

HOST = "dev@server"


@pytest.fixture
def server(env, monkeypatch):
    ssh = env / "ssh"
    ssh.write_text(f"#!/bin/sh\nexec {sys.executable} {FIXTURES / 'fake_ssh.py'} \"$@\"\n")
    ssh.chmod(0o755)
    (env / "server-home").mkdir()
    monkeypatch.setenv("COLIDAY_SSH_BIN", str(ssh))
    monkeypatch.setenv("FAKE_SSH_HOME", str(env / "server-home"))
    monkeypatch.setenv("FAKE_SSH_LOG", str(env / "ssh.jsonl"))
    remote.home.cache_clear()
    r = env / "srv" / "app"
    (r / "sub").mkdir(parents=True)
    (r / "sub" / "a.txt").write_text("a\n")
    _git(r, "init", "-q")
    _git(r, "add", ".")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return r


def _ssh_count(env) -> int:
    path = env / "ssh.jsonl"
    return len(path.read_text().splitlines()) if path.exists() else 0


def test_서버의_worktree_스냅샷은_접속_한_번으로_만든다(env, server):
    (server / "sub" / "a.txt").write_text("서버에서 고침\n")
    (server / "new.txt").write_text("new\n")
    status = _git(server, "status", "--porcelain")
    head = _git(server, "rev-parse", "HEAD")
    before = _ssh_count(env)

    commit = gitops.snapshot(f"{HOST}:{server}", "스냅샷")

    assert _ssh_count(env) == before + 1
    assert _git(server, "show", f"{commit}:sub/a.txt") == "서버에서 고침\n"
    assert _git(server, "show", f"{commit}:new.txt") == "new\n"
    assert _git(server, "status", "--porcelain") == status and _git(server, "rev-parse", "HEAD") == head
    _git(server, "checkout", "-q", "--", ".")
    (server / "new.txt").unlink()
    assert gitops.snapshot(f"{HOST}:{server}", "스냅샷") == head.strip()  # 바뀐 것이 없으면 HEAD


def test_서버의_작업에서도_파생한다(env, server):
    app = f"{HOST}:{server}"
    assert cli.main(["run", "부모", "-C", app + "/sub"]) == 0
    home = env / "server-home"
    parent_wt = home / ".coliday" / "worktrees" / "app-1"
    (parent_wt / "sub" / "a.txt").write_text("부모가 고침\n")

    assert cli.main(["run", "--from", "1", "자식"]) == 0

    child = _store().get_job(2)
    assert child.workdir == f"{HOST}:{home}/.coliday/worktrees/app-2/sub" and child.branch == "coliday/2"
    assert (home / ".coliday" / "worktrees" / "app-2" / "sub" / "a.txt").read_text() == "부모가 고침\n"
    assert gitops.diff(child.workdir, child.base_commit) == ""
