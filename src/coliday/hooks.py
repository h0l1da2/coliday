"""Claude 팀장 실행에 거는 PreToolUse 훅. 지시문만으로는 지켜지지 않는 규칙을 프로그램이 직접 막는다.

- Agent(Task): 리뷰 담당 호출 수를 세어 상한을 넘으면 막는다 (`review_gate`).
- Bash: Codex 래퍼(Codex 로 일하는 팀원을 대신 실행하는 Claude 팀원)는 정해진 명령만 실행할 수 있다.
  래퍼가 백그라운드로 넘어간 Codex 를 `pgrep -f <경로>` + sleep 반복문으로 기다리다, 그 경로가 팀장과 반복문 자신의
  명령줄에도 들어 있어 영원히 끝나지 않은 일이 있었다. 래퍼가 할 일은 정해져 있으니 허용 목록 밖은 모두 막는다.

훅 명령은 `python -m coliday.hooks <설정 파일>` 이다. 설정 파일(작업 폴더의 hooks.json)에는 카운터 경로, 래퍼 역할과
실행 파일, 작업 명세 폴더가 들어 있다. 훅 입력의 agent_type 은 그 Bash 를 실행한 팀원 이름이다 (팀장이면 없다).
종료 코드 2 면 Claude Code 가 그 호출을 막고 stderr 를 에이전트에게 준다. 훅 자체의 문제로는 막지 않는다 (0).
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

from coliday import review_gate

_MATCHER = "Agent|Task|Bash"
_SHELL_META = set(";&|<>`$\n\\(){}*?")  # 명령을 잇거나 바꿔 치는 글자: 허용 목록 비교를 피해 가지 못하게


def write_config(path: Path, *, review_counter: Path, wrappers: dict[str, str], tasks_dir: str) -> None:
    """wrappers 는 {래퍼 역할: 그 역할의 Codex 실행 파일 경로} 다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"review_counter": str(review_counter), "wrappers": wrappers, "tasks_dir": tasks_dir}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def claude_hooks(config: Path) -> dict:
    """`claude --settings` 에 합칠 훅 설정."""
    command = f"{shlex.quote(sys.executable)} -m coliday.hooks {shlex.quote(str(config))}"
    return {"hooks": {"PreToolUse": [{"matcher": _MATCHER, "hooks": [{"type": "command", "command": command}]}]}}


def wrapper_reason(command: str, runner: str, tasks_dir: str) -> str | None:
    """Codex 래퍼가 이 Bash 명령을 실행해도 되면 None, 아니면 막는 이유."""
    if _allowed(command.strip(), runner, tasks_dir.rstrip("/") + "/"):
        return None
    run = shlex.quote(runner)
    return (
        "coliday: Codex 래퍼는 Bash 로 다음 명령만 실행할 수 있습니다. "
        f"`{run} <작업 명세 파일>`(실행), `{run} --wait <작업 명세 파일>`(끝날 때까지 기다리기), "
        "`git status --short`, `git diff --stat`, `cat <작업 명세 파일>.done <작업 명세 파일>.out`. "
        "다른 명령과 묶지 말고 하나씩 실행하세요. pgrep·ps·sleep 반복문으로 기다리지 말고 --wait 를 쓰세요."
    )


def _allowed(command: str, runner: str, tasks_prefix: str) -> bool:
    if not command or any(ch in _SHELL_META for ch in command):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    in_tasks = lambda path: path.startswith(tasks_prefix) and ".." not in path.split("/")
    head, rest = tokens[0], tokens[1:]
    if head == runner:
        if rest[:1] == ["--wait"]:
            rest = rest[1:]
        return len(rest) == 1 and in_tasks(rest[0])
    if head == "git":
        if rest[:1] == ["-C"] and len(rest) >= 2:
            rest = rest[2:]
        return rest in (["status", "--short"], ["diff", "--stat"])
    if head == "cat":
        return bool(rest) and all(in_tasks(p) and p.endswith((".done", ".out")) for p in rest)
    return False


def decide(config: dict, data: dict) -> str | None:
    """훅 입력 하나에 대해 막을 이유를 돌려준다. 막지 않으면 None."""
    tool = data.get("tool_name")
    tool_input = data.get("tool_input") or {}
    if tool in ("Agent", "Task"):
        return review_gate.take(Path(config["review_counter"]), tool_input.get("subagent_type"))
    if tool == "Bash":
        runner = (config.get("wrappers") or {}).get(data.get("agent_type") or "")
        if runner:
            return wrapper_reason(str(tool_input.get("command") or ""), runner, str(config.get("tasks_dir") or ""))
    return None


def main(argv: list[str], stdin: str) -> tuple[int, str]:
    """(종료 코드, stderr). 설정이나 입력을 읽지 못하면 막지 않는다."""
    try:
        config = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
        reason = decide(config, json.loads(stdin))
    except (IndexError, ValueError, AttributeError, KeyError, TypeError, OSError):
        return 0, ""
    return (2, reason) if reason else (0, "")


if __name__ == "__main__":
    code, message = main(sys.argv[1:], sys.stdin.read())
    if message:
        sys.stderr.write(message + "\n")
    raise SystemExit(code)
