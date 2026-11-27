"""채팅(작업)을 차례(turn)로 나누기.

차례 = 사람의 요청 하나 + 그 요청에 AI 가 한 일과 보고. 자동 묶음은 채팅이 아니라 차례를 묶는다 (topics.py).
첫 차례는 jobs.prompt, 그 뒤 차례는 이어서 시킨 요청(FOLLOW_UP 이벤트) 하나다. 차례의 보고는 그 범위 안의 마지막 RESULT.
DB 도 화면도 모르는 순수한 계산이다 (Store.turn_events 가 읽은 줄을 받는다).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from coliday.events import LEAD
from coliday.guard import ALLOWED_NOTE

if TYPE_CHECKING:
    from coliday.store import EventRow, Job

# store.py 의 이벤트 종류와 같은 값 (store 를 가져오면 서로 가져오는 고리가 생긴다)
FOLLOW_UP = "prompt"
QUEUED = "queued"
RESULT = "result"
ERROR = "error"
START = "start"
KINDS = (
    FOLLOW_UP,
    QUEUED,
    RESULT,
    ERROR,
    START,
)  # 차례를 나누는 데 필요한 이벤트 (Store.turn_events)

ACTIVE = frozenset({"queued", "running"})  # 아직 끝나지 않은 차례: 묶지 않는다
STATES = ("running", "queued", "succeeded", "failed", "cancelled", "done", "no_report")


@dataclass(frozen=True)
class Turn:
    job_id: int
    turn_id: int  # 0 = 첫 요청(jobs.prompt), 그 밖 = 그 차례를 연 FOLLOW_UP 이벤트 id. (job_id, turn_id) 는 바뀌지 않는다
    index: int  # 채팅 안의 몇 번째 요청 (1부터)
    asked_at: float  # 첫 차례 jobs.created_at, 그 밖 FOLLOW_UP 이벤트 시각
    question: str  # 사람이 쓴 요청 (.env 허용 뒤 붙는 안내문은 뺐다)
    report: str | None  # 범위 안 마지막 팀장 보고
    state: str  # STATES 중 하나
    ended_at: float | None
    members: tuple[str, ...]  # 범위 안에서 일을 시작한 팀원 (팀장 제외, 처음 나온 순서)
    errors: int  # 범위 안 오류 이벤트 수

    @property
    def active(self) -> bool:
        return self.state in ACTIVE

    @property
    def seconds(self) -> float | None:
        """요청부터 끝날 때까지 걸린 시간 (끝나지 않았거나 알 수 없으면 None)."""
        return (
            None if self.ended_at is None else max(0.0, self.ended_at - self.asked_at)
        )


def question_text(message: str) -> str:
    """요청 본문에서 .env 허용 안내문(ALLOWED_NOTE)을 뺀다. 안내문만 있던 요청이면 빈 문자열 (새 차례가 아니다).

    Store.turn_backlog 의 SQL(REPLACE 후 TRIM)과 같은 기준이어야 차례 수가 어긋나지 않는다.
    """
    text = message.replace(ALLOWED_NOTE, "")
    return re.sub(r"\n{3,}", "\n\n", text).strip(" \t\r\n")


def split(job: Job, rows: Sequence[EventRow]) -> tuple[list[Turn], list[str]]:
    """채팅을 차례로 나눈다. (차례들, 아직 전달되지 않은 대기 요청들).

    rows 는 그 작업의 이벤트 (Store.turn_events 또는 Store.events). 순서는 id 로 다시 맞춘다.
    - .env 허용 안내문만 있는 FOLLOW_UP 은 새 차례가 아니라 앞 차례의 연장이다 (그 보고가 앞 차례의 보고가 된다).
    - 대기 요청(QUEUED)은 전달되기 전까지 차례가 아니다.
    - 마지막 차례의 상태는 작업 상태, 앞 차례는 보고·오류로 정한다 (done / failed / no_report).
    """
    rows = sorted((r for r in rows if r.job_id == job.id), key=lambda r: r.id)
    starts: list[tuple[int, float, str]] = [(0, job.created_at, job.prompt)]
    for row in rows:
        if row.kind == FOLLOW_UP:
            text = question_text(row.message)
            if text:
                starts.append((row.id, row.ts, text))
    queued = [
        text
        for text in (question_text(r.message) for r in rows if r.kind == QUEUED)
        if text
    ]
    any_result = any(r.kind == RESULT for r in rows)

    result: list[Turn] = []
    for i, (turn_id, asked_at, question) in enumerate(starts):
        upper = starts[i + 1][0] if i + 1 < len(starts) else math.inf
        inside = [r for r in rows if turn_id < r.id < upper and r.kind != QUEUED]
        reports = [r for r in inside if r.kind == RESULT]
        errors = [r for r in inside if r.kind == ERROR]
        members = tuple(
            dict.fromkeys(
                r.actor for r in inside if r.kind == START and r.actor != LEAD
            )
        )
        report = reports[-1].message if reports else None
        last = i + 1 == len(starts)
        if last:
            state = job.status
            if (
                report is None and not any_result
            ):  # 보고를 이벤트로 남기기 전 버전에서 끝난 작업
                report = job.result
            ended_at = job.finished_at if job.status not in ACTIVE else None
        else:
            if any(r.actor == LEAD for r in errors):
                state = "failed"
            elif reports:
                state = "done"
            elif errors:
                state = "failed"
            else:
                state = "no_report"
            ended_at = (
                (reports[-1].ts if reports else inside[-1].ts) if inside else None
            )
        result.append(
            Turn(
                job_id=job.id,
                turn_id=turn_id,
                index=i + 1,
                asked_at=asked_at,
                question=question,
                report=report,
                state=state,
                ended_at=ended_at,
                members=members,
                errors=len(errors),
            )
        )
    return result, queued


def clip(text: str | None, limit: int) -> str:
    """한 줄로 모으고 limit 자에서 자른다 (목록 한 칸에 넣는 글)."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: max(0, limit - 1)] + "…"
