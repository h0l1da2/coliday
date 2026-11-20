"""경로와 실행 설정. 모두 환경변수로 덮어쓸 수 있다."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    home: Path
    claude_bin: str
    permission_mode: str
    codex_bin: str = "codex"

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


def load() -> Config:
    home = Path(os.environ.get("COLIDAY_HOME", Path.home() / ".coliday")).expanduser()
    return Config(
        home=home,
        claude_bin=os.environ.get("COLIDAY_CLAUDE_BIN", "claude"),
        permission_mode=os.environ.get("COLIDAY_PERMISSION_MODE", "auto"),
        codex_bin=os.environ.get("COLIDAY_CODEX_BIN", "codex"),
    )
