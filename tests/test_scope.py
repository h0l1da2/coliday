"""작업의 소속 프로젝트 계산 (순수 함수)."""

from __future__ import annotations

import pytest

from coliday import config, jobs, remote, scope
from coliday.scope import OTHER, candidate_repos, contains, has_other, location, owner, scope_jobs, scope_of
from coliday.store import Job, Store


def _job(job_id: int, repo: str, workdir: str, branch: str | None = "coliday/x") -> Job:
    return Job(
        id=job_id, prompt="p", repo=repo, workdir=workdir, branch=branch, base_commit=None, status="queued",
        phase="", pid=None, session_id=None, model=None, result=None, cost_usd=None, created_at=1.0,
        started_at=None, finished_at=None,
    )


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("COLIDAY_HOME", str(tmp_path / "home"))
    return config.load()


def test_worktree_작업은_실제_worktree_경로에서_하위_폴더를_찾는다(cfg):
    # 폴더 이름 규칙은 jobs.worktree_path 가 정한다: 그 결과로 만든 경로에서 거꾸로 찾아야 한다
    job = _job(12, "/r/shop", "")
    wt = jobs.worktree_path(cfg, job)
    assert location(_job(12, "/r/shop", remote.join(wt, "fe/src"))) == "/r/shop/fe/src"
    assert location(_job(12, "/r/shop", wt)) == "/r/shop"
    # 다른 작업 번호의 폴더 이름과는 섞이지 않는다
    assert location(_job(13, "/r/shop", remote.join(wt, "fe"))) == "/r/shop"


def test_worktree_없는_작업과_만드는_중인_작업():
    assert location(_job(1, "/r", "/r/sub", branch=None)) == "/r/sub"
    assert location(_job(1, "/plain", "", branch=None)) == "/plain"
    assert location(_job(1, "/r", "")) == "/r"                     # worktree 를 만드는 중이거나 실패
    assert location(_job(1, "/r", "/somewhere/else")) == "/r"


def test_서버_작업은_호스트와_포트를_유지한다(monkeypatch, cfg):
    monkeypatch.setattr(remote, "home", lambda host: "/home/dev")
    job = _job(7, "dev@host:2222:/srv/app", "")
    wt = jobs.worktree_path(cfg, job)
    assert wt == "dev@host:2222:/home/dev/.coliday/worktrees/app-7"
    assert location(_job(7, "dev@host:2222:/srv/app", f"{wt}/api")) == "dev@host:2222:/srv/app/api"


def test_owner는_가장_가까운_상위이고_접두_함정과_호스트를_구분한다():
    projects = ["/r", "/r/a", "/r/ab", "dev@h:/r/a", "dev@h:2222:/srv"]
    assert owner("/r/a/x", projects) == "/r/a"
    assert owner("/r/ab", projects) == "/r/ab"
    assert owner("/r/abc", projects) == "/r"                  # /r/ab 의 아래가 아니다
    assert owner("/r/a/", projects) == "/r/a"
    assert owner("/other", projects) is None
    assert owner("dev@h:/r/a/b", projects) == "dev@h:/r/a"     # 로컬 /r/a 와 섞이지 않는다
    assert owner("dev@h:/r", projects) is None
    assert owner("dev@h:/srv/x", projects) is None             # 포트가 다르면 다른 호스트
    assert owner("dev@h:2222:/srv/x", projects) == "dev@h:2222:/srv"
    assert owner("/anything", ["/"]) == "/"
    assert owner(OTHER, projects) is None
    assert contains("/", "/x") and not contains("/x", "/") and not contains("/r", "dev@h:/r")


def test_scope_of는_소속이_없으면_기타다():
    job = _job(3, "/r", "/r/b", branch=None)
    assert scope_of(job, ["/r/a"]) == OTHER
    assert scope_of(job, ["/r/a", "/r"]) == "/r"
    assert scope_of(job, ["/r/b", "/r"]) == "/r/b"
    assert scope_of(job, []) == OTHER


def test_candidate_repos():
    repos = ["/r", "/r/a/inner", "/x", "dev@h:/r", "/rr"]
    projects = ["/r/a", "/r/b", "/x"]
    # 하위 폴더 프로젝트: 그 프로젝트를 품는 레포와 그 아래 레포
    assert candidate_repos("/r/a", repos, projects) == ["/r", "/r/a/inner"]
    assert candidate_repos("/x", repos, projects) == ["/x"]
    # 기타: 어떤 프로젝트와도 같지 않고 그 아래도 아닌 레포 (/r 최상위에서 실행한 작업이 있을 수 있다)
    assert candidate_repos(OTHER, repos, projects) == ["/r", "dev@h:/r", "/rr"]
    assert candidate_repos(OTHER, [], projects) == []
    assert scope.OTHER == "?other"


# ── worktree 뿌리 ──────────────────────────────────────────────────────────
def test_worktree_폴더_이름이_하위_경로에_다시_나와도_뿌리를_기준으로_한다(cfg):
    wt = jobs.worktree_path(cfg, _job(12, "/r/app-2", ""))
    job = _job(12, "/r/app-2", f"{wt}/api/app-2-12/sub")
    expected = "/r/app-2/api/app-2-12/sub"
    assert location(job, str(cfg.worktrees_dir)) == expected
    assert location(job) == expected                        # worktrees 폴더를 넘기지 않으면 첫 번째 일치
    assert location(_job(12, "/r/app-2", "/wt/app-2-12/api/app-2-12/sub")) == expected


def test_worktrees_폴더의_상위_경로에_같은_이름이_있어도_뿌리를_기준으로_한다(tmp_path, monkeypatch):
    # 홈 폴더 위쪽에 우연히 worktree 폴더와 같은 이름이 있다
    monkeypatch.setenv("COLIDAY_HOME", str(tmp_path / "app-12" / "home"))
    cfg = config.load()
    wt = jobs.worktree_path(cfg, _job(12, "/r/app", ""))
    assert "/app-12/" in wt.removesuffix("/app-12")
    job = _job(12, "/r/app", f"{wt}/fe")
    assert location(job, str(cfg.worktrees_dir)) == "/r/app/fe"
    assert scope_of(job, ["/r/app", "/r/app/fe"], str(cfg.worktrees_dir)) == "/r/app/fe"
    # 상위와 하위에 모두 같은 이름이 있어도 뿌리 아래만 붙인다
    both = _job(12, "/r/app", f"{wt}/x/app-12/y")
    assert location(both, str(cfg.worktrees_dir)) == "/r/app/x/app-12/y"


def test_서버_작업은_서버의_worktrees_폴더_아래를_뿌리로_삼는다(monkeypatch, cfg):
    monkeypatch.setattr(remote, "home", lambda host: "/home/app-7")   # 홈 이름이 worktree 폴더와 같다
    job = _job(7, "dev@host:/srv/app", "")
    wt = jobs.worktree_path(cfg, job)
    assert wt == "dev@host:/home/app-7/.coliday/worktrees/app-7"
    assert location(_job(7, "dev@host:/srv/app", f"{wt}/api"), str(cfg.worktrees_dir)) == "dev@host:/srv/app/api"
    assert location(_job(7, "dev@host:/srv/app", f"{wt}/a/app-7/b")) == "dev@host:/srv/app/a/app-7/b"


# ── 소속으로 걸러 가져오기 ──────────────────────────────────────────────────
@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "jobs.db")
    yield s
    s.close()


def _insert(store: Store, count: int, workdir: str, repo: str = "/r", start: float = 0.0) -> None:
    """작업을 직접 넣는다 (worktree 없이 하위 폴더에서 실행한 작업). 넣은 순서대로 나중 것이 최근이다."""
    store._conn.executemany(
        "INSERT INTO jobs(prompt, repo, workdir, status, created_at) VALUES ('p', ?, ?, 'succeeded', ?)",
        [(repo, workdir, start + n) for n in range(count)],
    )


def test_같은_레포의_다른_하위_폴더_작업이_많아도_고른_프로젝트의_작업이_보인다(store):
    _insert(store, 1, "/r/a")
    _insert(store, 500, "/r/b", start=10)
    projects = ["/r/a", "/r/b"]

    found = scope_jobs(store, "/r/a", store.repos(), projects)

    assert [j.workdir for j in found] == ["/r/a"]
    assert len(scope_jobs(store, "/r/b", store.repos(), projects)) == 100


def test_고른_프로젝트의_작업이_100개가_찰_때까지_여러_쪽을_읽는다(store):
    _insert(store, 150, "/r/a")
    _insert(store, 1200, "/r/b", start=1000)
    found = scope_jobs(store, "/r/a", store.repos(), ["/r/a", "/r/b"])
    assert len(found) == 100 and {j.workdir for j in found} == {"/r/a"}
    assert [j.created_at for j in found] == sorted((j.created_at for j in found), reverse=True)
    assert found[0].created_at == 149.0                          # 가장 최근 것부터


def test_레포와_프로젝트가_1대1이면_쿼리_한_번으로_끝난다(store, monkeypatch):
    _insert(store, 300, "/r")
    calls = []
    real = store.list_jobs
    monkeypatch.setattr(store, "list_jobs", lambda *a, **k: calls.append(k) or real(*a, **k))
    assert len(scope_jobs(store, "/r", store.repos(), ["/r"])) == 100
    assert len(calls) == 1


def test_list_jobs의_다음_쪽은_순서대로_겹치지_않고_이어진다(store):
    _insert(store, 7, "/r")
    store._conn.execute("UPDATE jobs SET created_at = 3 WHERE id IN (2, 5, 6)")  # 같은 시각은 번호로 가른다
    whole = [j.id for j in store.list_jobs(100)]
    pages, after = [], None
    while page := store.list_jobs(2, after=after):
        pages += [j.id for j in page]
        after = page[-1]
    assert pages == whole and len(set(pages)) == 7


def test_기타_줄은_첫_500개_밖의_기타_작업도_찾는다(store):
    _insert(store, 1, "/r")                       # 레포 최상위에서 실행: 어느 프로젝트에도 속하지 않는다
    _insert(store, 500, "/r/b", start=10)
    assert has_other(store, store.repos(), ["/r/a", "/r/b"]) is True
    # 기타 작업이 없으면 끝까지 보고 없다고 한다
    store._conn.execute("DELETE FROM jobs WHERE workdir = '/r'")
    assert has_other(store, store.repos(), ["/r/a", "/r/b"]) is False


def test_기타_줄은_프로젝트를_목록에서_빼면_나타난다(store):
    _insert(store, 1, "/r/a")
    _insert(store, 500, "/r/b", start=10)
    assert has_other(store, store.repos(), ["/r/a", "/r/b"]) is False
    assert has_other(store, store.repos(), ["/r/b"]) is True     # /r/a 를 뺐다


def test_안에_프로젝트가_없는_레포는_작업을_읽지_않고_기타로_본다(store, monkeypatch):
    _insert(store, 3, "/x", repo="/x")
    monkeypatch.setattr(store, "list_jobs", lambda *a, **k: pytest.fail("작업을 읽었다"))
    assert has_other(store, store.repos(), ["/r"]) is True
    assert has_other(store, [], ["/r"]) is False
    assert has_other(store, ["/x"], []) is True
