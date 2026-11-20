"""경로와 실행 설정. 모두 환경변수로 덮어쓸 수 있다."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    home: Path
    claude_bin: str
    permission_mode: str
    codex_bin: str = "codex"
    max_attempts: int = 5  # 실패한 작업을 처음 포함해 몇 번까지 시도할지 (팀장·팀원 지침에 들어간다)
    open_bin: str = "open"  # 폴더를 파일 탐색기로 여는 명령

    @property
    def db_path(self) -> Path:
        return self.home / "jobs.db"

    @property
    def jobs_dir(self) -> Path:
        return self.home / "jobs"

    @property
    def worktrees_dir(self) -> Path:
        return self.home / "worktrees"

    @property
    def agents_override(self) -> Path:
        return self.home / "agents.json"

    @property
    def models_path(self) -> Path:
        return self.home / "models.json"

    @property
    def projects_path(self) -> Path:
        return self.home / "projects.json"

    def job_dir(self, job_id: int) -> Path:
        return self.jobs_dir / str(job_id)

    def artifacts_dir(self, job_id: int) -> Path:
        """작업이 남기는 스크린샷·영상 등 결과물 폴더."""
        return self.job_dir(job_id) / "artifacts"


def load() -> Config:
    home = Path(os.environ.get("COLIDAY_HOME", Path.home() / ".coliday")).expanduser()
    return Config(
        home=home,
        claude_bin=os.environ.get("COLIDAY_CLAUDE_BIN", "claude"),
        permission_mode=os.environ.get("COLIDAY_PERMISSION_MODE", "auto"),
        codex_bin=os.environ.get("COLIDAY_CODEX_BIN", "codex"),
        max_attempts=_max_attempts(os.environ.get("COLIDAY_MAX_ATTEMPTS")),
        open_bin=os.environ.get("COLIDAY_OPEN_BIN") or ("open" if sys.platform == "darwin" else "xdg-open"),
    )


def _max_attempts(raw: str | None) -> int:
    """COLIDAY_MAX_ATTEMPTS. 비어 있으면 5, 1 이상의 정수가 아니면 오류 (조용히 기본값으로 돌리면 설정이 먹은 줄 안다)."""
    if raw is None or not raw.strip():
        return 5
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise SystemExit(f"COLIDAY_MAX_ATTEMPTS 는 1 이상의 정수여야 합니다: {raw!r}")
    return value
