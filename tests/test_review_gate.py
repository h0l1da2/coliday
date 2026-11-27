"""리뷰 담당 호출 상한 (review_gate.py)."""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from coliday import review_gate, team

SRC = Path(__file__).resolve().parents[1] / "src"


def test_리뷰_담당만_세고_상한을_넘는_호출부터_막는다(tmp_path):
    counter = tmp_path / "review-count.json"
    review_gate.reset(counter)
    assert review_gate.take(counter, "coder") is None and review_gate.take(counter, None) is None
    for _ in range(team.MAX_REVIEW_CALLS):
        assert review_gate.take(counter, "reviewer") is None
    reason = review_gate.take(counter, "reviewer")
    assert reason and "리뷰 반복 상한" in reason and "남은 문제" in reason and "차단 문제 남음" in reason
    assert review_gate.take(counter, "security-Reviewer") is not None      # 이름에 review 가 든 역할은 모두 센다
    assert review_gate.take(counter, "coder") is None                      # 상한 뒤에도 다른 팀원은 막지 않는다
    assert review_gate.refused(counter) == 2

    review_gate.reset(counter)                                             # 다음 실행(요청)은 새로 센다
    assert review_gate.refused(counter) == 0 and review_gate.take(counter, "reviewer") is None


def test_동시에_불러도_상한만큼만_허용한다(tmp_path):
    counter = tmp_path / "review-count.json"
    review_gate.reset(counter)
    results = []
    threads = [threading.Thread(target=lambda: results.append(review_gate.take(counter, "reviewer"))) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(None) == team.MAX_REVIEW_CALLS and review_gate.refused(counter) == 12 - team.MAX_REVIEW_CALLS


def _hook(command, stdin):
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    return subprocess.run(command, shell=True, input=stdin, capture_output=True, text=True, env=env)


def test_훅은_상한을_넘으면_종료_코드_2와_이유를_돌려준다(tmp_path):
    counter = tmp_path / "jobs" / "1" / "review-count.json"
    review_gate.reset(counter)
    entry = review_gate.claude_hooks(counter)["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "Agent|Task"
    command = entry["hooks"][0]["command"]
    payload = lambda role: json.dumps({"tool_name": "Agent", "tool_input": {"subagent_type": role}})

    codes = [_hook(command, payload("reviewer")).returncode for _ in range(team.MAX_REVIEW_CALLS)]
    assert codes == [0] * team.MAX_REVIEW_CALLS
    blocked = _hook(command, payload("reviewer"))
    assert blocked.returncode == 2 and "리뷰 반복 상한" in blocked.stderr
    assert _hook(command, payload("coder")).returncode == 0
    assert _hook(command, "이상한 입력").returncode == 0                   # 훅 자체의 문제로 작업을 막지 않는다
