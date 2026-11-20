"""`claude -p --output-format stream-json` 이벤트를 사람이 읽는 타임라인으로 바꾼다.

확인한 이벤트 형태 (Claude Code 2.1.x):
- assistant 메시지: parent_tool_use_id 가 없으면 팀장, 있으면 그 Agent 호출로 뜬 서브에이전트
- tool_use name 이 Agent/Task 면 서브에이전트 분배 (input.subagent_type, input.description)
- system/task_started, task_notification: 서브에이전트 시작/종료 (tool_use_id 로 연결)
- result: 턴 종료. 백그라운드 서브에이전트가 있으면 여러 번 나오므로 마지막 비어있지 않은 결과를 쓴다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

LEAD = "팀장"
_DISPATCH_TOOLS = frozenset({"Agent", "Task"})
_MAX_LEN = 160


@dataclass(frozen=True)
class Event:
    actor: str
    kind: str  # start | dispatch | tool | text | done | phase | error
    message: str


@dataclass
class Translator:
    session_id: str | None = None
    model: str | None = None
    result: str | None = None
    cost_usd: float | None = None
    is_error: bool = False
    lead: str = LEAD  # 최상위 메시지의 주체. Codex 팀장 아래에서 팀원을 따로 실행할 때는 팀원 이름
    _agents: dict[str, str] = field(default_factory=dict)  # Agent tool_use_id -> 표시 이름
    _started: bool = False

    def feed(self, raw: dict) -> list[Event]:
        kind = raw.get("type")
        if kind == "system":
            return self._system(raw)
        if kind == "assistant":
            return self._assistant(raw)
        if kind == "result":
            self._result(raw)
        return []

    def _system(self, raw: dict) -> list[Event]:
        sub = raw.get("subtype")
        if sub == "init":
            self.session_id = raw.get("session_id") or self.session_id
            if self._started:
                return []
            self._started = True
            self.model = raw.get("model")
            return [Event(self.lead, "start", f"작업 시작 (model={self.model})")]
        if sub == "task_started":
            name = self._remember(raw.get("tool_use_id"), raw.get("subagent_type"))
            return [Event(name, "start", _clip(raw.get("description") or "작업 시작"))]
        if sub == "task_notification":
            name = self._agents.get(raw.get("tool_use_id") or "", "서브에이전트")
            status = raw.get("status") or "completed"
            return [
                Event(name, "done", f"종료 ({status})"),
                Event(self.lead, "phase", f"{name} 결과 검토 중"),
            ]
        return []

    def _assistant(self, raw: dict) -> list[Event]:
        parent = raw.get("parent_tool_use_id")
        actor = self._agents.get(parent, "서브에이전트") if parent else self.lead
        content = (raw.get("message") or {}).get("content")
        if not isinstance(content, list):
            return []
        out: list[Event] = []
        for block in content:
            btype = block.get("type")
            if btype == "tool_use":
                out.append(self._tool_use(actor, block))
            elif btype == "text" and actor == self.lead:
                text = (block.get("text") or "").strip()
                if text:
                    out.append(Event(actor, "text", _clip(text.splitlines()[0])))
        return out

    def _tool_use(self, actor: str, block: dict) -> Event:
        name = block.get("name") or "?"
        inp = block.get("input") or {}
        if name in _DISPATCH_TOOLS:
            target = self._remember(block.get("id"), inp.get("subagent_type"))
            desc = inp.get("description") or ""
            return Event(actor, "dispatch", _clip(f"{target}에게 분배: {desc}".rstrip(": ")))
        if name == "SubagentHandback":
            return Event(actor, "tool", "결과 보고 제출")
        return Event(actor, "tool", _clip(f"{name} {_target(inp)}".strip()))

    def _result(self, raw: dict) -> None:
        self.session_id = raw.get("session_id") or self.session_id
        cost = raw.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            self.cost_usd = max(self.cost_usd or 0.0, float(cost))
        if raw.get("is_error"):
            self.is_error = True
        text = raw.get("result")
        if isinstance(text, str) and text.strip():
            self.result = text

    def _remember(self, tool_use_id: str | None, subagent_type: str | None) -> str:
        if tool_use_id and tool_use_id in self._agents:
            return self._agents[tool_use_id]
        name = subagent_type or "general-purpose"
        if tool_use_id:
            self._agents[tool_use_id] = name
        return name


@dataclass
class CodexTranslator:
    """`codex exec --json` 이벤트 번역기 (codex-cli 0.16x 에서 확인한 형태).

    - thread.started: 세션 시작 (thread_id 로 resume)
    - item.started/completed: command_execution(셸 명령), file_change, agent_message(마지막 것이 최종 답)
    - turn.failed / error: 실패
    """

    lead: str = LEAD
    hide: str | None = None  # 이 문자열이 든 셸 명령은 타임라인에 넣지 않는다 (팀원 분배 명령)
    session_id: str | None = None
    model: str | None = None
    result: str | None = None
    cost_usd: float | None = None  # Codex 는 토큰 수만 알려 준다
    is_error: bool = False

    def feed(self, raw: dict) -> list[Event]:
        kind = raw.get("type")
        item = raw.get("item") if isinstance(raw.get("item"), dict) else {}
        if kind == "thread.started":
            self.session_id = raw.get("thread_id") or self.session_id
            return [Event(self.lead, "start", "작업 시작 (codex)")]
        if kind in ("turn.failed", "error"):
            self.is_error = True
            error = raw.get("error") if isinstance(raw.get("error"), dict) else {}
            return [Event(self.lead, "error", _clip(str(error.get("message") or raw.get("message") or "Codex 오류")))]
        if kind == "item.started" and item.get("type") == "command_execution":
            command = _shell_command(str(item.get("command") or ""))
            if self.hide and self.hide in command:
                return []
            return [Event(self.lead, "tool", _clip(f"Bash {command}"))]
        if kind == "item.completed" and item.get("type") == "file_change":
            changes = item.get("changes") if isinstance(item.get("changes"), list) else []
            names = ", ".join(os.path.basename(str(c.get("path"))) for c in changes if isinstance(c, dict))
            return [Event(self.lead, "tool", _clip(f"Edit {names}".strip()))]
        if kind == "item.completed" and item.get("type") == "agent_message":
            text = str(item.get("text") or "").strip()
            if text:
                self.result = text
                return [Event(self.lead, "text", _clip(text.splitlines()[0]))]
        return []


def _shell_command(command: str) -> str:
    """Codex 는 명령을 `/bin/zsh -lc '...'` 로 감싸 보고한다. 타임라인에는 안쪽 명령만 보여 준다."""
    for marker in (" -lc ", " -c "):
        head, sep, inner = command.partition(marker)
        if sep and "/" in head and " " not in head.strip():
            inner = inner.strip()
            if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "'\"":
                inner = inner[1:-1]
            return inner
    return command


def _target(inp: dict) -> str:
    for key in ("file_path", "notebook_path", "path"):
        if inp.get(key):
            return os.path.basename(str(inp[key]))
    for key in ("command", "pattern", "url", "query", "description", "skill"):
        if inp.get(key):
            return str(inp[key]).splitlines()[0]
    return ""


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _MAX_LEN else text[: _MAX_LEN - 1] + "…"
