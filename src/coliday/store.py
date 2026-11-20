"""작업과 이벤트를 저장하는 SQLite 저장소.

여러 터미널(여러 worker 프로세스)이 동시에 쓰므로 WAL 모드와 busy timeout을 사용한다.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
USER = "사용자"
FOLLOW_UP = "prompt"  # 끝난 작업에 이어서 시킨 요청 이벤트의 kind
QUEUED = "queued"  # 실행 중에 보낸 요청. 지금 실행이 끝나면 FOLLOW_UP 으로 바뀌어 이어서 전달된다
RESULT = "result"  # 실행 한 번의 팀장 보고 전문. 이어서 시켜도 이전 보고가 타임라인에 남게 이벤트로 둔다

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt        TEXT    NOT NULL,
    repo          TEXT    NOT NULL,
    workdir       TEXT    NOT NULL,
    branch        TEXT,
    base_commit   TEXT,
    status        TEXT    NOT NULL,
    phase         TEXT    NOT NULL DEFAULT '',
    pid           INTEGER,
    session_id    TEXT,
    model         TEXT,
    result        TEXT,
    cost_usd      REAL,
    created_at    REAL    NOT NULL,
    started_at    REAL,
    finished_at   REAL
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  INTEGER NOT NULL REFERENCES jobs(id),
    ts      REAL    NOT NULL,
    actor   TEXT    NOT NULL,
    kind    TEXT    NOT NULL,
    message TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, id);
"""

_UPDATABLE = frozenset(
    {
        "workdir", "branch", "base_commit", "status", "phase", "pid", "session_id", "model",
        "result", "cost_usd", "started_at", "finished_at",
    }
)


@dataclass(frozen=True)
class Job:
    id: int
    prompt: str
    repo: str
    workdir: str
    branch: str | None
    base_commit: str | None
    status: str
    phase: str
    pid: int | None
    session_id: str | None
    model: str | None
    result: str | None
    cost_usd: float | None
    created_at: float
    started_at: float | None
    finished_at: float | None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True)
class EventRow:
    id: int
    job_id: int
    ts: float
    actor: str
    kind: str
    message: str


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, timeout=10, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def create_job(self, *, prompt: str, repo: str, workdir: str, model: str | None) -> Job:
        cur = self._conn.execute(
            "INSERT INTO jobs(prompt, repo, workdir, status, model, created_at) "
            "VALUES (?, ?, ?, 'queued', ?, ?)",
            (prompt, repo, workdir, model, time.time()),
        )
        return self.get_job(int(cur.lastrowid))

    def get_job(self, job_id: int) -> Job:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return Job(**dict(row))

    def list_jobs(self, limit: int = 30) -> list[Job]:
        rows = self._conn.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Job(**dict(r)) for r in rows]

    def update_job(self, job_id: int, **fields: object) -> None:
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"수정할 수 없는 필드: {sorted(unknown)}")
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))

    def start_job(self, job_id: int, pid: int) -> bool:
        """queued 일 때만 running 으로 바꾼다. 그 사이 취소됐으면 False."""
        cur = self._conn.execute(
            "UPDATE jobs SET status = 'running', pid = ?, started_at = ?, phase = '팀장 시작 중' "
            "WHERE id = ? AND status = 'queued'",
            (pid, time.time(), job_id),
        )
        return cur.rowcount == 1

    def requeue_job(self, job_id: int, prompt: str) -> bool:
        """끝난 작업을 queued 로 되돌리고 후속 요청을 기록한다. 끝난 상태가 아니면(이미 다른 곳에서 이어갔으면) False.

        상태 변경과 요청 기록을 한 트랜잭션으로 묶는다. 따로 하면 그 사이에 다른 프로세스가
        요청 없는 queued 작업을 보고 'worker 시작 실패'로 정리할 수 있다.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self._conn.execute(
                "UPDATE jobs SET status = 'queued', pid = NULL, finished_at = NULL, phase = '이어서 작업 대기 중' "
                "WHERE id = ? AND status IN ('succeeded','failed','cancelled')",
                (job_id,),
            )
            if cur.rowcount == 1:
                # worker 가 죽어 전달되지 못한 대기 요청이 남아 있으면 버리지 않고 앞에 붙인다.
                self.add_event(job_id, USER, FOLLOW_UP, "\n\n".join([*self._take_queued(job_id), prompt]))
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return cur.rowcount == 1

    def queue_message(self, job_id: int, prompt: str) -> bool:
        """실행 중인 작업에 요청을 대기시킨다. 그 사이 작업이 끝났으면 넣지 않고 False (그때는 바로 이어서 시키면 된다)."""
        cur = self._conn.execute(
            "INSERT INTO events(job_id, ts, actor, kind, message) SELECT ?, ?, ?, ?, ? "
            "WHERE EXISTS (SELECT 1 FROM jobs WHERE id = ? AND status IN ('queued','running'))",
            (job_id, time.time(), USER, QUEUED, prompt, job_id),
        )
        return cur.rowcount == 1

    def clear_queued(self, job_id: int) -> None:
        self._conn.execute("DELETE FROM events WHERE job_id = ? AND kind = ?", (job_id, QUEUED))

    def _take_queued(self, job_id: int) -> list[str]:
        """대기 중인 요청을 꺼낸다 (트랜잭션 안에서 부른다)."""
        rows = self._conn.execute(
            "SELECT message FROM events WHERE job_id = ? AND kind = ? ORDER BY id", (job_id, QUEUED)
        ).fetchall()
        self._conn.execute("DELETE FROM events WHERE job_id = ? AND kind = ?", (job_id, QUEUED))
        return [r["message"] for r in rows]

    def finish_or_continue(self, job_id: int, status: str, **fields: object) -> bool:
        """실행 한 번을 마친다. 대기 중인 요청이 있으면 끝내지 않고 그 요청을 후속 요청으로 바꾼 뒤 False 를 돌려준다.

        '대기 요청 확인'과 '끝내기'를 한 트랜잭션으로 묶는다. 따로 하면 그 사이에 들어온 요청이
        끝난 작업에 남아 영영 전달되지 않는다. 이미 취소된 작업이면 아무것도 하지 않고 True.
        """
        if status not in TERMINAL_STATUSES:
            raise ValueError(status)
        extra = {k: v for k, v in fields.items() if k in _UPDATABLE}
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None or row["status"] in TERMINAL_STATUSES:
                self._conn.execute("COMMIT")
                return True
            queued = self._take_queued(job_id)
            if queued:
                self.add_event(job_id, USER, FOLLOW_UP, "\n\n".join(queued))
                extra["phase"] = "대기 중이던 요청 처리 시작"
            else:
                extra |= {"status": status, "finished_at": time.time()}
            assigns = ", ".join(f"{k} = ?" for k in extra)
            self._conn.execute(f"UPDATE jobs SET {assigns} WHERE id = ?", (*extra.values(), job_id))
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return not queued

    def last_follow_up(self, job_id: int) -> EventRow | None:
        row = self._conn.execute(
            "SELECT * FROM events WHERE job_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
            (job_id, FOLLOW_UP),
        ).fetchone()
        return EventRow(**dict(row)) if row else None

    def has_result_event(self, job_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM events WHERE job_id = ? AND kind = ? LIMIT 1", (job_id, RESULT)
        ).fetchone()
        return row is not None

    def finish_job(self, job_id: int, status: str, **fields: object) -> bool:
        """이미 종료 상태(예: 사용자가 취소함)면 덮어쓰지 않는다. 갱신했으면 True."""
        if status not in TERMINAL_STATUSES:
            raise ValueError(status)
        extra = {k: v for k, v in fields.items() if k in _UPDATABLE}
        assigns = ", ".join(["status = ?", "finished_at = ?", *(f"{k} = ?" for k in extra)])
        cur = self._conn.execute(
            f"UPDATE jobs SET {assigns} WHERE id = ? AND status NOT IN ('succeeded','failed','cancelled')",
            (status, time.time(), *extra.values(), job_id),
        )
        return cur.rowcount == 1

    def delete_job(self, job_id: int) -> bool:
        """끝난 작업과 그 이벤트를 지운다. 끝난 상태가 아니면(그 사이 이어서 실행됐으면) 지우지 않고 False."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self._conn.execute(
                "DELETE FROM jobs WHERE id = ? AND status IN ('succeeded','failed','cancelled')", (job_id,)
            )
            if cur.rowcount == 1:
                self._conn.execute("DELETE FROM events WHERE job_id = ?", (job_id,))
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return cur.rowcount == 1

    def add_event(self, job_id: int, actor: str, kind: str, message: str) -> None:
        self._conn.execute(
            "INSERT INTO events(job_id, ts, actor, kind, message) VALUES (?, ?, ?, ?, ?)",
            (job_id, time.time(), actor, kind, message),
        )

    def events(self, job_id: int, after_id: int = 0) -> list[EventRow]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE job_id = ? AND id > ? ORDER BY id",
            (job_id, after_id),
        ).fetchall()
        return [EventRow(**dict(r)) for r in rows]
