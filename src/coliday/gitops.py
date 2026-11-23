"""작업별 git worktree 관리. 위치가 `host:/경로` 면 그 서버에서 ssh 로 실행한다 (`remote.py`)."""

from __future__ import annotations

import os
import posixpath
import shlex
import subprocess
from pathlib import Path

from coliday import remote

Location = str | os.PathLike


class GitError(RuntimeError):
    pass


def _git(cwd: Location, *args: str) -> str:
    host, path = remote.split(cwd)
    if host is not None:
        try:
            proc = remote.run(host, shlex.join(["git", "-C", path, *args]))
        except remote.RemoteError as exc:
            raise GitError(str(exc)) from exc
    else:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
        )
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git {' '.join(args)} 실패")
    return proc.stdout


def repo_root(path: Location) -> str | None:
    """레포 최상위 위치 (원격이면 `host:/경로`). git 저장소가 아니면 None."""
    try:
        top = _git(path, "rev-parse", "--show-toplevel").strip()
    except GitError:
        return None
    host, _ = remote.split(path)
    return f"{host}:{top}" if host is not None else top


def head_commit(repo: Location) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def is_dirty(repo: Location) -> bool:
    return bool(_git(repo, "status", "--porcelain").strip())


def add_worktree(repo: Location, path: Location, branch: str, base: str) -> None:
    host, target = remote.split(path)
    if host is not None:
        try:
            remote.check(host, f"mkdir -p {shlex.quote(posixpath.dirname(target))}")
        except remote.RemoteError as exc:
            raise GitError(str(exc)) from exc
    else:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-b", branch, target, base)


def remove_worktree(repo: Location, path: Location, branch: str | None, force: bool) -> None:
    args = ["worktree", "remove", remote.split(path)[1]]
    if force:
        args.insert(2, "--force")
    _git(repo, *args)
    if branch:
        _git(repo, "branch", "-D" if force else "-d", branch)


def diff(workdir: Location, base: str | None) -> str:
    """base 커밋 대비 작업 디렉토리 변경(커밋 포함)과 새 파일 내용을 함께 돌려준다."""
    host, path = remote.split(workdir)
    if host is not None:
        return _remote_diff(host, path, base)
    tracked = _git(workdir, "diff", base or "HEAD")
    # -z: 한글 등 비 ASCII 파일명이 따옴표·escape 되지 않게 한다.
    untracked = [n for n in _git(workdir, "ls-files", "-z", "--others", "--exclude-standard").split("\0") if n]
    parts = [tracked]
    for name in untracked:
        proc = subprocess.run(
            ["git", "-C", str(workdir), "diff", "--no-index", "--", "/dev/null", name],
            capture_output=True, text=True, check=False,
        )
        parts.append(proc.stdout)  # --no-index 는 차이가 있으면 종료 코드 1
    return "".join(parts)


def _remote_diff(host: str, path: str, base: str | None) -> str:
    """같은 결과를 접속 한 번으로 가져온다 (화면이 몇 초마다 다시 부른다)."""
    script = (
        f"cd {shlex.quote(path)} && git --no-pager diff {shlex.quote(base or 'HEAD')} && "
        "{ git ls-files -z --others --exclude-standard "
        "| xargs -0 -n 1 git --no-pager diff --no-index -- /dev/null; true; }"  # 새 파일의 diff 는 종료 코드 1
    )
    try:
        return remote.check(host, script)
    except remote.RemoteError as exc:
        raise GitError(str(exc)) from exc
