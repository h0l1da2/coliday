"""작업별 git worktree 관리. 위치가 `host:/경로` 면 그 서버에서 ssh 로 실행한다 (`remote.py`)."""

from __future__ import annotations

import os
import posixpath
import re
import shlex
import subprocess
import tempfile
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


# 스냅샷 커밋의 작성자. 사용자의 git 설정(user.name·email)이 없어도 커밋을 만들 수 있게 직접 준다.
_SNAPSHOT_IDENTITY = {
    "GIT_AUTHOR_NAME": "coliday", "GIT_AUTHOR_EMAIL": "coliday@localhost",
    "GIT_COMMITTER_NAME": "coliday", "GIT_COMMITTER_EMAIL": "coliday@localhost",
}


def snapshot(worktree: Location, message: str) -> str:
    """worktree 의 지금 상태(커밋하지 않은 변경과 추적하지 않는 새 파일, .gitignore 된 것은 빼고)를 커밋으로 만들어 그 id 를 돌려준다.

    임시 인덱스를 쓰므로 그 worktree 의 파일·인덱스·HEAD·브랜치는 그대로다 (커밋은 어느 브랜치에도 달지 않는다).
    바뀐 것이 없으면 새 커밋을 만들지 않고 HEAD 를 돌려준다.
    """
    host, path = remote.split(worktree)
    if host is not None:
        return _remote_snapshot(host, path, message)
    with tempfile.TemporaryDirectory(prefix="coliday-snapshot-") as tmp:
        # 빈 파일은 깨진 인덱스로 보므로 아직 없는 경로를 준다
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(tmp, "index"), **_SNAPSHOT_IDENTITY}

        def git(*args: str) -> str:
            proc = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=False, env=env)
            if proc.returncode != 0:
                raise GitError(proc.stderr.strip() or f"git {' '.join(args)} 실패")
            return proc.stdout.strip()

        git("read-tree", "HEAD")
        git("add", "-A", ":/")  # 하위 폴더를 줘도 worktree 전체를 담는다
        tree = git("write-tree")
        head = git("rev-parse", "HEAD")
        if tree == git("rev-parse", "HEAD^{tree}"):
            return head
        return git("commit-tree", "--no-gpg-sign", tree, "-p", head, "-m", message)


def _remote_snapshot(host: str, path: str, message: str) -> str:
    """같은 일을 서버에서 접속 한 번으로 한다."""
    identity = " ".join(f"{k}={shlex.quote(v)}" for k, v in _SNAPSHOT_IDENTITY.items())
    script = (
        f"set -e; cd {shlex.quote(path)}; "
        'tmp=$(mktemp -d); trap \'rm -rf "$tmp"\' EXIT; GIT_INDEX_FILE="$tmp/index"; export GIT_INDEX_FILE; '
        "git read-tree HEAD; git add -A :/; tree=$(git write-tree); head=$(git rev-parse HEAD); "
        'if [ "$tree" = "$(git rev-parse "HEAD^{tree}")" ]; then echo "$head"; '
        f'else {identity} git commit-tree --no-gpg-sign "$tree" -p "$head" -m {shlex.quote(message)}; fi'
    )
    try:
        out = remote.check(host, script, timeout=120).strip()
    except remote.RemoteError as exc:
        raise GitError(str(exc)) from exc
    if not re.fullmatch(r"[0-9a-f]{40,64}", out.splitlines()[-1] if out else ""):
        raise GitError(f"{host}: 스냅샷 커밋을 만들지 못했습니다: {out or '출력 없음'}")
    return out.splitlines()[-1]


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
