"""리뷰 담당을 부르는 횟수를 실행(요청) 하나마다 세고, 상한을 넘으면 그 호출을 막는다.

팀장 지시문의 "리뷰 반복 상한"은 모델이 따르는 규칙이라 어길 수 있다. 재리뷰가 매번 새 문제를 찾아
리뷰 → 재작업 → 재리뷰가 끝없이 돌면 작업 하나가 30분을 넘긴다. 그래서 프로그램이 직접 센다.

- Claude 팀장: `--settings` 로 PreToolUse 훅을 건다. 훅은 `python -m coliday.review_gate <카운터 파일>` 이고,
  Agent(Task) 도구로 리뷰 담당을 부를 때마다 센다. 상한을 넘으면 종료 코드 2 로 호출을 막고, 그 이유는 팀장에게 전달된다.
- Codex 팀장: 팀원 호출이 `members.Server` 를 거치므로 거기서 `take` 로 센다.

리뷰 담당은 역할 이름에 "review" 가 든 팀원이다 (기본 `reviewer`). 카운터는 실행마다 `reset` 으로 0 에서 시작한다.
"""

from __future__ import annotations

import fcntl
import json
import shlex
import sys
from pathlib import Path

from coliday.team import MAX_REVIEW_CALLS

_DISPATCH_MATCHER = "Agent|Task"


def is_review_role(role: str | None) -> bool:
    return bool(role) and "review" in str(role).lower()


def reset(path: Path) -> None:
    """이번 실행의 카운터를 0 으로 둔다 (이어서 시킨 요청은 그 요청 안에서 새로 센다)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"reviews": 0, "refused": 0}), encoding="utf-8")


def take(path: Path, role: str | None, limit: int = MAX_REVIEW_CALLS) -> str | None:
    """role 을 부르려 한다. 막아야 하면 팀장에게 줄 이유를, 아니면 None 을 돌려준다.

    리뷰 담당이 아니면 세지 않는다. 동시에 여러 리뷰를 부를 수 있어 파일을 잠그고 센다.
    """
    if not is_review_role(role):
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        try:
            state = json.loads(fh.read() or "{}")
        except ValueError:
            state = {}
        reviews, refused = int(state.get("reviews", 0)), int(state.get("refused", 0))
        allowed = reviews < limit
        state = {"reviews": reviews + 1 if allowed else reviews, "refused": refused if allowed else refused + 1}
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps(state))
    if allowed:
        return None
    return (
        f"coliday: 리뷰 반복 상한에 도달했습니다 (이 요청에서 리뷰 담당은 이미 {limit}번 불렀습니다). "
        "리뷰를 더 맡기지 말고, 마지막 리뷰에서 나온 문제를 재작업하지 말고 최종 보고의 \"남은 문제\"에 심각도와 함께 적은 뒤 끝내세요. "
        "[P0]·[P1] 이 남았으면 보고 맨 위에 \"차단 문제 남음\"이라고 쓰세요."
    )


def refused(path: Path) -> int:
    """이번 실행에서 막은 리뷰 호출 수. 카운터를 읽지 못하면 0."""
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("refused", 0))
    except (OSError, ValueError, AttributeError):
        return 0


def claude_hooks(path: Path) -> dict:
    """`claude --settings` 에 합칠 훅 설정."""
    command = f"{shlex.quote(sys.executable)} -m coliday.review_gate {shlex.quote(str(path))}"
    return {"hooks": {"PreToolUse": [{"matcher": _DISPATCH_MATCHER, "hooks": [{"type": "command", "command": command}]}]}}


def hook_main(argv: list[str], stdin: str) -> tuple[int, str]:
    """PreToolUse 훅. (종료 코드, stderr) 를 돌려준다. 2 면 Claude Code 가 그 호출을 막고 stderr 를 팀장에게 준다.

    입력을 읽지 못하는 등 훅 자체의 문제로는 작업을 막지 않는다 (0).
    """
    try:
        data = json.loads(stdin)
        role = (data.get("tool_input") or {}).get("subagent_type")
        reason = take(Path(argv[0]), role)
    except (IndexError, ValueError, AttributeError, OSError):
        return 0, ""
    return (2, reason) if reason else (0, "")


if __name__ == "__main__":
    code, message = hook_main(sys.argv[1:], sys.stdin.read())
    if message:
        sys.stderr.write(message + "\n")
    raise SystemExit(code)
