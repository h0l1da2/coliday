"""작업 시작·취소·정리. CLI 와 TUI 가 함께 쓴다."""

from __future__ import annotations

import os
import posixpath
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

from coliday import gitops, guard, projects, remote, team
from coliday.config import Config
from coliday.joblist import JobGroup
from coliday.store import RESULT, USER, Job, Store

STATUS_ICON = {"queued": "…", "running": "●", "succeeded": "✓", "failed": "✗", "cancelled": "■"}
_QUEUED_TIMEOUT_SEC = 60
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"})
_VIDEO_SUFFIXES = frozenset({".webm", ".mp4", ".mov", ".mkv", ".avi"})


class JobError(Exception):
    """사용자에게 그대로 보여줄 수 있는 작업 오류."""


class NotARepoError(JobError):
    """git 저장소가 아닌 폴더에서 worktree 를 만들려 했다. worktree 없이 다시 시도할 수 있다."""


class WorktreeRemoveError(JobError):
    """worktree 를 지우지 못함 (보통 검토하지 않은 변경이 남아 있어서). force 로 다시 시도할 수 있다."""


@dataclass(frozen=True)
class Launched:
    job: Job
    warnings: list[str] = field(default_factory=list)
    project: str | None = None  # 새 작업을 시작한 위치 (정규화한 프로젝트 경로)
    queued: bool = False  # 작업이 실행 중이라 요청을 대기시켰다 (지금 실행이 끝나면 이어서 전달된다)


def launch(
    cfg: Config, store: Store, prompt: str, directory: str | Path,
    *, no_worktree: bool = False, model: str | None = None, allow_env: bool = False,
) -> Launched:
    """allow_env 는 사람이 이 작업에서 .env 파일을 읽어도 된다고 한 경우에만 True 다 (기본은 읽지 못하게 막는다)."""
    prompt = prompt.strip()
    if not prompt:
        raise JobError("요청 내용이 비어 있습니다.")
    target = _location(directory)
    host = remote.split(target)[0]
    warnings: list[str] = []
    model = team.canonical(model or lead_model(cfg))
    check_team(cfg)
    try:
        check_codex(cfg, model)  # 서버의 프로젝트도 에이전트는 이 컴퓨터에서 돈다
        check_env_guard(cfg, model, allow_env)
        root = gitops.repo_root(target)
    except remote.RemoteError as exc:
        raise JobError(str(exc)) from exc
    try:
        list_projects(cfg, store)  # 이 작업을 만들기 전에 기존 작업으로 목록을 채워 둔다
    except JobError:
        pass

    if no_worktree or root is None:
        if root is None and not no_worktree:
            raise NotARepoError(
                f"git 저장소가 아닙니다: {target}. 이 디렉토리에서 직접 작업하려면 worktree 없이 실행하세요 (--no-worktree)."
            )
        job = store.create_job(prompt=prompt, repo=root or target, workdir=target, model=model)
    else:
        if gitops.is_dirty(root):
            warnings.append("커밋하지 않은 변경은 worktree 에 포함되지 않습니다 (HEAD 기준으로 작업).")
        base = gitops.head_commit(root)
        # 하위 디렉토리에서 실행했다면 worktree 안의 같은 위치에서 작업한다.
        inside = os.path.relpath(remote.split(target)[1], remote.split(root)[1])
        job = _create_in_worktree(cfg, store, prompt, root, base, inside, model)

    _start(cfg, store, job, allow_env)
    try:
        add_project(cfg, store, target)
    except JobError as exc:  # 목록 파일이 깨져도 작업은 이미 시작됐다
        warnings.append(f"프로젝트 목록에 추가하지 못했습니다: {exc}")
    return Launched(job, warnings, project=target)


def derive(
    cfg: Config, store: Store, parent_id: int, prompt: str, *, model: str | None = None, allow_env: bool = False,
) -> Launched:
    """부모 작업의 지금 작업 디렉토리 상태(커밋하지 않은 변경 포함)에서 새 작업을 시작한다.

    부모 worktree 를 스냅샷 커밋으로 만들고 거기서 새 worktree·브랜치를 연다 (자식 diff 에는 자식 변경만 나온다).
    부모가 worktree 없이 실행한 작업이면 같은 디렉토리에서 worktree 없이 실행한다. 세션은 새로 시작하고
    .env 읽기 허용은 물려받지 않는다 (allow_env 로 따로 준다). 부모가 실행 중이어도 된다.
    """
    prompt = prompt.strip()
    if not prompt:
        raise JobError("요청 내용이 비어 있습니다.")
    try:
        parent = store.get_job(parent_id)
    except KeyError:
        raise JobError(f"작업 #{parent_id} 을(를) 찾을 수 없습니다.") from None
    if not parent.workdir or not _is_dir(parent.workdir):
        raise JobError(f"작업 #{parent.id} 의 작업 디렉토리가 없습니다 (정리된 작업). 파생할 수 없습니다.")
    model = team.canonical(model or lead_model(cfg))
    check_team(cfg)
    try:
        check_codex(cfg, model)
        check_env_guard(cfg, model, allow_env)
    except remote.RemoteError as exc:
        raise JobError(str(exc)) from exc

    if parent.branch is None:
        job = store.create_job(
            prompt=prompt, repo=parent.repo, workdir=parent.workdir, model=model, parent_id=parent.id,
        )
    else:
        try:
            top = gitops.repo_root(parent.workdir)
            if top is None:
                raise gitops.GitError(f"git worktree 가 아닙니다: {parent.workdir}")
            base = gitops.snapshot(top, f"coliday: 작업 #{parent.id} 의 작업 상태 (파생 작업의 시작점)")
        except (gitops.GitError, remote.RemoteError) as exc:
            raise JobError(f"작업 #{parent.id} 의 상태를 저장하지 못했습니다: {exc}") from exc
        inside = _relative(parent.workdir, top)
        job = _create_in_worktree(cfg, store, prompt, parent.repo, base, inside, model, parent_id=parent.id)
    _start(cfg, store, job, allow_env)
    # project 는 비워 둔다: 부모의 레포 최상위는 연결된 프로젝트 목록에 없을 수 있다 (하위 폴더를 연결한 경우)
    return Launched(job)


def _relative(workdir: str, top: str) -> str:
    """worktree 최상위(top)에서 본 workdir 의 상대 위치. 밖이면 최상위."""
    host, path = remote.split(workdir)
    top_path = remote.split(top)[1]
    if host is None:  # 링크가 낀 경로(/var ↔ /private/var)를 같게 본다
        path, top_path = os.path.realpath(path), os.path.realpath(top_path)
    inside = posixpath.relpath(path, top_path) if host is not None else os.path.relpath(path, top_path)
    return "." if inside == ".." or inside.startswith(("../", ".." + os.sep)) else inside


def _create_in_worktree(
    cfg: Config, store: Store, prompt: str, repo: str, base: str, inside: str, model: str | None,
    *, parent_id: int | None = None,
) -> Job:
    """repo 의 base 커밋에서 새 worktree·브랜치를 만든 작업. inside 는 worktree 안에서 일할 상대 위치다."""
    job = store.create_job(prompt=prompt, repo=repo, workdir="", model=model, parent_id=parent_id)
    branch = f"coliday/{job.id}"
    try:
        wt = worktree_path(cfg, job)
        gitops.add_worktree(repo, wt, branch, base)
    except (gitops.GitError, remote.RemoteError) as exc:
        store.finish_job(job.id, "failed", phase=f"worktree 생성 실패: {exc}")
        raise JobError(f"worktree 생성 실패: {exc}") from exc
    store.update_job(job.id, workdir=remote.join(wt, inside), branch=branch, base_commit=base)
    return store.get_job(job.id)


def _start(cfg: Config, store: Store, job: Job, allow_env: bool) -> None:
    if allow_env:
        store.set_allow_env(job.id, True)
    _spawn_worker(cfg, job.id)


def create_group(store: Store, name: str, project: str) -> JobGroup:
    """project 는 그룹이 속한 프로젝트 (projects.json 의 정규화된 경로). 이름은 그 프로젝트 안에서만 겹치지 않으면 된다."""
    try:
        return store.create_group(name, project=project)
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise JobError(str(exc)) from exc


def rename_group(store: Store, group_id: int, name: str) -> None:
    try:
        store.rename_group(group_id, name)
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise JobError(str(exc)) from exc


def delete_group(store: Store, group_id: int) -> None:
    """그룹만 지운다 (들어 있던 작업은 그룹 없음). 이미 없는 그룹이면 아무 일도 하지 않는다."""
    store.delete_group(group_id)


def set_group(store: Store, job_ids: Iterable[int], group_id: int | None) -> None:
    """작업들을 그룹에 넣거나 (None) 뺀다. 그 사이 다른 화면에서 그룹을 지웠으면 JobError."""
    try:
        store.set_group(list(job_ids), group_id)
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise JobError(str(exc)) from exc


def continue_job(
    cfg: Config, store: Store, job_id: int, prompt: str, *, model: str | None = None, allow_env: bool | None = None,
) -> Launched:
    """작업의 세션과 작업 디렉토리를 그대로 이어받아 후속 요청을 실행한다.

    allow_env 를 주면 이번 실행부터 .env 읽기 허용 여부를 바꾼다 (None 이면 작업을 시작할 때 정한 그대로).

    작업이 아직 실행 중이면 요청을 대기시킨다: 지금 실행이 끝나는 대로 같은 세션에 이어서 전달된다.
    """
    prompt = prompt.strip()
    if not prompt:
        raise JobError("요청 내용이 비어 있습니다.")
    job = reconcile(store, store.get_job(job_id))
    if not job.is_terminal and store.queue_message(job.id, prompt):
        notes = ["이 요청에 준 --model 은 무시됩니다 (실행 중인 팀장이 이어서 처리)."] if model else []
        if allow_env is not None and allow_env != store.allow_env(job.id):
            store.set_allow_env(job.id, allow_env)  # 지금 실행에는 적용되지 않는다: 이 요청을 처리하는 다음 실행부터다
            notes.append(".env 읽기 허용 여부는 지금 실행이 끝난 뒤, 이 요청을 처리할 때부터 적용됩니다.")
        return Launched(job, notes, queued=True)
    job = store.get_job(job_id)  # 대기시키려는 사이에 끝났으면 바로 이어서 시킨다
    if not job.workdir or not _is_dir(job.workdir):
        raise JobError(f"작업 #{job.id} 의 작업 디렉토리가 없습니다 (정리된 작업). 새 작업으로 시작하세요.")
    warnings: list[str] = []
    # 고른 값이 Claude 기본 모델(claude)이면 이전 실행이 Claude 였을 때 그 모델을 그대로 쓴다.
    model = team.canonical(model or lead_model(cfg))
    switched = team.is_codex(model) != team.is_codex(job.model)
    check_codex(cfg, model)
    check_env_guard(cfg, model, store.allow_env(job.id) if allow_env is None else allow_env)
    check_team(cfg)
    if switched and job.session_id:
        # Claude 세션과 Codex 세션은 서로 이어받을 수 없다. 작업 디렉토리(코드 변경)는 그대로 쓴다.
        warnings.append("팀장이 Claude↔Codex 로 바뀌어 새 세션으로 시작합니다 (이전 대화 내용은 이어지지 않음).")
    elif not job.session_id:
        warnings.append("이전 세션 기록이 없어 새 세션으로 시작합니다 (이전 대화 내용은 이어지지 않음).")
    if job.result and not store.has_result_event(job.id):
        # 보고를 이벤트로 남기기 전에 끝난 작업: 이어서 실행하면 result 가 덮이므로 지금 타임라인에 옮겨 둔다.
        store.add_event(job.id, "팀장", RESULT, job.result)
    if not store.requeue_job(job.id, prompt):
        raise JobError(f"작업 #{job.id} 은(는) 이미 다른 곳에서 이어서 실행했습니다.")
    if model or switched:  # Codex → Claude 기본 모델이면 model 을 비운다
        store.update_job(job.id, model=model)
    if switched:
        store.update_job(job.id, session_id=None)
    if allow_env is not None:
        store.set_allow_env(job.id, allow_env)
    _spawn_worker(cfg, job.id)
    return Launched(store.get_job(job.id), warnings)


def lead_model(cfg: Config) -> str:
    """팀장에 적용되는 설정 값 (`claude`, `codex`, `claude:<모델>`, …)."""
    try:
        return team.lead_spec(team.load_models(cfg.models_path))
    except (OSError, ValueError) as exc:
        raise JobError(f"모델 설정을 읽을 수 없습니다: {exc}") from exc


def check_codex(cfg: Config, model: str | None) -> None:
    """에이전트와 모델이 안 맞거나 codex 실행 파일이 없으면 미리 알린다 (실행 중에 실패하는 것보다 낫다)."""
    if problem := team.mismatch(model):
        raise JobError(f"{problem} (바꾸기: coliday model)")
    if team.is_codex(model) and shutil.which(cfg.codex_bin) is None:
        raise JobError(f"Codex CLI 를 찾을 수 없습니다: {cfg.codex_bin}. 설치하거나 COLIDAY_CODEX_BIN 으로 경로를 지정하세요.")


def set_env_access(cfg: Config, store: Store, job_id: int, allow: bool) -> str:
    """사람이 작업 도중이나 끝난 뒤에 그 작업의 .env 읽기를 허용하거나 다시 막는다. 한 일을 설명하는 문장을 돌려준다.

    허용은 그 작업(세션)에만 해당한다. 실행 중인 에이전트의 권한은 바꿀 수 없으므로, 지금 실행이 .env 때문에
    멈추고 끝나면 같은 세션을 허용된 상태로 이어서 실행한다 (그렇지 않으면 이어서 실행하지 않는다: worker 가 판단).
    에이전트가 허용을 기다리며 멈춘 채 끝난 작업은 바로 이어서 시작한다.
    """
    job = reconcile(store, store.get_job(job_id))
    if store.allow_env(job.id) == allow:
        return f"작업 #{job.id} 은(는) 이미 .env 읽기를 {'허용한' if allow else '막은'} 상태입니다."
    if not allow:
        store.set_allow_env(job.id, False)
        store.add_event(job.id, USER, "phase", "사람이 .env 읽기를 다시 막았습니다")
        when = "다음에 이어서 시킬 때부터" if job.is_terminal else "지금 실행이 끝난 뒤 다음 실행부터"
        return f"작업 #{job.id}: {when} .env 를 읽지 못하게 막습니다."
    store.add_event(job.id, USER, "phase", "사람이 이 작업에서 .env 읽기를 허용했습니다")
    if not job.is_terminal:
        store.set_allow_env(job.id, True)
        if store.queue_message(job.id, guard.ALLOWED_NOTE):
            return (
                f"작업 #{job.id}: .env 읽기를 허용했습니다. 지금 실행이 .env 때문에 멈추면 허용된 상태로 이어서 진행하고, "
                "그렇지 않으면 다음에 이어서 시킬 때부터 적용됩니다."
            )
        job = store.get_job(job.id)  # 그 사이에 끝났다
    waiting = guard.NEED_MARKER in (job.result or "")
    if waiting:
        continue_job(cfg, store, job.id, guard.ALLOWED_NOTE, allow_env=True)
        return f"작업 #{job.id}: .env 읽기를 허용하고 이어서 시작했습니다."
    store.set_allow_env(job.id, True)
    return f"작업 #{job.id}: .env 읽기를 허용했습니다. 다음에 이어서 시킬 때부터 적용됩니다."


def check_env_guard(cfg: Config, model: str | None, allow_env: bool) -> None:
    """.env 읽기를 막을 수 있는 환경인지 작업을 만들기 전에 확인한다. 막을 수 없으면 막지 않은 채 돌리지 않고 알린다."""
    if allow_env:
        return
    codex_lead = team.is_codex(model)
    binary = cfg.codex_bin if codex_lead else cfg.claude_bin
    name, minimum = ("codex", guard.MIN_CODEX) if codex_lead else ("claude", guard.MIN_CLAUDE)
    version = guard.local_version(binary)
    if version is None or version < minimum:
        raise JobError(
            f"이 컴퓨터의 {name}({guard.show(version)})는 .env 읽기를 막지 못합니다. {guard.show(minimum)} 이상으로 올리세요 "
            f"(예: {name} update). {guard.HOW_TO_ALLOW}."
        )
    if codex_lead:
        return
    # Claude 의 샌드박스: macOS 는 기본으로 있고, 리눅스는 bubblewrap 과 socat 이 있어야 한다.
    missing = [t for t in ("bwrap", "socat") if shutil.which(t) is None] if sys.platform.startswith("linux") else []
    if missing:
        raise JobError(
            f"이 컴퓨터에 {', '.join(missing)} 이(가) 없어 .env 읽기를 막을 수 없습니다. 설치하세요 "
            f"(예: sudo apt install bubblewrap socat). {guard.HOW_TO_ALLOW}."
        )


def _remote(func, *args):
    """서버 접속 오류를 사용자에게 보여 줄 오류로 바꾼다."""
    try:
        return func(*args)
    except remote.RemoteError as exc:
        raise JobError(str(exc)) from exc


def _location(directory: str | Path) -> str:
    """사용자가 준 위치를 정규화하고 실제로 있는지 확인한다."""
    try:
        target = projects.normalize(directory)
    except remote.RemoteError as exc:
        raise JobError(str(exc)) from exc
    if not _is_dir(target):
        raise JobError(f"디렉토리가 없습니다: {target}")
    return target


def _is_dir(location: str) -> bool:
    if remote.is_remote(location):
        return bool(_remote(remote.is_dir, location))
    return Path(location).is_dir()


def check_team(cfg: Config) -> None:
    """팀 설정(team.json, agents.json)이 잘못됐으면 작업을 만들기 전에 알린다."""
    try:
        team.load_agents(cfg.agents_override, team.load_custom(cfg.team_path))
    except (OSError, ValueError) as exc:
        raise JobError(f"팀 설정이 잘못됐습니다: {exc} (바꾸기: coliday team)") from exc


def set_model(cfg: Config, role: str, model: str | None) -> None:
    check_codex(cfg, model)
    try:
        agents = team.load_agents(cfg.agents_override, team.load_custom(cfg.team_path))
        team.save_models(cfg.models_path, team.with_model(team.load_models(cfg.models_path), agents, role, model))
    except (OSError, ValueError) as exc:
        raise JobError(str(exc)) from exc


def model_rows(cfg: Config) -> list[tuple[str, str, bool]]:
    try:
        return team.model_rows(
            team.load_agents(cfg.agents_override, team.load_custom(cfg.team_path)), team.load_models(cfg.models_path)
        )
    except (OSError, ValueError) as exc:
        raise JobError(f"모델 설정을 읽을 수 없습니다: {exc}") from exc


def _project_entries(cfg: Config, store: Store) -> dict[str, str | None]:
    """경로 → 붙인 이름. 목록 파일이 아직 없으면 기존 작업의 레포로 한 번 채운다."""
    try:
        if not cfg.projects_path.exists():
            seen = [j.repo for j in reversed(store.list_jobs(200)) if projects.exists(j.repo)]
            projects.save(cfg.projects_path, dict.fromkeys(seen))
        return projects.load(cfg.projects_path)
    except (OSError, ValueError) as exc:
        raise JobError(f"프로젝트 목록을 읽을 수 없습니다: {exc}") from exc


def list_projects(cfg: Config, store: Store) -> list[str]:
    """연결된 프로젝트 디렉토리."""
    return list(_project_entries(cfg, store))


def project_names(cfg: Config, store: Store) -> dict[str, str]:
    """경로 → 표시 이름 (붙인 이름이 없으면 폴더 이름)."""
    return {p: n or remote.name(p) for p, n in _project_entries(cfg, store).items()}


def add_project(
    cfg: Config, store: Store, directory: str | Path, name: str | None = None, password: str | None = None,
) -> str:
    """목록에 추가하고 정규화한 경로를 돌려준다.

    name 이 None 이면 이름을 건드리지 않고(이미 있으면 그대로), 빈 문자열이면 폴더 이름으로 되돌린다.
    password 는 서버의 프로젝트에만 쓴다: 키로 접속되지 않으면 이 비밀번호로 한 번 접속해 SSH 키를 등록한다 (저장하지 않는다).
    """
    host = remote.split(str(directory).strip())[0]
    if password and host is not None:
        try:
            if not remote.key_works(host):  # 이미 키로 접속되면 서버를 건드리지 않는다
                remote.install_key(host, password)
        except remote.RemoteError as exc:
            raise JobError(str(exc)) from exc
    items = _project_entries(cfg, store)
    # 이미 연결된 항목은 이름만 바꾸는 것이다: 다시 확인하지 않는다 (서버에 접속하지 못해도 이름은 바꿀 수 있게).
    path = str(directory) if str(directory) in items else _location(directory)
    updated = dict(items)
    updated[path] = items.get(path) if name is None else (" ".join(name.split()) or None)
    if list(updated.items()) != list(items.items()):
        try:
            projects.save(cfg.projects_path, updated)
        except OSError as exc:
            raise JobError(f"프로젝트 목록을 저장할 수 없습니다: {exc}") from exc
    return path


def remove_project(cfg: Config, store: Store, directory: str | Path) -> bool:
    """목록에서만 뺀다 (디렉토리와 작업은 건드리지 않는다). 목록에 없었으면 False."""
    try:
        path = projects.normalize(directory)
    except remote.RemoteError:  # 서버에 접속하지 못해도 목록에서는 뺄 수 있어야 한다
        path = str(directory)
    items = _project_entries(cfg, store)
    if path not in items:
        return False
    try:
        projects.save(cfg.projects_path, {p: n for p, n in items.items() if p != path})
    except OSError as exc:
        raise JobError(f"프로젝트 목록을 저장할 수 없습니다: {exc}") from exc
    return True


OTHER_DIR = "_other"  # 하위 폴더에 있던 사진·영상이 아닌 파일(찍는 데 쓴 HTML·스크립트·로그 등)을 모아 두는 곳


def artifacts(cfg: Config, job_id: int) -> list[Path]:
    """작업이 남긴 결과물 파일 (스크린샷·영상, 사용자가 확인할 문서 등). 숨김 파일과 `_other/` 는 뺀다. 파일을 옮기지는 않는다."""
    root = cfg.artifacts_dir(job_id)
    if not root.is_dir():
        return []
    try:
        return sorted(
            p for p in root.rglob("*")
            if p.is_file() and not _skipped(p.relative_to(root).parts)
        )
    except OSError:  # 팀원이 정리하는 중에 읽은 경우
        return []


def _skipped(parts: tuple[str, ...]) -> bool:
    return parts[0] == OTHER_DIR or any(part.startswith(".") for part in parts)


def is_media(path: Path) -> bool:
    return path.suffix.lower() in _IMAGE_SUFFIXES | _VIDEO_SUFFIXES


def organize(root: Path) -> None:
    """결과물 폴더를 사용자가 열었을 때 한눈에 보이게 정리한다. 실행이 끝난 뒤에만 부른다.

    - 사진·영상은 하위 폴더에 있어도 root 바로 아래로 꺼낸다 (`verify/wide-ok.png` → `verify-wide-ok.png`).
    - 맨 위에 둔 나머지 파일(사용자가 확인할 문서·데이터)은 그대로 둔다.
    - 하위 폴더에 있는 나머지 파일(찍는 데 쓴 HTML·스크립트·로그)은 지우지 않고 `_other/` 아래로 옮긴다.
    팀원이 작업 공간으로 쓰는 동안 옮기면 그 작업이 깨지므로 실행 중에는 건드리지 않는다.
    """
    try:
        files = [p for p in root.rglob("*") if p.is_file()]
    except OSError:
        return
    for path in sorted(files):
        parts = path.relative_to(root).parts
        if _skipped(parts) or len(parts) == 1:
            continue
        if is_media(path):
            target = root / "-".join(parts)
        else:
            target = root / OTHER_DIR / Path(*parts)
        stem, suffix, n = target.stem, target.suffix, 2
        while target.exists():  # 같은 이름이 이미 있으면 번호를 붙인다
            target = target.with_name(f"{stem}-{n}{suffix}")
            n += 1
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            path.rename(target)
        except OSError:
            continue
    for folder in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            folder.rmdir()  # 비어 있을 때만 지워진다
        except OSError:
            pass


def artifacts_summary(files: list[Path]) -> str:
    """'3개 (사진 2, 영상 1)', '파일 2개' 같은 한 줄 요약."""
    images = sum(p.suffix.lower() in _IMAGE_SUFFIXES for p in files)
    videos = sum(p.suffix.lower() in _VIDEO_SUFFIXES for p in files)
    kinds = [f"{label} {n}" for label, n in (("사진", images), ("영상", videos)) if n]
    other = len(files) - images - videos
    if not kinds:
        return f"파일 {other}개" if other else "사진·영상 없음"
    return f"{images + videos}개 ({', '.join(kinds)})" + (f" + 기타 파일 {other}개" if other else "")


def open_artifacts(cfg: Config, store: Store, job_id: int) -> Path:
    """결과물 폴더를 파일 탐색기(Finder)로 연다. 아직 비어 있어도 연다 (나중에 생긴 파일을 그 창에서 볼 수 있다).

    끝난 작업이면 열기 전에 정리한다 (이 기능이 생기기 전에 끝난 작업도 열 때 정리된다).
    """
    root = cfg.artifacts_dir(job_id)
    try:
        root.mkdir(parents=True, exist_ok=True)
        if reconcile(store, store.get_job(job_id)).is_terminal:
            organize(root)
    except OSError as exc:
        raise JobError(f"폴더를 열 수 없습니다: {exc}\n직접 여세요: {root}") from exc
    return open_folder(cfg, root)


def open_folder(cfg: Config, path: Path) -> Path:
    """path 가 폴더면 그 폴더를, 파일이면 그 파일이 든 폴더를 파일 탐색기로 연다. 연 폴더를 돌려준다.

    파일을 실행하거나 앱으로 열지는 않는다 (경로는 에이전트의 보고에서 온다). macOS 의 `open` 은 `.app` 같은
    묶음 폴더를 실행하므로, 파일과 이름에 확장자가 붙은 폴더는 Finder 에서 위치만 보여 준다 (`open -R`).
    """
    path = Path(path)
    if not path.exists():
        raise JobError(f"없는 경로입니다: {path}")
    folder = path if path.is_dir() else path.parent
    cmd = [cfg.open_bin, str(folder)]
    if sys.platform == "darwin" and Path(cfg.open_bin).name == "open" and (folder != path or folder.suffix):
        cmd = [cfg.open_bin, "-R", str(path)]
        folder = path.parent
    try:
        subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError as exc:
        raise JobError(f"폴더를 열 수 없습니다 ({cfg.open_bin}): {exc}\n직접 여세요: {folder}") from exc
    return folder


def local_workdir(cfg: Config, job: Job) -> str:
    """에이전트가 이 컴퓨터에서 일한 폴더. 서버의 프로젝트면 작업별 폴더다 (서버에서 가져온 파일이 있다)."""
    if remote.is_remote(job.workdir or job.repo):
        return str(cfg.job_dir(job.id) / "work")
    return job.workdir or job.repo


# 이미 링크인 곳 | `코드` | 맨 경로(/…, ~/…, ./…, ../…). URL 의 // 와 단어 사이의 / 는 경로로 보지 않는다.
_PATH_TOKEN = re.compile(
    r"(!?\[[^\]\n]*\]\([^)\n]*\))|`([^`\n]+)`|(?<![\w/.~@:-])((?:~|\.{1,2})?/[^\s`<>\"'|*\[\]()]+)"
)
_PATH_TAIL = re.compile(r"(:\d+(?::\d+)?|[^\x00-\x7f]|[.,:;])$")  # 줄 번호, 붙여 쓴 조사, 문장 부호


_SEARCH_SKIP = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", ".gradle"})
_SEARCH_LIMIT = 20000  # 잘못 적힌 경로의 실제 위치를 찾을 때 훑는 항목 수의 상한 (화면이 멈추지 않게)


class _Finder:
    """보고에 적힌 경로가 없을 때 같은 이름의 파일·폴더를 작업 폴더 아래에서 찾는다.

    에이전트가 중간 폴더를 빼먹고 적는 일이 있다 (`out/a.csv` 라고 적었는데 실제로는 `out/_output/a.csv`).
    이름이 같은 것이 하나뿐일 때만 알려 준다. 처음 필요할 때 한 번만 훑는다.
    """

    def __init__(self, roots: list[str]) -> None:
        self._roots = roots
        self._names: dict[str, list[Path]] | None = None

    def find(self, text: str) -> Path | None:
        name = Path(_PATH_TAIL.sub("", text.rstrip().rstrip("/"))).name
        if not name or name in (".", ".."):
            return None
        if self._names is None:
            self._names = self._scan()
        found = self._names.get(name, [])
        return found[0] if len(found) == 1 else None

    def _scan(self) -> dict[str, list[Path]]:
        names: dict[str, list[Path]] = {}
        seen = 0
        for root in self._roots:
            for folder, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if d not in _SEARCH_SKIP]
                for name in (*dirs, *files):
                    names.setdefault(name, []).append(Path(folder) / name)
                seen += len(dirs) + len(files)
                if seen > _SEARCH_LIMIT:
                    return {}  # 다 훑지 못했으면 '하나뿐'인지 알 수 없다
        return names


def link_paths(markdown: str, workdir: str, within: str | None = None, search: list[str] | tuple[str, ...] = ()) -> str:
    """보고(마크다운)에 적힌 경로 가운데 이 컴퓨터에 실제로 있는 것을 링크로 바꾼다 (누르면 폴더를 연다).

    상대 경로는 `코드` 로 적힌 것만 workdir 기준으로 본다. within 을 주면 그 아래의 경로만 바꾼다
    (서버의 프로젝트: 보고에 적힌 서버의 경로가 이 컴퓨터에도 우연히 있을 수 있다). 코드 블록 안은 건드리지 않는다.
    경로처럼 적혔는데 없는 것은 workdir 와 search 아래에서 같은 이름을 찾아, 하나뿐이면 실제 위치를 옆에 링크로 붙인다.
    """
    finder = _Finder([root for root in (workdir, *search) if root and os.path.isdir(root)])
    out, fenced = [], False
    for line in markdown.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and not line.startswith("    "):
            line = _PATH_TOKEN.sub(lambda m: _linked(m, workdir, within, finder), line)
        out.append(line)
    return "\n".join(out)


def _linked(match: re.Match, workdir: str, within: str | None, finder: _Finder) -> str:
    link, code, bare = match.groups()
    if link:
        return link
    text = code or bare
    found = _existing(text, workdir, within, relative=bool(code))
    if not found:
        # 경로처럼 생긴 것만 찾아본다 (`git status`, `main`, URL 은 경로가 아니다)
        looks_like_path = "/" in text.strip("/") and " " not in text.strip() and "://" not in text
        actual = finder.find(text) if looks_like_path else None
        if actual is None:
            return match.group(0)
        shown = os.path.relpath(actual, workdir) if actual.is_relative_to(workdir) else str(actual)
        return f"{match.group(0)} (실제 위치: [`{shown}`]({quote(str(actual), safe='/')}))"
    path, length = found
    href = quote(str(path), safe="/")
    if code:  # 줄 번호가 붙어 있어도 코드 전체를 링크 글자로 둔다
        return f"[`{code}`]({href})"
    return f"[{bare[:length]}]({href}){bare[length:]}"


def _existing(text: str, workdir: str, within: str | None, *, relative: bool) -> tuple[Path, int] | None:
    """text 의 앞부분이 가리키는 실제 경로와 그 길이. 뒤에 붙은 줄 번호·조사·문장 부호는 떼어 가며 찾는다."""
    text = text.rstrip()
    for _ in range(8):
        if not text.strip("/~. "):
            return None
        path = Path(text).expanduser()
        if not path.is_absolute():
            if not relative and not text.startswith((".", "~")):
                return None
            path = Path(workdir) / path
        try:
            path = Path(os.path.abspath(path))
            inside = within is None or path.is_relative_to(within)
            if inside and path.exists() and path != Path(path.anchor):
                return path, len(text)
        except (OSError, ValueError):
            return None
        shorter = _PATH_TAIL.sub("", text)
        if shorter == text:
            return None
        text = shorter
    return None


def role_prompts(cfg: Config, role: str | None = None) -> list[tuple[str, str]]:
    try:
        custom = team.load_custom(cfg.team_path)
        return team.role_prompts(
            team.load_agents(cfg.agents_override, custom), team.load_models(cfg.models_path), role, cfg.max_attempts,
            custom.lead_prompt,
        )
    except (OSError, ValueError) as exc:
        raise JobError(str(exc)) from exc


def _team(cfg: Config) -> tuple[dict[str, dict], team.Custom]:
    return team.base_agents(cfg.agents_override), team.load_custom(cfg.team_path)


def team_roles(cfg: Config) -> list[tuple[str, str, str]]:
    """(역할, 상태, 설명) 목록. 첫 줄은 팀장. 뺀 기본 역할도 되돌릴 수 있게 나온다."""
    try:
        base, custom = _team(cfg)
        agents = team.merge_agents(base, custom)
    except (OSError, ValueError) as exc:
        raise JobError(f"팀 설정을 읽을 수 없습니다: {exc}") from exc
    return [
        (role, state, "팀원에게 일을 나눠 맡기고 최종 보고한다." if role == team.LEAD_ROLE
         else str((agents.get(role) or base.get(role) or {}).get("description") or ""))
        for role, state in team.role_states(base, custom)
    ]


def role_definition(cfg: Config, role: str) -> dict:
    """고칠 수 있는 원문: 팀장은 {"prompt"}, 팀원은 {"description", "prompt", "tools"} (tools 가 None 이면 모든 도구).

    실행할 때 자동으로 붙는 규칙(재시도, 결과물)은 들어 있지 않다.
    """
    try:
        base, custom = _team(cfg)
        if role == team.LEAD_ROLE:
            return {"prompt": team.lead_template(custom).strip()}
        agents = team.merge_agents(base, custom)
    except (OSError, ValueError) as exc:
        raise JobError(f"팀 설정을 읽을 수 없습니다: {exc}") from exc
    spec = agents.get(role) or base.get(role)  # 뺀 기본 역할도 원래 정의를 볼 수 있다
    if not isinstance(spec, dict):
        raise JobError(f"알 수 없는 역할: {role} (가능: {', '.join([team.LEAD_ROLE, *agents])})")
    tools = spec.get("tools")
    return {
        "description": str(spec.get("description") or ""),
        "prompt": str(spec.get("prompt") or "").strip(),
        "tools": list(tools) if isinstance(tools, list) else None,
    }


def set_role(
    cfg: Config, role: str, *, description: str | None = None, prompt: str | None = None, tools: object = team.KEEP,
) -> None:
    """역할을 고치거나 새 팀원을 넣는다 (다음 실행부터 적용). 주지 않은 항목은 그대로 둔다. lead 는 prompt 만 받는다."""
    try:
        base, custom = _team(cfg)
        if role == team.LEAD_ROLE:
            if description is not None or tools is not team.KEEP:
                raise ValueError("팀장(lead)은 지시문(prompt)만 바꿀 수 있습니다")
            if prompt is None:
                raise ValueError("바꿀 지시문을 주세요")
            updated = team.with_lead_prompt(custom, prompt)
        else:
            updated = team.with_member(custom, base, role, description=description, prompt=prompt, tools=tools)
        team.save_custom(cfg.team_path, updated)
    except (OSError, ValueError) as exc:
        raise JobError(str(exc)) from exc


def remove_role(cfg: Config, role: str) -> None:
    try:
        base, custom = _team(cfg)
        team.save_custom(cfg.team_path, team.without_member(custom, base, role))
    except (OSError, ValueError) as exc:
        raise JobError(str(exc)) from exc


def reset_role(cfg: Config, role: str | None) -> None:
    """역할 하나(또는 None 이면 전부)를 기본값으로 되돌린다."""
    try:
        base, custom = _team(cfg)
        team.save_custom(cfg.team_path, team.reset_role(custom, base, role))
    except (OSError, ValueError) as exc:
        raise JobError(str(exc)) from exc


def _spawn_worker(cfg: Config, job_id: int) -> None:
    job_dir = cfg.job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    with (job_dir / "worker.log").open("a") as log:  # 이어서 실행해도 이전 로그를 남긴다
        subprocess.Popen(
            [sys.executable, "-m", "coliday", "_worker", str(job_id)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
        )


def worktree_path(cfg: Config, job: Job) -> str:
    """작업의 worktree 위치. 서버의 레포면 그 서버의 `~/.coliday/worktrees` 아래다.

    폴더 이름 `{레포 이름}-{작업 번호}` 는 scope.location 이 작업의 소속 프로젝트를 계산하는 데 쓴다 (바꾸면 함께 바꾼다).
    """
    folder = f"{remote.name(job.repo)}-{job.id}"
    host = remote.split(job.repo)[0]
    if host is not None:
        return f"{host}:{remote.home(host)}/.coliday/worktrees/{folder}"
    return str(cfg.worktrees_dir / folder)


def cancel(store: Store, job: Job) -> bool:
    """취소했으면 True, 이미 끝난 작업이면 False."""
    job = reconcile(store, job)
    if job.is_terminal:
        return False
    store.finish_job(job.id, "cancelled", phase="사용자가 취소함")
    store.clear_queued(job.id)  # 취소한 작업에 대기시킨 요청은 전달하지 않는다
    if job.pid and alive(job.pid):
        try:
            os.killpg(job.pid, signal.SIGTERM)  # worker 는 자기 세션의 리더라 pgid == pid
        except ProcessLookupError:
            pass
    return True


REMOTE_JOBS = '"$HOME"/.coliday/jobs'  # 예전 버전이 서버에 두던 작업 폴더 (에이전트를 서버에서 돌리던 때). 지울 때만 쓴다


def remove(cfg: Config, store: Store, job: Job, *, force: bool = False) -> str:
    """끝난 작업을 목록에서 지운다. worktree·브랜치가 있으면 함께 지우고, 그게 실패하면 작업도 남긴다."""
    job = reconcile(store, job)
    if not job.is_terminal:
        raise JobError("실행 중인 작업입니다. 먼저 취소하세요.")
    removed_worktree = False
    note = ""
    host = remote.split(job.workdir or job.repo)[0]
    if job.branch:  # worktree 없이 실행한 작업은 기록만 지운다 (프로젝트 디렉토리는 건드리지 않는다)
        try:
            wt = worktree_path(cfg, job)
            if _is_dir(wt):
                gitops.remove_worktree(job.repo, wt, job.branch, force)
                removed_worktree = True
        except (gitops.GitError, JobError, remote.RemoteError) as exc:
            if host is None or not force:
                raise WorktreeRemoveError(f"삭제 실패 (검토하지 않은 변경이 있으면 --force 필요): {exc}") from exc
            # 없어진 서버의 작업도 목록에서는 지울 수 있어야 한다. 강제 삭제면 기록만 지운다.
            note = f" 서버의 worktree 와 브랜치는 지우지 못했습니다: {exc}"
    children = store.children(job.id)
    meta = store.job_meta().get(job.id)
    if not store.delete_job(job.id):
        raise JobError(f"작업 #{job.id} 은(는) 다시 실행 중이라 삭제하지 않았습니다.")
    if children:
        moved = ", ".join(f"#{c}" for c in children)
        where = f"#{meta.parent_id} 밑으로" if meta and meta.parent_id is not None else "최상위로"
        note += f" 하위 작업 {moved} 은(는) {where} 옮겼습니다."
    shutil.rmtree(cfg.job_dir(job.id), ignore_errors=True)
    if host is not None:
        remote.spawn(host, f"rm -rf {REMOTE_JOBS}/{job.id}")  # 예전 버전이 서버에 남긴 것
    return f"작업 #{job.id} 을(를) 삭제했습니다" + (f" (worktree 와 브랜치 {job.branch} 포함)." if removed_worktree else ".") + note


def reconcile(store: Store, job: Job) -> Job:
    """worker 가 비정상 종료돼 running/queued 로 남은 작업을 failed 로 정리한다.

    job 은 조금 전에 읽은 것일 수 있다. 그 사이 worker 가 시작했거나 다시 실행됐으면 바꾸지 않는다 (읽은 상태·pid 가 같을 때만).
    """
    if job.status == "running" and job.pid and not alive(job.pid):
        store.finish_job(job.id, "failed", unchanged=job, phase="worker 비정상 종료")
        return store.get_job(job.id)
    if job.status == "queued" and time.time() - _queued_at(store, job) > _QUEUED_TIMEOUT_SEC:
        store.finish_job(job.id, "failed", unchanged=job, phase="worker 시작 실패")
        return store.get_job(job.id)
    return job


def _queued_at(store: Store, job: Job) -> float:
    """이어서 시킨 작업은 만든 시각이 아니라 마지막 후속 요청 시각부터 대기한 것이다."""
    follow_up = store.last_follow_up(job.id)
    return follow_up.ts if follow_up else job.created_at


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def ago(sec: float) -> str:
    if sec < 60:
        return f"{int(sec)}s"
    if sec < 3600:
        return f"{int(sec // 60)}m"
    if sec < 86400:
        return f"{int(sec // 3600)}h"
    return f"{int(sec // 86400)}d"


def one_line(text: str | None, width: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"
