"""Textual headless(run_test) 로 TUI 동작을 검증한다."""

import asyncio
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import pytest

pytest.importorskip("textual")

from textual.widgets import DataTable, Input, Label, Markdown, Static, TextArea  # noqa: E402

from coliday import config, jobs, team  # noqa: E402
from coliday.store import RESULT, Store  # noqa: E402
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


def test_git_저장소가_아닌_디렉토리로_시작하면_직접_작업할지_묻는다(env, tmp_path):
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
            assert isinstance(app.screen, ConfirmScreen) and store.list_jobs() == []   # 묻기 전에는 만들지 않는다
            question = str(app.screen.query_one(Label).render())
            assert "git 저장소가 아닙니다" in question and "되돌릴 수도 없습니다" in question
            label, dialog = app.screen.query_one(Label), app.screen.query_one("#dialog")
            assert label.region.right <= dialog.content_region.right    # 긴 줄이 창 밖으로 잘리지 않고
            assert label.region.height > question.count("\n") + 1       # 줄바꿈된다
            await pilot.press("n")
            await pilot.pause()
            assert store.list_jobs() == [] and len(app.screen_stack) == 1

            chat = app.query_one("#chat", Input)                # 채팅 입력으로 시작해도 같다
            chat.focus()
            chat.value = "배포 스크립트 고쳐줘"
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen) and chat.value == "배포 스크립트 고쳐줘"
            await pilot.press("y")
            await _until(pilot, lambda: len(store.list_jobs()) == 1)
            job = store.list_jobs()[0]
            assert job.workdir == str(plain.resolve()) and job.branch is None   # worktree 없이 그 폴더에서 직접
            assert chat.value == ""

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
    store.finish_job(store.create_job(prompt="레포의 작업", repo=str(repo.resolve()), workdir=str(repo.resolve()),
                                      model=None).id, "succeeded")

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            table = app.query_one("#projects", DataTable)
            assert list(app._row_snapshot) == ["1"]
            # TUI 를 연 레포가 자동으로 연결되고 선택된다
            assert jobs.list_projects(cfg, store) == [str(other.resolve()), str(repo.resolve())]
            assert app.project_dir == str(repo.resolve())
            assert table.cursor_row == 1 and table.region.height <= 8
            assert app.focused is table  # 처음에는 작업 위치부터 고른다

            await pilot.press("up")
            await pilot.pause()
            assert app.project_dir == str(other.resolve())
            assert "other" in table.border_title
            assert list(app._row_snapshot) == [] and app.selected_id is None   # 레포의 작업은 보이지 않는다
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

            job = store.get_job(2)
            assert job.repo == str(other.resolve()) and list(app._row_snapshot) == ["2"]
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
            await pilot.pause()
            assert app.focused is app.query_one("#projects", DataTable)  # 프로젝트가 없어도 프로젝트 목록에서 시작
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

            # 작업 목록에서 고른 채로 Delete(맥의 delete 는 backspace): 바로 지우지 않고 물어본다
            for key in ("delete", "backspace"):
                table.focus()
                await pilot.press(key)
                await pilot.pause()
                assert isinstance(app.screen, ConfirmScreen) and "삭제할까요" in app.screen._question
                await pilot.press("n")
                await pilot.pause()
                assert table.row_count == 2 and len(app.screen_stack) == 1
            chat = app.query_one("#chat", Input)           # 입력 칸에서는 글자를 지우는 키다
            chat.focus()
            chat.value = "ab"
            await pilot.press("backspace")
            await pilot.pause()
            assert chat.value == "a" and len(app.screen_stack) == 1
            chat.value = ""
            app.query_one("#projects").focus()             # 프로젝트 목록에서는 아무 일도 없다
            await pilot.press("delete")
            await pilot.pause()
            assert len(app.screen_stack) == 1 and table.row_count == 2
            table.focus()

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
            # 타임라인 비우기(remove_children)는 다음 이벤트 처리에서 끝난다: 그것까지 기다린다.
            await _until(pilot, lambda: table.row_count == 0 and "첫 작업" not in _timeline_text(app))
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
            timeline = app.query_one("#timeline")
            # 마지막 줄을 쓸 때 미뤄 둔 '맨 아래로' 스크롤이 scroll_home 뒤에 올 수 있다: 맨 위에 머물 때까지 다시 올린다.
            for _ in range(10):
                timeline.scroll_home(animate=False)   # 요청 줄이 보이게 (긴 경로는 여러 줄을 차지한다)
                await pilot.pause(0.3)
                if timeline.scroll_y == 0:
                    break
            assert timeline.scroll_y == 0

            await drag(pilot, "#timeline", (1, 0), (60, 14))   # 내 질문 머리 줄(나 · 시각) 만큼 더
            await _until(pilot, lambda: bool(copied))  # 복사는 드래그가 끝난 뒤에 처리된다 (느린 컴퓨터에서 0.3초를 넘긴다)
            assert "주문 취소 API 추가" in copied[-1]
            assert "architect에게 분배: 주문 취소 설계" in copied[-1]
            assert any("복사했습니다" in n.message for n in app._notifications)

            await drag(pilot, "#diff-pane", (1, 1), (10, 7))
            await _until(pilot, lambda: "+new file" in copied[-1])

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

            # 사진·영상이 아닌 파일(사용자가 확인할 문서 등)만 있어도 폴더 줄이 보이고 열린다
            (cfg.artifacts_dir(plain.id) / "report.md").write_text("x")
            await _until(pilot, lambda: bar.display)
            assert "결과물 파일 1개" in str(bar.content) and f"jobs/{plain.id}/artifacts" in str(bar.content)
            await pilot.click("#artifacts")
            await _until(pilot, lambda: len((env / "opened.txt").read_text().split()) == 3)
            assert (env / "opened.txt").read_text().split()[-1] == str(cfg.artifacts_dir(plain.id))

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


def test_프로젝트_추가_창은_SSH를_골랐을_때만_서버_칸을_보여_준다(env, repo, monkeypatch):
    from textual.widgets import Select
    cfg = config.load()
    store = Store(cfg.db_path)
    calls = []
    real = jobs.add_project

    def add_project(cfg, store, directory, name=None, password=None):
        calls.append((str(directory), name, password))
        return str(directory) if "@" in str(directory) else real(cfg, store, directory, name, password)

    monkeypatch.setattr(jobs, "add_project", add_project)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(120, 40)) as pilot:
            calls.clear()
            await pilot.press("ctrl+g")
            screen = app.screen
            shown = lambda: {i.id for i in screen.query(Input) if all(n.display for n in i.ancestors_with_self)}
            assert shown() == {"dir", "name"} and not screen.query_one("#ssh-hint").display   # 처음에는 이 컴퓨터의 폴더

            screen.query_one("#kind", Select).value = "ssh"
            await pilot.pause()
            assert shown() == {"host", "port", "remote-dir", "password", "name"} and screen.query_one("#ssh-hint").display
            assert screen.query_one("#password", Input).password
            hint = str(screen.query_one("#ssh-hint").render())
            assert "저장하지 않는다" in hint and "이 컴퓨터에서 실행" in hint   # 에이전트가 어디서 도는지 알려 준다
            dialog = screen.query_one("#dialog")
            assert dialog.region.bottom <= 40 and dialog.max_scroll_y == 0   # 칸이 늘어도 창이 화면에 다 들어온다

            screen.query_one("#remote-dir", Input).value = "/srv/app"
            await pilot.press("enter")                        # 서버 주소가 비었다
            await pilot.pause()
            assert app.screen is screen and calls == []
            screen.query_one("#host", Input).value = "dev@server"
            screen.query_one("#port", Input).value = "70000"
            screen.query_one("#host", Input).focus()
            await pilot.press("enter")
            await pilot.pause()
            assert app.screen is screen and any("포트는 1~65535" in n.message for n in app._notifications)

            screen.query_one("#port", Input).value = "2222"
            screen.query_one("#password", Input).value = " s3cret "   # 비밀번호는 앞뒤 공백도 그대로
            await pilot.press("enter")
            await pilot.pause()
            assert calls == [("dev@server:2222:/srv/app", None, " s3cret ")]

            await pilot.press("ctrl+g")                       # 포트와 비밀번호를 비우면 붙이지 않는다
            app.screen.query_one("#kind", Select).value = "ssh"
            await pilot.pause()
            app.screen.query_one("#host", Input).value = "dev@server"
            app.screen.query_one("#remote-dir", Input).value = "~/app"
            app.screen.query_one("#host", Input).focus()
            await pilot.press("enter")
            await pilot.pause()
            assert calls[-1] == ("dev@server:~/app", None, None)

            await pilot.press("ctrl+g")                       # 이 컴퓨터의 폴더
            app.screen.query_one("#dir", Input).value = str(repo)
            await pilot.press("enter")
            await pilot.pause()
            assert calls[-1] == (str(repo), None, None)

            await pilot.press("ctrl+o")
            await pilot.press("ctrl+e")                       # 이름 바꾸기에는 위치 선택도 서버 칸도 없다
            assert isinstance(app.screen, AddProjectScreen)
            assert not app.screen.query("#kind") and not app.screen.query("#password")
            assert app.screen.query_one("#dir", Input).disabled
            await pilot.press("enter")
            await pilot.pause()
            assert calls[-1][2] is None

    try:
        asyncio.run(scenario())
    finally:
        store.close()


def test_env_읽기_허용은_새_작업_창에서_사람이_켠_작업에만_들어간다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            chat = app.query_one("#chat", Input)                # 채팅 입력으로 시작한 작업은 허용하지 않는다
            chat.focus()
            chat.value = "첫 작업"
            await pilot.press("enter")
            await _until(pilot, lambda: len(store.list_jobs()) == 1)
            assert not store.allow_env(1)

            await pilot.press("ctrl+n")
            box = app.screen.query_one("#allow-env")
            assert box.value is False                           # 기본은 꺼져 있다
            app.screen.query_one("#prompt", Input).value = "둘째 작업"
            box.value = True
            await pilot.press("enter")
            await _until(pilot, lambda: len(store.list_jobs()) == 2)
            assert store.allow_env(2) and not store.allow_env(1)

    try:
        _run(scenario())
    finally:
        store.close()


def test_작업_도중에_env_읽기를_버튼으로_그_작업에만_허용한다(env, repo, monkeypatch):
    from coliday import guard
    from coliday.tui import EnvBar
    cfg = config.load()
    store = Store(cfg.db_path)
    first = jobs.launch(cfg, store, "첫 작업", repo).job
    other = jobs.launch(cfg, store, "다른 작업", repo).job

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: all(store.get_job(j.id).is_terminal for j in (first, other)))
            app.select_job(first.id)
            await pilot.pause()
            bar = app.query_one("#env-access", EnvBar)
            assert "차단 중" in str(bar.render()) and not bar.has_class("-needed")

            # 에이전트가 .env 가 필요하다고 멈췄다: 줄이 눈에 띄게 바뀐다
            store.update_job(first.id, result=f"{guard.NEED_MARKER}\n.env 의 DB_URL 이 필요합니다.")
            await _until(pilot, lambda: bar.has_class("-needed"))
            assert "읽어야 합니다" in str(bar.render())

            await pilot.click("#env-access")                      # 버튼(줄)을 누르면 먼저 묻는다
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen) and not store.allow_env(first.id)
            await pilot.press("n")
            await pilot.pause()
            assert not store.allow_env(first.id)

            await pilot.press("ctrl+l")                           # 단축키도 같다
            await pilot.pause()
            await pilot.press("y")
            await _until(pilot, lambda: store.allow_env(first.id))
            assert not store.allow_env(other.id)                  # 그 작업에만 해당한다
            # 허용을 기다리던 작업은 같은 세션으로 바로 이어서 실행한다
            await _until(pilot, lambda: any(e.message == guard.ALLOWED_NOTE for e in store.events(first.id)))
            await _until(pilot, lambda: "허용됨" in str(bar.render()))

            await _until(pilot, lambda: store.get_job(first.id).is_terminal)
            await pilot.press("ctrl+l")                           # 다시 막는 쪽은 묻지 않는다
            await _until(pilot, lambda: not store.allow_env(first.id))
            assert len(app.screen_stack) == 1

    try:
        _run(scenario())
    finally:
        store.close()


def test_위치_줄과_보고에_적힌_경로를_누르면_그_폴더가_열린다(env, repo, monkeypatch):
    fake_open = env / "opener"
    fake_open.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> {env / 'opened.txt'}\n")
    fake_open.chmod(0o755)
    monkeypatch.setenv("COLIDAY_OPEN_BIN", str(fake_open))
    cfg = config.load()
    store = Store(cfg.db_path)
    job = jobs.launch(cfg, store, "보고서 작성", repo, no_worktree=True).job
    made = Path(job.workdir) / "docs"
    opened = lambda: (env / "opened.txt").read_text().split() if (env / "opened.txt").exists() else []

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: store.get_job(job.id).is_terminal)
            made.mkdir()
            (made / "report.md").write_text("x")
            store.add_event(job.id, "팀장", RESULT, "보고서를 `docs/report.md` 에 만들었습니다. 없는 경로: `docs/none.md`")
            await _until(pilot, lambda: any("docs/report.md" in w.source for w in app.query(Markdown)))
            report = next(w for w in app.query(Markdown) if "docs/report.md" in w.source)
            href = quote(str(made / "report.md"), safe="/")   # 한글이 든 경로도 링크가 깨지지 않게 인코딩한다
            assert f"[`docs/report.md`]({href})" in report.source and "[`docs/none.md`]" not in report.source

            report.post_message(Markdown.LinkClicked(report, href))   # 링크를 누른 것과 같다
            await _until(pilot, lambda: len(opened()) == 1)
            assert opened() == [str(made)]   # 파일을 열지 않고 그 파일이 든 폴더를 연다

            app.query_one("#timeline").scroll_home(animate=False)   # 위치 줄은 타임라인 맨 위에 있다
            await pilot.pause(0.2)
            await pilot.click(".folder-line")
            await _until(pilot, lambda: len(opened()) == 2)
            assert opened()[-1] == job.workdir

            report.post_message(Markdown.LinkClicked(report, str(made / "gone.md")))   # 그사이 지워진 경로
            await pilot.pause(0.3)
            assert len(opened()) == 2

    try:
        _run(scenario())
    finally:
        store.close()


def test_작업_목록에서_Space로_여러_개를_골라_한꺼번에_삭제한다(env, repo, monkeypatch):
    cfg = config.load()
    store = Store(cfg.db_path)
    for prompt in ("첫 작업", "둘째 작업", "셋째 작업"):
        jobs.launch(cfg, store, prompt, repo, no_worktree=True)
    monkeypatch.setenv("FAKE_CLAUDE_TOUCH", "created.txt")   # 넷째는 worktree 에 검토하지 않은 변경을 남긴다
    jobs.launch(cfg, store, "넷째 작업", repo)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            table = app.query_one("#jobs", DataTable)
            chat = app.query_one("#chat", Input)
            await _until(pilot, lambda: all(j.is_terminal for j in store.list_jobs()))
            await _until(pilot, lambda: app.selected_id == 4)
            table.focus()
            assert "Space" in str(table.border_subtitle)

            await pilot.press("space")            # #4 선택, 커서는 한 줄 내려간다
            await pilot.press("space")            # #3 선택
            await pilot.pause()
            assert app._marked == {4, 3} and app.selected_id == 2
            assert chat.value == "" and app.focused is table       # Space 는 채팅 입력으로 넘어가지 않는다
            assert "2개 선택" in str(table.border_subtitle)
            assert str(table.get_cell("4", "st")).startswith("✔") and not str(table.get_cell("2", "st")).startswith("✔")

            await pilot.press("up")
            await pilot.press("space")            # #3 을 다시 누르면 선택이 풀린다
            await pilot.pause()
            assert app._marked == {4}
            await pilot.press("escape")           # Esc 는 선택을 모두 푼다
            await pilot.pause()
            assert app._marked == set() and not str(table.get_cell("4", "st")).startswith("✔")

            for _ in range(3):                    # 커서를 맨 위로 올려 #4, #3, #2 를 고른다
                await pilot.press("up")
            for _ in range(3):
                await pilot.press("space")
            await pilot.pause()
            assert app._marked == {4, 3, 2}

            await pilot.press("delete")           # 바로 지우지 않고 무엇을 지우는지 물어본다
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            assert "선택한 작업 3개(#4, #3, #2)" in app.screen._question
            await pilot.press("n")
            await pilot.pause()
            assert table.row_count == 4 and app._marked == {4, 3, 2}

            await pilot.press("delete")
            await pilot.press("y")
            await pilot.pause()
            # 지울 수 있는 것은 지우고, 변경이 남은 worktree 는 한 번 더 묻는다
            assert [j.id for j in store.list_jobs()] == [4, 1]
            assert isinstance(app.screen, ConfirmScreen) and "작업 #4 의 worktree 에 검토하지 않은 변경" in app.screen._question
            await pilot.press("n")
            await pilot.pause()
            assert [j.id for j in store.list_jobs()] == [4, 1] and app._marked == {4}   # 남은 것은 선택된 채로 둔다

            await pilot.press("ctrl+r")           # Ctrl+R 도 선택한 것을 지운다
            await pilot.press("y")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            assert [j.id for j in store.list_jobs()] == [1] and app._marked == set()
            assert table.row_count == 1 and app.selected_id == 1
            assert "Space" in str(table.border_subtitle)

    try:
        _run(scenario())
    finally:
        store.close()


def test_Ctrl_Y는_보이지_않는_부분까지_타임라인_전체를_복사한다(env, repo, monkeypatch):
    copied = []   # 실제 클립보드를 건드리지 않는다
    monkeypatch.setattr(ColidayApp, "copy_to_clipboard", lambda self, text: copied.append(text))
    cfg = config.load()
    store = Store(cfg.db_path)
    jobs.launch(cfg, store, "첫 작업", repo, no_worktree=True)
    jobs.launch(cfg, store, "주문 취소 API 추가", repo)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            table = app.query_one("#jobs", DataTable)
            await _until(pilot, lambda: all(j.is_terminal for j in store.list_jobs()))
            await _until(pilot, lambda: app.selected_id == 2)
            for n in range(300):   # 화면을 훨씬 넘는 긴 타임라인
                store.add_event(2, "architect", "tool", f"Bash step-{n}")
            app.query_one("#chat", Input).focus()   # 채팅 입력 중에도 된다

            await pilot.press("ctrl+y")
            await pilot.pause()
            text = copied[-1]
            lines = text.splitlines()
            assert lines[0].startswith("▌나 · ") and lines[1] == "▌주문 취소 API 추가"     # 내 질문은 ▌ 막대
            assert lines[2].startswith("위치: ") and "브랜치" in lines[2]
            assert "architect에게 분배: 주문 취소 설계" in text and "최종 보고: 완료" in text and "◆ AI 보고 (" in text
            assert re.search(r"^◆ AI 보고 \(\d\d:\d\d:\d\d\)\n    ", text, re.M)              # AI 보고는 들여쓴다
            assert "Bash step-0" in text and "Bash step-299" in text
            assert re.search(r"^\d\d:\d\d:\d\d architect  Bash step-7$", text, re.M)   # 화면의 줄 모양 그대로
            assert "succeeded" in text and "첫 작업" not in text
            assert any("타임라인을 복사했습니다" in n.message for n in app._notifications)

            table.focus()                     # Space 로 고른 작업이 있으면 그것들을 모두 복사한다
            await pilot.press("space")
            await pilot.press("space")
            await pilot.press("ctrl+y")
            await pilot.pause()
            both = copied[-1]
            assert both.startswith("▌나 · ") and both.splitlines()[1] == "▌첫 작업"
            assert "\n\n▌나 · " in both and both.index("▌첫 작업") < both.index("▌주문 취소 API 추가")

    try:
        _run(scenario())
    finally:
        store.close()


# ── 묶음: 평평한 목록 + 오른쪽 위 묶음 패널 (고정·그룹·파생) ─────────────────────────
from textual.widgets import Checkbox, OptionList  # noqa: E402

from coliday.tui import BundlePanel, ListMenuScreen, NameScreen  # noqa: E402


def _keys(app) -> list[str]:
    return list(app._row_snapshot)


def _cursor_key(app) -> str:
    return _keys(app)[app.query_one("#jobs", DataTable).cursor_row]


def _cell(app, key: str, col: str = "prompt") -> str:
    return str(app.query_one("#jobs", DataTable).get_cell(key, col))


def _done(store, repo, prompt, status="succeeded", parent_id=None):
    job = store.create_job(prompt=prompt, repo=str(repo), workdir=str(repo), model=None, parent_id=parent_id)
    store.finish_job(job.id, status, phase=status)
    return job


def _notified(app, text: str) -> bool:
    return any(text in str(n.message) for n in app._notifications)


def _panel(app) -> BundlePanel:
    return app.query_one("#bundle", BundlePanel)


def _bundle_ids(app) -> list:
    return [o.id for o in _panel(app)._options]


def _bundle_texts(app) -> list[str]:
    return [str(o.prompt) for o in _panel(app)._options]


async def _expand(pilot, app, bundle_id: int) -> None:
    """묶음을 → 로 펼친 상태로 만든다 (기본은 `1. 이름` 한 줄뿐이다)."""
    app._bundle_open[bundle_id] = True
    app._show_panel(f"b:{bundle_id}")
    await pilot.pause()


def _gd_part(app) -> tuple[list, list[str]]:
    """묶음 패널 아래 그룹·파생 구역(gd 머리 다음부터 끝까지)의 (id, 글)."""
    ids, texts = _bundle_ids(app), _bundle_texts(app)
    if "gd" not in ids:
        return [], []
    start, end = ids.index("gd") + 1, len(ids)  # 그룹·파생 구역이 패널의 마지막이다
    return ids[start:end], texts[start:end]


def _gd_ids(app) -> list:
    return _gd_part(app)[0]


def _gd_texts(app) -> list[str]:
    return _gd_part(app)[1]


def _app(cfg, store, default_dir, gd_open=True):
    """화면. gd_open: 그룹·파생 구역을 → 로 펼친 상태로 시작한다 (펼치기·접기 자체는 따로 검증한다)."""
    app = ColidayApp(cfg, store, default_dir=str(default_dir))
    app._gd_open = gd_open
    return app


def _highlight(app, option_id: str) -> None:
    panel = _panel(app)
    panel.highlighted = panel.get_option_index(option_id)


async def _menu(pilot, app, option_id: str) -> None:
    """ctrl+b 묶음 메뉴에서 항목 하나를 고른다. 메뉴를 열면 포커스는 묶음 패널로 옮겨져 있다."""
    await pilot.press("ctrl+b")
    await pilot.pause()
    assert isinstance(app.screen, ListMenuScreen)
    assert app.screen_stack[0].focused is _panel(app)
    options = app.screen.query_one(OptionList)
    options.highlighted = options.get_option_index(option_id)
    await pilot.press("enter")
    await pilot.pause()


async def _to_key(pilot, app, key: str) -> None:
    """커서를 그 줄로 옮긴다 (↑↓ 로)."""
    table = app.query_one("#jobs", DataTable)
    table.focus()
    await pilot.pause()
    target = _keys(app).index(key)
    while table.cursor_row != target:
        await pilot.press("down" if table.cursor_row < target else "up")
    await pilot.pause()


def test_고정한_작업은_맨_위에_고정_표시로_보이고_풀_수_있다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)
    _done(store, repo, "첫 작업")
    _done(store, repo, "둘째 작업", "failed")
    _done(store, repo, "셋째 작업")

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.query_one("#jobs", DataTable).row_count == 3)
            assert _keys(app) == ["3", "2", "1"]
            await _to_key(pilot, app, "1")
            await _menu(pilot, app, "pin")
            assert store.job_meta()[1].pinned and len(app.screen_stack) == 1
            assert _keys(app) == ["1", "3", "2"] and _cursor_key(app) == "1"     # 머리줄 없이 맨 위로
            assert not any(key.startswith("h:") for key in _keys(app))
            assert _cell(app, "1").startswith("📌 ") and "📌" not in _cell(app, "3")
            assert "📌 고정" in str(_panel(app).border_title)
            assert app.focused is _panel(app)                                   # 메뉴를 닫으면 묶음 패널로
            await pilot.press("ctrl+b")
            await pilot.pause()
            ids = [o.id for o in app.screen.query_one(OptionList)._options]
            assert "unpin" in ids and "pin" not in ids
            await pilot.press("escape")
            await pilot.pause()
            assert app.focused is _panel(app)
            await _menu(pilot, app, "unpin")
            assert not store.job_meta()[1].pinned and _keys(app) == ["3", "2", "1"]
            assert "📌" not in str(_panel(app).border_title)

    try:
        _run(scenario())
    finally:
        store.close()


def test_그룹을_만들고_넣고_빼고_이름을_바꾸고_지워도_작업은_남는다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)
    _done(store, repo, "첫 작업")
    _done(store, repo, "둘째 작업")
    _done(store, repo, "셋째 작업")

    async def name(pilot, app, text):
        assert isinstance(app.screen, NameScreen)
        app.screen.query_one("#name", Input).value = text
        await pilot.press("enter")
        await pilot.pause()

    async def scenario():
        app = _app(cfg, store, repo)
        async with app.run_test(size=(160, 45)) as pilot:
            table = app.query_one("#jobs", DataTable)
            await _until(pilot, lambda: table.row_count == 3)
            table.focus()
            await pilot.press("space", "space")       # #3, #2 를 고른다
            await pilot.pause()
            assert app._marked == {3, 2}
            await _menu(pilot, app, "group_new")
            await name(pilot, app, "결제")
            group = store.list_groups()[0]
            assert group.name == "결제" and {i for i, m in store.job_meta().items() if m.group_id == group.id} == {2, 3}
            assert _keys(app) == ["3", "2", "1"]      # 평평한 목록 그대로 (머리줄 없음)
            assert _cell(app, "3").startswith("▣결제 ") and _cell(app, "2").startswith("▣결제 ") and "▣" not in _cell(app, "1")

            await _to_key(pilot, app, "3")
            assert _gd_texts(app)[0] == "그룹: 결제 (2)" and _gd_ids(app) == [None, "m:3", "m:2"]
            assert _gd_texts(app)[1].startswith("▶ ")

            await pilot.press("escape")               # 선택을 풀고 선택한 작업 하나로
            await _menu(pilot, app, "group_new")
            await name(pilot, app, "결제")            # 이름이 겹치면 알리고 만들지 않는다
            assert len(store.list_groups()) == 1 and _notified(app, "이미 있는 그룹 이름")

            await _to_key(pilot, app, "2")
            await _menu(pilot, app, "ungroup")
            assert store.job_meta()[2].group_id is None and "그룹:" not in " ".join(_bundle_texts(app)) and "gd" not in _bundle_ids(app)
            await _to_key(pilot, app, "3")
            assert _gd_texts(app)[0] == "그룹: 결제 (1)"

            await _menu(pilot, app, "rename_group")   # 그룹에 든 작업을 고른 채 메뉴로
            assert app.screen.query_one("#name", Input).value == "결제"
            await name(pilot, app, "결제 v2")
            assert store.list_groups()[0].name == "결제 v2" and _cell(app, "3").startswith("▣결제 v2 ")
            assert _gd_texts(app)[0] == "그룹: 결제 v2 (1)"
            spare = store.create_group("기타", project=str(repo.resolve()))
            await _menu(pilot, app, "rename_group")
            await name(pilot, app, "기타")
            assert _notified(app, "이미 있는 그룹 이름") and store.list_groups()[0].name == "결제 v2"

            store.set_group([2], group.id)            # 패널에서 ctrl+x 로 구성 작업 하나를 뺀다
            await _until(pilot, lambda: "m:2" in _bundle_ids(app))
            _panel(app).focus()
            await pilot.pause()
            _highlight(app, "m:2")
            await pilot.press("ctrl+x")
            await pilot.pause()
            assert store.job_meta()[2].group_id is None and "m:2" not in _bundle_ids(app)
            assert store.job_meta()[3].group_id == group.id and len(app.screen_stack) == 1

            await _menu(pilot, app, f"delete_empty:{spare.id}")   # 빈 그룹은 메뉴에서 지운다
            assert [g.name for g in store.list_groups()] == ["결제 v2"]

            await _menu(pilot, app, "delete_group")
            assert store.list_groups() == []
            assert [j.id for j in store.list_jobs()] == [3, 2, 1]          # 작업은 남는다
            assert "▣" not in _cell(app, "3") and app.selected_id == 3

            await pilot.press("ctrl+b")               # 그룹이 없는 작업에서는 그룹 이름 바꾸기·삭제가 없다
            await pilot.pause()
            ids = [o.id for o in app.screen.query_one(OptionList)._options]
            assert "rename_group" not in ids and "delete_group" not in ids
            await pilot.press("escape")

    try:
        _run(scenario())
    finally:
        store.close()


def test_새_작업_창에서_파생하면_목록에_부모_표시가_보이고_묶음_패널에_부모와_자식이_나온다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)
    parent = jobs.launch(cfg, store, "부모 작업", repo).job

    async def scenario():
        app = _app(cfg, store, repo)
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: store.get_job(parent.id).is_terminal)
            await _until(pilot, lambda: app.selected_id == parent.id)
            project = app.project_dir

            await _menu(pilot, app, "derive")         # 묶음 메뉴에서 고르면 파생이 켜진 채로 열린다
            assert isinstance(app.screen, NewJobScreen) and app.screen.query_one("#derive", Checkbox).value
            await pilot.press("escape")
            await pilot.pause()

            await pilot.press("ctrl+n")
            screen = app.screen
            box = screen.query_one("#derive", Checkbox)
            assert f"#{parent.id}" in str(box.label) and box.value is False      # 기본은 꺼짐
            assert not screen.query_one("#dir", Input).disabled and screen.query_one("#no-worktree").display
            box.value = True
            await pilot.pause()
            assert screen.query_one("#dir", Input).disabled and not screen.query_one("#no-worktree").display
            screen.query_one("#prompt", Input).value = "이어서 테스트 추가"
            screen.query_one("#prompt", Input).focus()
            await pilot.press("enter")
            # 파생 작업은 백그라운드에서 만든다: 다 만들어지면 새 작업이 선택된다
            await _until(pilot, lambda: len(store.list_jobs()) == 2 and app.selected_id == store.list_jobs()[0].id)
            child = store.list_jobs()[0]
            assert store.job_meta()[child.id].parent_id == parent.id and child.branch == f"coliday/{child.id}"
            assert _keys(app) == [str(child.id), str(parent.id)]                 # 최근 순 (트리 없음)
            assert _cell(app, str(child.id)).startswith(f"↳#{parent.id} ") and "↳" not in _cell(app, str(parent.id))
            assert app.selected_id == child.id and app.project_dir == project   # 프로젝트 선택은 바뀌지 않는다
            assert f"p:{parent.id}" in _bundle_ids(app) and "파생" in _gd_texts(app)
            assert any(t.endswith("(부모)") and f"#{parent.id}" in t for t in _bundle_texts(app))
            await _until(pilot, lambda: store.get_job(child.id).is_terminal)

            await _to_key(pilot, app, str(parent.id))
            assert app.selected_id == parent.id and f"c:{child.id}" in _bundle_ids(app)

            await pilot.press("ctrl+r")
            await pilot.press("y")
            await pilot.pause()
            await _until(pilot, lambda: [j.id for j in store.list_jobs()] == [child.id])
            assert store.job_meta().get(child.id) is None or store.job_meta()[child.id].parent_id is None
            await _until(pilot, lambda: _keys(app) == [str(child.id)])
            assert _cell(app, str(child.id)) == "이어서 테스트 추가" and app.selected_id == child.id
            # 지운 부모는 고를 수 없는 줄로 남는다 (만든 출처)
            await _until(pilot, lambda: f"↑ #{parent.id} (지워짐)" in " ".join(_bundle_texts(app)))
            assert f"p:{parent.id}" not in _bundle_ids(app)

    try:
        _run(scenario())
    finally:
        store.close()


def test_옛_화면_설정_파일이_있어도_평평한_목록으로_열리고_파일은_그대로다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)
    _done(store, repo, "첫 작업")
    ui = cfg.home / "ui.json"
    ui.parent.mkdir(parents=True, exist_ok=True)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.query_one("#jobs", DataTable).row_count == 1)
            assert _keys(app) == ["1"] and app.selected_id == 1
            assert str(app.query_one("#jobs", DataTable).border_title) == "작업 목록 · myrepo"

    try:
        for content in ('{"group_by": "status", "collapsed": ["status:succeeded", "job:1"]}', "{깨진 json"):
            ui.write_text(content)
            _run(scenario())
            assert ui.read_text() == content          # 읽지도 쓰지도 않는다
    finally:
        store.close()


# ── 리뷰 회귀: 메뉴 대상 고정 / 파생은 백그라운드 ─────────────────────

import threading  # noqa: E402

from coliday.jobs import JobError  # noqa: E402


async def _choose(pilot, app, option_id: str) -> None:
    """열려 있는 묶음 메뉴에서 항목 하나를 고른다."""
    assert isinstance(app.screen, ListMenuScreen)
    options = app.screen.query_one(OptionList)
    options.highlighted = options.get_option_index(option_id)
    await pilot.press("enter")
    await pilot.pause()


async def _open_menu_on(pilot, app, key: str) -> None:
    await _to_key(pilot, app, key)
    await pilot.press("ctrl+b")
    await pilot.pause()
    assert isinstance(app.screen, ListMenuScreen)


def test_묶음_메뉴는_연_때의_작업에만_적용하고_그사이_지워졌으면_바꾸지_않는다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)
    for prompt in ("첫", "둘째", "셋째", "넷째", "다섯째"):
        _done(store, repo, prompt)
    group = store.create_group("배포", project=str(repo.resolve()))
    other = store.create_group("기타", project=str(repo.resolve()))

    async def deleted_elsewhere(pilot, app, job_id: int) -> None:
        """다른 화면에서 지운 것처럼 store 에서 지우고 1초 새로고침을 기다린다."""
        assert store.delete_job(job_id)
        await _until(pilot, lambda: str(job_id) not in _keys(app))

    async def scenario():
        app = _app(cfg, store, repo)
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: "1" in _keys(app))

            # 고정: #1 에서 메뉴를 열고 #1 을 지우면 새로고침이 #2 를 고른다. 그래도 #2 를 고정하지 않는다.
            await _open_menu_on(pilot, app, "1")
            await deleted_elsewhere(pilot, app, 1)
            assert app.selected_id == 2                       # 재현 조건: 커서는 다른 작업으로 옮겨졌다
            await _choose(pilot, app, "pin")
            assert not any(m.pinned for m in store.job_meta().values())
            assert len(app.screen_stack) == 1 and _notified(app, "#1 이(가) 그사이 지워져")

            # 그룹에 넣기
            await _open_menu_on(pilot, app, "2")
            await deleted_elsewhere(pilot, app, 2)
            await _choose(pilot, app, f"group:{group.id}")
            assert not any(m.group_id is not None for m in store.job_meta().values())

            # 새 그룹: 이름 창이 열려 있는 동안 지워졌으면 그룹도 만들지 않는다
            await _open_menu_on(pilot, app, "3")
            await _choose(pilot, app, "group_new")
            assert isinstance(app.screen, NameScreen)
            await deleted_elsewhere(pilot, app, 3)
            app.screen.query_one("#name", Input).value = "새 그룹"
            await pilot.press("enter")
            await pilot.pause()
            assert [g.name for g in store.list_groups()] == ["배포", "기타"]
            assert not any(m.group_id is not None for m in store.job_meta().values())

            # 파생: 지워진 작업에서 파생하는 창을 열지 않는다 (지금 커서의 #5 로 바꾸지도 않는다)
            await _open_menu_on(pilot, app, "4")
            await deleted_elsewhere(pilot, app, 4)
            await _choose(pilot, app, "derive")
            assert len(app.screen_stack) == 1 and _notified(app, "#4 이(가) 그사이 지워져")

            # 그룹 삭제·이름 바꾸기·빈 그룹 삭제: 메뉴를 연 그룹이 지워졌으면 다른 그룹을 건드리지 않는다
            for option in ("delete_group", "rename_group", "delete_empty"):
                target = store.create_group(f"임시 {option}", project=str(repo.resolve()))
                if option != "delete_empty":
                    store.set_group([5], target.id)
                    await _until(pilot, lambda: "m:5" in _bundle_ids(app))
                await _until(pilot, lambda: target.id in {g.id for g in app._groups})
                await _open_menu_on(pilot, app, "5")
                store.delete_group(target.id)
                await _until(pilot, lambda: "m:5" not in _bundle_ids(app) and target.id not in {g.id for g in app._groups})
                await _choose(pilot, app, f"delete_empty:{target.id}" if option == "delete_empty" else option)
                assert len(app.screen_stack) == 1 and _notified(app, f"그룹 '임시 {option}' 이(가) 그사이 지워져")
                assert [g.name for g in store.list_groups()] == ["배포", "기타"]
            assert [j.id for j in store.list_jobs()] == [5] and other.id in {g.id for g in store.list_groups()}

            # 빈 그룹 삭제: 그사이 작업이 들어갔으면 지우지 않는다
            await _open_menu_on(pilot, app, "5")
            store.set_group([5], other.id)
            await _choose(pilot, app, f"delete_empty:{other.id}")
            assert other.id in {g.id for g in store.list_groups()} and _notified(app, "그사이 작업이 들어가")
            store.set_group([5], None)

            # 대상이 남아 있으면 커서가 그사이 옮겨져도 메뉴를 연 작업에 적용한다
            _done(store, repo, "여섯째")
            await _until(pilot, lambda: "6" in _keys(app))
            await _open_menu_on(pilot, app, "5")
            app._move_to_row(_keys(app).index("6"))
            await _choose(pilot, app, "pin")
            assert {i for i, m in store.job_meta().items() if m.pinned} == {5}

    try:
        _run(scenario())
    finally:
        store.close()


def _slow_derive(monkeypatch, release: threading.Event, fail: Exception | None = None) -> list:
    """jobs.derive 를 release 될 때까지 기다리게 한다 (서버 스냅샷이 오래 걸리는 상황). 부른 기록을 돌려준다."""
    calls: list = []
    real = jobs.derive

    def slow(cfg, store, parent_id, prompt, **kwargs):
        calls.append((threading.current_thread() is threading.main_thread(), store))
        release.wait(30)
        if fail is not None:
            raise fail
        return real(cfg, store, parent_id, prompt, **kwargs)

    monkeypatch.setattr(jobs, "derive", slow)
    return calls


async def _submit_derive(pilot, app, prompt: str) -> None:
    await pilot.press("ctrl+n")
    screen = app.screen
    assert isinstance(screen, NewJobScreen)
    screen.query_one("#derive", Checkbox).value = True
    screen.query_one("#prompt", Input).value = prompt
    screen.query_one("#prompt", Input).focus()
    await pilot.press("enter")
    await pilot.pause()


def test_파생_작업은_백그라운드에서_만들고_그동안_화면이_반응하며_끝나면_새_작업이_선택된다(env, repo, monkeypatch):
    cfg = config.load()
    store = Store(cfg.db_path)
    parent = _done(store, repo, "부모 작업")
    release = threading.Event()
    calls = _slow_derive(monkeypatch, release)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.selected_id == parent.id)
            await _submit_derive(pilot, app, "이어서 테스트 추가")
            await _until(pilot, lambda: calls)
            assert _notified(app, "파생 작업을 만드는 중") and len(app.screen_stack) == 1

            # 만드는 동안에도 키가 처리되고 1초 새로고침이 돈다
            await pilot.press("ctrl+b")
            await pilot.pause()
            assert isinstance(app.screen, ListMenuScreen)
            await pilot.press("escape")
            await pilot.pause()
            store.update_job(parent.id, phase="그동안 바뀐 단계")
            await _until(pilot, lambda: "그동안 바뀐 단계" in _cell(app, str(parent.id), "phase"))

            # 같은 부모에서 겹쳐 만들지 않는다
            await _submit_derive(pilot, app, "한 번 더")
            assert len(calls) == 1 and _notified(app, "이미 만드는 중")

            release.set()
            await _until(pilot, lambda: len(store.list_jobs()) == 2 and app.selected_id == store.list_jobs()[0].id)
            child = store.list_jobs()[0]
            assert child.prompt == "이어서 테스트 추가" and store.job_meta()[child.id].parent_id == parent.id
            assert _keys(app) == [str(child.id), str(parent.id)] and _cell(app, str(child.id)).startswith(f"↳#{parent.id} ")
            assert _notified(app, f"작업 #{child.id} 시작 (#{parent.id} 에서 파생)")
            in_main, used = calls[0]
            assert not in_main and used is not store                   # 화면 스레드 밖에서 따로 연 연결로
            assert not app._deriving
            await _until(pilot, lambda: store.get_job(child.id).is_terminal)

    try:
        _run(scenario())
    finally:
        release.set()
        store.close()


@pytest.mark.parametrize("error, message", [
    (JobError("작업 #1 의 상태를 저장하지 못했습니다: 접속 끊김"), "상태를 저장하지 못했습니다: 접속 끊김"),
    (RuntimeError("예상 못한 오류"), "파생 작업을 만들지 못했습니다"),
])
def test_파생_작업을_만들지_못하면_오류를_알리고_앱은_계속된다(env, repo, monkeypatch, error, message):
    cfg = config.load()
    store = Store(cfg.db_path)
    parent = _done(store, repo, "부모 작업")
    release = threading.Event()
    release.set()
    calls = _slow_derive(monkeypatch, release, fail=error)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.selected_id == parent.id)
            await _submit_derive(pilot, app, "실패할 파생")
            await _until(pilot, lambda: _notified(app, message))
            assert not app._deriving and len(app.screen_stack) == 1
            assert [j.id for j in store.list_jobs()] == [parent.id] and app.selected_id == parent.id
            await _submit_derive(pilot, app, "다시 시도")               # 같은 부모에서 다시 할 수 있다
            await _until(pilot, lambda: len(calls) == 2)

    try:
        _run(scenario())
    finally:
        store.close()


def test_파생_작업을_만드는_중에_끝내도_앱이_기다리지_않는다(env, repo, monkeypatch):
    cfg = config.load()
    store = Store(cfg.db_path)
    parent = _done(store, repo, "부모 작업")
    release = threading.Event()
    calls = _slow_derive(monkeypatch, release, fail=JobError("끝난 뒤"))
    closed_at: list[float] = []

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.selected_id == parent.id)
            await _submit_derive(pilot, app, "오래 걸리는 파생")
            await _until(pilot, lambda: calls)
            closed_at.append(time.monotonic())

    try:
        _run(scenario())   # run_test 를 나오고 이벤트 루프가 닫힐 때 스레드를 기다리면 30초 걸린다
        assert time.monotonic() - closed_at[0] < 10
    finally:
        release.set()
        store.close()


# ── 프로젝트를 고르면 그 프로젝트의 작업만 ───────────────────────────────────────

from coliday import scope as scopes  # noqa: E402


def _job_in(store, where, prompt, status="succeeded", parent_id=None):
    """where(디렉토리)에서 worktree 없이 실행한 것처럼 기록한 작업."""
    path = str(Path(where).resolve())
    job = store.create_job(prompt=prompt, repo=path, workdir=path, model=None, parent_id=parent_id)
    if status != "queued":
        store.finish_job(job.id, status, phase=status)
    return job


def _project_keys(app) -> list[str]:
    return [key.value for key in app.query_one("#projects", DataTable).rows]


async def _to_project(pilot, app, key: str) -> None:
    """프로젝트 목록 커서를 그 줄로 옮긴다 (↑↓ 로)."""
    table = app.query_one("#projects", DataTable)
    table.focus()
    target = _project_keys(app).index(key)
    while table.cursor_row != target:
        await pilot.press("down" if table.cursor_row < target else "up")
    await pilot.pause()


def _run_cell(app, key: str) -> str:
    return str(app.query_one("#projects", DataTable).get_cell(key, "run"))


def test_프로젝트를_고르면_그_작업만_보이고_돌아오면_보던_작업을_다시_고른다(env, repo, tmp_path):
    other = _second_repo(tmp_path)
    cfg = config.load()
    store = Store(cfg.db_path)
    jobs.add_project(cfg, store, other)
    _job_in(store, repo, "A 첫 작업")
    _job_in(store, repo, "A 둘째 작업")
    _job_in(store, other, "B 작업")
    a, b = str(repo.resolve()), str(other.resolve())

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: _keys(app) == ["2", "1"])
            assert app.scope == a and app.selected_id == 2
            assert "myrepo" in str(app.query_one("#jobs", DataTable).border_title)
            await _to_key(pilot, app, "1")
            assert app.selected_id == 1

            chat = app.query_one("#chat", Input)
            chat.value = "쓰던 글"
            await _to_project(pilot, app, b)
            assert app.scope == b and app.project_dir == b
            assert _keys(app) == ["3"] and app.selected_id == 3
            assert "other" in str(app.query_one("#jobs", DataTable).border_title)
            assert "보는 중: other" in str(app.query_one("#projects", DataTable).border_title)
            assert "B 작업" in _timeline_text(app) and "A 첫 작업" not in _timeline_text(app)
            assert str(app.query_one("#diff-pane").border_title) == "변경 사항 #3"
            assert chat.value == "쓰던 글" and _notified(app, "입력 중인 글은 이제 #3(other)에 보냅니다")

            await _to_project(pilot, app, a)
            assert _keys(app) == ["2", "1"] and app.selected_id == 1 and _cursor_key(app) == "1"   # 전에 보던 작업
            assert "A 첫 작업" in _timeline_text(app)
            await pilot.press("ctrl+n")
            assert app.screen.query_one("#dir", Input).value == a     # 새 작업도 보는 프로젝트에서
            await pilot.press("escape")

    try:
        _run(scenario())
    finally:
        store.close()


def test_프로젝트를_바꾸면_Space로_고른_것이_풀린다(env, repo, tmp_path):
    other = _second_repo(tmp_path)
    cfg = config.load()
    store = Store(cfg.db_path)
    jobs.add_project(cfg, store, other)
    _job_in(store, repo, "A 첫 작업")
    _job_in(store, repo, "A 둘째 작업")
    _job_in(store, other, "B 작업")

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: _keys(app) == ["2", "1"])
            app.query_one("#jobs", DataTable).focus()
            await pilot.press("space", "space")
            await pilot.pause()
            assert app._marked == {1, 2}
            await _to_project(pilot, app, str(other.resolve()))
            assert app._marked == set() and str(app.query_one("#jobs", DataTable).border_subtitle).startswith("Space 로")
            await pilot.press("ctrl+r")                                # 지울 대상은 보이는 작업 하나뿐
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen) and "#3" in app.screen._question and "#1" not in app.screen._question
            await pilot.press("n")
            await _to_project(pilot, app, str(repo.resolve()))
            assert app._marked == set()

    try:
        _run(scenario())
    finally:
        store.close()


def test_다른_프로젝트의_실행_중_작업은_개수로_보이고_끝나면_알린다(env, repo, tmp_path):
    other = _second_repo(tmp_path)
    cfg = config.load()
    store = Store(cfg.db_path)
    jobs.add_project(cfg, store, other)
    mine = _job_in(store, repo, "A 실행 중", status="queued")
    theirs = _job_in(store, other, "B 실행 중", status="queued")
    for job in (mine, theirs):
        assert store.start_job(job.id, pid=os.getpid())         # 살아 있는 pid: 정리되지 않는다
    a, b = str(repo.resolve()), str(other.resolve())

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: _run_cell(app, b) == "● 1" and _run_cell(app, a) == "● 1")
            assert _keys(app) == [str(mine.id)]
            store.finish_job(theirs.id, "failed", phase="실패")
            store.finish_job(mine.id, "succeeded", phase="완료")
            await _until(pilot, lambda: _notified(app, f"other: 작업 #{theirs.id} 실패"))
            await _until(pilot, lambda: _run_cell(app, b) == "" and _run_cell(app, a) == "")
            await pilot.pause(1.2)
            assert not _notified(app, f"작업 #{mine.id} 완료")          # 보고 있는 프로젝트의 작업은 알리지 않는다

    try:
        _run(scenario())
    finally:
        store.close()


def test_하위_폴더_프로젝트의_worktree_작업은_그_하위_폴더에_속한다(env, tmp_path):
    top = tmp_path / "mono"
    (top / "fe").mkdir(parents=True)
    (top / "fe" / "app.js").write_text("x\n")
    (top / "README").write_text("r\n")
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(top), *args], check=True)
    cfg = config.load()
    store = Store(cfg.db_path)
    jobs.add_project(cfg, store, top)
    jobs.add_project(cfg, store, top / "fe")
    in_fe = jobs.launch(cfg, store, "프론트 작업", top / "fe").job
    at_top = jobs.launch(cfg, store, "전체 작업", top).job
    assert in_fe.repo == at_top.repo == str(top.resolve()) and in_fe.branch  # 둘 다 최상위 레포의 worktree 작업이다

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(top / "fe"))
        async with app.run_test(size=(160, 45)) as pilot:
            assert app.scope == str((top / "fe").resolve())
            await _until(pilot, lambda: _keys(app) == [str(in_fe.id)])
            await _to_project(pilot, app, str(top.resolve()))
            assert _keys(app) == [str(at_top.id)] and app.selected_id == at_top.id
            assert scopes.OTHER not in _project_keys(app)
            await _until(pilot, lambda: all(store.get_job(j.id).is_terminal for j in (in_fe, at_top)))

    try:
        _run(scenario())
    finally:
        store.close()


def test_목록에서_뺀_프로젝트의_작업은_기타에_보이고_기타에서는_만들기를_막는다(env, repo, tmp_path):
    other = _second_repo(tmp_path)
    cfg = config.load()
    store = Store(cfg.db_path)
    jobs.add_project(cfg, store, other)
    _job_in(store, repo, "A 작업")
    _job_in(store, other, "B 작업")
    b = str(other.resolve())

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: _keys(app) == ["1"])
            assert scopes.OTHER not in _project_keys(app)              # 기타 작업이 없으면 줄도 없다
            await _to_project(pilot, app, b)
            await pilot.press("ctrl+x")
            await pilot.pause()
            assert _project_keys(app) == [str(repo.resolve()), scopes.OTHER]
            assert app.scope == str(repo.resolve()) and _keys(app) == ["1"]   # 첫 줄로 옮긴다

            await _to_project(pilot, app, scopes.OTHER)
            assert app.scope == scopes.OTHER and _keys(app) == ["2"] and app.selected_id == 2
            assert app.project_dir == str(repo.resolve())              # 새 작업 위치는 마지막 실제 프로젝트
            assert "기타 보는 중" in str(app.query_one("#projects", DataTable).border_title)
            for key in ("ctrl+e", "ctrl+x"):
                await pilot.press(key)
                await pilot.pause()
                assert len(app.screen_stack) == 1
            assert _notified(app, "기타는 프로젝트가 아닙니다")
            assert jobs.list_projects(cfg, store) == [str(repo.resolve())]

            await pilot.press("ctrl+b")
            await pilot.pause()
            option = app.screen.query_one(OptionList).get_option("group_new")
            assert option.disabled and "기타에서는 그룹을 쓸 수 없습니다" in str(option.prompt)
            assert "pin" in [o.id for o in app.screen.query_one(OptionList)._options]   # 고정은 된다
            await pilot.press("escape")
            await pilot.pause()
            assert app.focused is app.query_one("#bundle")             # 메뉴를 닫으면 묶음 패널로

            app._clear_selection()                                     # 작업을 고르지 않은 채 보내면
            chat = app.query_one("#chat", Input)
            chat.focus()
            chat.value = "어디에 만들지?"
            await pilot.press("enter")
            await pilot.pause()
            assert [j.id for j in store.list_jobs()] == [2, 1] and chat.value == "어디에 만들지?"
            assert _notified(app, "기타에서는 새 작업을 시작할 수 없습니다")

            store.delete_job(2)                                       # 기타 작업이 없어지면 줄도 사라진다
            await _until(pilot, lambda: scopes.OTHER not in _project_keys(app))
            assert app.scope == str(repo.resolve()) and _keys(app) == ["1"]

    try:
        _run(scenario())
    finally:
        store.close()


def test_그룹은_만든_프로젝트에서만_보이고_다른_프로젝트는_같은_이름을_쓴다(env, repo, tmp_path):
    other = _second_repo(tmp_path)
    cfg = config.load()
    store = Store(cfg.db_path)
    jobs.add_project(cfg, store, other)
    _job_in(store, repo, "A 작업")
    _job_in(store, other, "B 작업")
    a, b = str(repo.resolve()), str(other.resolve())

    async def name(pilot, app, text):
        assert isinstance(app.screen, NameScreen)
        app.screen.query_one("#name", Input).value = text
        await pilot.press("enter")
        await pilot.pause()

    async def scenario():
        app = _app(cfg, store, repo)
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: _keys(app) == ["1"])
            await _menu(pilot, app, "group_new")
            await name(pilot, app, "결제")
            [ga] = store.list_groups(project=a)
            assert _keys(app) == ["1"] and _gd_texts(app)[0] == "그룹: 결제 (1)"

            await _to_project(pilot, app, b)
            assert _keys(app) == ["2"]                                 # A 의 그룹은 보이지 않는다
            await pilot.press("ctrl+b")
            await pilot.pause()
            assert f"group:{ga.id}" not in [o.id for o in app.screen.query_one(OptionList)._options]
            await pilot.press("escape")
            await pilot.pause()
            await _menu(pilot, app, "group_new")
            await name(pilot, app, "결제")
            assert not _notified(app, "이미 있는 그룹 이름")
            [gb] = store.list_groups(project=b)
            assert gb.name == "결제" and gb.id != ga.id and _keys(app) == ["2"]
            assert _gd_texts(app)[0] == "그룹: 결제 (1)" and _gd_ids(app)[1] == "m:2"

            store.set_group([1], gb.id)                                # 다른 프로젝트의 그룹에 든 작업은 그룹 없이 보인다
            await _to_project(pilot, app, a)
            assert _keys(app) == ["1"] and app.selected_id == 1 and "▣" not in _cell(app, "1")
            assert not any(t.startswith("그룹:") for t in _bundle_texts(app))
            await _to_key(pilot, app, "1")
            await pilot.press("ctrl+b")
            await pilot.pause()
            ids = [o.id for o in app.screen.query_one(OptionList)._options]
            assert "ungroup" not in ids and f"delete_empty:{ga.id}" in ids   # A 의 결제 그룹은 이제 비었다
            await pilot.press("escape")

    try:
        _run(scenario())
    finally:
        store.close()


def test_다른_프로젝트로_옮긴_뒤_파생이_끝나면_화면은_그대로이고_알리기만_한다(env, repo, tmp_path, monkeypatch):
    other = _second_repo(tmp_path)
    cfg = config.load()
    store = Store(cfg.db_path)
    jobs.add_project(cfg, store, other)
    parent = _job_in(store, repo, "부모 작업")
    theirs = _job_in(store, other, "B 작업")
    release = threading.Event()
    calls = _slow_derive(monkeypatch, release)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.selected_id == parent.id)
            await _submit_derive(pilot, app, "이어서")
            await _until(pilot, lambda: calls)
            await _to_project(pilot, app, str(other.resolve()))
            assert app.selected_id == theirs.id
            release.set()
            await _until(pilot, lambda: _notified(app, f"(#{parent.id} 에서 파생) — 프로젝트 myrepo"))
            child = store.list_jobs()[0]
            assert child.id not in (parent.id, theirs.id)
            assert app.scope == str(other.resolve()) and app.selected_id == theirs.id and _keys(app) == [str(theirs.id)]
            await _to_project(pilot, app, str(repo.resolve()))
            assert _keys(app) == [str(child.id), str(parent.id)]      # 최근 순, ↳#부모 표시
            assert _cell(app, str(child.id)).startswith(f"↳#{parent.id} ")
            await _until(pilot, lambda: store.get_job(child.id).is_terminal)

    try:
        _run(scenario())
    finally:
        release.set()
        store.close()


def test_닫히는_중에_처리되는_이벤트와_타이머는_지운_위젯을_찾지_않는다(env, repo):
    # run_test 는 앱의 메시지 처리와 동시에 종료한다: 위젯을 지운 뒤 쌓여 있던 커서 이동 이벤트가 처리돼도 오류가 나면 안 된다
    cfg = config.load()
    store = Store(cfg.db_path)
    _done(store, repo, "끝난 작업")

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.query_one("#jobs", DataTable).row_count == 1)
            table = app.query_one("#jobs", DataTable)
            stale = DataTable.RowHighlighted(table, 0, table.coordinate_to_cell_key((0, 0)).row_key)
        # 닫힌 뒤(위젯이 없다)에 처리돼도 건너뛴다
        assert not app.is_running
        app._on_highlight(stale)
        app._tick()
        app._show_diff(1, "x", False)

    try:
        _run(scenario())
    finally:
        store.close()


# ── 시작 포커스와 묶음 패널 ─────────────────────────────────────────────

def test_시작하면_프로젝트_목록에_포커스가_있고_주기_갱신_뒤에도_유지된다(env, repo, tmp_path):
    other = _second_repo(tmp_path)
    cfg = config.load()
    store = Store(cfg.db_path)
    jobs.add_project(cfg, store, other)
    _job_in(store, repo, "A 작업")
    _job_in(store, other, "B 작업")

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            projects = app.query_one("#projects", DataTable)
            await _until(pilot, lambda: _keys(app) == ["1"] and app.focused is not None)
            assert app.focused is projects
            await pilot.pause(2.5)                                     # 1초 새로고침을 두 번 넘게 지나도
            assert app.focused is projects
            _job_in(store, repo, "다른 터미널에서 만든 작업")           # 목록을 다시 그려도
            await _until(pilot, lambda: _keys(app) == ["3", "1"])
            assert app.focused is projects and app.selected_id == 1

    try:
        _run(scenario())
    finally:
        store.close()


def test_프로젝트가_없어도_프로젝트_목록에서_시작한다(env, repo, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    cfg = config.load()
    store = Store(cfg.db_path)

    async def empty():
        app = ColidayApp(cfg, store, default_dir=str(plain))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.focused is not None)
            assert app.focused is app.query_one("#projects", DataTable) and _project_keys(app) == []
            assert "Ctrl+G 로 추가" in str(app.query_one("#projects", DataTable).border_title)
            assert not _panel(app).has_class("-shown")                 # 선택한 작업이 없으면 묶음 패널도 없다

    async def other_only():
        app = ColidayApp(cfg, store, default_dir=str(plain))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.focused is not None and _keys(app) == ["1"])
            projects = app.query_one("#projects", DataTable)
            assert app.focused is projects and _project_keys(app) == [scopes.OTHER] and projects.cursor_row == 0
            assert app.scope == scopes.OTHER

    try:
        _run(empty())
        _job_in(store, repo, "목록에 없는 곳의 작업")                  # 기타 줄만 있다
        _run(other_only())
    finally:
        store.close()


def test_묶음_패널은_혼자인_채팅도_묶음으로_보이고_그룹과_파생은_접힌_구역에_있다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)
    for prompt in ("첫 작업", "둘째 작업", "셋째 작업"):
        _done(store, repo, prompt)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.selected_id == 3)
            panel = _panel(app)
            assert panel.has_class("-shown") and str(panel.border_title) == "묶음 · #3 의 일 1개"
            # 아직 안 묶인 차례도 임시 묶음(◌) 한 줄로 보인다. Enter 로 펼쳐야 내 질문과 AI 가 한 일이 나온다
            assert _bundle_ids(app) == ["u:3:0"]                           # 메뉴 줄도 없다 (메뉴는 Ctrl+B)
            assert _bundle_texts(app)[0] == '◌ 묶기 전 · "셋째 작업"'
            panel.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            text = _bundle_texts(app)[0]
            assert text.startswith('◌ 묶기 전 · "셋째 작업"') and "\n  ▌나 #3 · 1번째 요청 · " in text
            assert "\n      ◆ AI ✓ 완료" in text

            bundle = _bind(store, 3, 0, name="셋째 일", line="셋째에서 한 일")   # 다른 터미널(worker)이 묶었다
            await _until(pilot, lambda: f"b:{bundle}" in _bundle_ids(app))
            assert _bundle_ids(app) == [f"b:{bundle}"]                      # 혼자인 묶음도 보인다: 번호와 이름 한 줄뿐
            assert _bundle_texts(app)[0] == "1. 셋째 일"
            await _expand(pilot, app, bundle)                              # → 로 펼치면 요약과 차례가 아래에 나온다
            assert _bundle_ids(app) == [f"b:{bundle}", None, "t:3:0", None]
            assert _bundle_texts(app)[0].startswith("▼ 1. 셋째 일  · 차례 1 · 채팅 #3")
            assert "셋째에서 한 일" in _bundle_texts(app)[2]

            group = store.create_group("결제 [v1]", project=str(repo.resolve()))   # 그룹·파생은 아래 접힌 구역 하나로
            store.set_group([3, 1], group.id)
            await _until(pilot, lambda: "gd" in _bundle_ids(app))
            assert _bundle_ids(app)[-1] == "gd" and "m:3" not in _bundle_ids(app)
            assert _bundle_texts(app)[-1] == '▸ 그룹 "결제 [v1]" (2)'                     # 대괄호도 그대로 보인다
            panel.focus()
            await pilot.pause()
            _highlight(app, "gd")
            await pilot.press("right")                                    # → 로 펼친다
            await pilot.pause()
            assert _gd_ids(app) == [None, "m:3", "m:1"] and _gd_texts(app)[0] == "그룹: 결제 [v1] (2)"
            assert _gd_texts(app)[1].startswith("▶ ") and "#3 셋째 작업" in _gd_texts(app)[1]
            assert _bundle_texts(app)[_bundle_ids(app).index("gd")].startswith("▾ ")
            _highlight(app, "m:1")
            await pilot.press("left")                                     # 구성 줄에서 ← 는 구역을 접는다
            await pilot.pause()
            assert "m:1" not in _bundle_ids(app) and panel.highlighted_option.id == "gd"
            await pilot.press("enter")                                    # Enter 도 펼친다
            await pilot.pause()
            assert "m:1" in _bundle_ids(app)

            await _to_key(pilot, app, "1")                                # 다른 채팅: 맨 위 줄에서 시작, 구역은 펼친 채
            assert str(panel.border_title) == "묶음 · #1 의 일 1개" and panel.highlighted_option.id == "u:1:0"
            assert _gd_ids(app) == [None, "m:3", "m:1"]
            _highlight(app, "m:3")
            await pilot.pause(1.5)                                        # 바뀐 것이 없으면 다시 그리지 않는다
            assert panel.highlighted_option.id == "m:3"
            store.set_group([2], group.id)                                # 바뀌어도 하이라이트한 줄은 그대로
            await _until(pilot, lambda: "m:2" in _bundle_ids(app))
            assert _gd_ids(app) == [None, "m:3", "m:2", "m:1"] and panel.highlighted_option.id == "m:3"

            assert store.delete_job(3)                                    # 구성 작업이 지워지면 사라진다
            await _until(pilot, lambda: "m:3" not in _bundle_ids(app))
            assert _gd_texts(app)[0] == "그룹: 결제 [v1] (2)" and app.selected_id == 1

            panel.focus()
            await pilot.pause()
            app._clear_selection()                                        # 선택한 작업이 없으면 숨고 포커스는 작업 목록으로
            await pilot.pause()
            assert not panel.has_class("-shown") and app.focused is app.query_one("#jobs", DataTable)

    try:
        _run(scenario())
    finally:
        store.close()


def test_묶음_패널에서_Enter로_묶인_작업으로_이동한다(env, repo, tmp_path):
    other = _second_repo(tmp_path)
    cfg = config.load()
    store = Store(cfg.db_path)
    jobs.add_project(cfg, store, other)
    parent = _job_in(store, repo, "부모 작업")
    child = _job_in(store, repo, "자식 작업", parent_id=parent.id)
    far = _job_in(store, other, "다른 프로젝트의 손자", parent_id=child.id)
    a, b = str(repo.resolve()), str(other.resolve())

    async def scenario():
        app = _app(cfg, store, repo)
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.selected_id == child.id)
            assert _gd_ids(app) == [None, f"p:{parent.id}", f"c:{far.id}"]
            assert _gd_texts(app)[2].endswith("(프로젝트 other)")
            panel = _panel(app)
            panel.focus()
            await pilot.pause()

            _highlight(app, f"p:{parent.id}")
            await pilot.press("enter")                                   # 같은 프로젝트: 목록 커서도 옮긴다
            await pilot.pause()
            assert app.selected_id == parent.id and _cursor_key(app) == str(parent.id)
            assert app.focused is panel and f"c:{child.id}" in _bundle_ids(app)   # 포커스는 패널에 남는다

            _highlight(app, f"c:{child.id}")
            await pilot.press("enter")
            await pilot.pause()
            assert app.selected_id == child.id and _cursor_key(app) == str(child.id)

            _highlight(app, f"c:{far.id}")
            await pilot.press("enter")                                   # 다른 프로젝트: 그 프로젝트로 옮겨 고른다
            await pilot.pause()
            assert app.scope == b and _keys(app) == [str(far.id)] and app.selected_id == far.id
            assert _notified(app, "프로젝트 other 로 옮겼습니다")
            assert f"p:{child.id}" in _bundle_ids(app) and "(프로젝트 myrepo)" in " ".join(_bundle_texts(app))

            await pilot.press("escape")                                  # Esc 는 작업 목록으로
            await pilot.pause()
            assert app.focused is app.query_one("#jobs", DataTable)
            await _to_project(pilot, app, a)
            assert app.selected_id == child.id

    try:
        _run(scenario())
    finally:
        store.close()


def test_ctrl_b는_묶음_패널로_가서_메뉴를_열고_Space로_고른_작업들에_적용한다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)
    for prompt in ("첫 작업", "둘째 작업", "셋째 작업"):
        _done(store, repo, prompt)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: _keys(app) == ["3", "2", "1"])
            await _to_key(pilot, app, "2")
            await pilot.press("space", "space")                          # #2, #1 을 고른다
            await pilot.pause()
            assert app._marked == {2, 1}
            assert str(_panel(app).border_subtitle).startswith("고른 2개에 적용(메뉴)")
            await pilot.press("ctrl+b")
            await pilot.pause()
            assert isinstance(app.screen, ListMenuScreen) and app.screen_stack[0].focused is _panel(app)
            assert "고른 2개" in str(app.screen.query_one(OptionList).get_option("pin").prompt)
            await _choose(pilot, app, "pin")
            assert {i for i, m in store.job_meta().items() if m.pinned} == {1, 2}
            assert _keys(app) == ["2", "1", "3"] and app.focused is _panel(app)

            app._clear_selection()                                       # 선택한 작업이 없으면 알리기만 한다
            await pilot.press("ctrl+b")
            await pilot.pause()
            assert len(app.screen_stack) == 1 and _notified(app, "선택된 작업이 없습니다")

    try:
        _run(scenario())
    finally:
        store.close()


def test_묶음_패널의_ctrl_e와_ctrl_x는_패널에서만_동작하고_프로젝트_목록과_겹치지_않는다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)
    for prompt in ("첫 작업", "둘째 작업", "셋째 작업"):
        _done(store, repo, prompt)
    group = store.create_group("결제 [v1]", project=str(repo.resolve()))
    store.set_group([3, 2], group.id)

    async def scenario():
        app = _app(cfg, store, repo)
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: "m:2" in _bundle_ids(app))
            panel = _panel(app)
            panel.focus()
            await pilot.pause()
            await pilot.press("ctrl+e")                                  # 패널: 그룹 이름 바꾸기
            await pilot.pause()
            assert isinstance(app.screen, NameScreen) and app.screen.query_one("#name", Input).value == "결제 [v1]"
            await pilot.press("escape")
            await pilot.pause()
            assert app.focused is panel

            app.query_one("#projects", DataTable).focus()                # 프로젝트 목록: 프로젝트 이름 바꾸기
            await pilot.pause()
            await pilot.press("ctrl+e")
            await pilot.pause()
            assert isinstance(app.screen, AddProjectScreen) and app.screen._title == "프로젝트 이름 바꾸기"
            await pilot.press("escape")
            await pilot.pause()

            panel.focus()
            await pilot.pause()
            _highlight(app, "gd")                                        # 구성 작업 줄이 아니면 안내만
            await pilot.press("ctrl+x")
            await pilot.pause()
            assert _notified(app, "그룹에서 뺄 작업을") and store.job_meta()[2].group_id == group.id
            _highlight(app, "m:2")
            await pilot.press("ctrl+x")                                  # 패널: 하이라이트한 작업을 그룹에서 뺀다
            await pilot.pause()
            assert store.job_meta()[2].group_id is None and store.job_meta()[3].group_id == group.id
            assert jobs.list_projects(cfg, store) == [str(repo.resolve())] and len(app.screen_stack) == 1

            chat = app.query_one("#chat", Input)                         # 채팅 입력의 ctrl+e 는 줄 끝으로
            chat.focus()
            chat.value = "abc"
            chat.cursor_position = 0
            await pilot.press("ctrl+e")
            await pilot.pause()
            assert len(app.screen_stack) == 1 and chat.cursor_position == 3

    try:
        _run(scenario())
    finally:
        store.close()


# ── 묶음 (모델이 채팅의 차례를 일 단위로 묶은 것): 묶음 패널과 ctrl+b 메뉴 ─────────────────
from coliday import topics  # noqa: E402
from coliday.store import TurnAnswer  # noqa: E402
from coliday.tui import BundleScreen, TopicScreen, TurnBlock  # noqa: E402


def _reported(store, repo, prompt, report="보고", status="succeeded", members=()):
    """첫 차례가 끝난 채팅 (보고 이벤트와 일한 팀원이 있는)."""
    job = store.create_job(prompt=prompt, repo=str(repo), workdir=str(repo), model=None)
    for member in members:
        store.add_event(job.id, member, "start", "")
    store.add_event(job.id, "팀장", RESULT, report)
    store.finish_job(job.id, status, phase=status)
    return job.id


def _follow(store, job_id, prompt, report="보고", status="succeeded", members=()):
    """끝난 채팅에 이어서 시킨 차례 하나 (worker 없이). 그 차례 id."""
    assert store.requeue_job(job_id, prompt)
    turn_id = store.last_follow_up(job_id).id
    for member in members:
        store.add_event(job_id, member, "start", "")
    if status == "failed":
        store.add_event(job_id, "팀장", "error", "실패")
    store.add_event(job_id, "팀장", RESULT, report)
    store.finish_job(job_id, status, phase=status)
    return turn_id


def _bind(store, job_id, turn_id, bundle=None, name="", line=""):
    """모델이 묶은 것처럼 차례 하나를 묶음에 넣는다. 묶음 id."""
    got = store.assign_turns(job_id, [TurnAnswer(turn_id, bundle, name, line)], expect={})
    return got[turn_id]


def _menu_ids(app) -> list[str]:
    return [o.id for o in app.screen.query_one(OptionList)._options]


def _menu_text(app, option_id: str) -> str:
    return str(app.screen.query_one(OptionList).get_option(option_id).prompt)


def _two_bundles(store, repo):
    """채팅 #2 가 두 묶음(주문 취소·세션)에 걸치고, 주문 취소 묶음에는 채팅 #1 의 차례도 있다."""
    a = _reported(store, repo, "주문 취소 API [v2] 만들어 줘", "## 결과\n- 취소 API 추가\n- 테스트 6개", members=("architect",))
    b = _reported(store, repo, "환불 계산 고쳐 줘", "쿠폰 이중 차감 수정", members=("coder",))
    t2 = _follow(store, b, "로그인 세션도 봐 줘", "세션 연장")
    long_report = "\n".join(f"- 목록 스타일 {n}번째 줄" for n in range(40))
    t3 = _follow(store, b, "취소 주문은 회색으로\n상세 [취소] 버튼은 숨기고\n목록도 고치고\n넷째 줄 요청", long_report,
                 status="failed", members=("coder", "reviewer"))
    cancel = _bind(store, a, 0, name="주문 취소", line="취소 API 와 테스트 추가")
    _bind(store, b, 0, cancel, line="환불 계산 수정")
    session = _bind(store, b, t2, name="세션", line="세션 연장")
    _bind(store, b, t3, cancel, line="회색 표시 시도, 테스트 실패로 멈춤")
    return a, b, t2, t3, cancel, session


def test_한_채팅이_두_묶음에_걸치면_번호와_이름만_보이고_펼치면_나와_AI가_기호로_구분된다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)
    a, b, t2, t3, cancel, session = _two_bundles(store, repo)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.selected_id == b and f"b:{session}" in _bundle_ids(app))
            ids, texts = _bundle_ids(app), _bundle_texts(app)
            assert str(_panel(app).border_title) == f"묶음 · #{b} 의 일 2개"
            # 기본은 부연 설명 없이 번호와 이름 한 줄씩: 이 채팅에서 다룬 순서대로 (가장 최근에 다룬 일이 아래)
            assert ids == [f"b:{session}", f"b:{cancel}"]
            assert texts[:2] == ["1. 세션", "2. 주문 취소"]
            assert _panel(app).highlighted_option.id == f"b:{session}"

            await _expand(pilot, app, cancel)                             # → 로 펼친 묶음에만 요약과 차례가 나온다
            ids, texts = _bundle_ids(app), _bundle_texts(app)
            assert ids[:2] == [f"b:{session}", f"b:{cancel}"] and texts[0] == "1. 세션"
            # 펼친 묶음 안에는 다른 채팅(#1)의 차례도 섞여 있다
            assert [i for i in ids if i and i.startswith("t:")] == [f"t:{b}:{t3}", f"t:{b}:0", f"t:{a}:0"]
            assert texts[1].startswith(f"▼ 2. 주문 취소  · 차례 3 · 채팅 #{b} #{a}")
            assert f"t:{b}:{t2}" not in ids

            turn = texts[ids.index(f"t:{b}:{t3}")].split("\n")
            assert turn[0].startswith(f"  ▌나 #{b} · 3번째 요청 · ")
            assert turn[1:4] == ["  ▌취소 주문은 회색으로", "  ▌상세 [취소] 버튼은 숨기고", "  ▌목록도 고치고…"]   # 질문은 3줄
            assert turn[4].startswith("      ◆ AI ✗ 실패 · 팀원 coder·reviewer · 오류 1")
            assert turn[5] == "      회색 표시 시도, 테스트 실패로 멈춤"
            other = texts[ids.index(f"t:{a}:0")]
            assert other.startswith(f"  ▌나 #{a} · 1번째 요청") and "(다른 채팅)" in other.split("\n")[0]
            assert "\n  ▌주문 취소 API [v2] 만들어 줘\n" in other                              # 대괄호도 그대로
            # 색이 없어도 구분된다: 차례 칸의 줄은 모두 '  ▌'(내 질문) 아니면 6칸 들여쓰기(AI) 로 시작한다
            for key in (f"t:{b}:{t3}", f"t:{b}:0", f"t:{a}:0"):
                lines = texts[ids.index(key)].split("\n")
                assert all(line.startswith("  ▌") or line.startswith("      ") for line in lines)
                assert sum(line.startswith("      ◆ AI ") for line in lines) == 1
            # 차례 사이에는 빈 줄 (고를 수 없다)
            assert ids[ids.index(f"t:{b}:{t3}") + 1] is None and texts[ids.index(f"t:{b}:{t3}") + 1] == ""
            assert _panel(app).highlighted_option.id == f"b:{cancel}"

            await _to_key(pilot, app, str(a))                             # 다른 채팅에서도 같은 묶음이 보인다 (번호는 그 채팅 기준)
            assert _bundle_ids(app)[0] == f"b:{cancel}" and f"b:{session}" not in _bundle_ids(app)
            assert _bundle_texts(app)[0].startswith("▼ 1. 주문 취소")
            assert "(다른 채팅)" in _bundle_texts(app)[_bundle_ids(app).index(f"t:{b}:{t3}")]

    try:
        _run(scenario())
    finally:
        store.close()


def test_다른_채팅의_차례에서_Enter는_그_채팅으로_이_채팅의_차례는_펼치고_방향키로_접는다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)
    a, b, t2, t3, cancel, session = _two_bundles(store, repo)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.selected_id == b and f"b:{session}" in _bundle_ids(app))
            panel = _panel(app)
            panel.focus()
            await pilot.pause()
            _highlight(app, f"b:{cancel}")
            await pilot.press("right")                                   # → : 묶음을 펼쳐야 차례 줄이 나온다
            await pilot.pause()
            assert f"t:{a}:0" in _bundle_ids(app) and panel.highlighted_option.id == f"b:{cancel}"

            _highlight(app, f"t:{a}:0")
            await pilot.press("enter")                                   # 다른 채팅의 차례: 그 채팅으로, 같은 차례에 커서
            await pilot.pause()
            assert app.selected_id == a and _cursor_key(app) == str(a) and app.focused is panel
            assert panel.highlighted_option.id == f"t:{a}:0"
            _highlight(app, f"t:{b}:{t3}")
            await pilot.press("enter")
            await pilot.pause()
            assert app.selected_id == b and panel.highlighted_option.id == f"t:{b}:{t3}"

            short = _bundle_texts(app)[_bundle_ids(app).index(f"t:{b}:{t3}")]
            await pilot.press("enter")                                   # 이 채팅의 차례: 펼친다 (질문 전문 + 보고 앞부분)
            await pilot.pause()
            opened = _bundle_texts(app)[_bundle_ids(app).index(f"t:{b}:{t3}")]
            assert "  ▌넷째 줄 요청" in opened and "넷째 줄" not in short
            assert "      · 목록 스타일 24번째 줄" in opened and "25번째 줄" not in opened   # 보고는 25줄까지
            assert opened.endswith("… 전문은 묶음 제목 줄에서 Enter (전체 보기)")
            await pilot.press("left")                                    # ← : 펼친 차례를 접는다
            await pilot.pause()
            assert _bundle_texts(app)[_bundle_ids(app).index(f"t:{b}:{t3}")] == short
            await pilot.press("left")                                    # 한 번 더: 그 묶음을 접고 제목 줄로
            await pilot.pause()
            assert f"t:{b}:{t3}" not in _bundle_ids(app) and panel.highlighted_option.id == f"b:{cancel}"
            assert _bundle_texts(app)[_bundle_ids(app).index(f"b:{cancel}")] == "2. 주문 취소"   # 접으면 다시 제목 한 줄
            _highlight(app, f"b:{session}")
            await pilot.press("right")                                   # → : 묶음을 펼친다
            await pilot.pause()
            assert f"t:{b}:{t2}" in _bundle_ids(app) and panel.highlighted_option.id == f"b:{session}"

            await pilot.press("escape")                                  # Esc 는 작업 목록으로
            await pilot.pause()
            assert app.focused is app.query_one("#jobs", DataTable)

    try:
        _run(scenario())
    finally:
        store.close()


def test_패널에서_다른_프로젝트의_채팅_차례로_Enter하면_그_프로젝트로_옮겨_간다(env, tmp_path):
    top = tmp_path / "mono"
    for sub in ("fe", "be"):
        (top / sub).mkdir(parents=True)
        (top / sub / "x.txt").write_text("x\n")
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(top), *args], check=True)
    cfg = config.load()
    store = Store(cfg.db_path)
    fe, be = (str((top / sub).resolve()) for sub in ("fe", "be"))
    jobs.add_project(cfg, store, top / "fe")
    jobs.add_project(cfg, store, top / "be")
    repo_key = str(top.resolve())

    def job(where, prompt):
        made = store.create_job(prompt=prompt, repo=repo_key, workdir=where, model=None)
        store.finish_job(made.id, "succeeded")
        return made.id

    a, b, c = job(fe, "프론트 취소 버튼"), job(fe, "프론트 기타"), job(be, "백엔드 취소 API")
    bundle = _bind(store, a, 0, name="주문 취소")                     # 묶음은 레포 기준이라 두 프로젝트에 걸친다
    _bind(store, c, 0, bundle)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(top / "fe"))
        async with app.run_test(size=(160, 45)) as pilot:
            assert app.scope == fe
            await _until(pilot, lambda: _keys(app) == [str(b), str(a)])
            await _to_key(pilot, app, str(a))
            assert _bundle_ids(app)[0] == f"b:{bundle}" and _bundle_texts(app)[0] == "1. 주문 취소"
            await _expand(pilot, app, bundle)
            assert [i for i in _bundle_ids(app) if i and i.startswith("t:")] == [f"t:{c}:0", f"t:{a}:0"]
            assert "(프로젝트 be)" in _bundle_texts(app)[_bundle_ids(app).index(f"t:{c}:0")].split("\n")[0]
            panel = _panel(app)
            panel.focus()
            _highlight(app, f"t:{c}:0")
            await pilot.press("enter")                                # 다른 프로젝트: 그 프로젝트로 옮겨 고른다
            await pilot.pause()
            assert app.scope == be and app.selected_id == c and _notified(app, "프로젝트 be 로 옮겼습니다")
            assert app.focused is panel and panel.highlighted_option.id == f"t:{c}:0"
            _highlight(app, f"t:{a}:0")
            await pilot.press("enter")
            await pilot.pause()
            assert app.scope == fe and app.selected_id == a

    try:
        _run(scenario())
    finally:
        store.close()


def test_실행_중인_차례는_분류_중_임시_묶음이고_묶지_못한_차례는_그렇게_보인다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)
    failed = _reported(store, repo, "묶기에 실패한 채팅", "한 일")
    log = cfg.job_dir(failed) / "topic.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("TopicError: 시간 초과\n")
    running = store.create_job(prompt="배포 스크립트 고쳐 줘", repo=str(repo), workdir=str(repo), model=None)
    assert store.start_job(running.id, os.getpid())                  # 살아 있는 pid: 정리되지 않는다
    store.update_job(running.id, phase="coder 가 고치는 중")
    assert store.queue_message(running.id, "버튼 색도 바꿔 줘")

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.selected_id == running.id)
            await _until(pilot, lambda: _bundle_ids(app)[:1] == [f"u:{running.id}:0"])
            assert _bundle_texts(app)[0] == '◌ 분류 중 · "배포 스크립트 고쳐 줘"'       # 기본은 한 줄
            assert any(t.startswith("  ⏳ 대기: 버튼 색도 바꿔 줘") for t in _bundle_texts(app))
            _panel(app).focus()
            await pilot.pause()
            await pilot.press("enter")                                 # 펼치면 지금 하는 일이 보인다
            await pilot.pause()
            text = _bundle_texts(app)[0]
            assert text.startswith('◌ 분류 중 · "배포 스크립트 고쳐 줘"\n')
            assert "      ◆ AI ⟳ 실행 중" in text and "일하는 중 — coder 가 고치는 중" in text

            await _to_key(pilot, app, str(failed))
            assert _bundle_texts(app)[0] == '◌ 묶지 못함 · "묶기에 실패한 채팅"\n  Ctrl+B → 안 묶인 차례 묶기'

    try:
        _run(scenario())
    finally:
        store.close()


def test_주기_갱신_뒤에도_펼침과_하이라이트가_유지되고_바뀌면_다시_그려도_그대로다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)
    a, b, t2, t3, cancel, session = _two_bundles(store, repo)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.selected_id == b and f"b:{session}" in _bundle_ids(app))
            panel = _panel(app)
            panel.focus()
            _highlight(app, f"b:{session}")
            await pilot.press("right")
            await pilot.pause()
            _highlight(app, f"t:{b}:{t2}")
            await pilot.press("enter")                                # 차례도 펼친다
            await pilot.pause()
            drawn = list(panel._options)
            await pilot.pause(2.5)                                    # 1초 새로고침을 두 번 넘게 지나도
            assert panel._options == drawn and panel.highlighted_option.id == f"t:{b}:{t2}"   # 다시 그리지 않는다
            store.rename_bundle(cancel, "주문 취소 흐름")              # 다른 터미널이 바꿨다: 다시 그려도 상태는 그대로
            await _until(pilot, lambda: "2. 주문 취소 흐름" in _bundle_texts(app))   # 펼치지 않은 묶음은 제목 한 줄
            assert panel.highlighted_option.id == f"t:{b}:{t2}" and f"t:{b}:{t2}" in app._panel_parent
            assert "세션 연장" in _bundle_texts(app)[_bundle_ids(app).index(f"t:{b}:{t2}")]
            assert (b, t2) in app._turn_open and app._bundle_open[session]

    try:
        _run(scenario())
    finally:
        store.close()


def test_미분류_차례가_7개_이상이면_더_보기로_이전_차례를_읽고_수동_이동할_수_있다(env, repo):
    from textual.widgets import Select

    cfg = config.load()
    store = Store(cfg.db_path)
    target_job = _reported(store, repo, "옮겨 놓을 묶인 일")
    target_bundle = _bind(store, target_job, 0, name="수동 분류 대상")
    job = _reported(store, repo, "오래된 미분류 요청")
    turn_ids = [0] + [_follow(store, job, f"미분류 {n}번째 요청") for n in range(2, 9)]
    log = cfg.job_dir(job) / "topic.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("TopicError: 분류 실패\n")

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.selected_id == job and "loose-more" in _bundle_ids(app))
            assert len([key for key in _bundle_ids(app) if key and key.startswith(f"u:{job}:")]) == 6
            _panel(app).focus()
            _highlight(app, "loose-more")
            await pilot.press("enter")
            await pilot.pause(1.5)  # 주기 갱신을 지나도 펼친 범위가 유지된다
            assert "loose-more" not in _bundle_ids(app)
            assert all(f"u:{job}:{turn_id}" in _bundle_ids(app) for turn_id in turn_ids)
            assert _panel(app).highlighted_option.id == f"u:{job}:{turn_ids[1]}"
            oldest = f"u:{job}:0"
            assert _bundle_texts(app)[_bundle_ids(app).index(oldest)].startswith('◌ 묶지 못함 · "오래된 미분류 요청"')

            _highlight(app, oldest)
            await pilot.press("ctrl+b")
            await pilot.pause()
            assert f"(#{job} 1번째: '오래된 미분류 요청')" in _menu_text(app, "turn_move")
            await _choose(pilot, app, "turn_move")
            app.screen.query_one("#topic", Select).value = target_bundle
            await pilot.pause()
            await pilot.click("#save")
            await pilot.pause()
            link = store.turn_links([job])[(job, 0)]
            assert (link.bundle_id, link.manual, link.source) == (target_bundle, True, "manual")

    try:
        _run(scenario())
    finally:
        store.close()


def test_13번째_묶음은_더_보기로_제목_전체_보기와_작업에_접근한다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)
    job = _reported(store, repo, "1번째 일")
    turn_ids = [0] + [_follow(store, job, f"{n}번째 일") for n in range(2, 14)]
    bundles = [_bind(store, job, turn_id, name=f"묶음 {n}") for n, turn_id in enumerate(turn_ids, 1)]

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.selected_id == job and "bundle-more" in _bundle_ids(app))
            assert len([key for key in _bundle_ids(app) if key and key.startswith("b:")]) == 12
            assert f"b:{bundles[0]}" not in _bundle_ids(app)
            _panel(app).focus()
            _highlight(app, "bundle-more")
            await pilot.press("enter")
            await pilot.pause(1.5)  # 주기 갱신 후에도 13번째 묶음을 계속 본다
            oldest = f"b:{bundles[0]}"
            assert "bundle-more" not in _bundle_ids(app) and oldest in _bundle_ids(app)
            assert _panel(app).highlighted_option.id == oldest

            _highlight(app, oldest)
            await pilot.press("ctrl+b")
            await pilot.pause()
            assert _menu_text(app, "bundle_rename") == "묶음 이름 바꾸기 (묶음 1)"
            assert _menu_text(app, "bundle_view") == "묶음 전체 보기 (묶음 1)"
            await pilot.press("escape")
            await pilot.pause()
            _highlight(app, oldest)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, BundleScreen) and app.screen._bundle.id == bundles[0]

    try:
        _run(scenario())
    finally:
        store.close()


def test_차례가_많은_묶음은_최근_6개와_이_채팅의_3개만_보이고_마지막_줄까지_스크롤된다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)
    mine = _reported(store, repo, "옛 채팅의 첫 요청")
    mine_turns = [0] + [_follow(store, mine, f"옛 채팅 {n}번째") for n in range(2, 6)]
    busy = _reported(store, repo, "바쁜 채팅의 첫 요청")
    busy_turns = [0] + [_follow(store, busy, f"바쁜 채팅 {n}번째") for n in range(2, 101)]
    bundle = store.assign_turns(busy, [TurnAnswer(t, None, "큰 일") for t in busy_turns], expect={})[0]
    store.assign_turns(mine, [TurnAnswer(t, bundle) for t in mine_turns], expect={})

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: _keys(app) == [str(busy), str(mine)])
            await _to_key(pilot, app, str(mine))
            assert _bundle_ids(app) == [f"b:{bundle}"] and _bundle_texts(app)[0] == "1. 큰 일"   # 차례가 많아도 한 줄
            await _expand(pilot, app, bundle)
            ids = _bundle_ids(app)
            shown = [i for i in ids if i and i.startswith("t:")]
            assert len(shown) == 9                                     # 펼치면 묶음 전체의 최근 6 + 이 채팅의 3
            assert sum(i.startswith(f"t:{mine}:") for i in shown) == 3
            assert _bundle_texts(app)[0].startswith("▼ 1. 큰 일  · 차례 105 · 채팅 #2 #1")
            assert _bundle_texts(app)[ids.index(f"more:{bundle}")] == "  … 이전 차례 96개 — Enter 로 전체 보기"
            panel = _panel(app)
            panel.focus()
            await pilot.pause()
            await pilot.press("end")                                   # 마지막 줄까지 잘리지 않고 닿는다
            await pilot.pause()
            # 고를 수 있는 마지막 줄 아래에는 빈 줄 하나뿐이다
            assert panel.highlighted_option.id == f"more:{bundle}" and panel.scroll_y >= panel.max_scroll_y - 1 > 0
            await pilot.press("home")
            _highlight(app, f"t:{mine}:{mine_turns[2]}")
            await pilot.pause()
            before = panel.scroll_y
            assert before > 0
            store.rename_bundle(bundle, "아주 큰 일")                  # 다시 그려도 보던 자리 그대로 (맨 위로 튀지 않는다)
            await _until(pilot, lambda: _bundle_texts(app)[0].startswith("▼ 1. 아주 큰 일"))
            assert panel.scroll_y == before and panel.highlighted_option.id == f"t:{mine}:{mine_turns[2]}"

    try:
        _run(scenario())
    finally:
        store.close()


def test_패널에_포커스가_있으면_패널이_3배로_커지고_변경_사항에_포커스면_그쪽이_커진다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)
    _reported(store, repo, "첫 작업")

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.selected_id == 1)
            await pilot.pause()
            panel, diff = _panel(app), app.query_one("#diff-pane")
            assert abs(panel.outer_size.height - diff.outer_size.height) <= 1          # 평소에는 반씩
            panel.focus()
            await pilot.pause()
            assert panel.outer_size.height >= 2.5 * diff.outer_size.height              # 패널 포커스: 3fr : 1fr
            await pilot.press("ctrl+b")                                                 # 메뉴가 떠도 그대로
            await pilot.pause()
            assert app.query_one("#right").has_class("-bundle-focus")
            await pilot.press("escape")
            await pilot.pause()
            diff.focus()
            await pilot.pause()
            assert diff.outer_size.height >= 2.5 * panel.outer_size.height              # 변경 사항 포커스: 그쪽이 3fr
            app.query_one("#jobs", DataTable).focus()
            await pilot.pause()
            assert abs(panel.outer_size.height - diff.outer_size.height) <= 1

    try:
        _run(scenario())
    finally:
        store.close()


def test_제목_줄에서_Enter하면_전체_보기가_열리고_전문을_보이며_Enter로_그_채팅으로_간다(env, repo, monkeypatch):
    copied = []
    monkeypatch.setattr(ColidayApp, "copy_to_clipboard", lambda self, text: copied.append(text))
    cfg = config.load()
    store = Store(cfg.db_path)
    question = "\n".join(f"긴 요청 [{n}] 줄" for n in range(30))
    report = "## 결과\n" + "\n".join(f"- 보고 {n}번째 줄" for n in range(40))
    other = _reported(store, repo, question, report, members=("architect",))
    mine = _reported(store, repo, "첫 요청")
    later = [0] + [_follow(store, mine, f"이어서 {n}") for n in range(2, 25)]
    bundle = _bind(store, other, 0, name="긴 일", line="긴 보고를 남김")
    store.assign_turns(mine, [TurnAnswer(t, bundle) for t in later], expect={})

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.selected_id == mine and f"b:{bundle}" in _bundle_ids(app))
            panel = _panel(app)
            panel.focus()
            _highlight(app, f"b:{bundle}")
            await pilot.press("enter")                               # 제목 줄: 전체 보기
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, BundleScreen)
            blocks = list(screen.query(TurnBlock))
            assert len(blocks) == 20                                  # 최근 20 차례부터
            assert [b.view.turn.index for b in blocks[-2:]] == [23, 24]   # 위가 오래된 것 (대화 순서)
            assert screen.focused is blocks[-1]
            more = screen.query_one("#bv-more")
            assert more.display and "남은 5개" in str(more.label)
            more.press()                                              # 이전 차례 더 보기
            await pilot.pause()
            blocks = list(screen.query(TurnBlock))
            assert len(blocks) == 25 and not more.display
            first = blocks[0]
            assert first.view.turn.job_id == other
            ask = str(first.query_one(".ask", Static).content)
            assert ask.startswith(f"나 #{other} · 1번째 요청") and "긴 요청 [29] 줄" in ask      # 질문 전문 (대괄호 그대로)
            assert first.query_one(".ask").styles.border_left[0] == "thick"                    # 질문은 왼쪽 굵은 막대
            assert str(first.query_one(".ai-head", Static).content).startswith("◆ AI ✓ 완료 · 팀원 architect")
            assert first.query_one(Markdown).source.endswith("- 보고 39번째 줄")               # 보고 전문 (Markdown)

            await pilot.press("ctrl+y")                               # 전체 복사
            await pilot.pause()
            assert copied and copied[-1].startswith("■ 긴 일") and f"▌나 #{other} · 1번째 요청" in copied[-1]
            assert "    ◆ AI ✓ 완료" in copied[-1] and "보고 39번째 줄" in copied[-1]

            first.focus()
            await pilot.press("down")                                 # ↓ 다음 차례
            await pilot.pause()
            assert screen.focused is blocks[1]
            await pilot.press("up", "up")                             # 맨 위에서 ↑ 는 그대로
            await pilot.pause()
            assert screen.focused is blocks[0]
            await pilot.press("enter")                                # 그 차례의 채팅으로 (다른 채팅)
            await pilot.pause()
            assert len(app.screen_stack) == 1 and app.selected_id == other
            assert panel.highlighted_option.id == f"t:{other}:0"

            _highlight(app, f"more:{bundle}")                         # '이전 차례 N개' 줄도 전체 보기
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, BundleScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == 1 and app.focused is panel

    try:
        _run(scenario())
    finally:
        store.close()


def test_전체_복사는_화면에_불러오지_않은_이전_페이지까지_복사한다(env, repo, monkeypatch):
    copied = []
    monkeypatch.setattr(ColidayApp, "copy_to_clipboard", lambda self, text: copied.append(text))
    cfg = config.load()
    store = Store(cfg.db_path)
    job = _reported(store, repo, "가장 오래된 요청", "가장 오래된 보고")
    turn_ids = [0] + [
        _follow(store, job, f"{n}번째 요청", f"{n}번째 보고") for n in range(2, 24)
    ]
    bundle = store.assign_turns(
        job, [TurnAnswer(turn_id, None, "전체 복사 묶음") for turn_id in turn_ids], expect={},
    )
    bundle_id = bundle[0]

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.selected_id == job and f"b:{bundle_id}" in _bundle_ids(app))
            _panel(app).focus()
            _highlight(app, f"b:{bundle_id}")
            await pilot.press("enter")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, BundleScreen)
            assert len(list(screen.query(TurnBlock))) == 20 and screen.query_one("#bv-more").display
            await pilot.press("ctrl+y")
            await pilot.pause()
            assert len(list(screen.query(TurnBlock))) == 20  # 복사를 위해 화면 위젯은 늘리지 않는다
            assert copied and "가장 오래된 요청" in copied[-1] and "23번째 보고" in copied[-1]
            assert copied[-1].count("▌나 #") == 23
            assert _notified(app, "차례 23개를 복사했습니다")

    try:
        _run(scenario())
    finally:
        store.close()


def test_전체_보기에서_고른_미리보기_밖_차례가_패널과_ctrl_b의_정확한_대상이_된다(env, repo):
    from textual.widgets import Select

    cfg = config.load()
    store = Store(cfg.db_path)
    destination_job = _reported(store, repo, "옮길 묶음의 차례")
    destination = _bind(store, destination_job, 0, name="옮길 묶음")
    job = _reported(store, repo, "가장 오래된 요청", "가장 오래된 보고")
    turn_ids = [0] + [_follow(store, job, f"{n}번째 요청", f"{n}번째 보고") for n in range(2, 24)]
    bundle = store.assign_turns(
        job, [TurnAnswer(turn_id, None, "정확한 포커스 묶음") for turn_id in turn_ids], expect={},
    )[0]

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.selected_id == job and f"b:{bundle}" in _bundle_ids(app))
            _panel(app).focus()
            _highlight(app, f"b:{bundle}")
            await pilot.press("enter")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, BundleScreen)
            screen.query_one("#bv-more").press()
            await pilot.pause()
            oldest = list(screen.query(TurnBlock))[0]
            assert oldest.view.turn.turn_id == 0
            oldest.focus()
            await pilot.press("enter")
            await pilot.pause(1.5)  # 주기 갱신 뒤에도 미리보기 밖의 정확한 차례를 유지한다
            assert app.screen is app.screen_stack[0]
            assert _panel(app).highlighted_option.id == f"t:{job}:0"
            assert f"t:{job}:0" in _bundle_ids(app)

            await pilot.press("ctrl+b")
            await pilot.pause()
            assert f"(#{job} 1번째: '가장 오래된 요청')" in _menu_text(app, "turn_move")
            await _choose(pilot, app, "turn_move")
            app.screen.query_one("#topic", Select).value = destination
            await pilot.pause()
            await pilot.click("#save")
            await pilot.pause()
            assert store.turn_links([job])[(job, 0)].bundle_id == destination
            assert _panel(app).highlighted_option.id == f"t:{job}:0"

    try:
        _run(scenario())
    finally:
        store.close()


def test_전체_보기는_연_시점의_멤버십과_내용을_더_보기와_복사에서_공유한다(env, repo, monkeypatch):
    copied = []
    monkeypatch.setattr(ColidayApp, "copy_to_clipboard", lambda self, text: copied.append(text))
    cfg = config.load()
    store = Store(cfg.db_path)
    job = _reported(store, repo, "스냅샷의 가장 오래된 요청", "스냅샷의 가장 오래된 보고")
    turn_ids = [0] + [_follow(store, job, f"{n}번째 요청", f"{n}번째 보고") for n in range(2, 24)]
    bundle = store.assign_turns(
        job, [TurnAnswer(turn_id, None, "스냅샷 묶음") for turn_id in turn_ids], expect={},
    )[0]
    destination_job = _reported(store, repo, "옮길 곳")
    destination = _bind(store, destination_job, 0, name="다른 묶음")
    other_terminal = Store(cfg.db_path)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.selected_id == destination_job)
            await _to_key(pilot, app, str(job))
            _panel(app).focus()
            _highlight(app, f"b:{bundle}")
            await pilot.press("enter")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, BundleScreen)
            assert "차례 23" in str(screen.query_one("#bv-title", Label).content)

            assert other_terminal.move_turns(job, [0], destination) == destination
            await pilot.press("ctrl+y")
            await pilot.pause()
            assert copied[-1].count("▌나 #") == 23
            assert "스냅샷의 가장 오래된 요청" in copied[-1]
            assert _notified(app, "차례 23개를 복사했습니다")

            screen.query_one("#bv-more").press()
            await pilot.pause()
            blocks = list(screen.query(TurnBlock))
            assert len(blocks) == 23
            assert blocks[0].view.turn.question == "스냅샷의 가장 오래된 요청"

    try:
        _run(scenario())
    finally:
        other_terminal.close()
        store.close()


def test_ctrl_b_메뉴로_차례를_옮기고_채팅의_차례를_모두_옮기고_묶음_이름을_바꾼다(env, repo, monkeypatch):
    monkeypatch.setattr(topics, "spawn_pending", lambda cfg: pytest.fail("묶기 프로세스를 띄우면 안 된다"))
    cfg = config.load()
    store = Store(cfg.db_path)
    a, b, t2, t3, cancel, session = _two_bundles(store, repo)
    from textual.widgets import Select

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.selected_id == b and f"b:{session}" in _bundle_ids(app))
            _highlight(app, f"b:{cancel}")
            await pilot.press("ctrl+b")                               # 하이라이트가 제목 줄이면: 마지막 끝난 차례, 그 묶음
            await pilot.pause()
            menu = _menu_ids(app)
            assert menu[-5:] == ["turn_move", "chat_move", "bundle_rename", "bundle_view", "bundle_sort"]   # 그룹·파생 항목 뒤
            assert f"(#{b} 3번째: '취소 주문은 회색으로" in _menu_text(app, "turn_move")
            assert _menu_text(app, "chat_move") == f"이 채팅의 차례 모두 옮기기 → … (#{b} · 차례 3)"
            assert _menu_text(app, "bundle_rename") == "묶음 이름 바꾸기 (주문 취소)"
            assert app.screen.query_one(OptionList).get_option("bundle_sort").disabled   # 안 묶인 차례가 없다
            await pilot.press("escape")
            await pilot.pause()

            await _expand(pilot, app, cancel)                         # 차례 줄은 펼친 묶음에만 있다
            _highlight(app, f"t:{b}:0")                               # 하이라이트한 차례를 옮긴다
            await pilot.press("ctrl+b")
            await pilot.pause()
            assert f"(#{b} 1번째: '환불 계산 고쳐 줘')" in _menu_text(app, "turn_move")
            await _choose(pilot, app, "turn_move")
            assert isinstance(app.screen, TopicScreen)
            select = app.screen.query_one("#topic", Select)
            assert select.value == cancel and not app.screen.query_one("#name").display
            select.value = session
            await pilot.pause()
            await pilot.click("#save")
            await pilot.pause()
            link = store.turn_links()[(b, 0)]
            assert (link.bundle_id, link.manual, link.source) == (session, True, "manual")
            await _until(pilot, lambda: _notified(app, f"#{b} 1번째 차례 을(를) 묶음 '세션' (으)로 옮겼습니다"))

            await pilot.press("ctrl+b")                               # 새 묶음으로 떼어 내기
            await pilot.pause()
            await _choose(pilot, app, "turn_move")
            app.screen.query_one("#topic", Select).value = 0
            await pilot.pause()
            name = app.screen.query_one("#name", Input)
            assert name.display
            name.focus()
            name.value = "환불 [계산]"
            await pilot.press("enter")
            await pilot.pause()
            new = store.turn_links()[(b, 0)].bundle_id
            assert new not in (cancel, session) and store.bundle(new).manual_name
            await _until(pilot, lambda: any(t.startswith("▼ 1. 환불 [계산]") for t in _bundle_texts(app)))   # 옮긴 곳은 펼쳐 보인다 (대괄호 그대로)

            await pilot.press("ctrl+b")                               # 이 채팅의 차례를 모두 한 묶음으로
            await pilot.pause()
            await _choose(pilot, app, "chat_move")
            app.screen.query_one("#topic", Select).value = cancel
            await pilot.pause()
            await pilot.click("#save")
            await pilot.pause()
            assert {k: v.bundle_id for k, v in store.turn_links([b]).items()} == {(b, 0): cancel, (b, t2): cancel, (b, t3): cancel}
            assert store.bundle(session) is None and store.bundle(new) is None      # 비게 된 묶음은 사라진다
            await _until(pilot, lambda: str(_panel(app).border_title) == f"묶음 · #{b} 의 일 1개")

            await pilot.press("ctrl+b")                               # 묶음 이름 바꾸기
            await pilot.pause()
            await _choose(pilot, app, "bundle_rename")
            assert isinstance(app.screen, TopicScreen) and app.screen.query_one("#name", Input).value == "주문 취소"
            app.screen.query_one("#name", Input).value = "주문 [취소]"
            await pilot.press("enter")
            await pilot.pause()
            assert (store.bundle(cancel).name, store.bundle(cancel).manual_name) == ("주문 [취소]", True)
            await _until(pilot, lambda: _bundle_texts(app)[0].startswith("▼ 1. 주문 [취소]"))

            await pilot.press("ctrl+b")                               # 묶음 전체 보기
            await pilot.pause()
            await _choose(pilot, app, "bundle_view")
            assert isinstance(app.screen, BundleScreen)
            await pilot.press("escape")

    try:
        _run(scenario())
    finally:
        store.close()


def test_ctrl_b_메뉴의_안_묶인_차례_묶기는_호출_수를_보이고_확인_후_spawn_pending을_부른다(env, repo, monkeypatch):
    calls = []
    monkeypatch.setattr(topics, "spawn_pending", lambda cfg: calls.append(cfg))
    cfg = config.load()
    store = Store(cfg.db_path)
    one = _reported(store, repo, "첫 작업")
    two = _reported(store, repo, "둘째 작업")
    _follow(store, two, "둘째의 둘째")
    done = _reported(store, repo, "묶인 작업")
    _bind(store, done, 0, name="혼자")
    running = store.create_job(prompt="실행 중", repo=str(repo), workdir=str(repo), model=None)   # 끝나지 않은 채팅은 빼고 센다

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: str(running.id) in _keys(app))
            await _to_key(pilot, app, str(one))
            await pilot.press("ctrl+b")
            await pilot.pause()
            assert _menu_text(app, "bundle_sort") == "안 묶인 차례 자동으로 묶기 (채팅 2 · 차례 3 · 호출 약 2번)"
            await _choose(pilot, app, "bundle_sort")
            assert isinstance(app.screen, ConfirmScreen)
            assert "안 묶인 끝난 차례 3개(채팅 2개)" in app.screen._question and "모델 호출 약 2번" in app.screen._question
            await pilot.press("n")
            await pilot.pause()
            assert calls == []
            await pilot.press("ctrl+b")
            await pilot.pause()
            await _choose(pilot, app, "bundle_sort")
            await pilot.press("y")
            await pilot.pause()
            assert calls == [cfg] and _notified(app, "묶음 패널에 나타납니다")

    try:
        _run(scenario())
    finally:
        store.close()


def test_작업_목록과_패널의_방향키_ctrl_e_ctrl_x는_묶음을_바꾸지_않는다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)
    for prompt in ("첫 작업", "둘째 작업"):
        _reported(store, repo, prompt)
    bundle = _bind(store, 1, 0, name="주문 취소")
    _bind(store, 2, 0, bundle)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: f"b:{bundle}" in _bundle_ids(app))
            await _expand(pilot, app, bundle)
            await _to_key(pilot, app, "2")
            assert "t:1:0" in _bundle_ids(app)
            drawn = list(_panel(app)._options)
            for key in ("ctrl+e", "ctrl+x", "left", "right"):          # 작업 목록: 묶음 키가 없다
                await pilot.press(key)
                await pilot.pause()
                assert len(app.screen_stack) == 1
            assert _panel(app)._options == drawn                       # →/← 는 패널에서만 펼치고 접는다
            panel = _panel(app)
            panel.focus()
            _highlight(app, "t:1:0")
            await pilot.press("ctrl+x")                                # 패널: 그룹 줄이 아니면 안내만
            await pilot.pause()
            assert _notified(app, "그룹에서 뺄 작업을") and store.turn_links()[(1, 0)].bundle_id == bundle
            await pilot.press("ctrl+e")                                # 패널: 그룹 이름 (그룹이 없으면 안내)
            await pilot.pause()
            assert len(app.screen_stack) == 1 and _notified(app, "그룹에 들어 있지 않습니다")
            assert [x.name for x in store.bundles()] == ["주문 취소"]

    try:
        _run(scenario())
    finally:
        store.close()


def test_작업_목록에는_묶음_표시가_없다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)
    for prompt in ("첫 작업", "둘째 작업", "셋째 작업"):
        _reported(store, repo, prompt)
    bundle = _bind(store, 1, 0, name="주문 취소", line="한 일")
    _bind(store, 3, 0, bundle)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: _keys(app) == ["3", "2", "1"])     # 순서는 그대로 (묶음으로 모으지 않는다)
            for key in ("3", "2", "1"):
                assert "주문 취소" not in _cell(app, key) and "한 일" not in _cell(app, key)
            assert _cell(app, "1") == "첫 작업"

    try:
        _run(scenario())
    finally:
        store.close()


def test_다른_터미널이_분류하면_다음_tick에_패널이_갱신되고_하이라이트가_유지된다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)
    for prompt in ("첫 작업", "둘째 작업", "셋째 작업"):
        _reported(store, repo, prompt)
    store.set_group([3, 1], store.create_group("결제", project=str(repo.resolve())).id)

    async def scenario():
        app = _app(cfg, store, repo)
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: "m:1" in _bundle_ids(app) and app.selected_id == 3)
            assert _bundle_ids(app)[0] == "u:3:0"
            _highlight(app, "m:1")
            bundle = _bind(store, 3, 0, name="주문", line="셋째에서 한 일")   # worker 가 분류를 마쳤다
            _bind(store, 2, 0, bundle)
            await _until(pilot, lambda: f"b:{bundle}" in _bundle_ids(app))
            assert _panel(app).highlighted_option.id == "m:1" and "u:3:0" not in _bundle_ids(app)
            assert _bundle_texts(app)[_bundle_ids(app).index(f"b:{bundle}")] == "1. 주문"   # 새 묶음도 제목 한 줄
            await _expand(pilot, app, bundle)
            assert "셋째에서 한 일" in _bundle_texts(app)[_bundle_ids(app).index("t:3:0")]
            assert "t:2:0" in _bundle_ids(app)

    try:
        _run(scenario())
    finally:
        store.close()


def test_옛_버전이_채팅_단위로_묶은_것은_화면이_차례_단위로_옮겨_보인다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)
    a = _reported(store, repo, "옛 작업 하나")
    b = _reported(store, repo, "옛 작업 둘")
    # 옛 버전(worker·TUI)이 쓰던 표에 직접 쓴다 (새 코드는 이 표를 읽기만 한다)
    store._conn.execute("INSERT INTO topics(repo, name, summary, manual_name) VALUES (?, '옛 묶음', '옛 요약', 0)", (str(repo),))
    store._conn.executemany("INSERT INTO job_topics(job_id, topic_id, manual, line) VALUES (?, 1, 0, ?)",
                            [(a, "하나에서 한 일"), (b, "")])

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.selected_id == b and "1. 옛 묶음" in _bundle_texts(app))
            await _expand(pilot, app, store.turn_links([b])[(b, 0)].bundle_id)
            assert [i for i in _bundle_ids(app) if i and i.startswith("t:")] == [f"t:{b}:0", f"t:{a}:0"]
            assert "하나에서 한 일" in _bundle_texts(app)[_bundle_ids(app).index(f"t:{a}:0")]
            assert store._conn.execute("SELECT COUNT(*) FROM job_topics").fetchone()[0] == 2   # 옛 표는 그대로

    try:
        _run(scenario())
    finally:
        store.close()


def test_메뉴를_연_사이_묶음이_지워지면_이름_바꾸기는_알리기만_한다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)
    for prompt in ("첫 작업", "둘째 작업", "셋째 작업"):
        _reported(store, repo, prompt)
    bundle = _bind(store, 1, 0, name="주문 취소")
    _bind(store, 2, 0, bundle)

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: "1" in _keys(app))
            await _open_menu_on(pilot, app, "2")
            assert store.delete_job(1) and store.delete_job(2)          # 다른 터미널에서 구성 채팅을 모두 지웠다
            assert store.bundle(bundle) is None
            await _choose(pilot, app, "bundle_rename")
            assert len(app.screen_stack) == 1 and _notified(app, "묶음 '주문 취소' 이(가) 그사이 없어져")

            other = _bind(store, 3, 0, name="혼자")
            await _until(pilot, lambda: f"b:{other}" in _bundle_ids(app))
            await pilot.press("ctrl+b")                                 # 이름 창이 열린 사이 없어져도 만들지 않는다
            await pilot.pause()
            await _choose(pilot, app, "bundle_rename")
            assert isinstance(app.screen, TopicScreen)
            store._conn.execute("DELETE FROM turn_bundles")
            store._conn.execute("DELETE FROM bundles")
            app.screen.query_one("#name", Input).value = "새 이름"
            await pilot.press("enter")
            await pilot.pause()
            assert store.bundles() == [] and _notified(app, "묶음 '혼자' 이(가) 그사이 없어져")

    try:
        _run(scenario())
    finally:
        store.close()


def test_타임라인에서도_내_질문과_AI_보고가_같은_기호로_구분된다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)
    job = _reported(store, repo, "첫 요청 [v1]", "## 첫 보고")
    _follow(store, job, "이어서 둘째 요청", "둘째 보고")

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.selected_id == job and "둘째 보고" in _timeline_text(app))
            timeline = app.query_one("#timeline")
            asks = list(timeline.query(".ask"))
            assert [str(w.content).split("\n")[1] for w in asks] == ["첫 요청 [v1]", "이어서 둘째 요청"]
            assert all(str(w.content).startswith("나 · ") and w.styles.border_left[0] == "thick" for w in asks)
            heads = [str(w.content) for w in timeline.query(".ai-head")]
            assert len(heads) == 2 and all(h.startswith("◆ AI 보고 (") for h in heads)
            reports = list(timeline.query(Markdown))
            assert [m.source for m in reports] == ["## 첫 보고", "둘째 보고"] and all(m.has_class("ai-report") for m in reports)
            assert all(m.styles.padding.left == 4 for m in reports)              # AI 보고는 들여쓴다
            assert "사용자" not in _timeline_text(app)                           # 이어서 시킨 요청은 로그 줄이 아니라 질문 블록

    try:
        _run(scenario())
    finally:
        store.close()



# ── 이전 리뷰 [P3] 회귀 ─────────────────────────────────────────────


def test_메뉴가_열린_사이_선택이_사라지면_메뉴를_닫은_뒤_작업_목록으로_간다(env, repo):
    cfg = config.load()
    store = Store(cfg.db_path)
    _done(store, repo, "하나뿐인 작업")

    async def scenario():
        app = ColidayApp(cfg, store, default_dir=str(repo))
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.selected_id == 1)
            await _open_menu_on(pilot, app, "1")
            assert app.screen_stack[0].focused is _panel(app)
            assert store.delete_job(1)                                  # 다른 터미널에서 지웠다: 패널이 숨는다
            await _until(pilot, lambda: app.selected_id is None)
            assert not _panel(app).has_class("-shown")
            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == 1 and app.focused is app.query_one("#jobs", DataTable)

    try:
        _run(scenario())
    finally:
        store.close()


def test_다른_프로젝트의_작업이_최근_100개_밖이면_옮겼다고만_하지_않는다(env, repo, tmp_path):
    other = _second_repo(tmp_path)
    cfg = config.load()
    store = Store(cfg.db_path)
    jobs.add_project(cfg, store, other)
    far = _job_in(store, other, "오래된 다른 프로젝트 작업")
    for i in range(100):
        _job_in(store, other, f"새 작업 {i}")
    mine = _job_in(store, repo, "이 프로젝트 작업", parent_id=far.id)

    async def scenario():
        app = _app(cfg, store, repo)
        async with app.run_test(size=(160, 45)) as pilot:
            await _until(pilot, lambda: app.selected_id == mine.id and f"p:{far.id}" in _bundle_ids(app))
            panel = _panel(app)
            panel.focus()
            _highlight(app, f"p:{far.id}")
            await pilot.press("enter")
            await pilot.pause()
            assert app.scope == str(other.resolve()) and app.selected_id != far.id
            assert _notified(app, f"#{far.id} 은(는) 최근 100개 밖이라 목록에 없습니다")
            assert not _notified(app, "프로젝트 other 로 옮겼습니다 (")

    try:
        _run(scenario())
    finally:
        store.close()
