"""작업과 이벤트를 저장하는 SQLite 저장소.

여러 터미널(여러 worker 프로세스)이 동시에 쓰므로 WAL 모드와 busy timeout을 사용한다.
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Collection, Iterable, Iterator

from .joblist import JobGroup, JobMeta

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
CREATE TABLE IF NOT EXISTS job_options (
    job_id    INTEGER PRIMARY KEY REFERENCES jobs(id),
    allow_env INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS job_meta (
    job_id    INTEGER PRIMARY KEY REFERENCES jobs(id),
    parent_id INTEGER,
    group_id  INTEGER,
    pinned_at REAL,
    origin_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_job_meta_parent ON job_meta(parent_id);
CREATE INDEX IF NOT EXISTS idx_jobs_repo ON jobs(repo);
"""

# 그룹은 프로젝트(projects.json 의 정규화된 경로)에 속한다. 이름은 같은 프로젝트 안에서만 겹치지 않으면 된다.
# _upgrade_groups 가 이 문장 하나만 다시 실행하므로 다른 표와 섞지 않고 따로 둔다.
_GROUPS_TABLE = """CREATE TABLE IF NOT EXISTS job_groups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project    TEXT    NOT NULL,
    name       TEXT    NOT NULL,
    created_at REAL    NOT NULL,
    UNIQUE(project, name)
)"""

# 자동 묶음: 작업이 끝날 때 팀장 모델이 비슷한 작업끼리 묶는다 (topics.py). 묶음은 레포(job.repo)에 속하고 작업 하나는 묶음 하나.
_TOPICS_TABLES = """
CREATE TABLE IF NOT EXISTS topics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo        TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    summary     TEXT    NOT NULL DEFAULT '',
    manual_name INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS job_topics (
    job_id   INTEGER PRIMARY KEY REFERENCES jobs(id),
    topic_id INTEGER NOT NULL REFERENCES topics(id),
    manual   INTEGER NOT NULL DEFAULT 0,
    line     TEXT    NOT NULL DEFAULT ''
);
"""

_SCHEMA += _TOPICS_TABLES + _GROUPS_TABLE + ";\n"

# 실행 중에 보낸 요청(QUEUED)은 전달될 때 FOLLOW_UP 으로 바뀐다. 둘 다 사용자가 이어서 시킨 것이다.
_SELECT_JOBS = (
    "SELECT j.*, (SELECT MAX(e.ts) FROM events e WHERE e.job_id = j.id AND e.kind IN ('prompt', 'queued')) AS asked_at "
    "FROM jobs j"
)

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
    asked_at: float | None = None  # 마지막으로 이어서 시킨 시각 (그런 적이 없으면 None). 표에는 없고 읽을 때 계산한다

    @property
    def last_asked(self) -> float:
        """사용자가 이 작업에 마지막으로 요청한 시각. 작업 목록의 순서와 '경과'의 기준이다."""
        return self.asked_at or self.created_at

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True)
class Topic:
    """자동 묶음: 작업이 끝날 때 팀장 모델이 비슷한 작업끼리 묶은 것. 같은 레포(job.repo) 안에서만 묶는다."""

    id: int
    repo: str
    name: str
    summary: str
    manual_name: bool  # 사람이 이름을 정했다: AI 가 다시 바꾸지 않는다


@dataclass(frozen=True)
class JobTopic:
    job_id: int
    topic_id: int
    manual: bool  # 사람이 이 자동 묶음으로 옮겼다
    line: str  # 이 작업이 한 일 한 줄 (모델이 쓴다)


@dataclass(frozen=True)
class EventRow:
    id: int
    job_id: int
    ts: float
    actor: str
    kind: str
    message: str


ANY_TOPIC = object()  # assign_topic(expect=…) 를 주지 않았다: 지금 어느 묶음에 있든 옮긴다


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, timeout=10, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(_SCHEMA)
        self._add_origin_column()
        self._upgrade_groups()

    def _add_origin_column(self) -> None:
        """origin_id 가 생기기 전 개발 중에 만든 job_meta 에 열을 더한다 (배포된 적 없는 형태라 한 번만 시도)."""
        columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(job_meta)")}
        if "origin_id" in columns:
            return
        try:
            self._conn.execute("ALTER TABLE job_meta ADD COLUMN origin_id INTEGER")
        except sqlite3.OperationalError as exc:  # 다른 프로세스가 먼저 더했다
            if "duplicate column" not in str(exc):
                raise

    def _upgrade_groups(self) -> None:
        """project 열이 없는 개발 중의 job_groups(name 만 UNIQUE)를 새로 만든다 (배포된 적 없는 형태).

        열만 더해서는 UNIQUE 를 바꿀 수 없어 표를 다시 만든다. 그 그룹들은 어느 프로젝트 것인지 알 수 없어 버리고,
        새 표의 번호가 1부터 다시 시작해 옛 번호와 겹치므로 job_meta.group_id 도 비운다 (고정·파생은 남는다).
        """
        if "project" in self._group_columns():
            return
        with self._transaction():
            if "project" in self._group_columns():  # 다른 프로세스가 먼저 바꿨다
                return
            self._conn.execute("UPDATE job_meta SET group_id = NULL")
            self._conn.execute("DROP TABLE job_groups")
            self._conn.execute(_GROUPS_TABLE)

    def _group_columns(self) -> set[str]:
        return {r["name"] for r in self._conn.execute("PRAGMA table_info(job_groups)")}

    def close(self) -> None:
        self._conn.close()

    def create_job(
        self, *, prompt: str, repo: str, workdir: str, model: str | None, parent_id: int | None = None,
    ) -> Job:
        """parent_id 를 주면 그 작업에서 파생된 작업으로 기록한다 (작업과 함께 한 트랜잭션으로).

        parent_id 는 목록의 부모라 부모를 지우면 조부모로 옮겨지고, origin_id 는 만든 출처라 그대로 남는다.
        """
        sql = "INSERT INTO jobs(prompt, repo, workdir, status, model, created_at) VALUES (?, ?, ?, 'queued', ?, ?)"
        params = (prompt, repo, workdir, model, time.time())
        if parent_id is None:
            return self.get_job(int(self._conn.execute(sql, params).lastrowid))
        with self._transaction():
            job_id = int(self._conn.execute(sql, params).lastrowid)
            self._conn.execute(
                "INSERT INTO job_meta(job_id, parent_id, origin_id) VALUES (?, ?, ?)", (job_id, parent_id, parent_id)
            )
        return self.get_job(job_id)

    # 작업별 선택 사항은 jobs 에 열을 늘리지 않고 따로 둔다 (실행 중인 이전 버전 worker 가 모르는 열을 읽고 죽지 않게).
    def set_allow_env(self, job_id: int, allow: bool) -> None:
        """사람이 이 작업에서 .env 파일을 읽어도 된다고 했는지 남긴다."""
        self._conn.execute(
            "INSERT INTO job_options(job_id, allow_env) VALUES (?, ?) "
            "ON CONFLICT(job_id) DO UPDATE SET allow_env = excluded.allow_env",
            (job_id, int(allow)),
        )

    def allow_env(self, job_id: int) -> bool:
        row = self._conn.execute("SELECT allow_env FROM job_options WHERE job_id = ?", (job_id,)).fetchone()
        return bool(row and row["allow_env"])

    def get_job(self, job_id: int) -> Job:
        row = self._conn.execute(f"{_SELECT_JOBS} WHERE j.id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return Job(**dict(row))

    def list_jobs(
        self, limit: int = 30, *, repos: Collection[str] | None = None, after: Job | None = None,
    ) -> list[Job]:
        """마지막으로 요청한 것이 위로 오게 늘어놓는다 (이어서 시킨 작업은 그때 맨 위로 올라온다).

        repos 를 주면 그 레포의 작업만 (비어 있으면 빈 목록). after 를 주면 그 작업보다 아래 순서인 것만
        (다음 쪽을 읽는다: 앞 쪽의 마지막 작업을 넘긴다).
        """
        if repos is not None and not repos:
            return []
        where, params = [], []
        if repos is not None:
            where.append(f"repo IN ({', '.join('?' * len(repos))})")
            params += list(repos)
        if after is not None:
            where.append("(COALESCE(asked_at, created_at) < ? OR (COALESCE(asked_at, created_at) = ? AND id < ?))")
            params += [after.last_asked, after.last_asked, after.id]
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        # asked_at 은 계산한 열이라 감싼 뒤에 거른다
        rows = self._conn.execute(
            f"SELECT * FROM ({_SELECT_JOBS}){clause} ORDER BY COALESCE(asked_at, created_at) DESC, id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [Job(**dict(r)) for r in rows]

    def repos(self) -> list[str]:
        """작업이 있는 레포 (화면이 매 초 부른다: 가볍게)."""
        return [r["repo"] for r in self._conn.execute("SELECT DISTINCT repo FROM jobs")]

    def jobs_stamp(self) -> tuple[int, int]:
        """(작업 수, 가장 큰 작업 번호). 작업이 생기거나 지워졌는지 가볍게 알아보는 데 쓴다."""
        row = self._conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM jobs").fetchone()
        return int(row[0]), int(row[1])

    def active_jobs(self) -> list[Job]:
        """실행 중이거나 대기 중인 작업 전체 (프로젝트와 상관없이)."""
        rows = self._conn.execute(
            f"{_SELECT_JOBS} WHERE j.status IN ('queued','running') ORDER BY j.id"
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

    def finish_job(self, job_id: int, status: str, *, unchanged: Job | None = None, **fields: object) -> bool:
        """이미 종료 상태(예: 사용자가 취소함)면 덮어쓰지 않는다. 갱신했으면 True.

        unchanged 를 주면 그 작업을 읽었을 때의 상태·pid·시작 시각이 지금도 같을 때만 바꾼다. 오래전에 읽은 작업으로
        정리(reconcile)하는 사이 worker 가 시작했으면(queued → running) 그 작업을 실패로 덮어쓰지 않는다.
        """
        if status not in TERMINAL_STATUSES:
            raise ValueError(status)
        extra = {k: v for k, v in fields.items() if k in _UPDATABLE}
        assigns = ", ".join(["status = ?", "finished_at = ?", *(f"{k} = ?" for k in extra)])
        where, params = "", ()
        if unchanged is not None:
            where = " AND status = ? AND pid IS ? AND started_at IS ?"
            params = (unchanged.status, unchanged.pid, unchanged.started_at)
        cur = self._conn.execute(
            f"UPDATE jobs SET {assigns} WHERE id = ? AND status NOT IN ('succeeded','failed','cancelled'){where}",
            (status, time.time(), *extra.values(), job_id, *params),
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
                self._conn.execute("DELETE FROM job_options WHERE job_id = ?", (job_id,))
                # 하위 작업은 지운 작업의 부모 밑으로 (없으면 최상위로) 올린다
                self._conn.execute(
                    "UPDATE job_meta SET parent_id = (SELECT parent_id FROM job_meta WHERE job_id = ?) "
                    "WHERE parent_id = ?",
                    (job_id, job_id),
                )
                self._conn.execute("DELETE FROM job_meta WHERE job_id = ?", (job_id,))
                self._conn.execute("DELETE FROM job_topics WHERE job_id = ?", (job_id,))
                self._drop_empty_topics()
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

    def last_result(self, job_id: int) -> str | None:
        """마지막 팀장 보고. 보고를 이벤트로 남기기 전 버전에서 끝난 작업이면 jobs.result."""
        row = self._conn.execute(
            "SELECT message FROM events WHERE job_id = ? AND kind = ? ORDER BY id DESC LIMIT 1", (job_id, RESULT)
        ).fetchone()
        if row is not None:
            return row["message"]
        row = self._conn.execute("SELECT result FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return row["result"] if row else None

    # ── 목록 정리: 파생 관계·그룹·고정 (jobs 표에 열을 늘리지 않고 job_meta 에 둔다) ──────────────
    def job_meta(self) -> dict[int, JobMeta]:
        """있는 작업의 부모·그룹·고정. 부모나 그룹이 (이전 버전이 지워서) 없으면 None 으로 본다. 기록이 없는 작업은 빠진다."""
        rows = self._conn.execute(
            "SELECT m.job_id, p.id AS parent_id, g.id AS group_id, m.pinned_at IS NOT NULL AS pinned "
            "FROM job_meta m JOIN jobs j ON j.id = m.job_id "
            "LEFT JOIN jobs p ON p.id = m.parent_id LEFT JOIN job_groups g ON g.id = m.group_id"
        ).fetchall()
        return {
            r["job_id"]: JobMeta(parent_id=r["parent_id"], group_id=r["group_id"], pinned=bool(r["pinned"]))
            for r in rows
        }

    def origin(self, job_id: int) -> int | None:
        """파생 작업을 만든 출처 작업 번호 (만들 때 기록, 부모를 지워도 바뀌지 않는다). 그 작업이 지금 있는지는 보지 않는다."""
        row = self._conn.execute("SELECT origin_id FROM job_meta WHERE job_id = ?", (job_id,)).fetchone()
        return row["origin_id"] if row else None

    def children(self, job_id: int) -> list[int]:
        """바로 아래 파생 작업 (만든 순서)."""
        rows = self._conn.execute(
            "SELECT m.job_id FROM job_meta m JOIN jobs j ON j.id = m.job_id WHERE m.parent_id = ? ORDER BY m.job_id",
            (job_id,),
        ).fetchall()
        return [r["job_id"] for r in rows]

    def set_pinned(self, job_ids: Iterable[int], pinned: bool) -> None:
        """이미 고정된 작업을 다시 고정해도 고정한 시각은 그대로 둔다. 없는 작업은 건너뛴다."""
        now = time.time() if pinned else None
        with self._transaction():
            for job_id in job_ids:
                self._conn.execute(
                    "INSERT INTO job_meta(job_id, pinned_at) SELECT id, ? FROM jobs WHERE id = ? "
                    "ON CONFLICT(job_id) DO UPDATE SET pinned_at = "
                    "CASE WHEN excluded.pinned_at IS NULL THEN NULL ELSE COALESCE(job_meta.pinned_at, excluded.pinned_at) END",
                    (now, job_id),
                )

    def set_group(self, job_ids: Iterable[int], group_id: int | None) -> None:
        """작업들을 그룹에 넣는다 (None 이면 그룹에서 뺀다). 없는 그룹이면 ValueError. 없는 작업은 건너뛴다."""
        with self._transaction():
            if group_id is not None and not self._conn.execute(
                "SELECT 1 FROM job_groups WHERE id = ?", (group_id,)
            ).fetchone():
                raise ValueError(f"없는 그룹입니다: {group_id}")
            for job_id in job_ids:
                self._conn.execute(
                    "INSERT INTO job_meta(job_id, group_id) SELECT id, ? FROM jobs WHERE id = ? "
                    "ON CONFLICT(job_id) DO UPDATE SET group_id = excluded.group_id",
                    (group_id, job_id),
                )

    def list_groups(self, project: str | None = None) -> list[JobGroup]:
        """project 의 그룹 (만든 순서). None 이면 모든 프로젝트의 그룹 (있는지 확인하는 용도)."""
        sql = "SELECT id, name, created_at FROM job_groups"
        params: tuple = ()
        if project is not None:
            sql += " WHERE project = ?"
            params = (project,)
        rows = self._conn.execute(f"{sql} ORDER BY created_at, id", params).fetchall()
        return [JobGroup(id=r["id"], name=r["name"], created_at=r["created_at"]) for r in rows]

    def create_group(self, name: str, *, project: str) -> JobGroup:
        """같은 프로젝트에 같은 이름이 있으면 ValueError (다른 프로젝트와는 겹쳐도 된다)."""
        name = _group_name(name)
        if not project:
            raise ValueError("그룹을 만들 프로젝트가 없습니다.")
        try:
            cur = self._conn.execute(
                "INSERT INTO job_groups(project, name, created_at) VALUES (?, ?, ?)", (project, name, time.time())
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"이미 있는 그룹 이름입니다: {name}") from exc
        row = self._conn.execute("SELECT * FROM job_groups WHERE id = ?", (cur.lastrowid,)).fetchone()
        return JobGroup(id=row["id"], name=row["name"], created_at=row["created_at"])

    def rename_group(self, group_id: int, name: str) -> None:
        name = _group_name(name)
        try:
            cur = self._conn.execute("UPDATE job_groups SET name = ? WHERE id = ?", (name, group_id))
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"이미 있는 그룹 이름입니다: {name}") from exc
        if cur.rowcount != 1:
            raise ValueError(f"없는 그룹입니다: {group_id}")

    def delete_group(self, group_id: int) -> bool:
        """그룹만 지운다. 들어 있던 작업은 그룹 없음이 된다. 없던 그룹이면 False."""
        with self._transaction():
            self._conn.execute("UPDATE job_meta SET group_id = NULL WHERE group_id = ?", (group_id,))
            cur = self._conn.execute("DELETE FROM job_groups WHERE id = ?", (group_id,))
        return cur.rowcount == 1

    # ── 자동 묶음 (topics.py 가 채운다) ─────────────────────────
    def topics(self, repo: str | None = None) -> list[Topic]:
        rows = self._conn.execute(
            "SELECT * FROM topics WHERE (? IS NULL OR repo = ?) ORDER BY id", (repo, repo)
        ).fetchall()
        return [Topic(r["id"], r["repo"], r["name"], r["summary"], bool(r["manual_name"])) for r in rows]

    def job_topics(self) -> dict[int, JobTopic]:
        """있는 작업의 자동 묶음. 이전 버전이 작업만 지우고 남긴 줄은 빠진다 (job_meta 와 같은 원칙)."""
        rows = self._conn.execute(
            "SELECT jt.* FROM job_topics jt JOIN jobs j ON j.id = jt.job_id"
        ).fetchall()
        return {r["job_id"]: JobTopic(r["job_id"], r["topic_id"], bool(r["manual"]), r["line"]) for r in rows}

    def job_topic(self, job_id: int) -> JobTopic | None:
        row = self._conn.execute(
            "SELECT jt.* FROM job_topics jt JOIN jobs j ON j.id = jt.job_id WHERE jt.job_id = ?", (job_id,)
        ).fetchone()
        return JobTopic(row["job_id"], row["topic_id"], bool(row["manual"]), row["line"]) if row else None

    def assign_topic(
        self, job_id: int, topic_id: int | None, *, name: str = "", line: str | None = None, manual: bool = False,
        expect: int | None | object = ANY_TOPIC, summary: str | None = None,
    ) -> int | None:
        """작업을 자동 묶음에 넣는다. topic_id 가 None 이면 name 으로 새 묶음을 만든다. 넣은 묶음의 id (작업·묶음이 없으면 None).

        묶음 만들기와 넣기, 비게 된 묶음 지우기를 한 트랜잭션으로 묶는다 (그사이 작업이 삭제돼 빈 묶음만 남지 않게).
        다른 레포의 묶음에는 넣지 않는다. line 을 주지 않으면 이전 한 줄 요약을 그대로 둔다. summary 를 주면 넣은 묶음의 요약도 바꾼다.

        expect 는 모델에게 묻기 전에 읽어 둔 이 작업의 묶음 id 다 (아직 안 묶였으면 None). 모델의 답을 기다리는 사이 사람이
        작업을 다른 묶음으로 옮겼으면(지금 묶음이 expect 와 다르면) 묶음과 사람이 옮겼다는 표시는 건드리지 않고 line 만 고친 뒤
        지금 든 묶음의 id 를 돌려준다. 새 묶음도 만들지 않고 summary 도 쓰지 않는다 (다른 묶음을 두고 쓴 요약이다).
        expect 를 주면 사람이 옮겼다는 표시(manual)는 지우지 않는다.
        """
        with self._transaction():
            job = self._conn.execute("SELECT repo FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                return None
            if expect is not ANY_TOPIC:
                row = self._conn.execute(
                    "SELECT topic_id, manual FROM job_topics WHERE job_id = ?", (job_id,)
                ).fetchone()
                if (row["topic_id"] if row else None) != expect:
                    if row is None:
                        return None
                    if line is not None:
                        self._conn.execute("UPDATE job_topics SET line = ? WHERE job_id = ?", (line, job_id))
                    return int(row["topic_id"])
                manual = manual or bool(row and row["manual"])
            if topic_id is None:
                cur = self._conn.execute(
                    "INSERT INTO topics(repo, name, manual_name) VALUES (?, ?, ?)", (job["repo"], name, int(manual))
                )
                topic_id = int(cur.lastrowid)
            elif not self._conn.execute(
                "SELECT 1 FROM topics WHERE id = ? AND repo = ?", (topic_id, job["repo"])
            ).fetchone():
                return None  # 없어졌거나 다른 레포의 묶음
            self._conn.execute(
                "INSERT INTO job_topics(job_id, topic_id, manual, line) VALUES (?, ?, ?, COALESCE(?, '')) "
                "ON CONFLICT(job_id) DO UPDATE SET topic_id = excluded.topic_id, manual = excluded.manual, "
                "line = COALESCE(?, line)",
                (job_id, topic_id, int(manual), line, line),
            )
            if summary is not None:
                self._conn.execute("UPDATE topics SET summary = ? WHERE id = ?", (summary, topic_id))
            self._drop_empty_topics()
        return topic_id

    def update_topic(
        self, topic_id: int, *, name: str | None = None, summary: str | None = None, manual_name: bool = False,
    ) -> None:
        """자동 묶음의 이름·요약을 바꾼다. 사람이 정한 이름은 manual_name 으로 바꿀 때만 바뀐다."""
        if summary is not None:
            self._conn.execute("UPDATE topics SET summary = ? WHERE id = ?", (summary, topic_id))
        if name and manual_name:
            self._conn.execute("UPDATE topics SET name = ?, manual_name = 1 WHERE id = ?", (name, topic_id))
        elif name:
            self._conn.execute("UPDATE topics SET name = ? WHERE id = ? AND manual_name = 0", (name, topic_id))

    def _drop_empty_topics(self) -> None:
        # 이전 버전이 작업만 지우고 남긴 줄도 함께 치운다 (트랜잭션 안에서 부른다).
        self._conn.execute("DELETE FROM job_topics WHERE job_id NOT IN (SELECT id FROM jobs)")
        self._conn.execute("DELETE FROM topics WHERE id NOT IN (SELECT topic_id FROM job_topics)")

    def get_jobs(self, job_ids: Collection[int]) -> dict[int, Job]:
        """여러 작업을 한 번에 읽는다 (없는 번호는 빠진다)."""
        ids = list(dict.fromkeys(job_ids))
        found: dict[int, Job] = {}
        for start in range(0, len(ids), 500):  # SQLite 의 변수 개수 한도 안에서 나눠 읽는다
            chunk = ids[start:start + 500]
            rows = self._conn.execute(
                f"{_SELECT_JOBS} WHERE j.id IN ({', '.join('?' * len(chunk))})", chunk
            ).fetchall()
            found.update({r["id"]: Job(**dict(r)) for r in rows})
        return found

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[None]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise


def _group_name(name: str) -> str:
    """앞뒤·연속 공백을 정리한 그룹 이름. 비면 ValueError."""
    name = " ".join(name.split())
    if not name:
        raise ValueError("그룹 이름이 비어 있습니다.")
    return name
