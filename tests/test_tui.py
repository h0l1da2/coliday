"""Textual headless(run_test) 로 TUI 동작을 검증한다."""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("textual")

from textual.widgets import DataTable, Input, Markdown, Static, TextArea  # noqa: E402

from coliday import config, jobs, team  # noqa: E402
from coliday.store import Store  # noqa: E402
from coliday.tui import (  # noqa: E402
    AddProjectScreen, ConfirmScreen, ColidayApp, ModelScreen, NewJobScreen, RoleEditScreen, TeamScreen,
)

FAKE = Path(__file__).parent / "fixtures" / "fake_claude.py"
SRC = Path(__file__).parents[1] / "src"


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake_bin = tmp_path / "claude"
    fake_bin.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("COLIDAY_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COLIDAY_CLAUDE_BIN", str(fake_bin))
    monkeypatch.setenv("FAKE_CLAUDE_ARGS", str(tmp_path / "args.json"))
    monkeypatch.setenv("PYTHONPATH", str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return tmp_path


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "myrepo"
    r.mkdir()
    (r / "a.txt").write_text("a\n")
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(r), *args], check=True)
    return r


def _run(coro):
    asyncio.run(coro)


async def _until(pilot, cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        await pilot.pause(0.1)
    raise AssertionError("시간 초과")


def _timeline_text(app) -> str:
    return "\n".join(
        w.source if isinstance(w, Markdown) else str(w.content) for w in app.query_one("#timeline").children
    )


def _diff_text(app) -> str:
    return str(app.query_one("#diff", Static).content)


def test_새_작업을_입력하면_실행되고_타임라인과_diff가_갱신된다(env, repo, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_TOUCH", "created.txt")
    cfg = config.load()
    store = Store(cfg.db_path)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.press("ctrl+n")
            assert isinstance(app.screen, NewJobScreen)
            app.screen.query_one("#prompt", Input).value = "주문 취소 API 추가"
            await pilot.press("enter")

            await _until(pilot, lambda: store.list_jobs() and store.list_jobs()[0].is_terminal)
            await _until(pilot, lambda: "최종 보고: 완료" in _timeline_text(app))
            await _until(pilot, lambda: "+new file" in _diff_text(app))

            assert app.selected_id == 1
            assert app.query_one("#jobs", DataTable).row_count == 1
            timeline = _timeline_text(app)
            assert "architect에게 분배: 주문 취소 설계" in timeline
            assert "Read OrderService.java" in timeline

    try:
        _run(scenario())
    finally:
        store.close()


def test_git_저장소가_아닌_디렉토리로_시작하면_오류를_알리고_작업을_만들지_않는다(env, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    cfg = config.load()
    store = Store(cfg.db_path)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(plain))
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.press("ctrl+n")
            app.screen.query_one("#prompt", Input).value = "x"
            await pilot.press("enter")
            await pilot.pause()
            assert store.list_jobs() == []
            assert any("git 저장소가 아닙니다" in n.message for n in app._notifications)

    try:
        _run(scenario())
    finally:
        store.close()


def test_실행중_작업을_확인후_취소한다(env, repo, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    cfg = config.load()
    store = Store(cfg.db_path)
    launched = jobs.launch(cfg, store, "오래 걸리는 일", repo)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: store.get_job(launched.job.id).status == "running")
            await _until(pilot, lambda: app.selected_id == launched.job.id)
            await pilot.press("ctrl+s")
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("y")
            await _until(pilot, lambda: store.get_job(launched.job.id).status == "cancelled")

    try:
        _run(scenario())
        pid = store.get_job(launched.job.id).pid
        end = time.time() + 10
        while time.time() < end and os.waitpid(pid, os.WNOHANG)[0] != pid:
            time.sleep(0.1)
    finally:
        store.close()


def test_목록을_다시_그려도_선택한_작업이_유지된다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)
    first = store.create_job(prompt="첫 작업", repo=str(repo), workdir=str(repo), model=None)
    store.finish_job(first.id, "succeeded", phase="완료", result="첫 보고")

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.selected_id == first.id)
            await _until(pilot, lambda: "첫 보고" in _timeline_text(app))

            second = store.create_job(prompt="둘째 작업", repo=str(repo), workdir=str(repo), model=None)
            store.finish_job(second.id, "succeeded", phase="완료")
            await _until(pilot, lambda: app.query_one("#jobs", DataTable).row_count == 2)
            await pilot.pause(0.3)

            assert app.selected_id == first.id
            assert "첫 보고" in _timeline_text(app)

    try:
        _run(scenario())
    finally:
        store.close()


def test_채팅_입력으로_끝난_작업에_이어서_시키면_같은_세션을_이어간다(env, repo):
    import json
    cfg = config.load()
    store = Store(cfg.db_path)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            chat = app.query_one("#chat", Input)
            assert "새 작업" in chat.placeholder
            # 작업이 없을 때: 그냥 글자를 치면 채팅 입력으로 들어가고, Enter 로 새 작업이 시작된다
            assert app.focused is not chat
            await pilot.press("n", "e", "w")
            assert app.focused is chat and chat.value == "new" and len(app.screen_stack) == 1
            chat.value = "주문 취소 API 추가"
            await pilot.press("enter")
            await _until(pilot, lambda: store.list_jobs() and store.list_jobs()[0].is_terminal)
            await _until(pilot, lambda: _timeline_text(app).count("최종 보고: 완료") == 1)
            assert chat.value == "" and store.get_job(1).repo == str(repo.resolve())
            await _until(pilot, lambda: "작업 #1 에 이어서" in chat.placeholder)

            # 작업 목록에서 Enter → 채팅 입력, Esc → 다시 목록
            await pilot.press("escape")
            assert app.focused is app.query_one("#jobs", DataTable)
            await pilot.press("enter")
            assert app.focused is chat

            await pilot.press("f")  # 예전 단축키 글자도 그냥 입력된다
            assert chat.value == "f"
            chat.value = "테스트도 추가해줘"
            await pilot.press("enter")

            await _until(pilot, lambda: store.last_follow_up(1) is not None and store.get_job(1).is_terminal)
            await _until(pilot, lambda: _timeline_text(app).count("최종 보고: 완료") == 2)
            assert app.query_one("#jobs", DataTable).row_count == 1 and chat.value == ""
            assert "테스트도 추가해줘" in _timeline_text(app)
            argv = json.loads((env / "args.json").read_text())["argv"]
            assert argv[argv.index("--resume") + 1] == "s-1"

    try:
        _run(scenario())
    finally:
        store.close()


def test_실행중인_작업에_보낸_채팅은_대기했다가_끝나면_전달된다(env, repo, monkeypatch):
    import json
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "2")
    cfg = config.load()
    store = Store(cfg.db_path)
    launched = jobs.launch(cfg, store, "처음 요청", repo)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            chat = app.query_one("#chat", Input)
            await _until(pilot, lambda: store.get_job(1).status == "running")
            await _until(pilot, lambda: app.selected_id == 1)
            await _until(pilot, lambda: "끝난 뒤 이어서 전달됩니다" in chat.placeholder)
            chat.focus()
            chat.value = "버튼 색도 바꿔줘"
            await pilot.press("enter")
            await pilot.pause()
            assert chat.value == ""  # 보내졌다 (대기)
            assert any("대기시켰습니다" in n.message for n in app._notifications)
            await _until(pilot, lambda: "⏳ 대기" in _timeline_text(app) and "버튼 색도 바꿔줘" in _timeline_text(app))

            await _until(pilot, lambda: store.get_job(1).is_terminal, timeout=30)
            await _until(pilot, lambda: _timeline_text(app).count("최종 보고: 완료") == 2)
            recorded = json.loads((env / "args.json").read_text())
            assert recorded["stdin"] == "버튼 색도 바꿔줘" and "--resume" in recorded["argv"]

    try:
        _run(scenario())
        assert launched.job.id == 1
    finally:
        store.close()


def test_채팅_입력_중에도_ctrl_단축키가_동작하고_대화_상자는_겹치지_않는다(env, repo, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    cfg = config.load()
    store = Store(cfg.db_path)
    launched = jobs.launch(cfg, store, "오래 걸리는 일", repo)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            chat = app.query_one("#chat", Input)
            await _until(pilot, lambda: store.get_job(launched.job.id).status == "running")
            await _until(pilot, lambda: app.selected_id == launched.job.id)
            await _until(pilot, lambda: "실행 중" in chat.placeholder)
            chat.focus()
            chat.value = "더 해줘"
            await pilot.press("ctrl+n")
            assert isinstance(app.screen, NewJobScreen)
            await pilot.press("ctrl+n", "ctrl+t", "x")
            assert isinstance(app.screen, NewJobScreen) and len(app.screen_stack) == 2
            assert app.screen.query_one("#prompt", Input).value == "x" and chat.value == "더 해줘"
            await pilot.press("escape")
            await pilot.press("ctrl+s")
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("y")
            await _until(pilot, lambda: store.get_job(launched.job.id).status == "cancelled")

    try:
        _run(scenario())
    finally:
        pid = store.get_job(launched.job.id).pid
        jobs.cancel(store, store.get_job(launched.job.id))
        end = time.time() + 10
        while time.time() < end and os.waitpid(pid, os.WNOHANG)[0] != pid:
            time.sleep(0.1)
        store.close()


def test_모델_창에서_기본_에이전트와_역할별_에이전트_모델을_고른다(env, repo, monkeypatch):
    from textual.widgets import Select
    from coliday import team
    fake_codex = env / "codex"
    fake_codex.write_text("#!/bin/sh\nexit 0\n")
    fake_codex.chmod(0o755)
    monkeypatch.setenv("COLIDAY_CODEX_BIN", str(fake_codex))
    cfg = config.load()
    store = Store(cfg.db_path)
    team.save_models(cfg.models_path, team.Models(lead="opus", agents={"reviewer": "codex:gpt-r"}))

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("ctrl+t")
            screen = app.screen
            assert isinstance(screen, ModelScreen)
            roles = screen._roles
            row = lambda role: roles.index(role)
            # 저장된 값이 에이전트와 모델로 나뉘어 보인다
            assert screen.query_one("#default-engine", Select).value == "claude"
            assert screen.query_one("#engine-0", Select).value == "claude"
            assert screen.query_one("#model-0", Input).value == "opus"
            assert screen.query_one(f"#engine-{row('reviewer')}", Select).value == "codex"
            assert screen.query_one(f"#model-{row('reviewer')}", Input).value == "gpt-r"
            assert screen.query_one(f"#engine-{row('coder')}", Select).value == "follow"

            # 에이전트를 바꾸면 그 줄의 모델 칸이 비워진다 (opus 가 남아 codex:opus 가 되지 않게)
            screen.query_one("#engine-0", Select).value = "codex"
            await pilot.pause()
            assert screen.query_one("#model-0", Input).value == ""
            assert screen.query_one(f"#model-{row('reviewer')}", Input).value == "gpt-r"  # 다른 줄은 그대로

            screen.query_one("#default-engine", Select).value = "codex"          # 기본 에이전트: codex
            screen.query_one("#engine-0", Select).value = "follow"               # 팀장: 기본을 따름
            screen.query_one(f"#engine-{row('coder')}", Select).value = "codex"  # coder: codex 의 특정 모델
            screen.query_one(f"#engine-{row('reviewer')}", Select).value = "claude"
            await pilot.pause()  # 에이전트를 고른 뒤에 모델을 적는다
            assert screen.query_one(f"#model-{row('reviewer')}", Input).value == ""  # gpt-r 가 남지 않는다
            screen.query_one(f"#model-{row('coder')}", Input).value = "gpt-6.1-sol"
            await pilot.pause()
            await pilot.click("#ok")
            await pilot.pause()

            assert team.load_models(cfg.models_path) == team.Models(
                default="codex", lead=None, agents={"reviewer": "claude", "coder": "codex:gpt-6.1-sol"})
            rows = dict((r, t) for r, t, _ in jobs.model_rows(cfg))
            assert (rows["lead"], rows["architect"], rows["reviewer"]) == ("codex", "codex", "claude")

            await pilot.press("ctrl+t")
            app.screen.query_one("#engine-0", Select).value = "claude"
            await pilot.pause()
            app.screen.query_one("#model-0", Input).value = "두 단어"
            app.screen.query_one("#model-0", Input).focus()
            await pilot.press("enter")
            await pilot.pause()
            assert any("올바르지 않습니다" in n.message for n in app._notifications)
            assert team.load_models(cfg.models_path).lead is None

    try:
        _run(scenario())
    finally:
        store.close()


def test_p로_역할별_프롬프트를_보고_닫는다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.press("ctrl+p")
            assert isinstance(app.screen, TeamScreen)
            preview = app.screen.query_one("#team-preview", Static)
            assert "coliday 팀장 모드" in str(preview.content)   # 첫 줄은 팀장
            for _ in range(4):
                await pilot.press("down")
            await _until(pilot, lambda: "너는 코드 리뷰어다" in str(preview.content))
            await pilot.press("escape")
            assert not isinstance(app.screen, TeamScreen)

    try:
        _run(scenario())
    finally:
        store.close()


def test_팀_화면에서_역할을_고치고_넣고_빼고_되돌린다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)

    def state(role):
        return {r: s for r, s, _ in jobs.team_roles(cfg)}.get(role)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.press("ctrl+p")
            preview = app.screen.query_one("#team-preview", Static)

            # 고치기: reviewer 의 프롬프트만 바꾼다. 채팅 입력의 단축키(Ctrl+S = 작업 취소)가 아니라 저장이 된다
            for _ in range(4):
                await pilot.press("down")
            await pilot.press("enter")
            assert isinstance(app.screen, RoleEditScreen)
            area = app.screen.query_one("#role-prompt", TextArea)
            assert area.text.startswith("너는 코드 리뷰어다") and "실패했을 때" not in area.text  # 자동 규칙은 원문에 없다
            area.text = "너는 보안 리뷰어다."
            await pilot.press("ctrl+s")
            await _until(pilot, lambda: isinstance(app.screen, TeamScreen))
            assert state("reviewer") == team.STATE_CHANGED and "너는 보안 리뷰어다." in str(preview.content)
            assert team.load_custom(cfg.team_path).agents == {"reviewer": {"prompt": "너는 보안 리뷰어다."}}

            # 추가: 빈 값으로는 저장되지 않고 화면도 닫히지 않는다 (쓴 내용이 남는다)
            await pilot.press("a")
            assert isinstance(app.screen, RoleEditScreen)
            app.screen.query_one("#role-name", Input).value = "qa"
            app.screen.query_one("#role-prompt", TextArea).text = "너는 QA 다."
            await pilot.press("ctrl+s")
            await pilot.pause(0.2)
            assert isinstance(app.screen, RoleEditScreen) and state("qa") is None   # 설명이 없다
            app.screen.query_one("#role-description", Input).value = "구현 뒤 검수"
            app.screen.query_one("#role-tools", Input).value = "Read, Bash"
            await pilot.press("ctrl+s")
            await _until(pilot, lambda: isinstance(app.screen, TeamScreen))
            assert state("qa") == team.STATE_ADDED
            assert team.load_agents(cfg.agents_override, team.load_custom(cfg.team_path))["qa"]["tools"] == ["Read", "Bash"]
            assert "- qa: 구현 뒤 검수" in jobs.role_prompts(cfg, "lead")[0][1]   # 팀장 지시문에도 들어간다

            # 저장하지 않은 변경은 Esc 한 번으로 버려지지 않는다
            await pilot.press("enter")   # 커서는 방금 넣은 qa
            app.screen.query_one("#role-prompt", TextArea).text = "버릴 내용"
            await pilot.press("escape")
            assert isinstance(app.screen, RoleEditScreen)
            await pilot.press("escape")
            await _until(pilot, lambda: isinstance(app.screen, TeamScreen))
            assert jobs.role_definition(cfg, "qa")["prompt"] == "너는 QA 다."

            # 빼기와 되돌리기
            for _ in range(8):
                await pilot.press("up")
            for _ in range(3):
                await pilot.press("down")   # codex-coder
            await pilot.press("d")
            await _until(pilot, lambda: state("codex-coder") == team.STATE_REMOVED)
            assert "codex-coder" not in team.load_agents(cfg.agents_override, team.load_custom(cfg.team_path))
            await pilot.press("r")
            await _until(pilot, lambda: state("codex-coder") == team.STATE_DEFAULT)
            await pilot.press("down")       # reviewer
            await pilot.press("r")
            await _until(pilot, lambda: state("reviewer") == team.STATE_DEFAULT)

            # 팀장 지시문
            for _ in range(8):
                await pilot.press("up")
            await pilot.press("enter")
            area = app.screen.query_one("#role-prompt", TextArea)
            assert "{team}" in area.text and "{max_attempts}" in area.text
            assert not app.screen.query("#role-description")
            area.text = "너는 팀장이다.\n{team}\n최대 {max_attempts}번."
            await pilot.press("ctrl+s")
            await _until(pilot, lambda: isinstance(app.screen, TeamScreen))
            assert state("lead") == team.STATE_CHANGED
            shown = str(preview.content)
            assert "너는 팀장이다." in shown and "- qa: 구현 뒤 검수" in shown and "최대 5번." in shown

    try:
        _run(scenario())
    finally:
        store.close()

def _second_repo(tmp_path):
    r = tmp_path / "other"
    r.mkdir()
    (r / "b.txt").write_text("b\n")
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(r), *args], check=True)
    return r


def test_프로젝트_목록에서_고른_디렉토리에서_새_작업을_시작한다(env, repo, tmp_path):
    import json
    other = _second_repo(tmp_path)
    cfg = config.load()
    store = Store(cfg.db_path)
    jobs.add_project(cfg, store, other)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            table = app.query_one("#projects", DataTable)
            # TUI 를 연 레포가 자동으로 연결되고 선택된다
            assert jobs.list_projects(cfg, store) == [str(other.resolve()), str(repo.resolve())]
            assert app.project_dir == str(repo.resolve())
            assert table.cursor_row == 1 and table.region.height <= 8
            assert app.focused is table  # 처음에는 작업 위치부터 고른다

            await pilot.press("up")
            await pilot.pause()
            assert app.project_dir == str(other.resolve())
            assert "other" in table.border_title
            await pilot.press("enter")  # 확정하면 작업 목록으로 넘어간다
            await pilot.pause()
            assert app.focused is app.query_one("#jobs", DataTable)
            assert app.project_dir == str(other.resolve())
            assert any("작업 위치: other" in n.message for n in app._notifications)
            await pilot.press("ctrl+o")
            assert app.focused is table

            await pilot.press("ctrl+n")
            assert app.screen.query_one("#dir", Input).value == str(other.resolve())
            app.screen.query_one("#prompt", Input).value = "x"
            await pilot.press("enter")
            await _until(pilot, lambda: store.list_jobs() and store.list_jobs()[0].is_terminal)

            job = store.get_job(1)
            assert job.repo == str(other.resolve())
            assert Path(json.loads((env / "args.json").read_text())["cwd"]).resolve() == Path(job.workdir).resolve()
            assert app.project_dir == str(other.resolve())  # 작업을 시작해도 선택이 유지된다

    try:
        _run(scenario())
    finally:
        store.close()


def test_프로젝트를_추가하고_빼며_다른_터미널의_변경도_반영한다(env, repo, tmp_path):
    other = _second_repo(tmp_path)
    third = tmp_path / "third"
    third.mkdir()
    cfg = config.load()
    store = Store(cfg.db_path)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            table = app.query_one("#projects", DataTable)
            await pilot.press("ctrl+o")
            await pilot.press("ctrl+g")
            assert isinstance(app.screen, AddProjectScreen)
            app.screen.query_one("#dir", Input).value = str(tmp_path / "nope")
            await pilot.press("enter")
            await pilot.pause()
            assert any("디렉토리가 없습니다" in n.message for n in app._notifications)
            assert table.row_count == 1

            await pilot.press("ctrl+g")
            app.screen.query_one("#dir", Input).value = str(other)
            await pilot.press("enter")
            await pilot.pause()
            assert table.row_count == 2 and app.project_dir == str(other.resolve())

            jobs.add_project(cfg, store, third)  # 다른 터미널에서 추가
            await _until(pilot, lambda: table.row_count == 3)
            await pilot.pause(0.3)
            assert app.project_dir == str(other.resolve()) and table.cursor_row == 1  # 선택은 그대로

            await pilot.press("ctrl+x")
            await pilot.pause()
            assert jobs.list_projects(cfg, store) == [str(repo.resolve()), str(third.resolve())]
            assert app.project_dir == str(repo.resolve()) and other.is_dir()

    try:
        _run(scenario())
    finally:
        store.close()


def test_git_저장소가_아닌_곳에서_열면_자동으로_연결하지_않는다(env, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    cfg = config.load()
    store = Store(cfg.db_path)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(plain))
        async with app.run_test(size=(160, 45)) as pilot:
            assert jobs.list_projects(cfg, store) == [] and app.project_dir is None
            assert app.focused is app.query_one("#jobs", DataTable)  # 고를 프로젝트가 없으면 작업 목록에서 시작
            await pilot.press("ctrl+x")
            await pilot.pause()
            await pilot.press("ctrl+n")
            assert app.screen.query_one("#dir", Input).value == str(plain)  # 연결된 프로젝트가 없으면 연 디렉토리

    try:
        _run(scenario())
    finally:
        store.close()


def test_작업_목록에서_바로_a로_이름을_붙여_프로젝트를_추가하고_e로_이름을_바꾼다(env, repo, tmp_path):
    other = _second_repo(tmp_path)
    cfg = config.load()
    store = Store(cfg.db_path)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            table = app.query_one("#projects", DataTable)
            await pilot.press("enter")
            await pilot.pause()
            assert app.focused is app.query_one("#jobs", DataTable)
            await pilot.press("ctrl+g")  # 프로젝트 패널로 가지 않아도 된다
            assert isinstance(app.screen, AddProjectScreen)
            app.screen.query_one("#dir", Input).value = str(other)
            app.screen.query_one("#name", Input).value = "쇼핑몰 프론트"
            await pilot.press("enter")
            await pilot.pause()

            assert jobs.project_names(cfg, store)[str(other.resolve())] == "쇼핑몰 프론트"
            assert app.project_dir == str(other.resolve())
            assert "쇼핑몰 프론트" in str(table.border_title)
            assert str(table.get_row(str(other.resolve()))[0]) == "쇼핑몰 프론트"

            await pilot.press("ctrl+o")
            await pilot.press("ctrl+e")
            assert isinstance(app.screen, AddProjectScreen)
            assert app.screen.query_one("#name", Input).value == "쇼핑몰 프론트"
            app.screen.query_one("#name", Input).value = ""
            await pilot.press("enter")
            await pilot.pause()
            assert jobs.project_names(cfg, store)[str(other.resolve())] == "other"
            assert str(table.get_row(str(other.resolve()))[0]) == "other"

    try:
        _run(scenario())
    finally:
        store.close()


@pytest.mark.parametrize("keys, cancelled", [
    (["enter"], True),            # 기본 선택은 예
    (["right", "enter"], False),  # 방향키로 아니오
    (["tab", "enter"], False),
    (["right", "left", "enter"], True),
    (["ㅛ"], True),               # 한글 입력 상태의 y
    (["ㅜ"], False),
    (["escape"], False),
    (["click:#yes"], True),
    (["click:#no"], False),
])
def test_확인_창은_방향키_엔터_마우스로_고를_수_있다(env, repo, monkeypatch, keys, cancelled):
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    cfg = config.load()
    store = Store(cfg.db_path)
    launched = jobs.launch(cfg, store, "오래 걸리는 일", repo)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: store.get_job(launched.job.id).status == "running")
            await _until(pilot, lambda: app.selected_id == launched.job.id)
            await pilot.press("ctrl+s")
            assert isinstance(app.screen, ConfirmScreen)
            for key in keys:
                if key.startswith("click:"):
                    await pilot.click(key.removeprefix("click:"))
                else:
                    await pilot.press(key)
            await pilot.pause()
            assert not isinstance(app.screen, ConfirmScreen)
            assert (store.get_job(launched.job.id).status == "cancelled") is cancelled

    try:
        _run(scenario())
    finally:
        pid = store.get_job(launched.job.id).pid
        jobs.cancel(store, store.get_job(launched.job.id))
        end = time.time() + 10
        while time.time() < end and os.waitpid(pid, os.WNOHANG)[0] != pid:
            time.sleep(0.1)
        store.close()


def test_r로_지운_작업은_목록에서_바로_사라지고_남은_작업을_보여준다(env, repo, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_TOUCH", "created.txt")
    cfg = config.load()
    store = Store(cfg.db_path)
    for prompt in ("첫 작업", "둘째 작업"):
        jobs.launch(cfg, store, prompt, repo)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            table = app.query_one("#jobs", DataTable)
            await _until(pilot, lambda: all(j.is_terminal for j in store.list_jobs()))
            await _until(pilot, lambda: app.selected_id == 2)  # 최신 작업이 맨 위

            # 변경이 남은 worktree: 한 번 더 확인하고, 아니오면 그대로 둔다
            await pilot.press("ctrl+r")
            await pilot.press("y")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen) and "검토하지 않은 변경" in app.screen._question
            await pilot.press("n")
            await pilot.pause()
            assert table.row_count == 2 and (cfg.worktrees_dir / "myrepo-2").is_dir()

            await pilot.press("ctrl+r")
            await pilot.press("y")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            assert [j.id for j in store.list_jobs()] == [1]
            assert table.row_count == 1 and app.selected_id == 1
            assert not (cfg.worktrees_dir / "myrepo-2").exists()
            assert "첫 작업" in _timeline_text(app) and "둘째 작업" not in _timeline_text(app)

            jobs.remove(cfg, store, store.get_job(1), force=True)  # 다른 터미널에서 삭제
            await _until(pilot, lambda: table.row_count == 0)
            assert app.selected_id is None and "첫 작업" not in _timeline_text(app)
            await pilot.press("ctrl+r")
            await pilot.pause()
            assert not isinstance(app.screen, ConfirmScreen)

    try:
        _run(scenario())
    finally:
        store.close()


def test_타임라인과_diff를_드래그하면_클립보드에_복사된다(env, repo, monkeypatch):
    from coliday import tui
    copied = []
    monkeypatch.setattr(tui, "_pbcopy", copied.append)  # 실제 클립보드를 건드리지 않는다
    monkeypatch.setenv("FAKE_CLAUDE_TOUCH", "created.txt")
    cfg = config.load()
    store = Store(cfg.db_path)
    jobs.launch(cfg, store, "주문 취소 API 추가", repo)

    async def drag(pilot, selector, start, end):
        await pilot.mouse_down(selector, offset=start)
        await pilot.hover(selector, offset=end)
        await pilot.mouse_up(selector, offset=end)
        await pilot.pause(0.3)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: store.get_job(1).is_terminal)
            await _until(pilot, lambda: "최종 보고: 완료" in _timeline_text(app))
            await _until(pilot, lambda: "+new file" in _diff_text(app))
            await pilot.pause(0.3)

            await drag(pilot, "#timeline", (1, 0), (60, 10))
            assert copied and "주문 취소 API 추가" in copied[-1]
            assert "architect에게 분배: 주문 취소 설계" in copied[-1]
            assert any("복사했습니다" in n.message for n in app._notifications)

            await drag(pilot, "#diff-pane", (1, 1), (10, 7))
            assert "+new file" in copied[-1]

            count = len(copied)
            await pilot.click("#jobs")  # 드래그 없이 클릭만 하면 다시 복사하지 않는다
            await pilot.pause(0.3)
            assert len(copied) == count

    try:
        _run(scenario())
    finally:
        store.close()


def test_이어간_작업도_이전_내용을_모두_보여주고_키보드로_올려볼_수_있다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)
    job = store.create_job(prompt="첫 요청", repo=str(repo), workdir=str(repo), model=None)
    for i in range(80):
        store.add_event(job.id, "coder", "tool", f"Edit file{i}.py")
    store.finish_job(job.id, "succeeded", phase="완료", result="첫 번째 보고", session_id="s-1")
    jobs.continue_job(cfg, store, job.id, "테스트도 추가해줘")

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))  # 이어서 시킨 뒤에 새로 연 화면
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: store.get_job(job.id).is_terminal)
            await _until(pilot, lambda: "최종 보고: 완료" in _timeline_text(app))
            text = _timeline_text(app)
            assert text.index("첫 요청") < text.index("Edit file0.py") < text.index("첫 번째 보고")
            assert text.index("첫 번째 보고") < text.index("테스트도 추가해줘") < text.index("최종 보고: 완료")
            assert text.count("첫 번째 보고") == 1 and text.count("최종 보고: 완료") == 1

            timeline = app.query_one("#timeline")
            await _until(pilot, lambda: timeline.max_scroll_y > 0 and timeline.scroll_y == timeline.max_scroll_y)
            chat = app.query_one("#chat", Input)
            chat.focus()
            await pilot.press("pageup")  # 채팅 입력 중에도 이전 내용으로 올라간다
            await pilot.pause()
            up = timeline.scroll_y
            assert up < timeline.max_scroll_y and app.focused is chat
            await pilot.press("alt+up")
            await pilot.pause()
            assert timeline.scroll_y < up
            await pilot.press("pagedown", "pagedown", "pagedown")
            await pilot.pause()
            assert timeline.scroll_y == timeline.max_scroll_y

    try:
        _run(scenario())
    finally:
        store.close()


@pytest.mark.parametrize("where", ["chat", "dialog", "jobs"])
def test_ctrl_c로_어디서든_종료하고_실행중인_작업은_계속된다(env, repo, monkeypatch, where):
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    cfg = config.load()
    store = Store(cfg.db_path)
    launched = jobs.launch(cfg, store, "오래 걸리는 일", repo)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: store.get_job(launched.job.id).status == "running")
            if where == "chat":
                app.query_one("#chat", Input).focus()
            elif where == "dialog":
                await pilot.press("ctrl+n")
            else:
                app.query_one("#jobs", DataTable).focus()
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.pause(0.3)
            assert app._exit

    try:
        _run(scenario())
        job = store.get_job(launched.job.id)
        assert job.status == "running" and jobs.alive(job.pid)
    finally:
        pid = store.get_job(launched.job.id).pid
        jobs.cancel(store, store.get_job(launched.job.id))
        end = time.time() + 10
        while time.time() < end and os.waitpid(pid, os.WNOHANG)[0] != pid:
            time.sleep(0.1)
        store.close()


def _footer_has(app, action: str) -> bool:
    return any(b.action == action and enabled for _, b, enabled, _ in app.screen.active_bindings.values())


def test_결과물_폴더는_결과물이_있는_작업에서만_보이고_클릭하거나_단축키로_연다(env, repo, monkeypatch):
    fake_open = env / "open"
    fake_open.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> {env / 'opened.txt'}\n")
    fake_open.chmod(0o755)
    monkeypatch.setenv("COLIDAY_OPEN_BIN", str(fake_open))
    cfg = config.load()
    store = Store(cfg.db_path)
    plain = jobs.launch(cfg, store, "화면 없는 작업", repo).job
    monkeypatch.setenv("FAKE_CLAUDE_ARTIFACT", "login-page.png,demo.webm")
    shots = jobs.launch(cfg, store, "로그인 화면 수정", repo).job

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            bar = app.query_one("#artifacts", Static)
            await _until(pilot, lambda: all(j.is_terminal for j in store.list_jobs()))
            await _until(pilot, lambda: app.selected_id == shots.id)
            await _until(pilot, lambda: "결과물 2개" in str(bar.content))
            text = str(bar.content)
            assert bar.display and bar.has_class("-has")
            assert "결과물 2개 (사진 1, 영상 1)" in text and f"jobs/{shots.id}/artifacts" in text

            await pilot.click("#artifacts")
            await _until(pilot, lambda: (env / "opened.txt").exists())
            assert (env / "opened.txt").read_text().split() == [str(cfg.artifacts_dir(shots.id))]

            app.query_one("#chat", Input).focus()
            await pilot.press("ctrl+f")   # 채팅 입력 중에도 열린다
            await _until(pilot, lambda: len((env / "opened.txt").read_text().split()) == 2)

            assert _footer_has(app, "open_artifacts")

            # 결과물이 없는 작업에서는 줄도 하단 단축키 안내도 없고, 다른 작업의 폴더가 열리지도 않는다
            app.query_one("#jobs", DataTable).focus()
            await pilot.press("down")
            await _until(pilot, lambda: app.selected_id == plain.id)
            await _until(pilot, lambda: not bar.display)
            assert str(bar.content) == "" and not _footer_has(app, "open_artifacts")
            await pilot.press("ctrl+f")
            await pilot.pause(0.3)
            assert len((env / "opened.txt").read_text().split()) == 2

            # 사진·영상이 아닌 파일만 있는 작업도 결과물이 없는 것으로 본다
            (cfg.artifacts_dir(plain.id) / "notes.log").write_text("x")
            await pilot.pause(1.5)
            assert not bar.display

            # 결과물이 있는 작업으로 돌아오면 그 작업의 폴더가 다시 보인다
            await pilot.press("up")
            await _until(pilot, lambda: app.selected_id == shots.id)
            await _until(pilot, lambda: bar.display)
            assert f"jobs/{shots.id}/artifacts" in str(bar.content) and _footer_has(app, "open_artifacts")

    try:
        _run(scenario())
    finally:
        store.close()


def test_새_버전이_있으면_화면을_열_때_알린다(env, repo, monkeypatch):
    from coliday import updater

    cfg = config.load()
    store = Store(cfg.db_path)
    monkeypatch.setattr(updater, "notice", lambda stamp: "새 버전이 있습니다 (커밋 3개). 받으려면: coliday update")

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause(0.2)
            assert "새 버전 있음" in app.sub_title
            assert sum("커밋 3개" in n.message for n in app._notifications) == 1
            app._check_update()   # 다시 확인해도 같은 알림을 또 띄우지 않는다
            await pilot.pause(0.1)
            assert sum("커밋 3개" in n.message for n in app._notifications) == 1

    try:
        _run(scenario())
    finally:
        store.close()
