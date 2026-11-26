from __future__ import annotations

import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

import scripts.repeat_tui_tests as repeat
from scripts.repeat_tui_tests import run_repetitions


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _wait_for_log_markers(output: Path, *markers: tuple[str, str]) -> Path | None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        candidates = list(output.glob("tui-repeat-*/run-01"))
        if candidates:
            log_paths = [candidates[0] / filename for filename, _ in markers]
            if all(path.exists() for path in log_paths) and all(
                marker in path.read_text(errors="replace")
                for path, (_, marker) in zip(log_paths, markers, strict=True)
            ):
                return candidates[0]
        time.sleep(0.02)
    return None


def test_repetitions_keep_separate_logs_and_real_exit_codes(tmp_path):
    fake = tmp_path / "fake_test.py"
    fake.write_text(
        "import sys\n"
        "print('fake stdout', flush=True)\n"
        "print('fake stderr', file=sys.stderr, flush=True)\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )
    stdout = io.BytesIO()
    stderr = io.BytesIO()

    first, caller_exit_code = run_repetitions(
        tmp_path / "results",
        2,
        [sys.executable, str(fake)],
        cwd=tmp_path,
        display_stdout=stdout,
        display_stderr=stderr,
    )
    second, _ = run_repetitions(
        tmp_path / "results",
        1,
        [sys.executable, str(fake)],
        cwd=tmp_path,
        display_stdout=io.BytesIO(),
        display_stderr=io.BytesIO(),
    )

    assert caller_exit_code == 1
    assert first != second
    assert b"fake stdout" not in stdout.getvalue()
    assert stdout.getvalue().count(b"started:") == 2
    assert stdout.getvalue().count(b"finished: exit 7") == 2
    assert stderr.getvalue() == b""
    assert _read_json(first / "execution.json")["exit_codes"] == [7, 7]
    for number in (1, 2):
        run_dir = first / f"run-{number:02d}"
        assert _read_json(run_dir / "started.json")["status"] == "running"
        assert _read_json(run_dir / "result.json")["exit_code"] == 7
        assert (run_dir / "stdout.log").read_text() == "fake stdout\n"
        assert (run_dir / "stderr.log").read_text() == "fake stderr\n"


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX pipes")
def test_closed_display_pipe_does_not_block_large_child_output(tmp_path):
    fake = tmp_path / "large_output.py"
    fake.write_text(
        "import sys\n"
        "sys.stdout.buffer.write(b'x' * (1024 * 1024))\n"
        "sys.stdout.buffer.flush()\n"
        "print('stderr complete', file=sys.stderr, flush=True)\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )
    output = tmp_path / "results"
    runner = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "from scripts.repeat_tui_tests import run_repetitions\n"
        "read_fd, write_fd = os.pipe()\n"
        "os.close(read_fd)\n"
        "with os.fdopen(write_fd, 'wb', buffering=0) as display:\n"
        f"    _, code = run_repetitions(Path({str(output)!r}), 1, "
        f"[sys.executable, {str(fake)!r}], cwd=Path({str(tmp_path)!r}), "
        "display_stdout=display, display_stderr=display)\n"
        "raise SystemExit(code)\n"
    )

    process = subprocess.run(
        [sys.executable, "-c", runner],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        timeout=10,
    )

    assert process.returncode == 1
    run_dir = next(output.glob("tui-repeat-*/run-01"))
    assert _read_json(run_dir / "result.json")["exit_code"] == 7
    assert (run_dir / "stdout.log").stat().st_size == 1024 * 1024
    assert (run_dir / "stderr.log").read_text() == "stderr complete\n"


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX pipes")
def test_default_stdout_pipe_preserves_parent_blocking_mode(tmp_path):
    output = tmp_path / "results"
    runner = (
        "from pathlib import Path; import sys; "
        "from scripts.repeat_tui_tests import run_repetitions; "
        f"raise SystemExit(run_repetitions(Path({str(output)!r}), 1, "
        "[sys.executable, '-c', 'print(42)'])[1])"
    )
    read_fd, write_fd = os.pipe()
    try:
        blocking_before = os.get_blocking(write_fd)
        process = subprocess.run(
            [sys.executable, "-c", runner],
            cwd=Path(__file__).resolve().parents[1],
            stdout=write_fd,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        blocking_after = os.get_blocking(write_fd)
    finally:
        os.close(write_fd)
        os.close(read_fd)

    assert process.returncode == 0, process.stderr.decode(errors="replace")
    assert blocking_before
    assert blocking_after == blocking_before
    execution_dir = next(output.glob("tui-repeat-*"))
    execution = _read_json(execution_dir / "execution.json")
    result = _read_json(execution_dir / "run-01" / "result.json")
    assert execution["status"] == "passed"
    assert execution["exit_codes"] == [0]
    assert execution["caller_exit_code"] == 0
    assert result["status"] == "passed"
    assert result["exit_code"] == 0
    assert (execution_dir / "run-01" / "stdout.log").read_text() == "42\n"


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX pipes and fds")
def test_repeated_full_display_pipe_reaps_forwarders_and_closes_fds(
    tmp_path, monkeypatch
):
    read_fd, write_fd = os.pipe()
    display = os.fdopen(write_fd, "wb", buffering=0, closefd=False)
    started: list[subprocess.Popen[bytes]] = []
    original_popen = repeat.subprocess.Popen

    def recording_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        if args and args[0] == [sys.executable, "-c", repeat._DISPLAY_FORWARDER]:
            started.append(process)
        return process

    monkeypatch.setattr(repeat.subprocess, "Popen", recording_popen)
    fd_root = Path("/proc/self/fd")
    if not fd_root.exists():
        fd_root = Path("/dev/fd")
    before_fds = {entry.name for entry in fd_root.iterdir()}
    before_threads = set(threading.enumerate())
    try:
        os.set_blocking(write_fd, False)
        while True:
            try:
                os.write(write_fd, b"x" * 4096)
            except BlockingIOError:
                break
        os.set_blocking(write_fd, True)

        for number in range(3):
            _, exit_code = run_repetitions(
                tmp_path / "results",
                1,
                [sys.executable, "-c", "pass"],
                cwd=tmp_path,
                display_stdout=display,
                display_stderr=display,
            )
            assert exit_code == 0, number

        assert len(started) == 3
        assert all(process.poll() is not None for process in started)
        assert set(threading.enumerate()) == before_threads
        assert {entry.name for entry in fd_root.iterdir()} == before_fds
        assert os.get_blocking(write_fd)
    finally:
        display.close()
        os.close(write_fd)
        os.close(read_fd)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_inherited_output_fds_do_not_delay_completed_run(tmp_path):
    group_file = tmp_path / "process_group"
    fake = tmp_path / "inherited_fd.py"
    fake.write_text(
        "import os, subprocess, sys\n"
        f"open({str(group_file)!r}, 'w').write(str(os.getpgrp()))\n"
        "code = \"import sys,time; print('grandchild stdout', flush=True); "
        "print('grandchild stderr', file=sys.stderr, flush=True); time.sleep(30)\"\n"
        "subprocess.Popen([sys.executable, '-c', code])\n"
        "print('parent stdout', flush=True)\n"
        "print('parent stderr', file=sys.stderr, flush=True)\n",
        encoding="utf-8",
    )
    output = tmp_path / "results"
    runner = (
        "from pathlib import Path; import sys; "
        "from scripts.repeat_tui_tests import run_repetitions; "
        f"_, code = run_repetitions(Path({str(output)!r}), 1, "
        f"[sys.executable, {str(fake)!r}], cwd=Path({str(tmp_path)!r})); "
        "raise SystemExit(code)"
    )
    process_group = None
    try:
        process = subprocess.run(
            [sys.executable, "-c", runner],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            timeout=10,
        )
        process_group = int(group_file.read_text())

        assert process.returncode == 0
        run_dir = _wait_for_log_markers(
            output,
            ("stdout.log", "grandchild stdout"),
            ("stderr.log", "grandchild stderr"),
        )
        assert run_dir is not None
        assert _read_json(run_dir / "result.json")["exit_code"] == 0
        assert "parent stdout" in (run_dir / "stdout.log").read_text()
        assert "parent stderr" in (run_dir / "stderr.log").read_text()
        os.killpg(process_group, 0)
    finally:
        if process_group is not None:
            try:
                os.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_interrupted_run_keeps_partial_logs_without_success_result(tmp_path):
    fake = tmp_path / "slow_test.py"
    fake.write_text(
        "import sys, time\n"
        "print('before wait', flush=True)\n"
        "print('error before wait', file=sys.stderr, flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    output = tmp_path / "results"
    runner = (
        "from pathlib import Path; import sys; "
        "from scripts.repeat_tui_tests import run_repetitions; "
        f"run_repetitions(Path({str(output)!r}), 1, "
        f"[sys.executable, {str(fake)!r}], cwd=Path({str(tmp_path)!r}))"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", runner],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    run_dir = _wait_for_log_markers(
        output,
        ("stdout.log", "before wait"),
        ("stderr.log", "error before wait"),
    )
    assert run_dir is not None

    process.send_signal(signal.SIGINT)
    _, runner_stderr = process.communicate(timeout=10)

    assert process.returncode != 0
    assert _read_json(run_dir / "started.json")["status"] == "running"
    interrupted = run_dir / "interrupted.json"
    assert interrupted.exists(), runner_stderr.decode(errors="replace")
    assert _read_json(interrupted)["status"] == "interrupted"
    assert (run_dir / "stdout.log").read_text() == "before wait\n"
    assert (run_dir / "stderr.log").read_text().startswith("error before wait\n")
    assert not (run_dir / "result.json").exists()


@pytest.mark.skipif(
    os.name != "posix", reason="requires POSIX signals and process groups"
)
def test_cli_sigterm_stops_child_and_records_interruption(tmp_path):
    child_pid = tmp_path / "child.pid"
    fake = tmp_path / "test_slow.py"
    fake.write_text(
        "import os, time\n"
        "def test_slow():\n"
        f"    open({str(child_pid)!r}, 'w').write(str(os.getpid()))\n"
        "    print('before SIGTERM', flush=True)\n"
        "    time.sleep(30)\n",
        encoding="utf-8",
    )
    output = tmp_path / "results"
    process = subprocess.Popen(
        [
            sys.executable,
            "scripts/repeat_tui_tests.py",
            str(output),
            "--runs",
            "2",
            "--",
            "-q",
            "-s",
            str(fake),
        ],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    run_dir = _wait_for_log_markers(output, ("stdout.log", "before SIGTERM"))
    assert run_dir is not None
    pytest_pid = int(child_pid.read_text())

    process.send_signal(signal.SIGTERM)
    _, runner_stderr = process.communicate(timeout=10)

    assert process.returncode == 143
    interrupted = run_dir / "interrupted.json"
    assert interrupted.exists(), runner_stderr.decode(errors="replace")
    assert _read_json(interrupted)["status"] == "interrupted"
    execution = _read_json(run_dir.parent / "execution.json")
    assert execution["status"] == "interrupted"
    assert execution["runs_completed"] == 0
    assert not (run_dir / "result.json").exists()
    assert not (run_dir.parent / "run-02").exists()
    assert (run_dir / "stdout.log").read_text().startswith("before SIGTERM\n")
    with pytest.raises(ProcessLookupError):
        os.kill(pytest_pid, 0)


@pytest.mark.skipif(
    os.name != "posix", reason="requires POSIX signals and process groups"
)
def test_cli_signal_during_popen_return_stops_new_session_child(tmp_path):
    output = tmp_path / "results"
    pytest_pid_file = tmp_path / "pytest.pid"
    fake = tmp_path / "test_slow.py"
    fake.write_text(
        "import time\ndef test_slow():\n    time.sleep(30)\n", encoding="utf-8"
    )
    runner = (
        "import os, signal\n"
        "from pathlib import Path\n"
        "import scripts.repeat_tui_tests as repeat\n"
        "original_popen = repeat.subprocess.Popen\n"
        "def signaling_popen(*args, **kwargs):\n"
        "    process = original_popen(*args, **kwargs)\n"
        f"    Path({str(pytest_pid_file)!r}).write_text(str(process.pid))\n"
        "    os.kill(os.getpid(), signal.SIGINT)\n"
        "    return process\n"
        "repeat.subprocess.Popen = signaling_popen\n"
        f"raise SystemExit(repeat.main([{str(output)!r}, '--runs', '1', '--', "
        f"'-q', {str(fake)!r}]))\n"
    )

    process = subprocess.run(
        [sys.executable, "-c", runner],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        timeout=10,
    )

    assert process.returncode == 130, process.stderr.decode(errors="replace")
    pytest_pid = int(pytest_pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pytest_pid, 0)
    run_dir = next(output.glob("tui-repeat-*/run-01"))
    assert _read_json(run_dir / "interrupted.json")["status"] == "interrupted"
    assert not (run_dir / "result.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX signals")
def test_cli_signal_after_result_write_keeps_completed_run_in_summary(tmp_path):
    output = tmp_path / "results"
    fake = tmp_path / "test_passes.py"
    fake.write_text("def test_passes():\n    pass\n", encoding="utf-8")
    runner = (
        "import os, signal\n"
        "import scripts.repeat_tui_tests as repeat\n"
        "original_write_json = repeat._write_json\n"
        "def signaling_write_json(path, value):\n"
        "    original_write_json(path, value)\n"
        "    if path.name == 'result.json':\n"
        "        os.kill(os.getpid(), signal.SIGTERM)\n"
        "repeat._write_json = signaling_write_json\n"
        f"raise SystemExit(repeat.main([{str(output)!r}, '--runs', '2', '--', "
        f"'-q', {str(fake)!r}]))\n"
    )

    process = subprocess.run(
        [sys.executable, "-c", runner],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        timeout=10,
    )

    assert process.returncode == 143, process.stderr.decode(errors="replace")
    execution_dir = next(output.glob("tui-repeat-*"))
    result = _read_json(execution_dir / "run-01" / "result.json")
    execution = _read_json(execution_dir / "execution.json")
    assert result["status"] == "passed"
    assert result["exit_code"] == 0
    assert execution["status"] == "interrupted"
    assert execution["runs_completed"] == 1
    assert execution["exit_codes"] == [0]
    assert not (execution_dir / "run-02").exists()


@pytest.mark.skipif(
    os.name != "posix", reason="requires POSIX signals and process groups"
)
def test_cli_signal_as_release_reaps_owner_never_signals_reused_group(tmp_path):
    output = tmp_path / "results"
    grandchild_pid_file = tmp_path / "grandchild.pid"
    fake = tmp_path / "test_spawns_grandchild.py"
    fake.write_text(
        "import subprocess, sys, time\n"
        "def test_spawns_grandchild():\n"
        '    code = "from pathlib import Path; import os, time; " '
        f'"Path({str(grandchild_pid_file)!r}).write_text(str(os.getpid())); " '
        '"time.sleep(30)"\n'
        "    subprocess.Popen([sys.executable, '-c', code])\n"
        f"    deadline = time.monotonic() + 5\n"
        f"    while not __import__('pathlib').Path({str(grandchild_pid_file)!r}).exists():\n"
        "        assert time.monotonic() < deadline\n"
        "        time.sleep(0.01)\n",
        encoding="utf-8",
    )
    runner = (
        "import os, signal\n"
        "import scripts.repeat_tui_tests as repeat\n"
        "original_wait = repeat.subprocess.Popen.wait\n"
        "original_signal_group = repeat._signal_process_group\n"
        "sent = False\n"
        f"signal_log = {str(tmp_path / 'group-signals')!r}\n"
        "def recording_signal_group(process_group, signum):\n"
        "    with open(signal_log, 'a') as stream:\n"
        "        stream.write(f'{process_group}:{signum}\\n')\n"
        "    original_signal_group(process_group, signum)\n"
        "def signaling_wait(self, *args, **kwargs):\n"
        "    global sent\n"
        "    result = original_wait(self, *args, **kwargs)\n"
        "    if result == 0 and not sent:\n"
        "        sent = True\n"
        "        os.kill(os.getpid(), signal.SIGTERM)\n"
        "    return result\n"
        "repeat._signal_process_group = recording_signal_group\n"
        "repeat.subprocess.Popen.wait = signaling_wait\n"
        f"raise SystemExit(repeat.main([{str(output)!r}, '--runs', '2', '--', "
        f"'-q', {str(fake)!r}]))\n"
    )

    process = subprocess.run(
        [sys.executable, "-c", runner],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        timeout=10,
    )

    assert process.returncode == 143, process.stderr.decode(errors="replace")
    grandchild_pid = int(grandchild_pid_file.read_text())
    try:
        os.kill(grandchild_pid, 0)
        assert not (tmp_path / "group-signals").exists()
        execution_dir = next(output.glob("tui-repeat-*"))
        result = _read_json(execution_dir / "run-01" / "result.json")
        execution = _read_json(execution_dir / "execution.json")
        assert result["status"] == "passed"
        assert result["exit_code"] == 0
        assert execution["status"] == "interrupted"
        assert execution["runs_completed"] == 1
        assert execution["exit_codes"] == [0]
        assert not (execution_dir / "run-02").exists()
    finally:
        try:
            os.kill(grandchild_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX pipes")
def test_full_default_stdout_pipe_does_not_block_sigterm_cleanup(tmp_path):
    output = tmp_path / "results"
    fake = tmp_path / "test_slow.py"
    fake.write_text(
        "import time\n"
        "def test_slow():\n"
        "    print('ready for SIGTERM', flush=True)\n"
        "    time.sleep(30)\n",
        encoding="utf-8",
    )
    read_fd, write_fd = os.pipe()
    filler = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,sys; data=b'x'*4096; "
            "[(os.write(sys.stdout.fileno(), data)) for _ in range(1024)]",
        ],
        stdout=write_fd,
    )
    time.sleep(0.1)

    process = subprocess.Popen(
        [
            sys.executable,
            "scripts/repeat_tui_tests.py",
            str(output),
            "--runs",
            "1",
            "--",
            "-q",
            "-s",
            str(fake),
        ],
        cwd=Path(__file__).resolve().parents[1],
        stdout=write_fd,
        stderr=subprocess.PIPE,
    )
    try:
        run_dir = _wait_for_log_markers(output, ("stdout.log", "ready for SIGTERM"))
        assert run_dir is not None
        process.send_signal(signal.SIGTERM)
        _, runner_stderr = process.communicate(timeout=10)

        assert process.returncode == 143, runner_stderr.decode(errors="replace")
        assert _read_json(run_dir / "interrupted.json")["status"] == "interrupted"
        execution = _read_json(run_dir.parent / "execution.json")
        assert execution["status"] == "interrupted"
        assert execution["runs_completed"] == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        filler.kill()
        filler.wait()
        os.close(write_fd)
        os.close(read_fd)


@pytest.mark.parametrize("signal_before_write", [True, False])
@pytest.mark.skipif(
    os.name != "posix", reason="requires POSIX signals and process groups"
)
def test_cli_signal_at_result_boundary_keeps_result_and_cleans_grandchild(
    tmp_path, signal_before_write
):
    output = tmp_path / "results"
    grandchild_pid_file = tmp_path / "grandchild.pid"
    fake = tmp_path / "test_spawns_grandchild.py"
    fake.write_text(
        "import subprocess, sys, time\n"
        "def test_spawns_grandchild():\n"
        '    code = "from pathlib import Path; import os, time; " '
        f'"Path({str(grandchild_pid_file)!r}).write_text(str(os.getpid())); " '
        "\"print('grandchild ready', flush=True); time.sleep(30)\"\n"
        "    subprocess.Popen([sys.executable, '-c', code])\n"
        "    deadline = time.monotonic() + 5\n"
        f"    while not __import__('pathlib').Path({str(grandchild_pid_file)!r}).exists():\n"
        "        assert time.monotonic() < deadline\n"
        "        time.sleep(0.01)\n"
        "    print('parent complete', flush=True)\n",
        encoding="utf-8",
    )
    runner = (
        "import os, signal\n"
        "import scripts.repeat_tui_tests as repeat\n"
        "original_write_json = repeat._write_json\n"
        f"signal_before_write = {signal_before_write!r}\n"
        "sent = False\n"
        "def signaling_write_json(path, value):\n"
        "    global sent\n"
        "    should_signal = path.name == 'result.json' and not sent\n"
        "    if should_signal and signal_before_write:\n"
        "        sent = True\n"
        "        os.kill(os.getpid(), signal.SIGTERM)\n"
        "    original_write_json(path, value)\n"
        "    if should_signal and not signal_before_write:\n"
        "        sent = True\n"
        "        os.kill(os.getpid(), signal.SIGTERM)\n"
        "repeat._write_json = signaling_write_json\n"
        f"raise SystemExit(repeat.main([{str(output)!r}, '--runs', '2', '--', "
        f"'-q', '-s', {str(fake)!r}]))\n"
    )

    process = subprocess.run(
        [sys.executable, "-c", runner],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        timeout=10,
    )

    assert process.returncode == 143, process.stderr.decode(errors="replace")
    grandchild_pid = int(grandchild_pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild_pid, 0)
    execution_dir = next(output.glob("tui-repeat-*"))
    run_dir = execution_dir / "run-01"
    assert _read_json(run_dir / "result.json")["exit_code"] == 0
    assert "parent complete" in (run_dir / "stdout.log").read_text()
    assert "grandchild ready" in (run_dir / "stdout.log").read_text()
    execution = _read_json(execution_dir / "execution.json")
    assert execution["status"] == "interrupted"
    assert execution["runs_completed"] == 1
    assert execution["exit_codes"] == [0]
    assert not (execution_dir / "run-02").exists()


@pytest.mark.skipif(
    os.name != "posix", reason="requires POSIX signals and process groups"
)
def test_cli_installs_sigint_handler_when_inherited_as_ignored(tmp_path):
    child_pid = tmp_path / "child.pid"
    fake = tmp_path / "test_slow.py"
    fake.write_text(
        "import os, time\n"
        "def test_slow():\n"
        f"    open({str(child_pid)!r}, 'w').write(str(os.getpid()))\n"
        "    print('before inherited SIGINT', flush=True)\n"
        "    time.sleep(30)\n",
        encoding="utf-8",
    )
    output = tmp_path / "results"

    def ignore_sigint() -> None:
        signal.signal(signal.SIGINT, signal.SIG_IGN)

    process = subprocess.Popen(
        [
            sys.executable,
            "scripts/repeat_tui_tests.py",
            str(output),
            "--runs",
            "2",
            "--",
            "-q",
            "-s",
            str(fake),
        ],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=ignore_sigint,
    )
    run_dir = _wait_for_log_markers(output, ("stdout.log", "before inherited SIGINT"))
    assert run_dir is not None
    pytest_pid = int(child_pid.read_text())

    process.send_signal(signal.SIGINT)
    _, runner_stderr = process.communicate(timeout=10)

    assert process.returncode == 130, runner_stderr.decode(errors="replace")
    assert _read_json(run_dir / "interrupted.json")["status"] == "interrupted"
    execution = _read_json(run_dir.parent / "execution.json")
    assert execution["status"] == "interrupted"
    assert execution["runs_completed"] == 0
    assert not (run_dir / "result.json").exists()
    assert not (run_dir.parent / "run-02").exists()
    with pytest.raises(ProcessLookupError):
        os.kill(pytest_pid, 0)


@pytest.mark.skipif(
    os.name != "posix", reason="requires POSIX signals and process groups"
)
def test_cli_signal_after_final_execution_write_cleans_grandchild(tmp_path):
    output = tmp_path / "results"
    grandchild_pid_file = tmp_path / "grandchild.pid"
    fake = tmp_path / "test_spawns_grandchild.py"
    fake.write_text(
        "import subprocess, sys, time\n"
        "def test_spawns_grandchild():\n"
        '    code = "from pathlib import Path; import os, time; " '
        f'"Path({str(grandchild_pid_file)!r}).write_text(str(os.getpid())); " '
        '"time.sleep(30)"\n'
        "    subprocess.Popen([sys.executable, '-c', code])\n"
        "    deadline = time.monotonic() + 5\n"
        f"    while not __import__('pathlib').Path({str(grandchild_pid_file)!r}).exists():\n"
        "        assert time.monotonic() < deadline\n"
        "        time.sleep(0.01)\n",
        encoding="utf-8",
    )
    runner = (
        "import os, signal\n"
        "import scripts.repeat_tui_tests as repeat\n"
        "original_write_json = repeat._write_json\n"
        "def signaling_write_json(path, value):\n"
        "    original_write_json(path, value)\n"
        "    if path.name == 'execution.json' and value.get('status') == 'passed':\n"
        "        os.kill(os.getpid(), signal.SIGTERM)\n"
        "repeat._write_json = signaling_write_json\n"
        f"raise SystemExit(repeat.main([{str(output)!r}, '--runs', '1', '--', "
        f"'-q', {str(fake)!r}]))\n"
    )

    process = subprocess.run(
        [sys.executable, "-c", runner],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        timeout=10,
    )

    assert process.returncode == 143, process.stderr.decode(errors="replace")
    grandchild_pid = int(grandchild_pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild_pid, 0)
    execution_dir = next(output.glob("tui-repeat-*"))
    assert _read_json(execution_dir / "run-01" / "result.json")["exit_code"] == 0
    execution = _read_json(execution_dir / "execution.json")
    assert execution["status"] == "interrupted"
    assert execution["runs_completed"] == 1
    assert execution["exit_codes"] == [0]


@pytest.mark.skipif(
    os.name != "posix", reason="requires POSIX signals and process groups"
)
def test_cli_signal_during_display_finish_keeps_result_and_cleans_group(tmp_path):
    output = tmp_path / "results"
    grandchild_pid_file = tmp_path / "grandchild.pid"
    fake = tmp_path / "test_spawns_grandchild.py"
    fake.write_text(
        "import subprocess, sys, time\n"
        "def test_spawns_grandchild():\n"
        '    code = "from pathlib import Path; import os, time; " '
        f'"Path({str(grandchild_pid_file)!r}).write_text(str(os.getpid())); " '
        '"time.sleep(30)"\n'
        "    subprocess.Popen([sys.executable, '-c', code])\n"
        "    deadline = time.monotonic() + 5\n"
        f"    while not __import__('pathlib').Path({str(grandchild_pid_file)!r}).exists():\n"
        "        assert time.monotonic() < deadline\n"
        "        time.sleep(0.01)\n",
        encoding="utf-8",
    )
    runner = (
        "import os, signal\n"
        "import scripts.repeat_tui_tests as repeat\n"
        "original_finish = repeat._DisplayWriter.finish\n"
        "sent = False\n"
        "def signaling_finish(self):\n"
        "    global sent\n"
        "    original_finish(self)\n"
        "    if not sent:\n"
        "        sent = True\n"
        "        os.kill(os.getpid(), signal.SIGTERM)\n"
        "repeat._DisplayWriter.finish = signaling_finish\n"
        f"raise SystemExit(repeat.main([{str(output)!r}, '--runs', '1', '--', "
        f"'-q', {str(fake)!r}]))\n"
    )

    process = subprocess.run(
        [sys.executable, "-c", runner],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        timeout=10,
    )

    assert process.returncode == 143, process.stderr.decode(errors="replace")
    grandchild_pid = int(grandchild_pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild_pid, 0)
    execution_dir = next(output.glob("tui-repeat-*"))
    assert _read_json(execution_dir / "run-01" / "result.json")["exit_code"] == 0
    execution = _read_json(execution_dir / "execution.json")
    assert execution["status"] == "interrupted"
    assert execution["runs_completed"] == 1
    assert execution["exit_codes"] == [0]


def test_result_write_failure_stops_owner_before_handoff(tmp_path, monkeypatch):
    class FakeOwner:
        stopped = False

        def wait_for_command(self, _interruption_requested):
            return 0

        def stop(self):
            self.stopped = True
            return None

    owner = FakeOwner()
    monkeypatch.setattr(
        repeat, "_start_process_group_owner", lambda *args, **kwargs: owner
    )
    original_write_json = repeat._write_json

    def failing_write_json(path, value):
        if path.name == "result.json":
            raise OSError("ENOSPC")
        original_write_json(path, value)

    monkeypatch.setattr(repeat, "_write_json", failing_write_json)

    with pytest.raises(OSError, match="ENOSPC"):
        run_repetitions(
            tmp_path / "results",
            1,
            [sys.executable, "-c", "pass"],
            cwd=tmp_path,
            display_stdout=io.BytesIO(),
            display_stderr=io.BytesIO(),
        )

    assert owner.stopped


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_release_timeout_kills_only_owner_not_process_group(monkeypatch):
    class FakeProcess:
        pid = 424242
        waits = 0
        killed = False

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("owner", timeout)
            return -signal.SIGKILL

        def kill(self):
            self.killed = True

    process = FakeProcess()
    group_signals = []
    monkeypatch.setattr(
        repeat,
        "_signal_process_group",
        lambda process_group, signum: group_signals.append((process_group, signum)),
    )

    repeat._ProcessGroupOwner(process).release()

    assert process.killed
    assert group_signals == []


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX file descriptors")
def test_wait_for_command_supports_fd_above_select_limit():
    import fcntl
    import resource

    soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft_limit != resource.RLIM_INFINITY and soft_limit <= 1025:
        pytest.skip("RLIMIT_NOFILE does not allow fd 1025")

    read_fd: int | None = None
    write_fd: int | None = None
    high_fd: int | None = None
    owner: repeat._ProcessGroupOwner | None = None
    try:
        read_fd, write_fd = os.pipe()
        try:
            high_fd = fcntl.fcntl(read_fd, fcntl.F_DUPFD, 1025)
        except OSError as error:
            pytest.skip(f"could not allocate fd 1025: {error}")
        os.close(read_fd)
        read_fd = None
        os.write(write_fd, b"0\n")
        os.close(write_fd)
        write_fd = None

        owner = repeat._ProcessGroupOwner(subprocess.Popen(["true"]), high_fd)
        high_fd = None
        assert owner.wait_for_command(None) == 0
    finally:
        if owner is not None:
            owner.close()
            owner.process.wait()
        for file_descriptor in (read_fd, write_fd, high_fd):
            if file_descriptor is not None:
                os.close(file_descriptor)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX pipes")
def test_second_pipe_failure_closes_first_pipe_fds(tmp_path, monkeypatch):
    real_pipe = os.pipe
    allocated: tuple[int, int] | None = None
    calls = 0

    def failing_second_pipe():
        nonlocal allocated, calls
        calls += 1
        if calls == 1:
            allocated = real_pipe()
            return allocated
        raise OSError("EMFILE")

    monkeypatch.setattr(repeat.os, "pipe", failing_second_pipe)
    with (
        (tmp_path / "stdout.log").open("wb") as stdout_log,
        (tmp_path / "stderr.log").open("wb") as stderr_log,
        pytest.raises(OSError, match="EMFILE"),
    ):
        repeat._start_process_group_owner(
            [sys.executable, "-c", "pass"],
            tmp_path,
            stdout_log,
            stderr_log,
        )

    assert allocated is not None
    for file_descriptor in allocated:
        with pytest.raises(OSError):
            os.fstat(file_descriptor)
