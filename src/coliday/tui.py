"""coliday TUI.

┌ 작업 목록 ──────────────┐┌ 변경 사항 (diff) ┐
├ 타임라인 ───────────────┤│                  │
└─────────────────────────┘└──────────────────┘

DB 는 메인 스레드에서만 읽고 쓴다. git diff 만 백그라운드 스레드에서 계산한다.

타임라인과 diff 는 마우스로 드래그해 복사할 수 있어야 한다. Textual 은 Text 를 그리는 Static 과
Markdown 위젯만 선택을 지원하므로(RichLog, rich Syntax/Markdown 은 안 됨) 그 위젯들로만 그린다.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Footer, Header, Input, Label, Markdown, Select, Static

from coliday import gitops, jobs, projects, team
from coliday.config import Config
from coliday.jobs import STATUS_ICON, JobError, WorktreeRemoveError
from coliday.store import RESULT, EventRow, Job, Store

_MAIN_ACTIONS = frozenset({
    "new_job", "focus_projects", "add_project", "rename_project", "remove_project",
    "models", "prompts", "cancel_job", "remove_job", "timeline_scroll", "open_artifacts",
})
POLL_SEC = 1.0
DIFF_EVERY_TICKS = 2
MAX_DIFF_CHARS = 300_000

_STATUS_STYLE = {
    "queued": "dim", "running": "bold yellow", "succeeded": "green",
    "failed": "bold red", "cancelled": "dim",
}
_ACTOR_COLORS = ["cyan", "green", "yellow", "blue", "magenta", "bright_cyan", "bright_green"]
_KIND_STYLE = {
    "dispatch": "bold", "done": "green", "error": "bold red", "phase": "italic", "text": "dim",
    "prompt": "bold",
}


class NewJobScreen(ModalScreen[tuple[str, str, bool] | None]):
    BINDINGS = [Binding("escape", "close", "닫기")]

    def __init__(self, default_dir: str) -> None:
        super().__init__()
        self._default_dir = default_dir

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("새 작업", id="dialog-title")
            yield Input(placeholder="팀장에게 시킬 일 (Enter 로 시작)", id="prompt")
            yield Input(value=self._default_dir, placeholder="프로젝트 디렉토리", id="dir")
            yield Checkbox("worktree 없이 이 디렉토리에서 직접 작업", id="no-worktree")
            with Horizontal(id="dialog-buttons"):
                yield Button("시작", variant="primary", id="ok")
                yield Button("닫기", id="close")

    def on_mount(self) -> None:
        self.query_one("#prompt", Input).focus()

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def _submit(self) -> None:
        prompt = self.query_one("#prompt", Input).value.strip()
        if not prompt:
            self.notify("요청 내용을 입력하세요.", severity="warning")
            return
        self.dismiss((prompt, self.query_one("#dir", Input).value, self.query_one("#no-worktree", Checkbox).value))

    @on(Button.Pressed, "#close")
    def action_close(self) -> None:
        self.dismiss(None)


class ArtifactBar(Static):
    """선택한 작업의 결과물 폴더 한 줄. 클릭하면 폴더를 연다. 결과물이 없으면 숨긴다."""

    def on_click(self) -> None:
        self.app.action_open_artifacts()


class ProjectTable(DataTable):
    """연결된 프로젝트 목록. 커서가 있는 줄이 새 작업을 시작할 프로젝트다."""

    BINDINGS = [
        Binding("ctrl+e", "app.rename_project", "이름 바꾸기", key_display="E"),
        Binding("ctrl+x", "app.remove_project", "목록에서 빼기", key_display="X"),
    ]


class AddProjectScreen(ModalScreen[tuple[str, str] | None]):
    """프로젝트 디렉토리와 이름 입력. 연결과 이름 바꾸기에 함께 쓴다."""

    BINDINGS = [Binding("escape", "close", "닫기")]

    def __init__(self, directory: str, name: str = "", title: str = "프로젝트 추가") -> None:
        super().__init__()
        self._directory = directory
        self._name = name
        self._title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="dialog-title")
            yield Input(value=self._directory, placeholder="프로젝트 디렉토리", id="dir")
            yield Input(value=self._name, placeholder="이름 (비우면 폴더 이름)", id="name")
            yield Label("Enter 로 저장, Tab 으로 칸 이동, Esc 로 닫기", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#dir", Input).focus()

    @on(Input.Submitted)
    def _submit(self) -> None:
        value = self.query_one("#dir", Input).value.strip()
        if not value:
            self.notify("디렉토리를 입력하세요.", severity="warning")
            return
        self.dismiss((value, self.query_one("#name", Input).value.strip()))

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


class PromptScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,enter", "close", "닫기")]

    def __init__(self, prompts: list[tuple[str, str]]) -> None:
        super().__init__()
        self._prompts = prompts

    def compose(self) -> ComposeResult:
        text = Text()
        for title, body in self._prompts:
            text.append(f"── {title} ──\n", style="bold")
            text.append(f"{body}\n\n")
        with VerticalScroll(id="prompts"):
            yield Static(text)

    def on_mount(self) -> None:
        pane = self.query_one("#prompts")
        pane.border_title = "역할별 프롬프트 (↑/↓ 스크롤, Esc 닫기)"
        pane.focus()

    def action_close(self) -> None:
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
    #artifacts { height: auto; padding: 0 1; background: $boost; color: $success; text-style: underline; display: none; }
    #artifacts.-has { display: block; }
    #artifacts:hover { background: $primary; color: $text; }
    #diff-pane { width: 2fr; border: round $accent; }
    #projects:focus-within, #jobs:focus-within, #timeline:focus, #diff-pane:focus-within { border: round $warning; }
    NewJobScreen, AddProjectScreen, ModelScreen, PromptScreen, ConfirmScreen { align: center middle; }
    #prompts { width: 90%; height: 90%; padding: 1 2; border: thick $primary; background: $surface; }
    .model-row { height: auto; }
    .model-role { width: 16; margin-top: 2; }
    .model-row .engine { width: 22; margin-top: 1; }
    .model-row Input { width: 1fr; }
    ModelScreen #dialog { width: 96; }
    #dialog { width: 80; height: auto; max-height: 100%; overflow-y: auto; padding: 1 2; border: thick $primary; background: $surface; }
    #dialog Input, #dialog Checkbox { margin-top: 1; }
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
        Binding("ctrl+p", "prompts", "프롬프트", priority=True, key_display="P"),
        Binding("ctrl+f", "open_artifacts", "결과물 폴더", priority=True, key_display="F"),
        Binding("ctrl+s", "cancel_job", "작업 취소", priority=True, key_display="S"),
        Binding("ctrl+r", "remove_job", "작업 삭제", priority=True, key_display="R"),
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
        self.project_dir: str | None = None  # 새 작업을 시작할 프로젝트 (목록에서 고른 것)
        self._projects: list[tuple[str, str]] | None = None  # (경로, 표시 이름)
        self._projects_error: str | None = None
        self.selected_id: int | None = None
        self._row_snapshot: dict[int, tuple] = {}
        self._last_event_id = 0
        self._result_shown = False
        self._result_event_seen = False
        self._last_status: str | None = None
        self._diff_text: str | None = None
        self._copied: str | None = None
        self._artifacts_text = ""
        self._ticks = 0

    # ── 화면 구성 ────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="left"):
                yield ProjectTable(id="projects", cursor_type="row", show_header=False)
                yield DataTable(id="jobs", cursor_type="row", zebra_stripes=True)
                yield VerticalScroll(id="timeline")
                yield ArtifactBar(id="artifacts")
                # select_on_focus 를 끄지 않으면 포커스를 받을 때 전체 선택돼 다음 글자가 쓴 내용을 지운다.
                yield Input(id="chat", select_on_focus=False)
            with VerticalScroll(id="diff-pane"):
                yield Static("작업을 선택하세요.", id="diff")
        yield Static("아래 단축키는 Ctrl 키를 누른 채로 입력합니다 (예: Ctrl+N 새 작업). 글자만 치면 채팅 입력으로 들어갑니다.", id="key-hint")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#jobs", DataTable)
        table.border_title = "작업 목록"
        self.query_one("#timeline").border_title = "타임라인"
        self.query_one("#timeline").border_subtitle = "PgUp/PgDn·휠로 이전 내용 · 드래그하면 복사"
        self.query_one("#diff-pane").border_title = "변경 사항"
        self.query_one("#diff-pane").border_subtitle = "드래그하면 복사"
        for key, label in (("st", ""), ("id", "#"), ("age", "경과"), ("repo", "레포"),
                           ("prompt", "요청"), ("phase", "현재 단계")):
            table.add_column(label, key=key)
        ptable = self.query_one("#projects", ProjectTable)
        ptable.add_column("이름", key="name")
        ptable.add_column("경로", key="path")
        opened = projects.normalize(self.default_dir)
        if gitops.repo_root(Path(opened)) is not None:  # TUI 를 연 레포는 자동으로 연결한다
            try:
                jobs.add_project(self.cfg, self.store, opened)
            except JobError:
                pass  # 원인은 refresh_projects 가 알린다
        self.refresh_projects(select=opened)
        self.refresh_jobs()
        self._update_chat_hint()
        # 작업 위치부터 고르게 한다. 연결된 프로젝트가 없으면 고를 것이 없으니 작업 목록에서 시작한다.
        (ptable if self._projects else table).focus()
        self.set_interval(POLL_SEC, self._tick)

    # ── 주기 갱신 ────────────────────────────────────────────
    def _tick(self) -> None:
        self._ticks += 1
        self.refresh_projects()
        self.refresh_jobs()
        self._poll_selected()
        self._update_chat_hint()
        self._update_artifacts()

    def refresh_projects(self, select: str | None = None) -> None:
        """목록 파일이 바뀌었으면(다른 터미널 포함) 다시 그린다. select 가 목록에 있으면 그 줄을 고른다."""
        try:
            names = jobs.project_names(self.cfg, self.store)
            self._projects_error = None
        except JobError as exc:
            names = {}
            if str(exc) != self._projects_error:  # 매 초 같은 알림을 띄우지 않는다
                self._projects_error = str(exc)
                self.notify(str(exc), severity="error", timeout=8)
        table = self.query_one("#projects", ProjectTable)
        items = list(names)
        if list(names.items()) != self._projects:
            self._projects = list(names.items())
            table.clear()
            for path, name in names.items():
                missing = "" if Path(path).is_dir() else "  (없음)"
                table.add_row(name, Text(projects.display(path) + missing, style="dim"), key=path)
            select = select or self.project_dir
        if select is not None:
            self.project_dir = select if select in items else (items[0] if items else None)
            if self.project_dir is not None:
                table.move_cursor(row=items.index(self.project_dir))
        self._show_project_title()

    def _show_project_title(self) -> None:
        name = dict(self._projects or []).get(self.project_dir or "")
        table = self.query_one("#projects", ProjectTable)
        # 제목은 markup 으로 해석되므로 대괄호를 쓰지 않는다.
        table.border_title = Text(f"프로젝트 · 새 작업 위치: {name}" if name else "프로젝트 · 없음 (Ctrl+G 로 추가)").markup
        table.border_subtitle = "↑↓ 선택 · Enter 확정 · Ctrl+G 추가 · Ctrl+E 이름 · Ctrl+X 빼기 · Ctrl+O 여기로 이동"

    @on(DataTable.RowHighlighted, "#projects")
    def _on_project_highlight(self, event: DataTable.RowHighlighted) -> None:
        # 목록을 다시 그릴 때 쌓인 오래된 이벤트는 무시한다 (작업 목록과 같다).
        if event.row_key.value is None or event.cursor_row != event.data_table.cursor_row:
            return
        self.project_dir = event.row_key.value
        self._show_project_title()

    @on(DataTable.RowSelected, "#projects")
    def _on_project_chosen(self, event: DataTable.RowSelected) -> None:
        """Enter(또는 클릭)로 작업 위치를 정하고 작업 목록으로 넘어간다."""
        if event.row_key.value is None:
            return
        self.project_dir = event.row_key.value
        self._show_project_title()
        self.notify(f"작업 위치: {dict(self._projects or []).get(self.project_dir, self.project_dir)} — ctrl+n 으로 새 작업")
        self.query_one("#jobs", DataTable).focus()

    def action_focus_projects(self) -> None:
        self.query_one("#projects", ProjectTable).focus()

    def action_add_project(self) -> None:
        # 추가 창에서 이름을 비우면 (이미 연결된 디렉토리의) 기존 이름을 지우지 않는다.
        self.push_screen(AddProjectScreen(self.default_dir), lambda a: self._save_project(a and (a[0], a[1] or None)))

    def action_rename_project(self) -> None:
        if self.project_dir is None:
            self.notify("선택된 프로젝트가 없습니다.", severity="warning")
            return
        current = dict(self._projects or []).get(self.project_dir, "")
        self.push_screen(AddProjectScreen(self.project_dir, current, "프로젝트 이름 바꾸기"), self._save_project)

    def _save_project(self, answer: tuple[str, str | None] | None) -> None:
        if not answer:
            return
        directory, name = answer
        try:
            added = jobs.add_project(self.cfg, self.store, directory, name)
        except JobError as exc:
            self.notify(str(exc), severity="error", timeout=8)
            return
        self.refresh_projects(select=added)
        self.notify(f"프로젝트 연결: {dict(self._projects or []).get(added, added)}")

    def action_remove_project(self) -> None:
        if self.project_dir is None:
            self.notify("선택된 프로젝트가 없습니다.", severity="warning")
            return
        removed = self.project_dir
        try:
            jobs.remove_project(self.cfg, self.store, removed)
        except JobError as exc:
            self.notify(str(exc), severity="error", timeout=8)
            return
        self.notify(f"목록에서 뺐습니다: {Path(removed).name} (디렉토리와 작업은 그대로)")
        self.refresh_projects()

    def refresh_jobs(self) -> None:
        table = self.query_one("#jobs", DataTable)
        rows = [jobs.reconcile(self.store, j) for j in self.store.list_jobs(100)]
        now = time.time()
        snapshot = {j.id: self._row(j, now) for j in rows}
        if list(snapshot) != list(self._row_snapshot):
            # 작업이 추가·삭제됐으면 다시 그리고 선택 위치를 유지한다.
            old_row = table.cursor_row
            table.clear()
            for job_id, cells in snapshot.items():
                table.add_row(*cells, key=str(job_id))
            if self.selected_id in snapshot:
                table.move_cursor(row=table.get_row_index(str(self.selected_id)))
            else:
                # 보던 작업이 삭제됐다 (다른 터미널에서 지운 경우 포함): 남은 작업 중 커서가 있는 것을 보여준다.
                self._clear_selection()
                if snapshot:
                    row = min(old_row, len(snapshot) - 1)  # 지운 자리의 작업 (마지막이었으면 그 위)
                    table.move_cursor(row=row)
                    self.select_job(list(snapshot)[row])
        else:
            for job_id, cells in snapshot.items():
                old = self._row_snapshot[job_id]
                for col, (a, b) in zip(("st", "id", "age", "repo", "prompt", "phase"), zip(old, cells)):
                    if a != b:
                        table.update_cell(str(job_id), col, b)
        self._row_snapshot = snapshot
        if not rows:
            self.query_one("#diff", Static).update(Text("아직 작업이 없습니다. 아래 입력창에 시킬 일을 쓰고 Enter 를 누르세요."))  # 문자열은 markup 으로 해석돼 [n] 이 사라진다

    @staticmethod
    def _row(job: Job, now: float) -> tuple:
        style = _STATUS_STYLE.get(job.status, "")
        return (
            Text(STATUS_ICON.get(job.status, "?"), style=style),
            str(job.id),
            jobs.ago(now - job.created_at),
            Path(job.repo).name,
            jobs.one_line(job.prompt, 40),
            Text(jobs.one_line(job.phase, 60), style=style),
        )

    @on(DataTable.RowHighlighted, "#jobs")
    def _on_highlight(self, event: DataTable.RowHighlighted) -> None:
        # 목록을 다시 그리면 하이라이트 이벤트가 여러 번 쌓인다. 현재 커서와 다른 오래된 이벤트는 무시한다.
        if event.row_key.value is None or event.cursor_row != event.data_table.cursor_row:
            return
        self.select_job(int(event.row_key.value))

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
        self._timeline_write(
            Text(f"요청: {job.prompt}", style="bold"),
            Text(f"위치: {job.workdir}" + (f"  (브랜치 {job.branch})" if job.branch else ""), style="dim"),
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
            items: list[Text | Markdown] = []
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
                items += [Text(f"── 팀장 보고 ({ts}) ──", style="bold"), Markdown(ev.message)]
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
                self._timeline_write(summary, Text("── 팀장 보고 ──", style="bold"), Markdown(job.result))
            else:
                self._timeline_write(summary)
        # 실행 중이면 주기적으로, 방금 끝났으면 한 번 더 diff 를 계산한다.
        just_finished = job.is_terminal and self._last_status not in (None, job.status)
        self._last_status = job.status
        if force_diff or just_finished or (not job.is_terminal and self._ticks % DIFF_EVERY_TICKS == 0):
            self._load_diff(job.id, job.workdir, job.base_commit)

    def _timeline_write(self, *items: Text | Markdown) -> None:
        log = self.query_one("#timeline", VerticalScroll)
        at_end = log.scroll_y >= log.max_scroll_y - 1  # 위로 올려 읽는 중이면 끌어내리지 않는다
        log.mount(*(item if isinstance(item, Markdown) else Static(item) for item in items))
        if at_end:
            self.call_after_refresh(log.scroll_end, animate=False)

    def _update_artifacts(self) -> None:
        bar = self.query_one("#artifacts", ArtifactBar)
        files = jobs.artifacts(self.cfg, self.selected_id) if self.selected_id is not None else []
        text = ""
        if files:
            folder = projects.display(str(self.cfg.artifacts_dir(self.selected_id)))
            text = f"📁 결과물 {jobs.artifacts_summary(files)}: {folder}  ← 클릭하면 폴더 열기 (Ctrl+F)"
        if text != self._artifacts_text:
            self._artifacts_text = text
            bar.update(Text(text))
            bar.set_class(bool(files), "-has")

    def action_open_artifacts(self) -> None:
        if self.selected_id is None:
            self.notify("선택된 작업이 없습니다.", severity="warning")
            return
        try:
            root = jobs.open_artifacts(self.cfg, self.selected_id)
        except JobError as exc:
            self.notify(str(exc), severity="warning", timeout=6)
            return
        self.notify(f"폴더를 열었습니다: {projects.display(str(root))}", timeout=3)

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
        if not workdir or not Path(workdir).is_dir():
            self.call_from_thread(self._show_diff, job_id, "작업 디렉토리가 없습니다. 정리됐거나 아직 만들어지지 않았습니다.", True)
            return
        try:
            text = gitops.diff(Path(workdir), base)
        except gitops.GitError as exc:
            self.call_from_thread(self._show_diff, job_id, f"diff 를 계산할 수 없습니다: {exc}", True)
            return
        self.call_from_thread(self._show_diff, job_id, text, False)

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
    def action_new_job(self) -> None:
        self.push_screen(NewJobScreen(self.project_dir or self.default_dir), self._start_job)

    def _start_job(self, answer: tuple[str, str, bool] | None) -> bool:
        """시작했으면 True."""
        if not answer:
            return False
        prompt, directory, no_worktree = answer
        try:
            launched = jobs.launch(self.cfg, self.store, prompt, directory, no_worktree=no_worktree)
        except (JobError, gitops.GitError) as exc:
            self.notify(str(exc), severity="error", timeout=8)
            return False
        for w in launched.warnings:
            self.notify(w, severity="warning", timeout=8)
        self.notify(f"작업 #{launched.job.id} 시작")
        self.refresh_projects(select=projects.normalize(directory))  # 직접 입력한 디렉토리도 연결된다
        self.refresh_jobs()
        table = self.query_one("#jobs", DataTable)
        table.move_cursor(row=table.get_row_index(str(launched.job.id)))
        self.select_job(launched.job.id)
        return True

    # ── 채팅 입력 ────────────────────────────────────────────
    def on_key(self, event: events.Key) -> None:
        """메인 화면에서는 어디에 포커스가 있든 글자를 치면 채팅 입력으로 들어간다."""
        if len(self.screen_stack) > 1:
            return  # 대화 상자가 떠 있을 때는 그쪽 입력이다
        chat = self.query_one("#chat", Input)
        if event.key == "escape" and self.focused is chat:
            self.query_one("#jobs", DataTable).focus()
            event.stop()
        elif event.is_printable and event.character and not isinstance(self.focused, Input):
            chat.focus()
            chat.insert_text_at_cursor(event.character)
            event.stop()

    @on(DataTable.RowSelected, "#jobs")
    def _on_job_chosen(self) -> None:
        self.query_one("#chat", Input).focus()

    @on(Input.Submitted, "#chat")
    def _chat_submitted(self, event: Input.Submitted) -> None:
        prompt = event.value.strip()
        if not prompt:
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
        if not job.is_terminal:
            self.notify(f"작업 #{job.id} 이(가) 아직 실행 중입니다. 끝난 뒤에 보낼 수 있습니다.", severity="warning")
            return False
        try:
            launched = jobs.continue_job(self.cfg, self.store, job.id, prompt)
        except (JobError, gitops.GitError) as exc:
            self.notify(str(exc), severity="error", timeout=8)
            return False
        for w in launched.warnings:
            self.notify(w, severity="warning", timeout=8)
        self.refresh_jobs()
        self._poll_selected()
        timeline = self.query_one("#timeline", VerticalScroll)
        self.call_after_refresh(timeline.scroll_end, animate=False)  # 보낸 요청이 보이게 맨 아래로
        return True

    def _update_chat_hint(self) -> None:
        chat = self.query_one("#chat", Input)
        if self.selected_id is None:
            where = dict(self._projects or []).get(self.project_dir or "", Path(self.default_dir).name)
            hint = f"새 작업으로 시킬 일 (Enter) — 위치: {where}"
        else:
            try:
                running = not self.store.get_job(self.selected_id).is_terminal
            except KeyError:
                return
            hint = (f"작업 #{self.selected_id} 실행 중 — 끝나면 이어서 보낼 수 있습니다" if running
                    else f"작업 #{self.selected_id} 에 이어서 시킬 일 (Enter)   ·   새 작업은 ctrl+n")
        if chat.placeholder != hint:
            chat.placeholder = hint

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # 메인 화면 단축키는 priority 라 대화 상자 안에서도 잡힌다. 대화 상자가 떠 있으면 막는다.
        if action in _MAIN_ACTIONS and len(self.screen_stack) > 1:
            return False
        return True

    def action_models(self) -> None:
        try:
            agents = team.load_agents(self.cfg.agents_override)
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
        try:
            self.push_screen(PromptScreen(jobs.role_prompts(self.cfg)))
        except JobError as exc:
            self.notify(str(exc), severity="error", timeout=8)

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

    def action_remove_job(self) -> None:
        job = self._selected_job()
        if job is None:
            return

        def remove(force: bool) -> None:
            try:
                message = jobs.remove(self.cfg, self.store, self.store.get_job(job.id), force=force)
            except KeyError:
                message = f"작업 #{job.id} 은(는) 이미 삭제됐습니다."
            except WorktreeRemoveError as exc:
                if force:
                    self.notify(str(exc), severity="error", timeout=10)
                    return
                self.push_screen(
                    ConfirmScreen(f"작업 #{job.id} 의 worktree 에 검토하지 않은 변경이 있습니다.\n변경을 버리고 삭제할까요?"),
                    lambda ok: remove(True) if ok else None,
                )
                return
            except (JobError, gitops.GitError) as exc:
                self.notify(str(exc), severity="error", timeout=10)
                return
            self.notify(message)
            self.refresh_jobs()  # 목록에서 바로 사라진다

        self.push_screen(
            ConfirmScreen(f"작업 #{job.id} 을(를) 삭제할까요? 목록·기록과 worktree·브랜치가 지워집니다."),
            lambda ok: remove(False) if ok else None,
        )


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


def _event_line(ev: EventRow) -> Text:
    ts = time.strftime("%H:%M:%S", time.localtime(ev.ts))
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
