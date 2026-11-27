"""채팅을 차례로 나누기 (turns.split) 와 그 재료를 읽는 Store.turn_events."""

from __future__ import annotations

from dataclasses import replace

from coliday import turns
from coliday.events import LEAD
from coliday.guard import ALLOWED_NOTE
from coliday.store import USER, EventRow, Job, Store

JOB = Job(
    id=7,
    prompt="주문 취소 API 추가",
    repo="/r",
    workdir="/r",
    branch=None,
    base_commit=None,
    status="succeeded",
    phase="",
    pid=None,
    session_id=None,
    model=None,
    result="마지막 보고",
    cost_usd=None,
    created_at=100.0,
    started_at=101.0,
    finished_at=500.0,
)


def _ev(
    id_: int, kind: str, message: str = "", actor: str = LEAD, ts: float | None = None
) -> EventRow:
    return EventRow(
        id=id_,
        job_id=JOB.id,
        ts=ts if ts is not None else 100.0 + id_,
        actor=actor,
        kind=kind,
        message=message,
    )


def test_이어서_시킨_요청이_없으면_첫_요청_하나가_차례다():
    [turn], queued = turns.split(JOB, [_ev(1, "start"), _ev(2, "result", "보고 1")])
    assert queued == []
    assert (turn.turn_id, turn.index, turn.asked_at, turn.question) == (
        0,
        1,
        100.0,
        "주문 취소 API 추가",
    )
    assert (turn.report, turn.state, turn.ended_at, turn.seconds) == (
        "보고 1",
        "succeeded",
        500.0,
        400.0,
    )


def test_이어서_시킨_요청마다_차례가_생기고_보고는_그_범위의_마지막_보고다():
    rows = [
        _ev(1, "start"),
        _ev(2, "result", "첫 보고"),
        _ev(3, "prompt", "환불도 해줘", actor=USER),
        _ev(4, "result", "중간"),
        _ev(5, "result", "환불 보고"),
        _ev(6, "prompt", "테스트 추가", actor=USER),
        _ev(7, "result", "테스트 보고"),
    ]
    found, _ = turns.split(JOB, list(reversed(rows)))  # 순서가 섞여 와도 id 로 맞춘다
    assert [(t.turn_id, t.index, t.question, t.report, t.state) for t in found] == [
        (0, 1, "주문 취소 API 추가", "첫 보고", "done"),
        (3, 2, "환불도 해줘", "환불 보고", "done"),
        (6, 3, "테스트 추가", "테스트 보고", "succeeded"),
    ]
    assert found[1].asked_at == 103.0 and found[1].ended_at == 105.0


def test_env_허용_안내문만_있는_요청은_앞_차례의_연장이다():
    rows = [
        _ev(1, "error", actor=LEAD),
        _ev(2, "prompt", ALLOWED_NOTE, actor=USER),
        _ev(3, "result", "허용 뒤 보고"),
    ]
    [turn], _ = turns.split(JOB, rows)
    assert turn.report == "허용 뒤 보고" and turn.errors == 1


def test_안내문과_사람_글이_합쳐진_요청은_차례이고_안내문은_뺀다():
    rows = [
        _ev(1, "result", "a"),
        _ev(2, "prompt", f"{ALLOWED_NOTE}\n\n로그도 남겨줘", actor=USER),
        _ev(3, "result", "b"),
    ]
    found, _ = turns.split(JOB, rows)
    assert [t.question for t in found] == ["주문 취소 API 추가", "로그도 남겨줘"]
    assert turns.question_text(f"앞\n\n{ALLOWED_NOTE}\n\n뒤") == "앞\n\n뒤"


def test_보고가_없는_앞_차례는_no_report_오류가_있으면_failed():
    rows = [
        _ev(1, "start"),  # 보고를 이벤트로 남기기 전 버전의 앞 차례
        _ev(2, "prompt", "둘째", actor=USER),
        _ev(3, "error", actor=LEAD),
        _ev(4, "prompt", "셋째", actor=USER),
        _ev(5, "error", actor="coder"),
        _ev(6, "result", "팀원 실패 뒤 복구"),
        _ev(7, "prompt", "넷째", actor=USER),
        _ev(8, "error", actor="coder"),
        _ev(9, "prompt", "다섯째", actor=USER),
        _ev(10, "result", "끝"),
    ]
    found, _ = turns.split(JOB, rows)
    assert [t.state for t in found] == [
        "no_report",
        "failed",
        "done",
        "failed",
        "succeeded",
    ]
    assert found[0].report is None and found[0].ended_at == 101.0


def test_보고_이벤트가_하나도_없는_옛_작업은_마지막_차례에_jobs_result():
    found, _ = turns.split(JOB, [_ev(1, "prompt", "둘째", actor=USER)])
    assert [(t.report, t.state) for t in found] == [
        (None, "no_report"),
        ("마지막 보고", "succeeded"),
    ]
    # 보고 이벤트가 하나라도 있으면 jobs.result 로 채우지 않는다 (그 보고는 앞 차례의 것이다)
    found, _ = turns.split(
        JOB, [_ev(1, "result", "첫"), _ev(2, "prompt", "둘째", actor=USER)]
    )
    assert [t.report for t in found] == ["첫", None]


def test_실행_중인_마지막_차례와_대기_요청():
    running = replace(JOB, status="running", finished_at=None)
    rows = [
        _ev(1, "result", "첫"),
        _ev(2, "prompt", "둘째", actor=USER),
        _ev(3, "queued", "셋째도", actor=USER),
        _ev(4, "queued", ALLOWED_NOTE, actor=USER),
    ]
    found, queued = turns.split(running, rows)
    assert [(t.index, t.state, t.active, t.ended_at) for t in found] == [
        (1, "done", False, 101.0),
        (2, "running", True, None),
    ]
    assert queued == ["셋째도"]  # 대기 요청은 전달되기 전까지 차례가 아니다


def test_팀원은_팀장을_빼고_처음_나온_순서로_한_번씩():
    rows = [
        _ev(1, "start", actor=LEAD),
        _ev(2, "start", actor="architect"),
        _ev(3, "start", actor="coder"),
        _ev(4, "start", actor="architect"),
        _ev(5, "error", actor="coder"),
        _ev(6, "result", "끝"),
    ]
    [turn], _ = turns.split(JOB, rows)
    assert turn.members == ("architect", "coder") and turn.errors == 1


def test_clip은_한_줄로_모으고_자른다():
    assert turns.clip("가 나\n\n다", 10) == "가 나 다"
    assert turns.clip("가나다라마바", 4) == "가나다…"
    assert turns.clip(None, 3) == ""


def test_turn_events는_필요한_이벤트만_시작_오류는_본문_없이_읽는다(tmp_path):
    store = Store(tmp_path / "jobs.db")
    job = store.create_job(prompt="p", repo="/r", workdir="/r", model=None)
    for kind, message in [
        ("start", "작업 시작 (model=x)"),
        ("tool", "Read a.py"),
        ("text", "긴 생각"),
        ("error", "비밀 꼬리"),
        ("result", "보고"),
        ("prompt", "다음"),
        ("queued", "대기"),
        ("phase", "검토 중"),
    ]:
        store.add_event(job.id, LEAD, kind, message)
    rows = store.turn_events(job.id)
    assert [(r.kind, r.message) for r in rows] == [
        ("start", ""),
        ("error", ""),
        ("result", "보고"),
        ("prompt", "다음"),
        ("queued", "대기"),
    ]
    assert "idx_events_job_kind" in {
        r[0]
        for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    store.close()


# ── 묶음 보여 주기 조립 (topics.panel_bundles·cli_bundles, TUI·CLI 공용) ─────────────────────


def _done_job(store: Store, prompt: str, repo: str = "/r") -> int:
    job = store.create_job(prompt=prompt, repo=repo, workdir=repo, model=None).id
    store.add_event(job, LEAD, "result", f"{prompt} 보고")
    store.finish_job(job, "succeeded")
    return job


def _more(store: Store, job: int, prompt: str, finish: bool = True) -> int:
    assert store.requeue_job(job, prompt)
    if finish:
        store.add_event(job, LEAD, "result", f"{prompt} 보고")
        store.finish_job(job, "succeeded")
    return store.last_follow_up(job).id


def test_panel_bundles는_선택한_채팅의_차례가_든_묶음을_최근_것부터_보여준다(tmp_path):
    from coliday import topics
    from coliday.store import TurnAnswer

    store = Store(tmp_path / "jobs.db")
    a = _done_job(store, "주문 취소")
    login = _more(store, a, "로그인도")
    b = _done_job(store, "다른 채팅의 주문 취소")
    order = store.assign_turns(a, [TurnAnswer(0, None, "주문 취소")], expect={})[0]
    alone = store.assign_turns(
        a, [TurnAnswer(login, None, "로그인", "로그인 고침")], expect={}
    )[login]
    store.move_turns(b, [0], order)
    running = _more(store, a, "아직 하는 중", finish=False)
    store.queue_message(a, "대기 요청")

    view = topics.panel_bundles(store, a)
    assert [v.bundle.name for v in view.bundles] == [
        "로그인",
        "주문 취소",
    ]  # 혼자인 묶음도 보이고, 이 채팅의 최근 차례가 든 묶음이 위
    first, second = view.bundles
    assert (first.turn_count, first.job_ids, first.hidden) == (1, (a,), 0)
    assert [(t.job.id, t.turn.index, t.link.line) for t in first.turns] == [
        (a, 2, "로그인 고침")
    ]
    assert [(t.job.id, t.turn.turn_id) for t in second.turns] == [
        (b, 0),
        (a, 0),
    ]  # 다른 채팅 차례도, 최근 것부터
    assert second.job_ids == (b, a) and second.last_at == second.turns[0].link.asked_at
    assert [(t.turn.turn_id, t.turn.state, t.link) for t in view.loose] == [
        (running, "queued", None)
    ]
    assert view.queued == ("대기 요청",) and view.more == 0
    assert topics.panel_bundles(store, 999) == topics.BundleList((), ())
    assert alone != order
    store.close()


def test_panel_bundles는_묶음마다_최근_N개와_이_채팅_차례를_더_넣고_나머지는_센다(
    tmp_path,
):
    from coliday import topics
    from coliday.store import TurnAnswer

    store = Store(tmp_path / "jobs.db")
    mine = _done_job(store, "내 채팅")
    bundle = store.assign_turns(mine, [TurnAnswer(0, None, "긴 일")], expect={})[0]
    for n in range(4):
        store.move_turns(_done_job(store, f"다른 {n}"), [0], bundle)
    view = topics.panel_bundles(store, mine, recent=2, extra=1)
    [item] = view.bundles
    assert [t.job.id for t in item.turns] == [5, 4, mine] and (
        item.turn_count,
        item.hidden,
    ) == (5, 2)
    assert topics.panel_bundles(store, mine, recent=2, extra=0).bundles[0].hidden == 3
    store.close()


def test_TurnCache는_이벤트와_상태가_같으면_다시_읽지_않는다(tmp_path, monkeypatch):
    from coliday import topics

    store = Store(tmp_path / "jobs.db")
    a = _done_job(store, "주문")
    reads = []
    real = store.turn_events
    monkeypatch.setattr(
        store, "turn_events", lambda job_id: reads.append(job_id) or real(job_id)
    )
    cache = topics.TurnCache()
    assert [t.index for t in cache.get(store, [a, 999])[a][1]] == [1]
    cache.get(store, [a])
    assert reads == [a]
    _more(store, a, "이어서")
    assert [t.index for t in cache.get(store, [a])[a][1]] == [1, 2] and reads == [a, a]
    store.close()


def test_TurnCache는_작업을_읽은_직후_시작한_후속_요청을_다음_조회에서_읽는다(
    tmp_path, monkeypatch
):
    from coliday import topics

    path = tmp_path / "jobs.db"
    store = Store(path)
    writer = Store(path)
    job_id = _done_job(store, "첫 요청")
    other_id = _done_job(store, "다른 채팅")
    get_jobs = store.get_jobs
    batches = []

    def read_then_start(ids):
        jobs = get_jobs(ids)
        batches.append(set(ids))
        assert writer.requeue_job(job_id, "그사이 보낸 요청")
        assert writer.start_job(job_id, 123)
        return jobs

    cache = topics.TurnCache()
    with monkeypatch.context() as patch:
        patch.setattr(store, "get_jobs", read_then_start)
        first = cache.get(store, [job_id, other_id])
    assert batches == [{job_id, other_id}]
    job, found, _ = first[job_id]
    assert job.status == "succeeded"
    assert [(t.question, t.state) for t in found] == [("첫 요청", "succeeded")]

    job, found, _ = cache.get(store, [job_id])[job_id]
    assert job.status == "running"
    assert [(t.question, t.state) for t in found] == [
        ("첫 요청", "done"),
        ("그사이 보낸 요청", "running"),
    ]
    writer.finish_job(job_id, "failed")  # 이벤트 추가 없이 상태만 바뀌어도 갱신한다.
    assert cache.get(store, [job_id])[job_id][1][-1].state == "failed"
    assert writer.delete_job(job_id)
    assert cache.get(store, [job_id]) == {}
    writer.close()
    store.close()


def test_cli_bundles는_최근에_차례가_든_묶음부터_보이고_안_묶인_차례를_모은다(tmp_path):
    from coliday import topics
    from coliday.store import TurnAnswer

    store = Store(tmp_path / "jobs.db")
    a = _done_job(store, "첫")
    old = store.assign_turns(a, [TurnAnswer(0, None, "옛 일")], expect={})[0]
    b = _done_job(store, "둘째")
    new = store.assign_turns(b, [TurnAnswer(0, None, "새 일")], expect={})[0]
    loose = _more(store, b, "아직 안 묶음")
    view = topics.cli_bundles(store, store.list_jobs(100), per_bundle=1)
    assert [v.bundle.id for v in view.bundles] == [new, old]
    assert [(t.job.id, t.turn.turn_id) for t in view.loose] == [(b, loose)]
    store.close()
