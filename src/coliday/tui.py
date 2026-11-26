"""coliday TUI.

┌ 작업 목록 ──────────────┐┌ 변경 사항 (diff) ┐
├ 타임라인 ───────────────┤│                  │
└─────────────────────────┘└──────────────────┘

DB 는 메인 스레드에서만 읽고 쓴다. git diff 만 백그라운드 스레드에서 계산한다.

타임라인과 diff 는 마우스로 드래그해 복사할 수 있어야 한다. Textual 은 Text 를 그리는 Static 과
Markdown 위젯만 선택을 지원하므로(RichLog, rich Syntax/Markdown 은 안 됨) 그 위젯들로만 그린다.
"""

from __future__ import annotations

import dataclasses
import functools
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import (
    Button, Checkbox, DataTable, Footer, Header, Input, Label, Markdown, OptionList, Select, Static, TextArea,
)
from textual.widgets.option_list import Option

from coliday import gitops, guard, joblist, jobs, prefs, projects, remote, team
from coliday import scope as scopes
from coliday.config import Config
from coliday.jobs import STATUS_ICON, JobError, NotARepoError, WorktreeRemoveError
from coliday.store import QUEUED, RESULT, EventRow, Job, Store

_MAIN_ACTIONS = frozenset({
    "new_job", "focus_projects", "add_project", "rename_project", "remove_project",
    "models", "prompts", "cancel_job", "remove_job", "remove_listed_job", "timeline_scroll", "open_artifacts", "env_access",
    "copy_timeline", "list_menu",
})
POLL_SEC = 1.0
DIFF_EVERY_TICKS = 2
MAX_DIFF_CHARS = 300_000

_GROUP_BY_LABEL: dict[str, str] = {"none": "없음", "status": "상태"}  # 순서가 ctrl+b 전환 순서다
_FINISHED_LABEL = {"succeeded": "완료", "failed": "실패", "cancelled": "취소"}
_OTHER_NAME = "기타"
_OTHER_CACHE_SEC = 5.0  # 작업을 다 읽어야 하는 기타 줄 판정을 다시 하기까지 (그 사이 생긴 기타 작업은 늦게 보인다)


def _while_open(method: Callable[..., None]) -> Callable[..., None]:
    """앱이 닫히는 중이면 건너뛴다 (이벤트·타이머·스레드 콜백용).

    닫을 때 화면의 위젯을 먼저 지우는데, 그 사이 쌓여 있던 이벤트(커서 이동 등)가 처리되면 지운 위젯을 찾다가
    NoMatches 로 종료 자체가 오류가 된다.
    """

    @functools.wraps(method)
    def wrapper(self: App, *args: object, **kwargs: object) -> None:
        if not self.is_running:
            return None
        return method(self, *args, **kwargs)

    return wrapper

_STATUS_STYLE = {
    "queued": "dim", "running": "bold yellow", "succeeded": "green",
    "failed": "bold red", "cancelled": "dim",
}
_ACTOR_COLORS = ["cyan", "green", "yellow", "blue", "magenta", "bright_cyan", "bright_green"]
_KIND_STYLE = {
    "dispatch": "bold", "done": "green", "error": "bold red", "phase": "italic", "text": "dim",
    "prompt": "bold",
}


class NewJobScreen(ModalScreen[tuple[str, str, bool, bool, int | None] | None]):
    """결과는 (요청, 디렉토리, worktree 없이, .env 읽기 허용, 파생할 부모 작업 번호 또는 None)이다."""

    BINDINGS = [Binding("escape", "close", "닫기")]

    def __init__(self, default_dir: str, derive_from: int | None = None, derive: bool = False) -> None:
        super().__init__()
        self._default_dir = default_dir
        self._derive_from = derive_from  # 작업 목록에서 고른 작업. 없으면 파생 칸이 없다
        self._derive = derive and derive_from is not None

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("새 작업", id="dialog-title")
            yield Input(placeholder="팀장에게 시킬 일 (Enter 로 시작)", id="prompt")
            if self._derive_from is not None:
                yield Checkbox(f"선택한 작업 #{self._derive_from} 에서 파생 (그 변경 위에서 새 worktree·새 세션)",
                               self._derive, id="derive")
            yield Input(value=self._default_dir, placeholder="프로젝트 디렉토리 (서버는 user@host:/경로)", id="dir")
            yield Checkbox("worktree 없이 이 디렉토리에서 직접 작업", id="no-worktree")
            # 기본은 막는다: 에이전트는 사람이 여기서 켠 작업에서만 .env 를 읽을 수 있다.
            yield Checkbox("이 작업에서 .env 파일 읽기 허용 (기본: 에이전트가 읽지 못하게 막음)", id="allow-env")
            with Horizontal(id="dialog-buttons"):
                yield Button("시작", variant="primary", id="ok")
                yield Button("닫기", id="close")

    def on_mount(self) -> None:
        self._show_derive()
        self.query_one("#prompt", Input).focus()

    @property
    def _deriving(self) -> bool:
        return self._derive_from is not None and self.query_one("#derive", Checkbox).value

    @on(Checkbox.Changed, "#derive")
    def _show_derive(self) -> None:
        # 파생 작업은 부모의 위치와 방식(worktree 여부)을 따른다: 고를 것을 잠근다.
        deriving = self._deriving
        self.query_one("#dir", Input).disabled = deriving
        self.query_one("#no-worktree", Checkbox).display = not deriving

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def _submit(self) -> None:
        prompt = self.query_one("#prompt", Input).value.strip()
        if not prompt:
            self.notify("요청 내용을 입력하세요.", severity="warning")
            return
        deriving = self._deriving
        self.dismiss((
            prompt, self.query_one("#dir", Input).value,
            self.query_one("#no-worktree", Checkbox).value and not deriving, self.query_one("#allow-env", Checkbox).value,
            self._derive_from if deriving else None,
        ))

    @on(Button.Pressed, "#close")
    def action_close(self) -> None:
        self.dismiss(None)


class ListMenuScreen(ModalScreen[str | None]):
    """목록 정리 메뉴 (ctrl+b). 결과는 고른 항목의 id 다. 묶기 기준 항목은 창을 닫지 않고 그 자리에서 바뀐다."""

    BINDINGS = [Binding("escape", "close", "닫기")]

    def __init__(self, items: list[tuple[str, str, bool]], cycle_group_by: Callable[[], str]) -> None:
        super().__init__()
        self._items = items  # (id, 글, 쓸 수 있음)
        self._cycle_group_by = cycle_group_by

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("목록 정리", id="dialog-title")
            # 그룹 이름에 든 대괄호가 markup 으로 해석되지 않게 Text 로 넘긴다.
            yield OptionList(*(Option(Text(label), id=key, disabled=not ok) for key, label, ok in self._items),
                             id="list-menu")
            yield Label("↑↓ 고르기 · Enter 실행 · Esc 닫기", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#list-menu", OptionList).focus()

    @on(OptionList.OptionSelected, "#list-menu")
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        if event.option_id == "group_by":  # Enter 마다 다음 기준으로 (바로 저장된다)
            event.option_list.replace_option_prompt("group_by", Text(self._cycle_group_by()))
            return
        self.dismiss(event.option_id)

    def action_close(self) -> None:
        self.dismiss(None)


class NameScreen(ModalScreen[str | None]):
    """이름 하나를 받는다 (그룹 만들기·이름 바꾸기)."""

    BINDINGS = [Binding("escape", "close", "닫기")]

    def __init__(self, title: str, value: str = "") -> None:
        super().__init__()
        self._title = title
        self._value = value

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(Text(self._title), id="dialog-title")
            yield Input(value=self._value, placeholder="그룹 이름", id="name")
            yield Label("Enter 로 저장, Esc 로 닫기", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#name", Input).focus()

    @on(Input.Submitted)
    def _submit(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        if not name:
            self.notify("이름을 입력하세요.", severity="warning")
            return
        self.dismiss(name)

    def action_close(self) -> None:
        self.dismiss(None)


class JobTable(DataTable):
    """작업 목록. 머리줄(묶음)과 작업 줄이 섞인다. 좌우 키는 접기·펴기와 부모로 이동이다 (글자 키는 채팅 입력이라 쓰지 않는다)."""

    BINDINGS = [
        Binding("left", "app.job_left", "접기", show=False),
        Binding("right", "app.job_right", "펴기", show=False),
    ]

    async def _on_click(self, event: events.Click) -> None:
        # DataTable 은 이미 고른 줄을 다시 눌러야 선택 이벤트를 낸다. 머리줄은 한 번 눌러도 접고 편다.
        # (이미 고른 머리줄을 누른 것은 RowSelected 로 처리된다.) 이 처리는 DataTable 의 클릭 처리보다 먼저 불린다.
        row = event.style.meta.get("row") if event.style else None
        if isinstance(row, int) and 0 <= row < self.row_count and row != self.cursor_row:
            key = self.coordinate_to_cell_key(Coordinate(row, 0)).row_key.value
            if key and key.startswith("h:"):
                self.call_later(self.app.toggle_section, key[2:])


class FolderLine(Static):
    """타임라인의 '위치' 줄. 클릭하면 그 폴더를 연다."""

    def __init__(self, text: Text, path: str) -> None:
        super().__init__(text, classes="folder-line")
        self._path = path

    def on_click(self) -> None:
        if not self.screen.get_selected_text():  # 경로를 드래그해 복사하는 중이면 열지 않는다
            self.app.open_path(self._path)


class ArtifactBar(Static):
    """선택한 작업의 결과물 폴더 한 줄. 클릭하면 폴더를 연다. 결과물이 없으면 숨긴다."""

    def on_click(self) -> None:
        self.app.action_open_artifacts()


class EnvBar(Static):
    """선택한 작업의 .env 읽기 상태 한 줄. 클릭하면 그 작업에 허용하거나 다시 막는다 (버튼)."""

    def on_click(self) -> None:
        self.app.action_env_access()


class ProjectTable(DataTable):
    """연결된 프로젝트 목록. 커서가 있는 줄의 프로젝트 작업만 작업 목록에 보이고, 새 작업도 거기서 시작한다.

    목록에 없는 위치의 작업이 있으면 맨 아래에 '기타' 줄이 생긴다 (키 scope.OTHER).
    """

    BINDINGS = [
        Binding("ctrl+e", "app.rename_project", "이름 바꾸기", key_display="E"),
        Binding("ctrl+x", "app.remove_project", "목록에서 빼기", key_display="X"),
    ]


_LOCAL, _SSH = "local", "ssh"


class AddProjectScreen(ModalScreen[tuple[str, str, str] | None]):
    """프로젝트 위치와 이름 입력. 결과는 (위치, 이름, 서버 비밀번호)다.

    추가할 때는 먼저 이 컴퓨터의 폴더인지 SSH 서버인지 고르고, SSH 일 때만 주소·포트·비밀번호 칸이 나온다.
    이름만 바꿀 때(adding=False)는 위치를 그대로 보여 주고 이름만 받는다.
    """

    BINDINGS = [Binding("escape", "close", "닫기")]

    def __init__(self, directory: str, name: str = "", title: str = "프로젝트 추가", adding: bool = True) -> None:
        super().__init__()
        self._directory = directory
        self._name = name
        self._title = title
        self._adding = adding

    def compose(self) -> ComposeResult:
        host, path = remote.split(self._directory)
        with Vertical(id="dialog"):
            yield Label(self._title, id="dialog-title")
            if not self._adding:
                yield Input(value=self._directory, id="dir", disabled=True)
                yield Input(value=self._name, placeholder="이름 (비우면 폴더 이름)", id="name")
                yield Label("Enter 로 저장, Esc 로 닫기", classes="hint")
                return
            yield Select(
                [("이 컴퓨터의 폴더", _LOCAL), ("SSH 로 접속하는 서버의 폴더", _SSH)],
                value=_LOCAL if host is None else _SSH, allow_blank=False, id="kind",
            )
            with Vertical(id="local-fields"):
                yield Input(value=path if host is None else "", placeholder="프로젝트 디렉토리 (예: ~/work_dir/shop-be)", id="dir")
            with Vertical(id="ssh-fields"):
                bare, _, port = (host or "").partition(":")
                with Horizontal(id="ssh-address"):
                    yield Input(value=bare, placeholder="서버 (예: dev@192.168.0.10, 또는 ~/.ssh/config 의 별칭)", id="host")
                    yield Input(value=port, placeholder="포트 (22)", id="port", max_length=5)
                yield Input(value=path if host is not None else "", placeholder="서버의 폴더 (예: /srv/shop, ~/shop)", id="remote-dir")
                yield Input(password=True, placeholder="비밀번호 (키로 접속되면 비운다)", id="password")
            yield Input(value=self._name, placeholder="이름 (비우면 폴더 이름)", id="name")
            yield Label("에이전트(claude, codex)는 이 컴퓨터에서 실행되고 SSH 로 서버의 파일을 읽고 고치고 명령을 실행한다: "
                        "서버에는 아무것도 설치하지 않아도 된다. 서버의 .env 는 읽지 못하게 막을 수 없다. "
                        "비밀번호는 한 번만 써서 이 컴퓨터의 SSH 키를 서버에 등록하고 저장하지 않는다 (그 뒤로는 키로 접속).",
                        classes="hint", id="ssh-hint")
            yield Label("Enter 로 저장, Tab 으로 칸 이동, Esc 로 닫기", classes="hint")

    def on_mount(self) -> None:
        if not self._adding:
            self.query_one("#name", Input).focus()
            return
        self._show_fields()
        self.query_one("#dir" if self._kind == _LOCAL else "#host", Input).focus()

    @property
    def _kind(self) -> str:
        return str(self.query_one("#kind", Select).value)

    def _show_fields(self) -> None:
        ssh = self._kind == _SSH
        self.query_one("#local-fields").display = not ssh
        self.query_one("#ssh-fields").display = ssh
        self.query_one("#ssh-hint").display = ssh

    @on(Select.Changed, "#kind")
    def _kind_changed(self) -> None:
        self._show_fields()

    def _text(self, selector: str) -> str:
        return self.query_one(selector, Input).value.strip()

    @on(Input.Submitted)
    def _submit(self) -> None:
        if not self._adding:
            self.dismiss((self._directory, self._text("#name"), ""))
            return
        if self._kind == _LOCAL:
            if not self._text("#dir"):
                self.notify("디렉토리를 입력하세요.", severity="warning")
                return
            self.dismiss((self._text("#dir"), self._text("#name"), ""))
            return
        host, path = self._text("#host"), self._text("#remote-dir")
        if not host or not path:
            self.notify("서버와 서버의 폴더를 입력하세요.", severity="warning")
            return
        try:
            location = f"{remote.with_port(host, self._text('#port'))}:{path}"
            remote.target(remote.split(location)[0] or "")  # 주소 형식이 틀리면 여기서 알린다
        except remote.RemoteError as exc:
            self.notify(str(exc), severity="warning")
            return
        self.dismiss((location, self._text("#name"), self.query_one("#password", Input).value))

    def action_close(self) -> None:
        self.dismiss(None)


_FOLLOW = "follow"  # 역할 선택 상자의 '기본 에이전트를 따름'


class ModelScreen(ModalScreen[dict[str, str] | None]):
    """맨 위에서 기본 에이전트를 고르고, 역할마다 에이전트와 모델을 따로 고를 수 있다.

    결과는 {역할: 값} 이고 값은 `claude`, `codex`, `claude:<모델>`, `codex:<모델>` 또는 ""(기본을 따름)이다.
    """

    BINDINGS = [Binding("escape", "close", "닫기")]

    def __init__(self, agents: dict[str, dict], models: team.Models) -> None:
        super().__init__()
        self._roles = [team.LEAD_ROLE, *agents]
        self._agents = agents
        self._models = models
        self._current = {team.LEAD_ROLE: models.lead or "", **{n: models.agents.get(n, "") for n in agents}}

    def compose(self) -> ComposeResult:
        engines = [("claude", team.CLAUDE), ("codex", team.CODEX)]
        default_engine, default_model = _split_spec(self._models.default)
        with Vertical(id="dialog"):
            yield Label("에이전트·모델 설정 (다음 실행부터 적용)", id="dialog-title")
            with Horizontal(classes="model-row"):
                yield Label("기본 에이전트", classes="model-role")
                yield Select(engines, value=default_engine, allow_blank=False, id="default-engine", classes="engine")
                yield Input(value=default_model, placeholder="모델 (비우면 기본 모델)", id="default-model")
            yield Label("아래에서 따로 고르지 않은 역할은 기본 에이전트로 일한다. 모델 칸은 비우면 그 에이전트의 기본 모델.",
                        classes="hint")
            for i, role in enumerate(self._roles):  # 팀원 이름은 위젯 id 로 못 쓰는 문자가 있을 수 있어 순번을 쓴다
                engine, model = _split_spec(self._current[role]) if self._current[role] else (_FOLLOW, "")
                with Horizontal(classes="model-row"):
                    yield Label("팀장 (lead)" if role == team.LEAD_ROLE else role, classes="model-role")
                    yield Select([("기본을 따름", _FOLLOW), *engines], value=engine, allow_blank=False,
                                 id=f"engine-{i}", classes="engine")
                    yield Input(value=model, placeholder=self._placeholder(role), id=f"model-{i}")
            yield Label("모델 이름은 모두 소문자로, 띄어쓰기 없이 하이픈으로 쓴다. 예: gpt-6.1-sol, opus (GPT-6.1 Sol ✗)",
                        classes="hint")
            yield Label("에이전트 codex + 모델 gpt-6.1-sol 은 codex:gpt-6.1-sol 로 저장된다.  모델 칸에서 Enter 또는 저장 버튼.",
                        classes="hint")
            with Horizontal(id="dialog-buttons"):
                yield Button("저장", variant="primary", id="ok")
                yield Button("닫기", id="close")

    def _placeholder(self, role: str) -> str:
        rows = {name: text for name, text, _ in team.model_rows(self._agents, team.Models(default=self._models.default))}
        return f"모델 (지금 기본: {rows[role]})"

    def on_mount(self) -> None:
        self.query_one("#default-engine", Select).focus()
        self._ready = False  # 처음 값을 채울 때 나오는 Changed 이벤트는 사용자가 바꾼 것이 아니다
        self.call_after_refresh(lambda: setattr(self, "_ready", True))

    @on(Select.Changed)
    def _engine_changed(self, event: Select.Changed) -> None:
        """에이전트를 바꾸면 그 줄의 모델 칸을 비운다. 모델 이름은 에이전트마다 달라 남겨 두면 codex:opus 같은 값이 된다."""
        if not getattr(self, "_ready", False):
            return
        model_id = "default-model" if event.select.id == "default-engine" else (event.select.id or "").replace("engine-", "model-")
        field = self.query_one(f"#{model_id}", Input)
        if field.value:
            field.value = ""
            self.notify("에이전트를 바꿔서 모델 칸을 비웠습니다 (비우면 그 에이전트의 기본 모델).", timeout=4)

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def _submit(self) -> None:
        answer = {team.DEFAULT_ROLE: _join_spec(self.query_one("#default-engine", Select).value,
                                                self.query_one("#default-model", Input).value)}
        for i, role in enumerate(self._roles):
            engine = self.query_one(f"#engine-{i}", Select).value
            model = self.query_one(f"#model-{i}", Input).value
            answer[role] = "" if engine == _FOLLOW else _join_spec(engine, model)
        self.dismiss(answer)

    @on(Button.Pressed, "#close")
    def action_close(self) -> None:
        self.dismiss(None)


def _split_spec(spec: str) -> tuple[str, str]:
    """설정 값 → (에이전트, 모델). 에이전트 없이 모델만 쓴 값은 Claude 모델이다."""
    if team.is_codex(spec):
        return team.CODEX, team.codex_model(spec) or ""
    return team.CLAUDE, team.canonical(spec) or ""


def _join_spec(engine: object, model: str) -> str:
    model = model.strip()
    return f"{engine}:{model}" if model else str(engine)


class TeamScreen(ModalScreen[None]):
    """팀 구성: 역할을 고르면 실행에 쓰이는 프롬프트가 아래에 보이고, 고치기·추가·빼기·되돌리기를 한다."""

    # 한글 자모: 한글 입력 상태에서 누른 a/d/r/q.
    BINDINGS = [
        Binding("escape,q,ㅂ", "close", "닫기"),
        Binding("a,ㅁ", "add", "추가"),
        Binding("d,ㅇ", "remove", "빼기"),
        Binding("r,ㄱ", "reset", "기본값으로"),
    ]

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self._roles: list[tuple[str, str, str]] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="team"):
            yield DataTable(id="team-table", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="prompts"):
                yield Static(id="team-preview")
            yield Label("Enter 고치기 · a 새 팀원 · d 팀에서 빼기 · r 기본값으로 되돌리기 · Tab 프롬프트 스크롤 · Esc 닫기"
                        "   (다음 실행부터 적용)", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#team").border_title = "팀 구성과 역할별 프롬프트"
        self.query_one("#prompts").border_title = "실행에 쓰이는 프롬프트"
        table = self.query_one("#team-table", DataTable)
        table.add_columns("역할", "상태", "설명")
        self._reload()
        table.focus()

    def _reload(self, select: str | None = None) -> None:
        table = self.query_one("#team-table", DataTable)
        select = select or self._role()
        try:
            self._roles = jobs.team_roles(self.cfg)
        except JobError as exc:
            self._roles = []
            self.query_one("#team-preview", Static).update(Text(str(exc), style="bold red"))
        table.clear()
        for role, state, description in self._roles:
            style = {team.STATE_REMOVED: "dim strike", team.STATE_DEFAULT: ""}.get(state, "bold yellow")
            name = "lead (팀장)" if role == team.LEAD_ROLE else role
            table.add_row(Text(name, style=style), Text(state, style=style), Text(description, style="dim"), key=role)
        names = [r for r, _, _ in self._roles]
        if names:
            table.move_cursor(row=names.index(select) if select in names else 0)
            self._preview()

    def _role(self) -> str | None:
        table = self.query_one("#team-table", DataTable)
        return self._roles[table.cursor_row][0] if self._roles and 0 <= table.cursor_row < len(self._roles) else None

    def _state(self, role: str) -> str:
        return next(state for r, state, _ in self._roles if r == role)

    @on(DataTable.RowHighlighted, "#team-table")
    def _preview(self) -> None:
        role = self._role()
        if role is None:
            return
        try:
            if self._state(role) == team.STATE_REMOVED:
                text = Text("팀에서 뺀 역할입니다. r 을 누르면 되돌립니다.\n\n", style="bold yellow")
                text.append(jobs.role_definition(self.cfg, role)["prompt"], style="dim")
            else:
                text = Text()
                for title, body in jobs.role_prompts(self.cfg, role):
                    text.append(f"── {title} ──\n", style="bold")
                    text.append(f"{body}\n")
        except JobError as exc:
            text = Text(str(exc), style="bold red")
        self.query_one("#team-preview", Static).update(text)
        self.query_one("#prompts", VerticalScroll).scroll_home(animate=False)

    @on(DataTable.RowSelected, "#team-table")
    def action_edit(self) -> None:
        role = self._role()
        if role is None:
            return
        if self._state(role) == team.STATE_REMOVED:
            self.notify("팀에서 뺀 역할입니다. r 로 되돌린 뒤 고치세요.", severity="warning")
            return
        try:
            definition = jobs.role_definition(self.cfg, role)
        except JobError as exc:
            self.notify(str(exc), severity="error", timeout=8)
            return
        self.app.push_screen(RoleEditScreen(self.cfg, role, definition), self._edited)

    def action_add(self) -> None:
        self.app.push_screen(RoleEditScreen(self.cfg, None, {"description": "", "prompt": "", "tools": None}), self._edited)

    def _edited(self, role: str | None) -> None:
        if role:
            self._reload(role)
            self.notify(f"{role} 을(를) 저장했습니다. 다음 실행부터 적용됩니다.")

    def action_remove(self) -> None:
        role = self._role()
        if role is None or role == team.LEAD_ROLE:
            self.notify("팀장은 뺄 수 없습니다.", severity="warning")
            return
        state = self._state(role)
        if state == team.STATE_REMOVED:
            return
        if state == team.STATE_ADDED:  # 새로 넣은 역할은 빼면 정의가 사라진다
            self.app.push_screen(
                ConfirmScreen(f"{role} 을(를) 지울까요? 직접 쓴 프롬프트가 사라집니다."),
                lambda yes: self._apply(lambda: jobs.remove_role(self.cfg, role), f"{role} 을(를) 지웠습니다.") if yes else None,
            )
            return
        self._apply(lambda: jobs.remove_role(self.cfg, role), f"{role} 을(를) 팀에서 뺐습니다. r 로 되돌릴 수 있습니다.", role)

    def action_reset(self) -> None:
        role = self._role()
        if role is None:
            return
        state = self._state(role)
        if state == team.STATE_DEFAULT:
            self.notify("이미 기본값입니다.")
        elif state == team.STATE_ADDED:
            self.notify("새로 넣은 역할은 기본값이 없습니다. 지우려면 d 를 누르세요.", severity="warning")
        else:
            self._apply(lambda: jobs.reset_role(self.cfg, role), f"{role} 을(를) 기본값으로 되돌렸습니다.", role)

    def _apply(self, change, message: str, select: str | None = None) -> None:
        try:
            change()
        except JobError as exc:
            self.notify(str(exc), severity="error", timeout=8)
            return
        self._reload(select)
        self.notify(message)

    def action_close(self) -> None:
        self.dismiss(None)


class RoleEditScreen(ModalScreen[str | None]):
    """역할 하나를 고치거나(role) 새 팀원을 만든다(role=None). 저장에 성공하면 역할 이름으로 닫힌다.

    저장이 실패하면 닫지 않는다 (길게 쓴 프롬프트가 사라지지 않게).
    """

    BINDINGS = [Binding("escape", "close", "닫기"), Binding("ctrl+s", "save", "저장", priority=True)]

    def __init__(self, cfg: Config, role: str | None, definition: dict) -> None:
        super().__init__()
        self.cfg = cfg
        self._role = role
        self._definition = definition
        self._warned = False

    @property
    def _is_lead(self) -> bool:
        return self._role == team.LEAD_ROLE

    def compose(self) -> ComposeResult:
        tools = self._definition.get("tools")
        with Vertical(id="dialog"):
            title = "새 팀원" if self._role is None else ("팀장 지시문" if self._is_lead else f"{self._role} 고치기")
            yield Label(f"{title} (다음 실행부터 적용)", id="dialog-title")
            if self._role is None:
                yield Input(placeholder="역할 이름 (영문·숫자·-, 예: qa)", id="role-name")
            if not self._is_lead:
                yield Input(value=self._definition.get("description") or "", id="role-description",
                            placeholder="설명: 팀장이 이걸 보고 언제 맡길지 정한다 (예: 구현이 끝난 뒤 사용자 시나리오를 검수한다)")
                yield Input(value=", ".join(tools) if tools else "", id="role-tools",
                            placeholder="도구를 쉼표로 (예: Read, Grep, Glob, Bash). 비우면 모든 도구. Edit·Write 가 없으면 코드를 못 고친다")
            yield TextArea(self._definition.get("prompt") or "", id="role-prompt", soft_wrap=True)
            if self._is_lead:
                yield Label("{team} 자리에 팀원 목록(이름과 설명)이, {max_attempts} 자리에 최대 시도 횟수가 들어간다. "
                            "결과물 폴더 규칙은 자동으로 뒤에 붙는다.", classes="hint")
            else:
                yield Label("재시도 규칙과 결과물 폴더 규칙은 자동으로 뒤에 붙는다. 모델은 모델 설정(Ctrl+T)에서 고른다.",
                            classes="hint")
            with Horizontal(id="dialog-buttons"):
                yield Button("저장 (Ctrl+S)", variant="primary", id="ok")
                yield Button("취소", id="close")

    def on_mount(self) -> None:
        self.query_one("#role-name" if self._role is None else "#role-prompt").focus()

    def _values(self) -> tuple[str, dict]:
        name = self._role or self.query_one("#role-name", Input).value.strip()
        values: dict = {"prompt": self.query_one("#role-prompt", TextArea).text}
        if not self._is_lead:
            values["description"] = self.query_one("#role-description", Input).value
            tools = [t.strip() for t in self.query_one("#role-tools", Input).value.split(",") if t.strip()]
            values["tools"] = tools or None
        return name, values

    def _dirty(self) -> bool:
        name, values = self._values()
        if self._role is None and name:
            return True
        return any(
            (v.strip() if isinstance(v, str) else v) != (self._definition.get(k) or (None if k == "tools" else ""))
            for k, v in values.items()
        )

    @on(Button.Pressed, "#ok")
    def action_save(self) -> None:
        name, values = self._values()
        try:
            if not name:
                raise JobError("역할 이름을 주세요.")
            if self._role is None and any(name == role for role, _, _ in jobs.team_roles(self.cfg)):
                raise JobError(f"이미 있는 역할입니다: {name}. 목록에서 골라 고치세요.")
            jobs.set_role(self.cfg, name, **values)
        except JobError as exc:
            self.notify(str(exc), severity="error", timeout=8)
            return
        self.dismiss(name)

    @on(Button.Pressed, "#close")
    def action_close(self) -> None:
        if self._dirty() and not self._warned:
            self._warned = True
            self.notify("저장하지 않은 변경이 있습니다. 버리려면 한 번 더 누르세요.", severity="warning", timeout=6)
            return
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    # ㅛ/ㅜ: 한글 입력 상태에서 누른 y/n.
    BINDINGS = [
        Binding("y,ㅛ", "answer(True)", "예"),
        Binding("n,ㅜ,escape", "answer(False)", "아니오"),
        Binding("left,up", "app.focus_previous", "이전", show=False),
        Binding("right,down", "app.focus_next", "다음", show=False),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self._question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(Text(self._question))  # 질문에 든 대괄호가 markup 으로 해석되지 않게 한다
            with Horizontal(id="dialog-buttons"):
                yield Button("예 (y)", variant="primary", id="yes")
                yield Button("아니오 (n)", id="no")
            yield Label("←/→ 또는 Tab 으로 고르고 Enter, 마우스 클릭도 됩니다.", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#yes", Button).focus()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)


class ColidayApp(App[None]):
    TITLE = "coliday"
    CSS = """
    #left { width: 3fr; }
    #key-hint { height: 1; padding: 0 1; color: $text-muted; }
    #projects { height: auto; max-height: 8; border: round $success; }
    #jobs { height: 2fr; border: round $primary; }
    #timeline { height: 3fr; border: round $secondary; }
    #timeline Markdown { margin: 0; padding: 0; }
    .folder-line:hover { background: $primary; color: $text; }
    #artifacts { height: auto; padding: 0 1; background: $boost; color: $text-muted; text-style: underline; display: none; }
    #artifacts.-shown { display: block; }
    #artifacts.-has { color: $success; }
    #artifacts:hover { background: $primary; color: $text; }
    #env-access { height: auto; padding: 0 1; background: $boost; color: $text-muted; display: none; }
    #env-access.-shown { display: block; }
    #env-access.-needed { background: $warning; color: $background; text-style: bold; }
    #env-access.-allowed { color: $success; }
    #env-access:hover { background: $primary; color: $text; }
    #diff-pane { width: 2fr; border: round $accent; }
    #chat, #chat:focus { border-left: none; border-right: none; }
    #prompt, #prompt:focus { border-left: none; border-right: none; }
    #projects:focus-within, #jobs:focus-within, #timeline:focus, #diff-pane:focus-within { border: round $warning; }
    NewJobScreen, AddProjectScreen, ModelScreen, TeamScreen, RoleEditScreen, ConfirmScreen, ListMenuScreen, NameScreen {
        align: center middle;
    }
    #list-menu { height: auto; max-height: 20; margin-top: 1; }
    #team { width: 92%; height: 92%; padding: 0 1; border: thick $primary; background: $surface; }
    #team-table { height: auto; max-height: 12; }
    #prompts { height: 1fr; padding: 0 1; border: round $secondary; }
    #prompts:focus { border: round $warning; }
    RoleEditScreen #dialog { width: 110; max-width: 96%; height: 92%; overflow-y: hidden; }
    #role-prompt { height: 1fr; min-height: 4; margin-top: 1; }  /* 남는 높이를 다 쓴다: 낮은 터미널에서도 버튼이 보인다 */
    .model-row { height: auto; }
    .model-role { width: 16; margin-top: 2; }
    .model-row .engine { width: 22; margin-top: 1; }
    .model-row Input { width: 1fr; }
    ModelScreen #dialog { width: 96; }
    #dialog { width: 80; height: auto; max-height: 100%; overflow-y: auto; padding: 1 2; border: thick $primary; background: $surface; }
    ConfirmScreen #dialog Label { width: 100%; }  /* 긴 질문이 잘리지 않고 줄바꿈되게 */
    #dialog Input, #dialog Checkbox, #dialog Select { margin-top: 1; }
    #local-fields, #ssh-fields, #ssh-address { height: auto; }
    #ssh-fields Input { margin-top: 0; }  /* 칸이 많다: 테두리만으로 구분해 낮은 터미널에서도 한 화면에 들어오게 */
    #ssh-fields { margin-top: 1; }
    #host { width: 1fr; }
    #port { width: 16; }
    #dialog-title { text-style: bold; }
    #dialog-buttons { height: auto; margin-top: 1; align-horizontal: right; }
    #dialog-buttons Button { margin-left: 2; }
    .hint { color: $text-muted; margin-top: 1; width: 100%; }  /* 긴 안내는 잘리지 않고 줄바꿈된다 */
    """
    BINDINGS = [
        # 글자 키는 채팅 입력에 쓰이므로 단축키는 모두 ctrl 조합이다. priority: 채팅 입력 중에도 동작한다.
        # 입력창의 줄 편집 키(ctrl+a/e/d/k/u/w)와 ctrl+m(=Enter)은 피했다.
        Binding("ctrl+n", "new_job", "새 작업", priority=True, key_display="N"),
        Binding("ctrl+o", "focus_projects", "프로젝트", priority=True, key_display="O"),
        Binding("ctrl+g", "add_project", "프로젝트 추가", priority=True, key_display="G"),
        Binding("ctrl+t", "models", "모델", priority=True, key_display="T"),
        Binding("ctrl+p", "prompts", "팀·프롬프트", priority=True, key_display="P"),
        Binding("ctrl+f", "open_artifacts", "결과물 폴더", priority=True, key_display="F"),
        Binding("ctrl+l", "env_access", ".env 허용", priority=True, key_display="L"),
        Binding("ctrl+y", "copy_timeline", "타임라인 복사", priority=True, key_display="Y"),
        # tmux 기본 접두 키(ctrl+b)와 겹친다: tmux 안에서는 ctrl+b 를 두 번 누르거나 접두 키를 바꾼다.
        Binding("ctrl+b", "list_menu", "목록 정리", priority=True, key_display="B"),
        Binding("ctrl+s", "cancel_job", "작업 취소", priority=True, key_display="S"),
        Binding("ctrl+r", "remove_job", "작업 삭제", priority=True, key_display="R"),
        # 작업 목록에서 고른 채로 누르는 Delete (맥의 delete 는 backspace 로 들어온다). 입력 칸에서는 글자를 지우는 키라 priority 가 아니다.
        Binding("delete,backspace", "remove_listed_job", "작업 삭제", show=False),
        Binding("tab", "focus_next", "패널 이동", show=False),
        # priority 가 아니다: 목록·diff 에 포커스가 있으면 그 패널이 스스로 넘기고, 채팅 입력 중에는 타임라인을 넘긴다.
        Binding("pageup,alt+up", "timeline_scroll(-1)", "타임라인 위로", show=False),
        Binding("pagedown,alt+down", "timeline_scroll(1)", "타임라인 아래로", show=False),
        # ctrl+c 는 Textual 기본으로는 복사다. 복사는 드래그하면 바로 되므로 종료에 쓴다 (작업은 백그라운드에서 계속된다).
        Binding("ctrl+c", "quit", "종료", priority=True, key_display="C"),
        Binding("ctrl+q", "quit", "종료", show=False, priority=True, key_display="Q"),
    ]
    ENABLE_COMMAND_PALETTE = False  # 기본 명령 팔레트가 ctrl+p 를 쓴다

    def __init__(self, cfg: Config, store: Store, default_dir: str | None = None) -> None:
        super().__init__()
        self.cfg = cfg
        self.store = store
        self.default_dir = default_dir or os.getcwd()
        self.project_dir: str | None = None  # 새 작업을 시작할 프로젝트 (기타를 보는 중이면 바로 전에 고른 프로젝트)
        self.scope: str | None = None  # 보는 프로젝트 목록 줄의 키 (프로젝트 경로, scope.OTHER, 없으면 None)
        self._projects: list[tuple[str, str]] | None = None  # (경로, 표시 이름)
        self._project_rows: tuple | None = None  # 그린 프로젝트 목록 (이름들, 기타 줄 있음): 바뀌었을 때만 다시 그린다
        self._run_cells: dict[str, str] = {}  # 프로젝트 줄 키 → 그린 실행 개수 글
        self._shown_scope: str | None = None  # 작업 목록을 마지막으로 그린 프로젝트
        self._remembered: dict[str, int] = {}  # 프로젝트 줄 키 → 그 프로젝트에서 마지막으로 보던 작업 (메모리만)
        self._active: dict[int, str] | None = None  # 실행·대기 중인 작업 → 소속 (None: 아직 한 번도 세지 않음)
        self._worktrees = str(cfg.worktrees_dir)  # 작업의 소속을 계산할 때 worktree 뿌리를 찾는 기준
        self._other_cache: tuple[tuple, float, bool] | None = None  # 기타 줄 판정 (후보 레포, 유효 시각, 결과)
        self._projects_error: str | None = None
        self.selected_id: int | None = None
        self._header_key: str | None = None  # 커서가 머리줄에 있으면 그 묶음 key (이때 selected_id 는 None)
        self._row_snapshot: dict[str, tuple] = {}  # 줄 키(joblist.row_key) → 셀
        self._lines: list[joblist.Line] = []  # 지금 그린 줄 (표의 줄 번호와 같은 순서)
        self._meta: dict[int, joblist.JobMeta] = {}
        self._groups: list[joblist.JobGroup] = []
        self._job_ids: set[int] = set()  # 목록에 있는 작업 (접힌 것 포함)
        self._prefs = prefs.load(cfg.ui_path)  # 묶기 기준과 접힘. 깨진 파일이면 기본값
        self._marked: set[int] = set()  # 작업 목록에서 Space 로 고른 작업들 (한꺼번에 삭제한다)
        self._deriving: set[int] = set()  # 지금 파생 작업을 만들고 있는 부모 (같은 부모에서 겹쳐 만들지 않게)
        self._last_event_id = 0
        self._result_shown = False
        self._result_event_seen = False
        self._last_status: str | None = None
        self._diff_text: str | None = None
        self._copied: str | None = None
        self._artifacts_text = ""
        self._env_state: tuple[bool, bool] | None = None  # (허용됨, 에이전트가 필요로 함)
        self._ticks = 0

    # ── 화면 구성 ────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="left"):
                yield ProjectTable(id="projects", cursor_type="row", show_header=False)
                yield JobTable(id="jobs", cursor_type="row", zebra_stripes=True)
                yield VerticalScroll(id="timeline")
                yield ArtifactBar(id="artifacts")
                yield EnvBar(id="env-access")
            with VerticalScroll(id="diff-pane"):
                yield Static("작업을 선택하세요.", id="diff")
        # 입력 칸은 화면 너비를 다 쓰고 좌우 테두리가 없다: 한글을 조합하는 동안 터미널(macOS 터미널 등)이 조합 중인 글자를
        # 커서 자리에 끼워 넣어 그 줄의 오른쪽을 밀어내는데, 오른쪽에 테두리나 다른 패널이 있으면 그것이 튀어나와 보인다.
        # select_on_focus 를 끄지 않으면 포커스를 받을 때 전체 선택돼 다음 글자가 쓴 내용을 지운다.
        yield Input(id="chat", select_on_focus=False)
        yield Static("아래 단축키는 Ctrl 키를 누른 채로 입력합니다 (예: Ctrl+N 새 작업). 글자만 치면 채팅 입력으로 들어갑니다.", id="key-hint")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#jobs", DataTable)
        self._update_jobs_title()
        self._update_marks_hint()
        self.query_one("#timeline").border_title = "타임라인"
        self.query_one("#timeline").border_subtitle = "PgUp/PgDn·휠로 이전 내용 · 드래그하면 복사 · Ctrl+Y 전체 복사"
        self.query_one("#diff-pane").border_title = "변경 사항"
        self.query_one("#diff-pane").border_subtitle = "드래그하면 복사"
        for key, label in (("st", ""), ("id", "#"), ("age", "경과"), ("repo", "레포"),
                           ("prompt", "요청"), ("phase", "현재 단계")):
            table.add_column(label, key=key)
        ptable = self.query_one("#projects", ProjectTable)
        ptable.add_column("이름", key="name")
        ptable.add_column("실행", key="run")  # 실행·대기 중인 작업 수 (다른 프로젝트 것도 보이게)
        ptable.add_column("경로", key="path")
        opened = projects.normalize(self.default_dir)
        if gitops.repo_root(opened) is not None:  # TUI 를 연 레포는 자동으로 연결한다
            try:
                jobs.add_project(self.cfg, self.store, opened)
            except JobError:
                pass  # 원인은 refresh_projects 가 알린다
        self.refresh_projects(select=opened)  # 연 디렉토리가 속한 프로젝트를 고른다 (하위 폴더면 가장 가까운 상위)
        self._watch_others()
        self.refresh_jobs()
        self._update_chat_hint()
        # 작업 위치부터 고르게 한다. 연결된 프로젝트가 없으면 고를 것이 없으니 작업 목록에서 시작한다.
        (ptable if self._projects else table).focus()
        self.set_interval(POLL_SEC, self._tick)
        self._check_update()
        self.set_timer(20, self._check_update)  # 방금 시작한 백그라운드 확인이 끝났을 즈음 한 번 더 본다

    # ── 주기 갱신 ────────────────────────────────────────────
    @_while_open
    def _tick(self) -> None:
        self._ticks += 1
        self.refresh_projects()
        self._watch_others()
        self.refresh_jobs()
        self._poll_selected()
        self._update_chat_hint()
        self._update_artifacts()

    def _project_paths(self) -> list[str]:
        return [path for path, _ in self._projects or []]

    def _scope_name(self, key: str | None) -> str:
        if key == scopes.OTHER:
            return _OTHER_NAME
        return dict(self._projects or []).get(key or "") or (remote.name(key) if key else "")

    def _has_other(self, paths: list[str]) -> bool:
        """목록에 있는 어느 프로젝트에도 속하지 않는 작업이 있는가 (기타 줄을 보일지).

        하위 폴더만 프로젝트인 레포가 있으면 그 레포의 작업을 다 읽어야 알 수 있어 결과를 잠깐 기억한다 (매 초 읽지 않게).
        작업이 생기거나 지워지면 바로 다시 보고, 그 밖의 변화(worktree 를 만들어 위치가 정해짐)는 기억이 끝나면 반영된다.
        """
        repos = self.store.repos()
        candidates = scopes.candidate_repos(scopes.OTHER, repos, paths) if paths else repos
        if not candidates:  # 거의 언제나 여기서 끝난다 (작업은 프로젝트 안에서 시작하므로)
            return False
        key = (tuple(paths), tuple(sorted(candidates)), self.store.jobs_stamp())  # 작업이 생기거나 지워지면 다시 본다
        cached = self._other_cache
        if cached is not None and cached[0] == key and time.monotonic() < cached[1]:
            return cached[2]
        found = scopes.has_other(self.store, repos, paths, self._worktrees)
        self._other_cache = (key, time.monotonic() + _OTHER_CACHE_SEC, found)
        return found

    def refresh_projects(self, select: str | None = None) -> None:
        """목록 파일이 바뀌었으면(다른 터미널 포함) 다시 그린다.

        select 를 주면 그 줄을 고른다. 목록에 없는 위치면 그 위치를 품는 가장 가까운 프로젝트를, 그것도 없으면 첫 줄을 고른다.
        보던 줄이 사라지면(목록에서 뺐거나 기타 작업이 모두 없어짐) 첫 줄로 옮긴다.
        """
        try:
            names = jobs.project_names(self.cfg, self.store)
            self._projects_error = None
        except JobError as exc:
            names = {}
            if str(exc) != self._projects_error:  # 매 초 같은 알림을 띄우지 않는다
                self._projects_error = str(exc)
                self.notify(str(exc), severity="error", timeout=8)
        table = self.query_one("#projects", ProjectTable)
        paths = list(names)
        self._projects = list(names.items())
        if self.project_dir is not None and self.project_dir not in names:
            self.project_dir = None  # 목록에서 뺀 곳에는 새 작업을 만들지 않는다 (기타를 보는 중이면 연 디렉토리로)
        other = self._has_other(paths)
        keys = [*paths, *([scopes.OTHER] if other else [])]
        drawn = (tuple(names.items()), other)
        explicit = select is not None
        if drawn != self._project_rows:
            self._project_rows = drawn
            table.clear()
            self._run_cells = {}
            for path, name in names.items():
                missing = "" if projects.exists(path) else "  (없음)"
                table.add_row(name, "", Text(projects.display(path) + missing, style="dim"), key=path)
            if other:
                table.add_row(Text(_OTHER_NAME, style="italic"), "", Text("목록에 없는 위치의 작업", style="dim"),
                              key=scopes.OTHER)
            select = select or self.scope
        self._show_run_counts()
        if select is not None:
            if select in keys:
                key: str | None = select
            elif explicit and (found := scopes.owner(select, paths)) is not None:
                key = found
            else:
                key = keys[0] if keys else None
            if key is not None:
                table.move_cursor(row=keys.index(key))
            self._set_scope(key)
        self._show_project_title()
        self._update_jobs_title()

    def _show_run_counts(self) -> None:
        """프로젝트 줄마다 실행·대기 중인 작업 수. 바뀐 칸만 고친다 (매 초 다시 그리면 깜빡인다)."""
        if self._project_rows is None:
            return
        table = self.query_one("#projects", ProjectTable)
        counts: dict[str, int] = {}
        for where in (self._active or {}).values():
            counts[where] = counts.get(where, 0) + 1
        names, other = self._project_rows
        for key in [*(path for path, _ in names), *([scopes.OTHER] if other else [])]:
            text = f"● {counts[key]}" if counts.get(key) else ""
            if self._run_cells.get(key, "") != text:
                self._run_cells[key] = text
                table.update_cell(key, "run", Text(text, style="bold yellow"), update_width=True)

    def _watch_others(self) -> None:
        """모든 프로젝트의 실행·대기 중인 작업을 센다. 다른 프로젝트의 작업이 끝나면 알린다.

        작업 목록은 보는 프로젝트의 작업만 정리(reconcile)하므로, 죽은 worker 로 남은 다른 프로젝트의 작업도 여기서 정리한다.
        """
        paths = self._project_paths()
        active: dict[int, str] = {}
        for job in self.store.active_jobs():
            job = jobs.reconcile(self.store, job)
            if not job.is_terminal:
                active[job.id] = scopes.scope_of(job, paths, self._worktrees)
        previous, self._active = self._active, active
        for job_id in (previous or {}).keys() - active.keys():
            try:
                job = self.store.get_job(job_id)
            except KeyError:
                continue  # 지워졌다
            where = scopes.scope_of(job, paths, self._worktrees)
            if job.is_terminal and where != self.scope:  # 보고 있는 프로젝트의 작업은 목록에서 바로 보인다
                self.notify(f"{self._scope_name(where)}: 작업 #{job.id} {_FINISHED_LABEL.get(job.status, job.status)}",
                            severity="error" if job.status == "failed" else "information", timeout=6, markup=False)
        self._show_run_counts()

    def _set_scope(self, key: str | None) -> None:
        """보는 프로젝트를 바꾼다: 그 프로젝트의 작업만 보이고 새 작업도 거기서 시작한다. 같은 값이면 아무것도 하지 않는다."""
        if key == self.scope:
            return
        if self.scope is not None and self.selected_id is not None:
            self._remembered[self.scope] = self.selected_id  # 돌아오면 이 작업을 다시 고른다
        self.scope = key
        if key != scopes.OTHER:
            self.project_dir = key
        self._show_project_title()
        self._update_jobs_title()
        self.refresh_jobs()
        self._update_chat_hint()
        chat = self.query_one("#chat", Input)
        if chat.value.strip():  # 글이 있으면 입력 칸의 안내(placeholder)가 보이지 않는다: 보낼 곳이 바뀐 것을 한 번 알린다
            name = self._scope_name(key)
            if self._header_key is not None or (self.selected_id is None and key == scopes.OTHER):
                where = f"아직 보낼 곳이 없습니다 ({name}). 작업을 고르세요"
            elif self.selected_id is not None:
                where = f"이제 #{self.selected_id}({name})에 보냅니다"
            else:
                where = f"이제 {name} 의 새 작업으로 보냅니다"
            self.notify(f"입력 중인 글은 {where}.", timeout=6, markup=False)

    def _show_project_title(self) -> None:
        table = self.query_one("#projects", ProjectTable)
        if self.scope == scopes.OTHER:
            where = self._scope_name(self.project_dir) if self.project_dir else Path(self.default_dir).name
            title = f"프로젝트 · 기타 보는 중 · 새 작업 위치: {where}"
        elif self.scope is not None:
            title = f"프로젝트 · 보는 중: {self._scope_name(self.scope)} (새 작업도 여기)"
        else:
            title = "프로젝트 · 없음 (Ctrl+G 로 추가)"
        # 제목은 markup 으로 해석되므로 대괄호를 쓰지 않는다.
        table.border_title = Text(title).markup
        table.border_subtitle = "↑↓ 고르면 그 프로젝트의 작업만 · Enter 확정 · Ctrl+G 추가 · Ctrl+E 이름 · Ctrl+X 빼기 · Ctrl+O 여기로"

    def _project_row_key(self) -> str | None:
        """프로젝트 목록 커서가 있는 줄의 키 (이벤트가 아니라 지금 표에서 읽는다: 다시 그린 뒤의 오래된 이벤트를 믿지 않는다)."""
        table = self.query_one("#projects", ProjectTable)
        if not table.row_count or not 0 <= table.cursor_row < table.row_count:
            return None
        return table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value

    @on(DataTable.RowHighlighted, "#projects")
    @_while_open
    def _on_project_highlight(self, event: DataTable.RowHighlighted) -> None:
        # 목록을 다시 그릴 때 쌓인 오래된 이벤트는 무시한다 (작업 목록과 같다).
        if event.row_key.value is None or event.cursor_row != event.data_table.cursor_row:
            return
        self._set_scope(self._project_row_key())

    @on(DataTable.RowSelected, "#projects")
    @_while_open
    def _on_project_chosen(self, event: DataTable.RowSelected) -> None:
        """Enter(또는 클릭)로 확정하고 작업 목록으로 넘어간다."""
        key = self._project_row_key()
        if key is None:
            return
        self._set_scope(key)
        if key == scopes.OTHER:
            self.notify("기타: 목록에 없는 위치의 작업입니다. 새 작업은 프로젝트를 고르거나 ctrl+n 으로 위치를 정하세요.")
        else:
            self.notify(f"작업 위치: {self._scope_name(key)} — ctrl+n 으로 새 작업", markup=False)
        self.query_one("#jobs", DataTable).focus()

    def action_focus_projects(self) -> None:
        self.query_one("#projects", ProjectTable).focus()

    def action_add_project(self) -> None:
        # 추가 창에서 이름을 비우면 (이미 연결된 디렉토리의) 기존 이름을 지우지 않는다.
        self.push_screen(AddProjectScreen(self.default_dir), lambda a: self._save_project(a and (a[0], a[1] or None, a[2])))

    def _real_project(self) -> str | None:
        """보는 줄이 실제 프로젝트면 그 경로. 아니면 알리고 None."""
        if self.scope == scopes.OTHER:
            self.notify("기타는 프로젝트가 아닙니다 (목록에 없는 위치의 작업을 모아 보여 줄 뿐입니다).", severity="warning")
            return None
        if self.scope is None:
            self.notify("선택된 프로젝트가 없습니다.", severity="warning")
        return self.scope

    def action_rename_project(self) -> None:
        path = self._real_project()
        if path is None:
            return
        current = dict(self._projects or []).get(path, "")
        self.push_screen(AddProjectScreen(path, current, "프로젝트 이름 바꾸기", adding=False), self._save_project)

    def _save_project(self, answer: tuple[str, str | None, str] | None) -> None:
        if not answer:
            return
        directory, name, password = answer
        try:
            added = jobs.add_project(self.cfg, self.store, directory, name, password or None)
        except JobError as exc:
            self.notify(str(exc), severity="error", timeout=8)
            return
        self.refresh_projects(select=added)
        self.notify(f"프로젝트 연결: {dict(self._projects or []).get(added, added)}")

    def action_remove_project(self) -> None:
        removed = self._real_project()
        if removed is None:
            return
        try:
            jobs.remove_project(self.cfg, self.store, removed)
        except JobError as exc:
            self.notify(str(exc), severity="error", timeout=8)
            return
        self.notify(f"목록에서 뺐습니다: {Path(removed).name} (디렉토리와 작업은 그대로)")
        self.refresh_projects()

    def _scope_jobs(self) -> list[Job]:
        """보는 프로젝트의 작업 (최근 100개)."""
        if self.scope is None:
            return []
        return scopes.scope_jobs(self.store, self.scope, self.store.repos(), self._project_paths(),
                                 worktrees=self._worktrees)

    def refresh_jobs(self, reveal: int | None = None) -> None:
        """보는 프로젝트의 작업 목록을 묶기 기준대로 다시 그린다. reveal 작업(기본: 보고 있는 작업)은 접힌 곳에 있어도 이번만 펼친다.

        줄 구성이 같으면 바뀐 셀만 고친다 (1초마다 다시 그리면 깜빡인다).
        보는 프로젝트가 바뀐 뒤 처음 그릴 때는 그 프로젝트에서 전에 보던 작업 → 첫 작업 줄 → 비움 순으로 고른다.
        """
        table = self.query_one("#jobs", DataTable)
        first_draw = self._shown_scope != self.scope
        self._shown_scope = self.scope
        rows = [jobs.reconcile(self.store, j) for j in self._scope_jobs()]
        real = self.scope not in (None, scopes.OTHER)
        self._groups = self.store.list_groups(project=self.scope) if real else []
        visible = {g.id for g in self._groups}
        # 다른 프로젝트의 그룹에 든 작업이 여기 보이면 그룹이 없는 것으로 다룬다 (머리줄·메뉴 모두 보이는 그룹 기준).
        self._meta = {  # 기록이 없는 작업은 빠져 있다 (joblist 는 기본값으로 본다)
            job_id: item if item.group_id is None or item.group_id in visible else dataclasses.replace(item, group_id=None)
            for job_id, item in self.store.job_meta().items()
        }
        self._job_ids = {j.id for j in rows}
        now = time.time()
        self._marked &= self._job_ids  # 다른 곳에서 지워졌거나 다른 프로젝트로 옮긴 작업은 선택에서도 뺀다
        if first_draw and reveal is None:
            remembered = self._remembered.get(self.scope or "")
            reveal = remembered if remembered in self._job_ids else None
        # 상태가 바뀌어 접힌 묶음으로 옮겨간 작업도 커서와 타임라인을 잃지 않게 펼친다 (접힘 설정은 그대로).
        self._lines = joblist.build(
            rows, self._meta, self._groups, group_by=self._prefs.group_by, collapsed=self._prefs.collapsed,
            reveal=self.selected_id if reveal is None and not first_draw else reveal,
        )
        self._update_marks_hint()
        snapshot = {joblist.row_key(line): self._line_cells(line, now) for line in self._lines}
        redrawn = list(snapshot) != list(self._row_snapshot)
        old_row = table.cursor_row
        if redrawn:
            # 줄이 늘거나 줄었다 (작업 추가·삭제, 접기·펴기, 묶음 이동, 프로젝트 바꿈): 다시 그리고 커서 위치를 유지한다.
            table.clear()
            for key, cells in snapshot.items():
                table.add_row(*cells, key=key)
        else:
            columns = ("st", "id", "age", "repo", "prompt", "phase")
            for key, cells in snapshot.items():
                old = self._row_snapshot[key]
                for col, (a, b) in zip(columns, zip(old, cells)):
                    if a != b:
                        table.update_cell(key, col, b)
        self._row_snapshot = snapshot
        keys = list(snapshot)
        job_rows = [i for i, line in enumerate(self._lines) if isinstance(line, joblist.JobLine)]
        if first_draw:
            # 다른 프로젝트로 왔다: 전에 보던 작업, 없으면 첫 작업 줄. 머리줄 커서는 이어받지 않는다.
            self._header_key = None
            if reveal is not None and str(reveal) in snapshot:
                self._move_to_row(keys.index(str(reveal)))
            elif job_rows:
                self._move_to_row(job_rows[0])
            elif self._lines:  # 모든 묶음이 접혀 머리줄만 있다
                self._move_to_row(0)
            else:
                self._clear_selection()
        elif redrawn:
            if reveal is not None:
                target = str(reveal)
            elif self.selected_id is not None:
                target = str(self.selected_id)
            else:
                target = f"h:{self._header_key}" if self._header_key is not None else None
            if target in snapshot:
                table.move_cursor(row=keys.index(target))
            else:
                # 보던 작업(또는 머리줄)이 사라졌다 (다른 터미널에서 지운 경우 포함): 같은 자리 근처의 작업을 보여준다.
                self._header_key = None
                self._clear_selection()
                if job_rows:
                    start = min(old_row, len(keys) - 1)  # 지운 자리의 작업 (마지막이었으면 그 위). 머리줄은 건너뛴다
                    row = next((i for i in job_rows if i >= start), job_rows[-1])
                    table.move_cursor(row=row)
                    self.select_job(self._lines[row].job.id)
                elif self._lines:  # 모든 묶음이 접혀 머리줄만 있다
                    table.move_cursor(row=0)
                    self._enter_header(self._lines[0].key)
        if not rows:
            # 문자열은 markup 으로 해석돼 [n] 이 사라진다
            if self.scope == scopes.OTHER or self.scope is None:
                text = "아직 작업이 없습니다. 아래 입력창에 시킬 일을 쓰고 Enter 를 누르세요."
            else:
                text = f"이 프로젝트({self._scope_name(self.scope)})에는 아직 작업이 없습니다. 아래 입력창에 시킬 일을 쓰고 Enter 를 누르세요."
            self.query_one("#diff", Static).update(Text(text))

    def _line_cells(self, line: joblist.Line, now: float) -> tuple:
        if isinstance(line, joblist.Header):
            arrow = "▸" if line.collapsed else "▾"
            return ("", "", "", "", Text(f"{arrow} {line.label} ({line.count})", style="bold"), "")
        return self._row(line.job, now, line.job.id in self._marked, line)

    @staticmethod
    def _row(job: Job, now: float, marked: bool = False, line: joblist.JobLine | None = None) -> tuple:
        style = _STATUS_STYLE.get(job.status, "")
        status = Text("✔" if marked else " ", style="bold yellow")  # 선택 표시는 늘 한 칸을 차지한다 (열 너비가 변하지 않게)
        status.append(STATUS_ICON.get(job.status, "?"), style=style)
        prompt: str | Text = jobs.one_line(job.prompt, 40)
        if line is not None and (line.depth or line.child_count or line.detached_parent is not None):
            tree = Text("  " * (line.depth - 1) + "└ " if line.depth else "", style="dim")
            if line.child_count:
                tree.append("▸ " if line.collapsed else "▾ ", style="bold")
            if line.detached_parent is not None:  # 부모가 다른 묶음에 있거나 목록 밖에 있다
                tree.append(f"↳#{line.detached_parent} ", style="dim")
            tree.append(prompt)
            if line.child_count:
                tree.append(f" (+{line.child_count})", style="dim")
            prompt = tree
        return (
            status,
            str(job.id),
            jobs.ago(now - job.last_asked),
            remote.name(job.repo),
            prompt,
            Text(jobs.one_line(job.phase, 60), style=style),
        )

    def _cursor_line(self) -> joblist.Line | None:
        """커서가 있는 줄. 표의 줄은 self._lines 와 같은 순서로 그린다."""
        table = self.query_one("#jobs", DataTable)
        row = table.cursor_row
        if not table.row_count or not 0 <= row < len(self._lines):
            return None
        return self._lines[row]

    def _row_of(self, key: str) -> int | None:
        keys = list(self._row_snapshot)
        return keys.index(key) if key in keys else None

    @on(DataTable.RowHighlighted, "#jobs")
    @_while_open
    def _on_highlight(self, event: DataTable.RowHighlighted) -> None:
        # 목록을 다시 그리면 하이라이트 이벤트가 여러 번 쌓인다. 현재 커서와 다른 오래된 이벤트는 무시하고,
        # 줄은 이벤트가 아니라 지금 커서에서 읽는다 (다시 그린 뒤 같은 줄 번호에 머리줄이 올 수 있다).
        if event.row_key.value is None or event.cursor_row != event.data_table.cursor_row:
            return
        line = self._cursor_line()
        if isinstance(line, joblist.Header):
            self._enter_header(line.key)
        elif line is not None:
            self.select_job(line.job.id)

    def _move_to_row(self, row: int) -> None:
        self.query_one("#jobs", DataTable).move_cursor(row=row)
        line = self._lines[row]
        if isinstance(line, joblist.Header):
            self._enter_header(line.key)
        else:
            self.select_job(line.job.id)

    def _enter_header(self, key: str) -> None:
        """커서가 머리줄에 왔다: 보던 작업이 없는 상태가 된다 (작업 대상 단축키는 '선택된 작업이 없습니다')."""
        self._header_key = key
        if self.selected_id is not None:
            self._clear_selection()
        else:
            self._update_chat_hint()

    def _clear_selection(self) -> None:
        self.selected_id = None
        self._diff_text = None
        log = self.query_one("#timeline")
        log.remove_children()
        log.border_title = "타임라인"
        self.query_one("#diff-pane").border_title = "변경 사항"
        self.query_one("#diff", Static).update("작업을 선택하세요.")
        self._update_chat_hint()
        self._update_artifacts()

    def select_job(self, job_id: int) -> None:
        self._header_key = None
        if job_id == self.selected_id:
            return
        self.selected_id = job_id
        self._last_event_id = 0
        self._result_shown = False
        self._result_event_seen = False
        self._last_status = None
        self._diff_text = None
        log = self.query_one("#timeline", VerticalScroll)
        log.remove_children()
        self.call_after_refresh(log.scroll_end, animate=False)  # 최신 내용부터 보여준다. 이전 내용은 위로 올려 본다
        job = self.store.get_job(job_id)
        log.border_title = f"타임라인 #{job.id}"
        where = Text(f"위치: {job.workdir}" + (f"  (브랜치 {job.branch})" if job.branch else ""), style="dim")
        local = None if remote.is_remote(job.workdir or job.repo) else job.workdir
        self._timeline_write(
            Text(f"요청: {job.prompt}", style="bold"),
            FolderLine(where.append("  ← 클릭하면 폴더 열기"), local) if local else where,
        )
        self.query_one("#diff-pane").border_title = f"변경 사항 #{job.id}"
        self.query_one("#diff", Static).update("diff 계산 중…")
        self._poll_selected(force_diff=True)
        self._update_chat_hint()
        self._update_artifacts()

    def _poll_selected(self, force_diff: bool = False) -> None:
        if self.selected_id is None:
            return
        try:
            job = self.store.get_job(self.selected_id)
        except KeyError:
            return
        new_events = self.store.events(job.id, after_id=self._last_event_id)
        if new_events:
            # 연달아 온 이벤트는 위젯 하나로 묶는다 (이벤트마다 위젯을 만들면 긴 작업에서 느려진다).
            items: list[Text | Widget] = []
            lines: list[Text] = []
            for ev in new_events:
                if ev.kind != RESULT:
                    lines.append(_event_line(ev))
                    continue
                self._result_event_seen = True
                if lines:
                    items.append(Text("\n").join(lines))
                    lines = []
                ts = time.strftime("%H:%M:%S", time.localtime(ev.ts))
                items += [Text(f"── 팀장 보고 ({ts}) ──", style="bold"), self._report(job, ev.message)]
            if lines:
                items.append(Text("\n").join(lines))
            self._timeline_write(*items)
            self._last_event_id = new_events[-1].id
        if not job.is_terminal:
            self._result_shown = False  # 이어서 시킨 작업은 끝나면 결과를 다시 보여준다
        if job.is_terminal and not self._result_shown:
            self._result_shown = True
            summary = Text(f"\n{STATUS_ICON.get(job.status, '?')} {job.status}  {job.phase}"
                           + (f"   비용 ${job.cost_usd:.4f}" if job.cost_usd is not None else ""),
                           style=_STATUS_STYLE.get(job.status, ""))
            if job.result and not self._result_event_seen:  # 보고를 이벤트로 남기기 전에 끝난 작업
                self._timeline_write(summary, Text("── 팀장 보고 ──", style="bold"), self._report(job, job.result))
            else:
                self._timeline_write(summary)
        # 실행 중이면 주기적으로, 방금 끝났으면 한 번 더 diff 를 계산한다.
        just_finished = job.is_terminal and self._last_status not in (None, job.status)
        self._last_status = job.status
        if force_diff or just_finished or (not job.is_terminal and self._ticks % DIFF_EVERY_TICKS == 0):
            self._load_diff(job.id, job.workdir, job.base_commit)

    @_while_open
    def _check_update(self) -> None:
        """새 버전이 있으면 한 번 알리고 제목 줄에 남겨 둔다."""
        from coliday import updater

        try:
            text = updater.notice(self.cfg.update_stamp)
        except Exception:  # 알림 때문에 화면이 죽으면 안 된다
            return
        if text and not self.sub_title:
            self.sub_title = "새 버전 있음 · coliday update"
            self.notify(text, title="업데이트", timeout=10)

    def _report(self, job: Job, text: str) -> Markdown:
        """팀장 보고. 적힌 경로 가운데 이 컴퓨터에 있는 것은 누르면 그 폴더가 열린다."""
        within = str(self.cfg.home) if remote.is_remote(job.workdir or job.repo) else None
        try:
            text = jobs.link_paths(text, jobs.local_workdir(self.cfg, job), within, [str(self.cfg.artifacts_dir(job.id))])
        except Exception:  # 링크를 못 만들어도 보고는 보여야 한다
            pass
        return Markdown(text, open_links=False)

    @on(Markdown.LinkClicked)
    @_while_open
    def _report_link(self, event: Markdown.LinkClicked) -> None:
        event.stop()
        if "://" in event.href or event.href.startswith("mailto:"):
            self.open_url(event.href)
        else:
            self.open_path(event.href)

    def open_path(self, path: str) -> None:
        try:
            folder = jobs.open_folder(self.cfg, Path(path))
        except JobError as exc:
            self.notify(str(exc), severity="warning", timeout=6)
            return
        self.notify(f"폴더를 열었습니다: {projects.display(str(folder))}", timeout=4)

    def _timeline_write(self, *items: Text | Widget) -> None:
        log = self.query_one("#timeline", VerticalScroll)
        at_end = log.scroll_y >= log.max_scroll_y - 1  # 위로 올려 읽는 중이면 끌어내리지 않는다
        log.mount(*(item if isinstance(item, Widget) else Static(item) for item in items))
        if at_end:
            self.call_after_refresh(log.scroll_end, animate=False)

    def _update_env_bar(self) -> None:
        """선택한 작업의 .env 읽기 상태. 에이전트가 허용을 기다리거나 읽으려다 막혔으면 눈에 띄게 한다."""
        bar = self.query_one("#env-access", EnvBar)
        state: tuple[bool, bool] | None = None
        if self.selected_id is not None:
            try:
                job = self.store.get_job(self.selected_id)
            except KeyError:
                job = None
            if job is not None:
                allowed = self.store.allow_env(job.id)
                needed = not allowed and (
                    guard.NEED_MARKER in (job.result or "")
                    or any(e.kind == "error" and ".env 파일을 읽으려다 막혔습니다" in e.message for e in self.store.events(job.id))
                )
                state = (allowed, needed)
        if state == self._env_state:
            return
        was_shown = self._env_state is not None
        self._env_state = state
        bar.set_class(state is not None, "-shown")
        bar.set_class(bool(state and state[1]), "-needed")
        bar.set_class(bool(state and state[0]), "-allowed")
        if state is not None:
            allowed, needed = state
            if allowed:
                text = "🔓 .env 읽기 허용됨 (이 작업에만)  ← 클릭하면 다시 차단 (Ctrl+L)"
            elif needed:
                text = "🔒 에이전트가 .env 를 읽어야 합니다  ← 클릭하면 이 작업에만 허용 (Ctrl+L)"
            else:
                text = "🔒 .env 읽기 차단 중  ← 클릭하면 이 작업에만 허용 (Ctrl+L)"
            bar.update(Text(text))
        if was_shown != (state is not None):
            self.refresh_bindings()

    def action_env_access(self) -> None:
        if self.selected_id is None:
            self.notify("선택된 작업이 없습니다.", severity="warning")
            return
        job_id = self.selected_id
        allow = not self.store.allow_env(job_id)

        def decided(yes: bool | None) -> None:
            if not yes:
                return
            try:
                message = jobs.set_env_access(self.cfg, self.store, job_id, allow)
            except (JobError, KeyError) as exc:
                self.notify(str(exc), severity="error", timeout=8)
                return
            self.notify(message, timeout=8)
            self._update_env_bar()
            self.refresh_jobs()

        if not allow:
            decided(True)  # 다시 막는 쪽은 묻지 않는다
            return
        self.push_screen(ConfirmScreen(
            f"작업 #{job_id} 에서 에이전트가 .env 파일을 읽도록 허용할까요?\n"
            "이 작업(세션)에만 적용됩니다. 읽은 내용은 에이전트의 모델(Claude·Codex)로 전달됩니다.\n"
            "실행 중이면 지금 실행이 끝난 뒤 허용된 상태로 이어서 진행합니다."
        ), decided)

    def _update_artifacts(self) -> None:
        self._update_env_bar()
        bar = self.query_one("#artifacts", ArtifactBar)
        files = jobs.artifacts(self.cfg, self.selected_id) if self.selected_id is not None else []
        text = ""
        # 지금 보고 있는 작업이 결과물(사진·영상, 사용자가 확인할 파일)을 남겼을 때만 보인다. 없으면 줄도 단축키도 없다.
        if files:
            folder = projects.display(str(self.cfg.artifacts_dir(self.selected_id)))
            text = f"📁 결과물 {jobs.artifacts_summary(files)}: {folder}  ← 클릭하면 폴더 열기 (Ctrl+F)"
        if text != self._artifacts_text:
            was_shown = bool(self._artifacts_text)
            self._artifacts_text = text
            bar.update(Text(text))
            bar.set_class(bool(text), "-shown")
            bar.set_class(bool(text), "-has")
            if was_shown != bool(text):
                self.refresh_bindings()  # 하단 단축키 안내의 '결과물 폴더'도 함께 켜고 끈다

    def action_open_artifacts(self) -> None:
        if self.selected_id is None:
            self.notify("선택된 작업이 없습니다.", severity="warning")
            return
        try:
            root = jobs.open_artifacts(self.cfg, self.store, self.selected_id)
        except (JobError, KeyError) as exc:
            self.notify(str(exc), severity="warning", timeout=6)
            return
        self.notify(f"폴더를 열었습니다: {projects.display(str(root))}", timeout=4)

    def action_timeline_scroll(self, direction: int) -> None:
        log = self.query_one("#timeline", VerticalScroll)
        if direction < 0:
            log.scroll_page_up(animate=False)
        else:
            log.scroll_page_down(animate=False)

    # ── 복사 ────────────────────────────────────────────────
    def on_mouse_up(self) -> None:
        # 드래그가 끝나면 바로 복사한다. 선택 범위는 이 이벤트 처리 뒤에 확정되므로 조금 늦춘다.
        self.set_timer(0.05, self._copy_selection)

    @_while_open
    def _copy_selection(self) -> None:
        text = self.screen.get_selected_text()
        if not text:
            self._copied = None
            return
        if text == self._copied:
            return  # 선택을 그대로 둔 채 다른 곳을 클릭함
        self._copied = text
        self.copy_to_clipboard(text)
        self.notify(f"복사했습니다 ({len(text)}자)", timeout=2)

    def copy_to_clipboard(self, text: str) -> None:
        super().copy_to_clipboard(text)  # 터미널 OSC 52. macOS 기본 터미널은 지원하지 않아 pbcopy 도 쓴다.
        if sys.platform == "darwin":
            _pbcopy(text)

    @work(thread=True, exclusive=True, group="diff")
    def _load_diff(self, job_id: int, workdir: str, base: str | None) -> None:
        if not workdir or not (remote.is_remote(workdir) or Path(workdir).is_dir()):
            self.call_from_thread(self._show_diff, job_id, "작업 디렉토리가 없습니다. 정리됐거나 아직 만들어지지 않았습니다.", True)
            return
        try:
            text = gitops.diff(workdir, base)
        except gitops.GitError as exc:
            self.call_from_thread(self._show_diff, job_id, f"diff 를 계산할 수 없습니다: {exc}", True)
            return
        self.call_from_thread(self._show_diff, job_id, text, False)

    @_while_open
    def _show_diff(self, job_id: int, text: str, is_message: bool) -> None:
        if job_id != self.selected_id or text == self._diff_text:
            return  # 다른 작업으로 옮겼거나 바뀐 게 없으면 스크롤을 건드리지 않는다
        self._diff_text = text
        widget = self.query_one("#diff", Static)
        if is_message:
            widget.update(Text(text, style="dim"))
        elif not text.strip():
            widget.update("변경 사항이 없습니다.")
        else:
            if len(text) > MAX_DIFF_CHARS:
                text = text[:MAX_DIFF_CHARS] + f"\n… (너무 길어 잘랐습니다. coliday diff {job_id} 로 전체 보기)"
            widget.update(_diff_renderable(text))

    # ── 동작 ────────────────────────────────────────────────
    def action_new_job(self, derive: bool = False, derive_from: int | None = None) -> None:
        # 작업 목록에서 작업을 고른 상태면 그 작업에서 파생할 수 있다 (기본은 꺼짐). 목록 정리 메뉴는 연 때의 작업을 넘긴다.
        parent = self.selected_id if derive_from is None else derive_from
        self.push_screen(NewJobScreen(self.project_dir or self.default_dir, parent, derive), self._start_job)

    def _start_job(self, answer: tuple | None) -> bool:
        """시작했으면 True (파생 작업은 백그라운드에서 만들기 시작했으면). answer 는 (요청, 디렉토리, worktree 없이[, .env 허용[, 파생할 부모]]).

        채팅 입력으로 시작한 작업은 .env 읽기를 허용하지 않는다 (허용은 새 작업 창에서만 켠다).
        """
        if not answer:
            return False
        prompt, directory, no_worktree, allow_env, parent = (*answer, *(False, None)[len(answer) - 3:])
        if parent is not None:
            return self._derive(parent, prompt, allow_env)
        try:
            launched = jobs.launch(
                self.cfg, self.store, prompt, directory, no_worktree=no_worktree, allow_env=allow_env,
            )
        except NotARepoError:
            # 배포 폴더처럼 git 저장소가 아닌 곳: 막지 않고, 직접 고치게 되는 것을 알린 뒤 물어본다.
            def decided(yes: bool | None) -> None:
                chat = self.query_one("#chat", Input)
                if yes and self._start_job((prompt, directory, True, allow_env)) and chat.value.strip() == prompt.strip():
                    chat.value = ""

            self.push_screen(ConfirmScreen(
                f"git 저장소가 아닙니다: {directory}\n"
                "worktree 없이 이 폴더에서 직접 작업할까요?\n"
                "에이전트가 이 폴더의 파일을 바로 고칩니다. 변경 내용(diff)을 볼 수 없고 되돌릴 수도 없습니다."
            ), decided)
            return False
        except (JobError, gitops.GitError) as exc:
            self.notify(str(exc), severity="error", timeout=8)
            return False
        self._show_started(launched)
        return True

    def _derive(self, parent: int, prompt: str, allow_env: bool) -> bool:
        """파생 작업을 백그라운드에서 만든다. 부모 worktree 스냅샷(서버면 최대 120초) 동안 화면이 멈추지 않게 한다.

        Textual 의 thread 워커는 asyncio 기본 executor 에서 돌아 앱이 끝날 때 끝나기를 기다린다 (종료가 멈춘다).
        그래서 데몬 스레드로 돌린다. 앱이 먼저 끝나면 결과는 버린다 (만든 작업은 worker 가 이어서 실행한다).
        """
        if parent in self._deriving:
            self.notify(f"#{parent} 에서 파생 작업을 이미 만드는 중입니다.", severity="warning")
            return False
        self._deriving.add(parent)
        self.notify(f"파생 작업을 만드는 중… (#{parent} 에서)", timeout=10)
        threading.Thread(
            target=self._derive_in_thread, args=(parent, prompt, allow_env), name=f"derive-{parent}", daemon=True,
        ).start()
        return True

    def _derive_in_thread(self, parent: int, prompt: str, allow_env: bool) -> None:
        # sqlite3 연결은 스레드를 넘겨 쓰지 않는다: 이 스레드에서 따로 열고 닫는다.
        try:
            store = Store(self.cfg.db_path)
            try:
                launched = jobs.derive(self.cfg, store, parent, prompt, allow_env=allow_env)
            finally:
                store.close()
        except (JobError, gitops.GitError) as exc:
            self._from_thread(self._derive_failed, parent, str(exc))
        except Exception as exc:  # 예상하지 못한 오류도 화면에 알리고 앱은 살린다
            self._from_thread(self._derive_failed, parent, f"파생 작업을 만들지 못했습니다: {exc!r}")
        else:
            self._from_thread(self._derive_done, parent, launched)

    def _from_thread(self, callback: Callable[..., None], *args: object) -> None:
        if not self.is_running:  # 앱이 이미 끝났다: 닫힌 루프에 보내지 않는다
            return
        try:
            self.call_from_thread(callback, *args)
        except RuntimeError:  # 그사이 앱이 끝났다
            pass

    @_while_open
    def _derive_failed(self, parent: int, message: str) -> None:
        self._deriving.discard(parent)
        self.notify(message, severity="error", timeout=8)

    @_while_open
    def _derive_done(self, parent: int, launched: jobs.Launched) -> None:
        self._deriving.discard(parent)
        self._show_started(launched, parent)

    def _show_started(self, launched: jobs.Launched, parent: int | None = None) -> None:
        """시작한 작업을 알리고 목록에서 고른다. 새 작업은 그 작업이 속한 프로젝트로 옮겨 고른다.

        파생 작업은 백그라운드에서 만드는 동안 다른 프로젝트로 옮겼을 수 있다: 그때는 화면을 옮기지 않고 알리기만 한다.
        """
        for w in launched.warnings:
            self.notify(w, severity="warning", timeout=8)
        self.refresh_projects()  # 직접 입력한 디렉토리도 연결된다: 그 줄을 먼저 그린다
        # launched.project 가 아니라 계산한 소속으로 (목록에 넣지 못했거나 하위 폴더를 연결한 경우도 맞게).
        where = scopes.scope_of(launched.job, self._project_paths(), self._worktrees)
        started = f"작업 #{launched.job.id} 시작" + (f" (#{parent} 에서 파생)" if parent is not None else "")
        if parent is not None and where != self.scope:
            self.notify(f"{started} — 프로젝트 {self._scope_name(where)}", timeout=8, markup=False)
            return
        self.notify(started)
        self._remembered[where] = launched.job.id  # 그 프로젝트로 옮기면 처음부터 이 작업을 고른다
        self.refresh_projects(select=where)
        self.refresh_jobs(reveal=launched.job.id)  # 접힌 묶음에 들어가도 그 줄이 보이게 먼저 펼쳐 그린다
        row = self._row_of(str(launched.job.id))
        if row is not None:
            self.query_one("#jobs", DataTable).move_cursor(row=row)
        self.select_job(launched.job.id)

    # ── 채팅 입력 ────────────────────────────────────────────
    def on_key(self, event: events.Key) -> None:
        """메인 화면에서는 어디에 포커스가 있든 글자를 치면 채팅 입력으로 들어간다."""
        if len(self.screen_stack) > 1:
            return  # 대화 상자가 떠 있을 때는 그쪽 입력이다
        chat = self.query_one("#chat", Input)
        table = self.query_one("#jobs", DataTable)
        if event.key == "escape" and self.focused is chat:
            table.focus()
            event.stop()
        elif event.key == "space" and self.focused is table:
            self._toggle_mark()  # 글자가 아니라 선택이다 (채팅 입력으로 넘기지 않는다)
            event.stop()
        elif event.key == "escape" and self.focused is table and self._marked:
            self._marked.clear()
            self.refresh_jobs()
            event.stop()
        elif event.is_printable and event.character and not isinstance(self.focused, Input):
            chat.focus()
            chat.insert_text_at_cursor(event.character)
            event.stop()

    @on(DataTable.RowSelected, "#jobs")
    @_while_open
    def _on_job_chosen(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value or ""
        if key.startswith("h:"):  # 머리줄: 접고 편다
            self.toggle_section(key[2:])
            return
        self.query_one("#chat", Input).focus()

    @on(Input.Submitted, "#chat")
    def _chat_submitted(self, event: Input.Submitted) -> None:
        prompt = event.value.strip()
        if not prompt:
            return
        if self._header_key is not None:  # 머리줄은 작업이 아니다: 쓴 글은 남겨 둔다
            self.notify("작업을 고르세요. 작업 목록의 커서가 머리줄(묶음)에 있습니다 (새 작업은 ctrl+n).", severity="warning")
            return
        if self.selected_id is None and self.scope == scopes.OTHER:  # 기타는 위치가 아니다: 어디에 만들지 모른다
            self.notify("기타에서는 새 작업을 시작할 수 없습니다. 프로젝트를 고르거나 ctrl+n 으로 위치를 정하세요.",
                        severity="warning")
            return
        if self.selected_id is None:  # 아직 작업이 없으면 고른 프로젝트에서 새 작업으로 시작한다
            sent = self._start_job((prompt, self.project_dir or self.default_dir, False))
        else:
            sent = self._continue_selected(prompt)
        if sent:
            event.input.clear()  # 보내지 못했으면 쓴 내용을 남겨 둔다

    def _continue_selected(self, prompt: str) -> bool:
        try:
            job = jobs.reconcile(self.store, self.store.get_job(self.selected_id))
        except KeyError:
            self.notify("선택한 작업이 삭제됐습니다.", severity="warning")
            return False
        try:
            launched = jobs.continue_job(self.cfg, self.store, job.id, prompt)
        except (JobError, gitops.GitError) as exc:
            self.notify(str(exc), severity="error", timeout=8)
            return False
        for w in launched.warnings:
            self.notify(w, severity="warning", timeout=8)
        if launched.queued:
            self.notify(f"작업 #{job.id} 이(가) 실행 중이라 대기시켰습니다. 지금 실행이 끝나면 이어서 전달됩니다.", timeout=6)
        self.refresh_jobs()
        self._poll_selected()
        timeline = self.query_one("#timeline", VerticalScroll)
        self.call_after_refresh(timeline.scroll_end, animate=False)  # 보낸 요청이 보이게 맨 아래로
        return True

    def _update_chat_hint(self) -> None:
        chat = self.query_one("#chat", Input)
        if self._header_key is not None:
            hint = "머리줄(묶음)에서는 보낼 수 없습니다 — 작업 목록에서 작업을 고르세요   ·   새 작업은 ctrl+n"
        elif self.selected_id is None and self.scope == scopes.OTHER:
            hint = "기타에서는 새 작업을 시작할 수 없습니다 — 작업을 고르거나 프로젝트를 고르세요   ·   새 작업은 ctrl+n"
        elif self.selected_id is None:
            where = dict(self._projects or []).get(self.project_dir or "", Path(self.default_dir).name)
            hint = f"새 작업으로 시킬 일 (Enter) — 위치: {where}"
        else:
            try:
                running = not self.store.get_job(self.selected_id).is_terminal
            except KeyError:
                return
            hint = (f"작업 #{self.selected_id} 실행 중 — 지금 보내면 이 실행이 끝난 뒤 이어서 전달됩니다 (Enter)" if running
                    else f"작업 #{self.selected_id} 에 이어서 시킬 일 (Enter)   ·   새 작업은 ctrl+n")
        if chat.placeholder != hint:
            chat.placeholder = hint

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # 메인 화면 단축키는 priority 라 대화 상자 안에서도 잡힌다. 대화 상자가 떠 있으면 막는다.
        if action in _MAIN_ACTIONS and len(self.screen_stack) > 1:
            return False
        if action == "open_artifacts" and not self._artifacts_text:
            return False  # 이 작업에 결과물이 없으면 단축키도 하단 안내도 없다
        if action == "env_access" and self._env_state is None:
            return False  # 선택한 작업이 없다
        return True

    def action_models(self) -> None:
        try:
            agents = team.load_agents(self.cfg.agents_override, team.load_custom(self.cfg.team_path))
            models = team.load_models(self.cfg.models_path)
        except (OSError, ValueError) as exc:
            self.notify(f"모델 설정을 읽을 수 없습니다: {exc}", severity="error", timeout=8)
            return

        def done(answer: dict[str, str] | None) -> None:
            if answer is None:
                return
            try:
                updated = models
                for role, name in answer.items():
                    jobs.check_codex(self.cfg, name)
                    updated = team.with_model(updated, agents, role, name or None)
                team.save_models(self.cfg.models_path, updated)
            except (OSError, ValueError, JobError) as exc:
                self.notify(str(exc), severity="error", timeout=8)
                return
            self.notify("모델 설정을 저장했습니다. 다음 실행부터 적용됩니다.")

        self.push_screen(ModelScreen(agents, models), done)

    def action_prompts(self) -> None:
        self.push_screen(TeamScreen(self.cfg))

    def _selected_job(self) -> Job | None:
        if self.selected_id is None:
            self.notify("선택된 작업이 없습니다.", severity="warning")
            return None
        return self.store.get_job(self.selected_id)

    def action_cancel_job(self) -> None:
        job = self._selected_job()
        if job is None:
            return
        if job.is_terminal:
            self.notify(f"작업 #{job.id} 은(는) 이미 종료됐습니다.")
            return

        def done(ok: bool | None) -> None:
            if ok and jobs.cancel(self.store, self.store.get_job(job.id)):
                self.notify(f"작업 #{job.id} 취소했습니다.")
                self.refresh_jobs()

        self.push_screen(ConfirmScreen(f"작업 #{job.id} 을(를) 취소할까요?"), done)

    def action_remove_listed_job(self) -> None:
        if self.focused is self.query_one("#jobs"):  # 프로젝트 목록 등 다른 곳에서 누른 것은 무시한다
            self.action_remove_job()

    def action_copy_timeline(self) -> None:
        """타임라인 전체를 글자로 복사한다 (화면에 보이지 않는 앞부분까지). Space 로 고른 작업이 있으면 그것들을 모두."""
        ids = sorted(self._marked) or ([self.selected_id] if self.selected_id is not None else [])
        parts = []
        for job_id in ids:
            try:
                parts.append(timeline_text(self.store, self.store.get_job(job_id)))
            except KeyError:
                continue  # 그사이 삭제됐다
        if not parts:
            self.notify("복사할 작업이 없습니다.", severity="warning")
            return
        text = "\n\n".join(parts)
        self._copied = text  # 마우스를 떼는 순간의 '선택 복사'가 이것을 덮어쓰지 않게 한다
        self.copy_to_clipboard(text)
        names = ", ".join(f"#{i}" for i in ids)
        self.notify(f"작업 {names} 의 타임라인을 복사했습니다 ({len(text.splitlines())}줄)", timeout=3)

    def _toggle_mark(self) -> None:
        """커서가 있는 작업을 선택하거나 선택을 푼다. 이어서 고르기 쉽게 커서를 한 줄 내린다.

        머리줄이면 그 묶음의 작업 전체(접힌 것 포함)를 고른다. 이미 모두 골랐으면 모두 푼다.
        """
        table = self.query_one("#jobs", DataTable)
        line = self._cursor_line()
        if isinstance(line, joblist.Header):
            ids = set(line.job_ids)
            self._marked = self._marked - ids if ids and ids <= self._marked else self._marked | ids
            self.refresh_jobs()
            return
        if self.selected_id is None or not table.row_count:
            return
        self._marked ^= {self.selected_id}
        self.refresh_jobs()
        table.action_cursor_down()

    def _update_marks_hint(self) -> None:
        table = self.query_one("#jobs", DataTable)
        if self._marked:
            shown = {line.job.id for line in self._lines if isinstance(line, joblist.JobLine)}
            hidden = len(self._marked - shown)  # 접힌 곳에 있어 보이지 않는 선택
            table.border_subtitle = (
                f"{len(self._marked)}개 선택" + (f" (접힌 곳 {hidden}개)" if hidden else "")
                + " · Delete 로 함께 삭제 · Esc 선택 해제 · Ctrl+B 정리"
            )
        else:
            table.border_subtitle = "Space 로 여러 개 선택 · Delete 삭제 · ←→ 접기 · Ctrl+B 정리"

    def _update_jobs_title(self) -> None:
        group_by = self._prefs.group_by
        title = "작업 목록"
        if self.scope is not None:
            title += f" · {self._scope_name(self.scope)}"
        if group_by != "none":
            title += f" · 묶기: {_GROUP_BY_LABEL[group_by]}"
        self.query_one("#jobs", DataTable).border_title = Text(title).markup  # 이름의 대괄호가 markup 으로 읽히지 않게

    # ── 목록 정리: 묶기·접기·고정·그룹 ─────────────────────────
    def _save_prefs(
        self, *, group_by: joblist.GroupBy | None = None, add: frozenset[str] = frozenset(),
        remove: frozenset[str] = frozenset(),
    ) -> None:
        """이번에 바꾼 것만 파일의 최신 설정에 적용해 저장한다 (다른 화면이 저장한 변경을 되돌리지 않게).

        저장한 결과(다른 화면의 변경 포함)를 이 화면의 설정으로 삼는다. 없어진 작업·그룹의 접힘 키는 함께 지운다.
        """
        group_ids = {g.id for g in self.store.list_groups()}
        stale = frozenset(key for key in self._prefs.collapsed | add if not self._key_alive(key, group_ids))
        remove = remove | stale
        try:
            self._prefs = prefs.update(self.cfg.ui_path, group_by=group_by, add=add, remove=remove)
        except OSError as exc:  # 저장하지 못해도 이번 화면에는 적용한다
            self._prefs = prefs.Prefs(
                self._prefs.group_by if group_by is None else group_by, (self._prefs.collapsed - remove) | (add - remove),
            )
            self.notify(f"화면 설정을 저장하지 못했습니다: {exc}", severity="warning", timeout=6)
        self._update_jobs_title()

    def _key_alive(self, key: str, group_ids: set[int]) -> bool:
        """접힘 키가 가리키는 작업·그룹이 아직 있는가. 목록(최근 100개) 밖이나 다른 화면이 방금 만든 것도 있는 것으로 본다."""
        kind, _, value = key.partition(":")
        if kind == "project":  # 없어진 '프로젝트로 묶기' 의 접힘 키
            return False
        if kind not in ("job", "group"):
            return True
        if not value.isdigit():
            return False
        if kind == "group":
            return int(value) in group_ids
        try:
            self.store.get_job(int(value))
        except KeyError:
            return False
        return True

    def _set_collapsed(self, key: str, collapsed: bool) -> None:
        if collapsed:
            self._save_prefs(add=frozenset({key}))
        else:
            self._save_prefs(remove=frozenset({key}))
        self.refresh_jobs()

    def toggle_section(self, key: str) -> None:
        """머리줄을 접거나 편다 (Enter·클릭). 커서는 그 머리줄에 둔다."""
        row = self._row_of(f"h:{key}")
        if row is None:
            return
        line = self._lines[row]
        self._move_to_row(row)  # 보던 작업이 그 묶음 안에 있어도 펼친 채로 남지 않게 먼저 머리줄로 옮긴다
        if isinstance(line, joblist.Header):
            self._set_collapsed(key, not line.collapsed)  # 보이는 그대로 뒤집는다

    def action_job_left(self) -> None:
        """펼친 부모나 머리줄이면 접고, 자식이면 부모로, 뿌리면 머리줄로 간다."""
        line = self._cursor_line()
        if line is None:
            return
        if isinstance(line, joblist.Header):
            if not line.collapsed:
                self._set_collapsed(line.key, True)
            return
        if line.child_count and not line.collapsed:
            self._set_collapsed(joblist.tree_key(line.job.id), True)
            return
        row = self.query_one("#jobs", DataTable).cursor_row
        for i in range(row - 1, -1, -1):
            above = self._lines[i]
            if line.depth > 0 and isinstance(above, joblist.JobLine) and above.depth == line.depth - 1:
                self._move_to_row(i)
                return
            if line.depth == 0 and isinstance(above, joblist.Header) and above.key == line.section:
                self._move_to_row(i)
                return

    def action_job_right(self) -> None:
        """접힌 머리줄이나 접힌 부모를 편다."""
        line = self._cursor_line()
        if isinstance(line, joblist.Header) and line.collapsed:
            self._set_collapsed(line.key, False)
        elif isinstance(line, joblist.JobLine) and line.collapsed:
            self._set_collapsed(joblist.tree_key(line.job.id), False)

    def _cycle_group_by(self) -> str:
        order = list(_GROUP_BY_LABEL)
        group_by = order[(order.index(self._prefs.group_by) + 1) % len(order)]
        self._save_prefs(group_by=group_by)
        self.refresh_jobs()
        return self._group_by_item()

    def _group_by_item(self) -> str:
        return f"묶기: 없음 / 상태 (지금: {_GROUP_BY_LABEL[self._prefs.group_by]})"

    def _targets(self) -> list[int]:
        """목록 정리의 대상: Space 로 고른 작업들, 없으면 커서가 있는 작업."""
        if self._marked:
            return sorted(self._marked)
        return [self.selected_id] if self.selected_id is not None else []

    def action_list_menu(self) -> None:
        targets = self._targets()
        what = (f"#{targets[0]}" if len(targets) == 1 else f"고른 {len(targets)}개") if targets else "작업을 고르세요"
        metas = [self._meta.get(i, joblist.JobMeta()) for i in targets]
        items: list[tuple[str, str, bool]] = [("group_by", self._group_by_item(), True)]
        if not targets or not all(m.pinned for m in metas):
            items.append(("pin", f"고정 ({what})", bool(targets)))
        if any(m.pinned for m in metas):
            items.append(("unpin", f"고정 해제 ({what})", True))
        # 그룹은 프로젝트마다 따로다. 기타는 프로젝트가 아니라 그룹을 만들거나 넣을 수 없다.
        project = self.scope if self.scope not in (None, scopes.OTHER) else None
        for group in self._groups:
            items.append((f"group:{group.id}", f"그룹에 넣기 → {group.name} ({what})", bool(targets)))
        if project is None:
            items.append(("group_new", "그룹에 넣기 (기타에서는 그룹을 쓸 수 없습니다 — 프로젝트를 고르세요)", False))
        else:
            items.append(("group_new", f"그룹에 넣기 → 새 그룹… ({what})", bool(targets)))
        if any(m.group_id is not None for m in metas):  # 보이는 그룹 기준 (refresh_jobs 가 다른 프로젝트 그룹은 지웠다)
            items.append(("ungroup", f"그룹에서 빼기 ({what})", True))
        if self.selected_id is not None:
            items.append(("derive", f"파생 작업 만들기 (#{self.selected_id} 에서)", True))
        else:
            items.append(("derive", "파생 작업 만들기 (작업을 고르세요)", False))
        group = self._cursor_group()
        if group is not None:
            items.append(("rename_group", f"그룹 이름 바꾸기 ({group.name})", True))
            items.append(("delete_group", f"그룹 삭제 ({group.name}, 작업은 남음)", True))
        # 대상은 메뉴를 연 지금 정한다: 메뉴가 열려 있는 동안 다른 화면에서 지워 1초 새로고침이 커서를 옮겨도
        # 메뉴에 적힌 작업·그룹에만 적용한다.
        derive_from = self.selected_id
        self.push_screen(
            ListMenuScreen(items, self._cycle_group_by),
            lambda choice: self._menu_chosen(choice, targets, derive_from, group, project),
        )

    def _cursor_group(self) -> joblist.JobGroup | None:
        """커서가 사용자 그룹 머리줄에 있으면 그 그룹."""
        key = self._header_key or ""
        return next((g for g in self._groups if key == f"group:{g.id}"), None)

    def _existing(self, job_ids: list[int]) -> list[int]:
        """아직 있는 작업만 (메뉴·이름 창이 열려 있는 동안 다른 화면에서 지웠을 수 있다)."""
        left = []
        for job_id in job_ids:
            try:
                self.store.get_job(job_id)
            except KeyError:
                continue
            left.append(job_id)
        return left

    def _targets_left(self, targets: list[int]) -> list[int]:
        """대상 중 남은 작업. 하나도 없으면 알리고 빈 목록."""
        left = self._existing(targets)
        if not left:
            gone = ", ".join(f"#{i}" for i in targets)
            self.notify(f"작업 {gone} 이(가) 그사이 지워져 아무것도 바꾸지 않았습니다.", severity="warning", timeout=6)
        return left

    def _group_left(self, group: joblist.JobGroup) -> bool:
        if any(g.id == group.id for g in self.store.list_groups()):
            return True
        self.notify(f"그룹 '{group.name}' 이(가) 그사이 지워져 아무것도 바꾸지 않았습니다.", severity="warning",
                    timeout=6, markup=False)
        self.refresh_jobs()
        return False

    def _menu_chosen(
        self, choice: str | None, targets: list[int], derive_from: int | None, group: joblist.JobGroup | None,
        project: str | None = None,
    ) -> None:
        """메뉴를 열 때 정한 대상(targets·derive_from·group)에 적용한다. 그사이 지워졌으면 바꾸지 않고 알린다."""
        if not choice:
            return
        if choice in ("pin", "unpin", "group_new", "ungroup") or choice.startswith("group:"):
            targets = self._targets_left(targets)
            if not targets:
                self.refresh_jobs()
                return
        names = ", ".join(f"#{i}" for i in targets)
        if choice in ("pin", "unpin"):
            self.store.set_pinned(targets, choice == "pin")
            self.notify(f"작업 {names} 을(를) " + ("고정했습니다." if choice == "pin" else "고정 해제했습니다."))
        elif choice.startswith("group:"):
            self._put_in_group(targets, int(choice.removeprefix("group:")))
        elif choice == "group_new":
            if project is None:
                return
            self.push_screen(NameScreen("새 그룹"), lambda name: self._new_group(targets, name, project))
            return
        elif choice == "ungroup":
            self._put_in_group(targets, None)
        elif choice == "derive":
            if derive_from is None or not self._targets_left([derive_from]):
                return
            self.action_new_job(derive=True, derive_from=derive_from)
            return
        elif choice == "rename_group" and group is not None:
            if self._group_left(group):
                self.push_screen(NameScreen("그룹 이름 바꾸기", group.name), lambda name: self._rename_group(group, name))
            return
        elif choice == "delete_group" and group is not None:
            if not self._group_left(group):
                return
            jobs.delete_group(self.store, group.id)
            self.notify(f"그룹 '{group.name}' 을(를) 지웠습니다. 들어 있던 작업은 그대로 남습니다.", markup=False)
            self.refresh_jobs()
            self._save_prefs()  # 지운 그룹의 접힘 키를 지운다
            return
        self.refresh_jobs()

    def _put_in_group(self, targets: list[int], group_id: int | None) -> None:
        try:
            jobs.set_group(self.store, targets, group_id)
        except JobError as exc:  # 그사이 다른 화면에서 그룹을 지웠다
            self.notify(str(exc), severity="error", timeout=8)
            return
        names = ", ".join(f"#{i}" for i in targets)
        group = next((g.name for g in self.store.list_groups() if g.id == group_id), None)
        # 그룹 이름에 든 대괄호가 markup 으로 해석되지 않게 한다.
        self.notify(f"작업 {names} 을(를) " + (f"그룹 '{group}' 에 넣었습니다." if group_id is not None else "그룹에서 뺐습니다."),
                    markup=False)

    def _new_group(self, targets: list[int], name: str | None, project: str) -> None:
        """project 는 메뉴를 연 때 보던 프로젝트다 (그사이 다른 터미널이 목록을 바꿔 화면이 옮겨져도 대상과 같은 곳에 만든다)."""
        if not name:
            return
        targets = self._targets_left(targets)  # 이름을 쓰는 동안 지워졌을 수 있다: 빈 그룹만 만들지 않는다
        if not targets:
            self.refresh_jobs()
            return
        try:
            group = jobs.create_group(self.store, name, project)
        except JobError as exc:  # 이름이 겹친다
            self.notify(str(exc), severity="warning", timeout=6, markup=False)
            return
        self._put_in_group(targets, group.id)
        self.refresh_jobs()

    def _rename_group(self, group: joblist.JobGroup, name: str | None) -> None:
        if not name or name == group.name:
            return
        try:
            jobs.rename_group(self.store, group.id, name)
        except JobError as exc:
            self.notify(str(exc), severity="warning", timeout=6, markup=False)
            return
        self.notify(f"그룹 이름을 '{name}' (으)로 바꿨습니다.", markup=False)
        self.refresh_jobs()

    def action_remove_job(self) -> None:
        """고른 작업을 삭제한다. Space 로 선택해 둔 작업이 있으면 그것들을 한꺼번에, 없으면 보고 있는 작업 하나를."""
        ids = sorted(self._marked, reverse=True)  # 접힌 곳에 있는 선택도 함께
        if not ids and self._header_key is not None:
            self.notify("머리줄은 지울 수 없습니다. 지울 작업을 고르거나 Space 로 묶음 전체를 고르세요.", severity="warning")
            return
        if not ids:
            job = self._selected_job()
            if job is None:
                return
            ids = [job.id]
        names = ", ".join(f"#{i}" for i in ids)

        def remove(targets: list[int], force: bool) -> None:
            done, dirty, errors, messages = [], [], [], []
            for job_id in targets:
                try:
                    messages.append(jobs.remove(self.cfg, self.store, self.store.get_job(job_id), force=force))
                    done.append(job_id)
                except KeyError:
                    messages.append(f"작업 #{job_id} 은(는) 이미 삭제됐습니다.")
                    done.append(job_id)
                except WorktreeRemoveError as exc:
                    (errors if force else dirty).append((job_id, str(exc)))
                except (JobError, gitops.GitError) as exc:
                    errors.append((job_id, str(exc)))
            self._marked -= set(done)
            if messages:
                self.notify("\n".join(messages))
            for job_id, reason in errors:  # 하나가 실패해도 나머지는 지운다. 실패한 것은 이유와 함께 남는다
                self.notify(f"작업 #{job_id}: {reason}", severity="error", timeout=10)
            self.refresh_jobs()  # 목록에서 바로 사라진다
            if dirty:
                left = [job_id for job_id, _ in dirty]
                self.push_screen(
                    ConfirmScreen(
                        f"작업 {', '.join(f'#{i}' for i in left)} 의 worktree 에 검토하지 않은 변경이 있습니다.\n"
                        "변경을 버리고 삭제할까요?"
                    ),
                    lambda ok: remove(left, True) if ok else None,
                )

        question = (
            f"작업 {names} 을(를) 삭제할까요? 목록·기록과 worktree·브랜치가 지워집니다." if len(ids) == 1
            else f"선택한 작업 {len(ids)}개({names})를 삭제할까요? 목록·기록과 worktree·브랜치가 지워집니다."
        )
        self.push_screen(ConfirmScreen(question), lambda ok: remove(ids, False) if ok else None)


def _pbcopy(text: str) -> None:
    try:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _diff_renderable(text: str) -> Text:
    out = Text()
    for line in text.splitlines():
        if line.startswith(("diff --git", "+++", "---", "index ", "new file", "deleted file")):
            style = "bold"
        elif line.startswith("@@"):
            style = "cyan"
        elif line.startswith("+"):
            style = "green"
        elif line.startswith("-"):
            style = "red"
        else:
            style = ""
        out.append(line + "\n", style=style)
    return out


def timeline_text(store: Store, job: Job) -> str:
    """작업 하나의 타임라인을 화면에 보이는 그대로 글자로 만든다: 요청, 위치, 시각·담당·내용 줄, 팀장 보고."""
    lines = [f"요청: {job.prompt}", f"위치: {job.workdir}" + (f"  (브랜치 {job.branch})" if job.branch else "")]
    reported = False
    for ev in store.events(job.id):
        if ev.kind == RESULT:
            reported = True
            ts = time.strftime("%H:%M:%S", time.localtime(ev.ts))
            lines += [f"── 팀장 보고 ({ts}) ──", ev.message.strip()]
        else:
            lines.append(_event_line(ev).plain.rstrip())
    if job.is_terminal:
        lines.append(f"{STATUS_ICON.get(job.status, '?')} {job.status}  {job.phase}"
                     + (f"   비용 ${job.cost_usd:.4f}" if job.cost_usd is not None else ""))
        if job.result and not reported:  # 보고를 이벤트로 남기기 전에 끝난 작업
            lines += ["── 팀장 보고 ──", job.result.strip()]
    return "\n".join(lines)


def _event_line(ev: EventRow) -> Text:
    ts = time.strftime("%H:%M:%S", time.localtime(ev.ts))
    if ev.kind == QUEUED:
        return Text(f"{ts} ⏳ 대기 (지금 실행이 끝나면 전달): {ev.message}", style="italic yellow")
    actor_style = "bold magenta" if ev.actor == "팀장" else _ACTOR_COLORS[sum(map(ord, ev.actor)) % len(_ACTOR_COLORS)]
    line = Text(f"{ts} ", style="dim")
    line.append(f"{ev.actor:<10} ", style=actor_style)
    line.append(ev.message, style=_KIND_STYLE.get(ev.kind, ""))
    return line


def run_tui(cfg: Config) -> int:
    store = Store(cfg.db_path)
    try:
        ColidayApp(cfg, store).run()
    finally:
        store.close()
    return 0
