"""가짜 CLI로 팀장의 대기 설정과 종료 처리를 검증한다."""

import json
import os
import shlex
import subprocess
import sys

import pytest

from coliday import members, team, worker
from coliday.config import Config
from coliday.store import Store

WAIT_ENV = "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"
WARNING = (
    'Background tasks still running {elapsed} after the last turn (subagent "단위 C TUI 구현"); '
    "stopping them. Set CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 to wait indefinitely."
)
FAKE_CLI = """
import json
import os
import sys
from pathlib import Path

if sys.argv[1:] == ["--version"]:
    print("2.1.331 (Claude Code)")
    sys.exit(0)

sys.stdin.read()
Path(call_path).write_text(json.dumps({
    "args": sys.argv[1:],
    "wait_ms": os.environ.get("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"),
    "java_options": os.environ.get("JAVA_TOOL_OPTIONS"),
}))
if sys.argv[1] == "-p":
    print(json.dumps({"type": "system", "subtype": "init", "session_id": "fake-session"}))
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "partial report"}))
sys.stderr.write(os.environ.get("FAKE_LEAD_STDERR", ""))
"""


@pytest.fixture
def setup(tmp_path, monkeypatch):
    cli = tmp_path / "fake-lead"
    script = f"call_path = {str(tmp_path / 'call.json')!r}\n{FAKE_CLI}"
    cli.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(script)} "$@"\n'
    )
    cli.chmod(0o755)
    monkeypatch.delenv(WAIT_ENV, raising=False)
    monkeypatch.delenv("FAKE_LEAD_STDERR", raising=False)
    # 이 가짜 CLI 는 부를 때마다 call.json 을 덮어쓴다: 작업이 끝난 뒤의 자동 묶기 호출은 막아 팀장 실행 기록만 남긴다.
    monkeypatch.setattr(worker.topics, "classify_quietly", lambda *args, **kwargs: None)
    cfg = Config(tmp_path / "home", str(cli), "auto", codex_bin=str(cli))
    store = Store(cfg.db_path)
    yield cfg, store, tmp_path
    store.close()


def test_fake_cli_version_exits_with_stdin_open(setup, monkeypatch):
    cfg, _, workdir = setup
    monkeypatch.setenv("FAKE_LEAD_STDERR", "job output only")
    with subprocess.Popen(
        [cfg.claude_bin, "--version"],
        cwd=workdir,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as proc:
        assert proc.wait(timeout=5) == 0
        stdout, stderr = proc.communicate()

    assert stdout.strip() == "2.1.331 (Claude Code)"
    assert stderr == ""
    assert not (workdir / "call.json").exists()


def test_fake_cli_records_under_fixture_path_from_another_cwd(setup):
    cfg, _, fixture_path = setup
    cwd = fixture_path / "other-cwd"
    cwd.mkdir()

    subprocess.run(
        [cfg.claude_bin, "-p"], cwd=cwd, input="", text=True, check=True, timeout=5
    )

    assert json.loads((fixture_path / "call.json").read_text())["args"] == ["-p"]
    assert not (cwd / "call.json").exists()


@pytest.mark.parametrize(
    ("model", "explicit", "expected"),
    [
        (None, None, "3600000"),
        (None, "0", "0"),
        (None, "900000", "900000"),
        ("codex", None, None),
    ],
)
def test_lead_wait_environment(setup, monkeypatch, model, explicit, expected):
    cfg, store, workdir = setup
    if explicit is not None:
        monkeypatch.setenv(WAIT_ENV, explicit)
    job = store.create_job(
        prompt="test", repo=str(workdir), workdir=str(workdir), model=model
    )
    store.set_allow_env(job.id, True)  # agent_env가 None이면 부모 환경을 이어받는다

    assert worker.run_job(cfg, store, job.id) == 0
    assert json.loads((workdir / "call.json").read_text())["wait_ms"] == expected
    assert os.environ.get(WAIT_ENV) == explicit


@pytest.mark.parametrize("configured", [None, "1200000"])
def test_lead_preserves_agent_env(setup, monkeypatch, configured):
    cfg, store, workdir = setup
    agent_env = {**os.environ, "JAVA_TOOL_OPTIONS": "-Djava.awt.headless=true"}
    if configured is not None:
        agent_env[WAIT_ENV] = configured
    monkeypatch.setattr(members, "agent_env", lambda guard: agent_env)
    job = store.create_job(
        prompt="test", repo=str(workdir), workdir=str(workdir), model=None
    )

    assert worker.run_job(cfg, store, job.id) == 0
    call = json.loads((workdir / "call.json").read_text())
    assert call["java_options"] == "-Djava.awt.headless=true"
    assert call["wait_ms"] == (configured or "3600000")
    assert agent_env.get(WAIT_ENV) == configured


def test_claude_warning_fails_only_the_turn_that_emitted_it(setup, monkeypatch):
    cfg, store, workdir = setup
    job = store.create_job(
        prompt="test", repo=str(workdir), workdir=str(workdir), model=None
    )
    store.set_allow_env(job.id, True)
    stderr = cfg.job_dir(job.id) / "stderr.log"
    original = WARNING.format(elapsed="10m")
    monkeypatch.setenv("FAKE_LEAD_STDERR", original + "\n" + "후속 로그\n" * 100)

    assert worker.run_job(cfg, store, job.id) == 1
    failed = store.get_job(job.id)
    assert failed.status == "failed"
    assert failed.phase == "Claude 백그라운드 대기 한도 초과"
    assert failed.result == "partial report"
    assert failed.session_id == "fake-session"

    assert store.requeue_job(job.id, "continue")
    monkeypatch.setenv("FAKE_LEAD_STDERR", "Waiting for background work to finish\n")
    assert worker.run_job(cfg, store, job.id) == 0
    assert store.get_job(job.id).status == "succeeded"
    args = json.loads((workdir / "call.json").read_text())["args"]
    assert args[args.index("--resume") + 1] == "fake-session"
    assert original in stderr.read_text(encoding="utf-8")

    assert store.requeue_job(job.id, "continue again")
    current = WARNING.format(elapsed="1h")
    monkeypatch.setenv("FAKE_LEAD_STDERR", current)
    assert worker.run_job(cfg, store, job.id) == 1
    assert store.get_job(job.id).status == "failed"
    errors = [e.message for e in store.events(job.id) if e.kind == "error"]
    assert len(errors) == 2
    assert "종료 코드 0" in errors[0] and original in errors[0]
    assert "종료 코드 0" in errors[1] and current in errors[1]


def test_lead_wait_instructions_are_included_with_custom_prompts():
    claude = team.lead_prompt(template="사용자 지시문")
    assert "결과를 받은 뒤 다음 단계" in claude
    assert "모든 팀원의 결과를 받은 뒤에만 팀장 턴을 끝낸다" in claude
    codex = team.codex_lead_prompt({}, "coliday _agent 1", template="사용자 지시문")
    assert "그 ID로 기존 세션을 계속 기다리고" in codex
