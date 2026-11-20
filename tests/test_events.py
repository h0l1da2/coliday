import json
from pathlib import Path

from coliday.events import LEAD, Translator

FIXTURE = Path(__file__).parent / "fixtures" / "stream.jsonl"


def _feed_all(t: Translator):
    events = []
    for line in FIXTURE.read_text().splitlines():
        events += t.feed(json.loads(line))
    return events


def test_분배_서브에이전트_도구_완료가_올바른_actor로_변환된다():
    events = [(e.actor, e.kind, e.message) for e in _feed_all(Translator())]

    assert events == [
        (LEAD, "start", "작업 시작 (model=claude-opus-5-5)"),
        (LEAD, "text", "설계부터 맡기겠습니다."),
        (LEAD, "dispatch", "architect에게 분배: 주문 취소 설계"),
        ("architect", "start", "주문 취소 설계"),
        ("architect", "tool", "Read OrderService.java"),
        ("architect", "tool", "결과 보고 제출"),
        ("architect", "done", "종료 (completed)"),
        (LEAD, "phase", "architect 결과 검토 중"),
        (LEAD, "tool", "Bash ./gradlew test"),
    ]


def test_result가_여러번_와도_마지막_비어있지_않은_보고와_최대_비용을_유지한다():
    t = Translator()
    _feed_all(t)

    assert t.result == "최종 보고: 완료"
    assert t.cost_usd == 0.05
    assert t.session_id == "s-1"
    assert t.is_error is False


def test_is_error_result가_하나라도_있으면_실패로_표시한다():
    t = Translator()
    t.feed({"type": "result", "is_error": True, "result": "API 오류"})
    t.feed({"type": "result", "is_error": False, "result": ""})

    assert t.is_error is True
    assert t.result == "API 오류"


def test_알수없는_tool_use_id의_서브에이전트_메시지는_기본_이름을_쓴다():
    events = Translator().feed(
        {"type": "assistant", "parent_tool_use_id": "unknown",
         "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "a/B.java"}}]}}
    )

    assert [(e.actor, e.message) for e in events] == [("서브에이전트", "Edit B.java")]


def test_긴_메시지는_잘라낸다():
    events = Translator().feed(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "가" * 500}]}}
    )

    assert len(events[0].message) == 160
    assert events[0].message.endswith("…")
