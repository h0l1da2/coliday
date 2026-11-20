"""작업별 git worktree 관리."""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def _git(cwd: Path | str, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git {' '.join(args)} 실패")
    return proc.stdout


def repo_root(path: Path) -> Path | None:
    try:
        return Path(_git(path, "rev-parse", "--show-toplevel").strip())
    except GitError:
        return None


def head_commit(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def is_dirty(repo: Path) -> bool:
    return bool(_git(repo, "status", "--porcelain").strip())


def add_worktree(repo: Path, path: Path, branch: str, base: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-b", branch, str(path), base)


def remove_worktree(repo: Path, path: Path, branch: str | None, force: bool) -> None:
    args = ["worktree", "remove", str(path)]
    if force:
        args.insert(2, "--force")
    _git(repo, *args)
    if branch:
        _git(repo, "branch", "-D" if force else "-d", branch)


def diff(workdir: Path, base: str | None) -> str:
    """base 커밋 대비 작업 디렉토리 변경(커밋 포함)과 새 파일 내용을 함께 돌려준다."""
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
