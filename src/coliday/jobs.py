"""작업 시작·취소·정리. CLI 와 TUI 가 함께 쓴다."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from coliday import gitops, projects, team
from coliday.config import Config
from coliday.store import RESULT, Job, Store

STATUS_ICON = {"queued": "…", "running": "●", "succeeded": "✓", "failed": "✗", "cancelled": "■"}
_QUEUED_TIMEOUT_SEC = 60


class JobError(Exception):
    """사용자에게 그대로 보여줄 수 있는 작업 오류."""


class WorktreeRemoveError(JobError):
    """worktree 를 지우지 못함 (보통 검토하지 않은 변경이 남아 있어서). force 로 다시 시도할 수 있다."""


@dataclass(frozen=True)
class Launched:
    job: Job
    warnings: list[str] = field(default_factory=list)


def launch(
    cfg: Config, store: Store, prompt: str, directory: str | Path,
    *, no_worktree: bool = False, model: str | None = None,
) -> Launched:
    prompt = prompt.strip()
    if not prompt:
        raise JobError("요청 내용이 비어 있습니다.")
    target = Path(directory).expanduser().resolve()
    if not target.is_dir():
        raise JobError(f"디렉토리가 없습니다: {target}")
    root = gitops.repo_root(target)
    warnings: list[str] = []
    model = team.canonical(model or lead_model(cfg))
    check_codex(cfg, model)
    try:
        list_projects(cfg, store)  # 이 작업을 만들기 전에 기존 작업으로 목록을 채워 둔다
    except JobError:
        pass

    if no_worktree or root is None:
        if root is None and not no_worktree:
            raise JobError("git 저장소가 아닙니다. 이 디렉토리에서 직접 작업하려면 worktree 없이 실행하세요 (--no-worktree).")
        job = store.create_job(prompt=prompt, repo=str(root or target), workdir=str(target), model=model)
    else:
        if gitops.is_dirty(root):
            warnings.append("커밋하지 않은 변경은 worktree 에 포함되지 않습니다 (HEAD 기준으로 작업).")
        base = gitops.head_commit(root)
        job = store.create_job(prompt=prompt, repo=str(root), workdir="", model=model)
        branch = f"coliday/{job.id}"
        wt = worktree_path(cfg, job)
        try:
            gitops.add_worktree(root, wt, branch, base)
        except gitops.GitError as exc:
            store.finish_job(job.id, "failed", phase=f"worktree 생성 실패: {exc}")
            raise JobError(f"worktree 생성 실패: {exc}") from exc
        # 하위 디렉토리에서 실행했다면 worktree 안의 같은 위치에서 작업한다.
        store.update_job(job.id, workdir=str(wt / target.relative_to(root)), branch=branch, base_commit=base)
        job = store.get_job(job.id)

    _spawn_worker(cfg, job.id)
    try:
        add_project(cfg, store, target)
    except JobError as exc:  # 목록 파일이 깨져도 작업은 이미 시작됐다
        warnings.append(f"프로젝트 목록에 추가하지 못했습니다: {exc}")
    return Launched(job, warnings)


def continue_job(cfg: Config, store: Store, job_id: int, prompt: str, *, model: str | None = None) -> Launched:
    """끝난 작업의 세션과 작업 디렉토리를 그대로 이어받아 후속 요청을 실행한다."""
    prompt = prompt.strip()
    if not prompt:
        raise JobError("요청 내용이 비어 있습니다.")
    job = reconcile(store, store.get_job(job_id))
    if not job.is_terminal:
        raise JobError(f"작업 #{job.id} 은(는) 아직 실행 중입니다. 끝난 뒤에 이어서 시킬 수 있습니다.")
    if not job.workdir or not Path(job.workdir).is_dir():
        raise JobError(f"작업 #{job.id} 의 작업 디렉토리가 없습니다 (정리된 작업). 새 작업으로 시작하세요.")
    warnings: list[str] = []
    # 고른 값이 Claude 기본 모델(claude)이면 이전 실행이 Claude 였을 때 그 모델을 그대로 쓴다.
    model = team.canonical(model or lead_model(cfg))
    switched = team.is_codex(model) != team.is_codex(job.model)
    check_codex(cfg, model)
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
    _spawn_worker(cfg, job.id)
    return Launched(store.get_job(job.id), warnings)


def lead_model(cfg: Config) -> str:
    """팀장에 적용되는 설정 값 (`claude`, `codex`, `claude:<모델>`, …)."""
    try:
        return team.lead_spec(team.load_models(cfg.models_path))
    except (OSError, ValueError) as exc:
        raise JobError(f"모델 설정을 읽을 수 없습니다: {exc}") from exc


def check_codex(cfg: Config, model: str | None) -> None:
    """Codex 모델을 고르는데 codex 실행 파일이 없으면 미리 알린다 (실행 중에 팀원이 실패하는 것보다 낫다)."""
    if team.is_codex(model) and shutil.which(cfg.codex_bin) is None:
        raise JobError(f"Codex CLI 를 찾을 수 없습니다: {cfg.codex_bin}. 설치하거나 COLIDAY_CODEX_BIN 으로 경로를 지정하세요.")


def set_model(cfg: Config, role: str, model: str | None) -> None:
    check_codex(cfg, model)
    try:
        agents = team.load_agents(cfg.agents_override)
        team.save_models(cfg.models_path, team.with_model(team.load_models(cfg.models_path), agents, role, model))
    except (OSError, ValueError) as exc:
        raise JobError(str(exc)) from exc


def model_rows(cfg: Config) -> list[tuple[str, str, bool]]:
    try:
        return team.model_rows(team.load_agents(cfg.agents_override), team.load_models(cfg.models_path))
    except (OSError, ValueError) as exc:
        raise JobError(f"모델 설정을 읽을 수 없습니다: {exc}") from exc


def _project_entries(cfg: Config, store: Store) -> dict[str, str | None]:
    """경로 → 붙인 이름. 목록 파일이 아직 없으면 기존 작업의 레포로 한 번 채운다."""
    try:
        if not cfg.projects_path.exists():
            seen = [j.repo for j in reversed(store.list_jobs(200)) if Path(j.repo).is_dir()]
            projects.save(cfg.projects_path, dict.fromkeys(seen))
        return projects.load(cfg.projects_path)
    except (OSError, ValueError) as exc:
        raise JobError(f"프로젝트 목록을 읽을 수 없습니다: {exc}") from exc


def list_projects(cfg: Config, store: Store) -> list[str]:
    """연결된 프로젝트 디렉토리."""
    return list(_project_entries(cfg, store))


def project_names(cfg: Config, store: Store) -> dict[str, str]:
    """경로 → 표시 이름 (붙인 이름이 없으면 폴더 이름)."""
    return {p: n or Path(p).name for p, n in _project_entries(cfg, store).items()}


def add_project(cfg: Config, store: Store, directory: str | Path, name: str | None = None) -> str:
    """목록에 추가하고 정규화한 경로를 돌려준다.

    name 이 None 이면 이름을 건드리지 않고(이미 있으면 그대로), 빈 문자열이면 폴더 이름으로 되돌린다.
    """
    path = projects.normalize(directory)
    if not Path(path).is_dir():
        raise JobError(f"디렉토리가 없습니다: {path}")
    items = _project_entries(cfg, store)
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
    path = projects.normalize(directory)
    items = _project_entries(cfg, store)
    if path not in items:
        return False
    try:
        projects.save(cfg.projects_path, {p: n for p, n in items.items() if p != path})
    except OSError as exc:
        raise JobError(f"프로젝트 목록을 저장할 수 없습니다: {exc}") from exc
    return True


def role_prompts(cfg: Config, role: str | None = None) -> list[tuple[str, str]]:
    try:
        return team.role_prompts(
            team.load_agents(cfg.agents_override), team.load_models(cfg.models_path), role, cfg.max_attempts
        )
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


def worktree_path(cfg: Config, job: Job) -> Path:
    return cfg.worktrees_dir / f"{Path(job.repo).name}-{job.id}"


def cancel(store: Store, job: Job) -> bool:
    """취소했으면 True, 이미 끝난 작업이면 False."""
    job = reconcile(store, job)
    if job.is_terminal:
        return False
    store.finish_job(job.id, "cancelled", phase="사용자가 취소함")
    if job.pid and alive(job.pid):
        try:
            os.killpg(job.pid, signal.SIGTERM)  # worker 는 자기 세션의 리더라 pgid == pid
        except ProcessLookupError:
            pass
    return True


def remove(cfg: Config, store: Store, job: Job, *, force: bool = False) -> str:
    """끝난 작업을 목록에서 지운다. worktree·브랜치가 있으면 함께 지우고, 그게 실패하면 작업도 남긴다."""
    job = reconcile(store, job)
    if not job.is_terminal:
        raise JobError("실행 중인 작업입니다. 먼저 취소하세요.")
    removed_worktree = False
    if job.branch:  # worktree 없이 실행한 작업은 기록만 지운다 (프로젝트 디렉토리는 건드리지 않는다)
        wt = worktree_path(cfg, job)
        if wt.exists():
            try:
                gitops.remove_worktree(Path(job.repo), wt, job.branch, force)
            except gitops.GitError as exc:
                raise WorktreeRemoveError(f"삭제 실패 (검토하지 않은 변경이 있으면 --force 필요): {exc}") from exc
            removed_worktree = True
    if not store.delete_job(job.id):
        raise JobError(f"작업 #{job.id} 은(는) 다시 실행 중이라 삭제하지 않았습니다.")
    shutil.rmtree(cfg.job_dir(job.id), ignore_errors=True)
    return f"작업 #{job.id} 을(를) 삭제했습니다" + (f" (worktree 와 브랜치 {job.branch} 포함)." if removed_worktree else ".")


def reconcile(store: Store, job: Job) -> Job:
    """worker 가 비정상 종료돼 running/queued 로 남은 작업을 failed 로 정리한다."""
    if job.status == "running" and job.pid and not alive(job.pid):
        store.finish_job(job.id, "failed", phase="worker 비정상 종료")
        return store.get_job(job.id)
    if job.status == "queued" and time.time() - _queued_at(store, job) > _QUEUED_TIMEOUT_SEC:
        store.finish_job(job.id, "failed", phase="worker 시작 실패")
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
