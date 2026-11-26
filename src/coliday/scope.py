"""작업이 어느 프로젝트에 속하는지 경로만으로 계산한다.

jobs 표에 열을 늘리지 않는다 (이전 버전 worker 가 `Job(**dict(row))` 로 읽는다). 옛 작업에도 그대로 적용된다.
작업이 속한 프로젝트는 작업을 시작한 위치와 같거나 그 위치를 품는 프로젝트 가운데 가장 가까운(경로가 가장 긴) 것이고,
없으면 "기타"(OTHER)다.
"""

from __future__ import annotations

import posixpath
from typing import TYPE_CHECKING, Iterable, Sequence

from coliday import remote

if TYPE_CHECKING:
    from coliday.store import Job, Store

OTHER = "?other"  # 기타 줄의 키. 로컬 경로(/…)·서버 경로(host:/…)와 겹치지 않는다


def _parts(location: str) -> tuple[str | None, str]:
    host, path = remote.split(location)
    return host, posixpath.normpath(path) if path.startswith("/") else path


def contains(parent: str, child: str) -> bool:
    """같은 호스트에서 child 가 parent 와 같거나 그 아래인가. `/r/a` 는 `/r/ab` 를 품지 않는다."""
    parent_host, parent_path = _parts(parent)
    child_host, child_path = _parts(child)
    if parent_host != child_host:
        return False
    if child_path == parent_path:
        return True
    prefix = parent_path.rstrip("/") + "/"  # 루트 "/" 는 그대로 "/"
    return child_path.startswith(prefix)


def location(job: Job, worktrees: str | None = None) -> str:
    """작업을 시작한 위치.

    worktree 없이 실행한 작업은 작업 디렉토리 그 자체다. worktree 작업은 workdir 에서 jobs.worktree_path 의
    worktree 뿌리 `{worktrees 폴더}/{레포 이름}-{작업 번호}` 를 찾아 그 아래 하위 경로를 레포 뒤에 붙인다
    (폴더 규칙을 바꾸면 여기도 바꾼다). worktrees 는 이 컴퓨터의 worktrees 폴더 (cfg.worktrees_dir).
    worktree 를 만드는 중(workdir 이 빔)이거나 만들지 못했으면 레포 최상위다.
    """
    if job.branch is None:
        return job.workdir or job.repo
    folder = f"{remote.name(job.repo)}-{job.id}"
    host, path = remote.split(job.workdir) if job.workdir else (None, "")
    parts = posixpath.normpath(path).split("/") if path else []
    at = _root(parts, folder, host, worktrees)
    if at is None:
        return job.repo
    inside = [p for p in parts[at + 1:] if p and p != "."]
    return remote.join(job.repo, *inside) if inside else job.repo


def _root(parts: list[str], folder: str, host: str | None, worktrees: str | None) -> int | None:
    """parts 에서 worktree 뿌리 폴더의 위치. 하위 경로에 같은 이름이 다시 나와도 뿌리를 고른다."""
    if host is None and worktrees:
        base = posixpath.normpath(worktrees).split("/")
        if parts[:len(base)] == base and parts[len(base):len(base) + 1] == [folder]:
            return len(base)
    if host is not None:  # 서버의 worktree 는 그 서버의 `~/.coliday/worktrees/` 바로 아래다 (홈은 접속해야 안다)
        for at in range(2, len(parts)):
            if parts[at - 2:at + 1] == [".coliday", "worktrees", folder]:
                return at
    # 뿌리 폴더를 알 수 없으면(worktrees 폴더를 옮겼거나 넘기지 않음) 첫 번째 일치: 뿌리는 하위 경로보다 앞에 온다
    return parts.index(folder) if folder in parts else None


def owner(location: str, projects: Sequence[str]) -> str | None:
    """location 을 품는 프로젝트 가운데 가장 가까운 것. 없으면 None."""
    found = [p for p in projects if contains(p, location)]
    return max(found, key=lambda p: len(_parts(p)[1].rstrip("/")), default=None)


def scope_of(job: Job, projects: Sequence[str], worktrees: str | None = None) -> str:
    """작업이 보이는 프로젝트 목록 줄의 키 (프로젝트 경로 또는 OTHER)."""
    return owner(location(job, worktrees), projects) or OTHER


def candidate_repos(scope: str, repos: Iterable[str], projects: Sequence[str]) -> list[str]:
    """scope 의 작업이 들어 있을 수 있는 레포 (DB 에서 먼저 거르는 데 쓴다. 정확한 소속은 scope_of 로 다시 본다).

    작업의 시작 위치는 언제나 그 레포 안이다. 그래서 프로젝트 P 의 작업은 레포가 P 와 같거나, P 아래이거나,
    P 를 품는 레포(하위 폴더를 프로젝트로 연결한 경우)에 있다. 기타의 작업은 어떤 프로젝트와도 같지 않고 그 아래도 아닌 레포에 있다.
    """
    if scope == OTHER:
        return [r for r in repos if not any(contains(p, r) for p in projects)]
    return [r for r in repos if contains(scope, r) or contains(r, scope)]


PAGE = 500  # 소속으로 거르며 더 읽을 때 한 번에 읽는 작업 수


def scope_jobs(
    store: Store, scope: str, repos: Iterable[str], projects: Sequence[str], *,
    limit: int = 100, worktrees: str | None = None,
) -> list[Job]:
    """scope 의 작업 가운데 최근 limit 개. 후보 레포로 DB 에서 먼저 거르고 소속을 다시 계산한다.

    같은 레포의 다른 하위 폴더 프로젝트 작업이 아무리 많아도 가려지지 않게 limit 개가 찰 때까지 다음 쪽을 읽는다.
    레포와 프로젝트가 1:1 이면 첫 쪽(limit 개)이 모두 이 프로젝트 것이라 쿼리 한 번으로 끝난다.
    """
    candidates = candidate_repos(scope, repos, projects)
    found: list[Job] = []
    after, size = None, limit
    while candidates:
        rows = store.list_jobs(size, repos=candidates, after=after)
        found += [job for job in rows if scope_of(job, projects, worktrees) == scope]
        if len(found) >= limit or len(rows) < size:
            break
        after, size = rows[-1], PAGE
    return found[:limit]


def has_other(store: Store, repos: Iterable[str], projects: Sequence[str], worktrees: str | None = None) -> bool:
    """어느 프로젝트에도 속하지 않는 작업이 있는가 (기타 줄을 보일지)."""
    repos = list(repos)
    if not projects:  # 연결된 프로젝트가 없으면 모든 작업이 기타다
        return bool(repos)
    candidates = candidate_repos(OTHER, repos, projects)
    # 안에 프로젝트가 하나도 없는 레포의 작업은 모두 기타다: 작업을 읽지 않고 판정한다 (repos 는 작업이 있는 레포다)
    if any(not any(contains(r, p) for p in projects) for r in candidates):
        return True
    after = None
    while candidates:  # 하위 폴더만 프로젝트인 레포: 그 밖에서 시작한 작업이 있는지 끝까지 본다
        rows = store.list_jobs(PAGE, repos=candidates, after=after)
        if any(scope_of(job, projects, worktrees) == OTHER for job in rows):
            return True
        if len(rows) < PAGE:
            break
        after = rows[-1]
    return False
