#!/usr/bin/env python3
"""Repeat the TUI pytest target while preserving every run's output."""

from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable, Sequence
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PYTEST_ARGS = ("-q", "tests/test_tui.py")


class _SignalInterruption(Exception):
    def __init__(self, signum: int) -> None:
        self.signum = signum


InterruptionRequested = Callable[[], int | None]


_POSIX_GROUP_OWNER = """\
import os
import subprocess
import sys

result_fd = int(sys.argv[1])
release_fd = int(sys.argv[2])
try:
    process = subprocess.Popen(sys.argv[3:])
    return_code = process.wait()
except OSError:
    return_code = 127
os.write(result_fd, f"{return_code}\\n".encode())
os.read(release_fd, 1)
"""


_DISPLAY_FORWARDER = """\
import os
import sys

source = sys.stdin.fileno()
target = sys.stdout.fileno()
try:
    while True:
        data = os.read(source, 4096)
        if not data:
            break
        while data:
            data = data[os.write(target, data):]
except (BrokenPipeError, OSError):
    pass
"""


class _DisplayWriter:
    """Best-effort FD display without letting a slow consumer block the runner."""

    def __init__(self) -> None:
        self._forwarders: dict[int, subprocess.Popen[bytes]] = {}

    def write_line(self, display: BinaryIO, message: str) -> None:
        encoded = f"{message}\n".encode()
        try:
            file_descriptor = display.fileno()
        except (AttributeError, OSError, ValueError):
            try:
                display.write(encoded)
                display.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
            return

        process = self._forwarders.get(file_descriptor)
        if process is None:
            process = self._start_forwarder(file_descriptor)
            if process is None:
                return
            self._forwarders[file_descriptor] = process
        if process.stdin is None:
            return
        try:
            os.write(process.stdin.fileno(), encoded)
        except (BlockingIOError, BrokenPipeError, OSError, ValueError):
            pass

    def finish(self) -> None:
        processes = list(self._forwarders.values())
        try:
            for process in processes:
                if process.stdin is None:
                    continue
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass
            deadline = time.monotonic() + 0.2
            for process in processes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    break
        finally:
            try:
                for process in processes:
                    if process.poll() is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                for process in processes:
                    process.wait()
            finally:
                self._forwarders.clear()

    @staticmethod
    def _start_forwarder(
        file_descriptor: int,
    ) -> subprocess.Popen[bytes] | None:
        try:
            process = subprocess.Popen(
                [sys.executable, "-c", _DISPLAY_FORWARDER],
                stdin=subprocess.PIPE,
                stdout=file_descriptor,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except (OSError, ValueError):
            return None
        if process.stdin is None:
            process.kill()
            process.wait()
            return None
        try:
            os.set_blocking(process.stdin.fileno(), False)
        except (AttributeError, OSError, ValueError):
            process.stdin.close()
            process.kill()
            process.wait()
            return None
        return process


class _ProcessGroupOwner:
    def __init__(
        self,
        process: subprocess.Popen[bytes],
        result_fd: int | None = None,
        release_fd: int | None = None,
    ) -> None:
        self.process = process
        self._result_fd = result_fd
        self._release_fd = release_fd

    def wait_for_command(
        self, interruption_requested: InterruptionRequested | None
    ) -> int:
        if self._result_fd is None:
            while True:
                try:
                    return self.process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    _raise_if_interruption_requested(interruption_requested)

        result = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(self._result_fd, selectors.EVENT_READ)
            while b"\n" not in result:
                if selector.select(timeout=0.1):
                    chunk = os.read(self._result_fd, 64)
                    if not chunk:
                        raise RuntimeError(
                            "process group owner exited without a result"
                        )
                    result.extend(chunk)
                    continue
                _raise_if_interruption_requested(interruption_requested)
        return int(result.partition(b"\n")[0])

    def release(self) -> None:
        try:
            if self._release_fd is not None:
                try:
                    os.write(self._release_fd, b"x")
                except (BrokenPipeError, OSError):
                    pass
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                # The command has completed. Never signal its process group here: a
                # normally detached grandchild is allowed to keep running.
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
                self.process.wait()
        finally:
            self.close()

    def stop(self) -> int | None:
        try:
            return _stop_process(self.process)
        finally:
            self.close()

    def close(self) -> None:
        for attribute in ("_result_fd", "_release_fd"):
            file_descriptor = getattr(self, attribute)
            if file_descriptor is not None:
                try:
                    os.close(file_descriptor)
                except OSError:
                    pass
                setattr(self, attribute, None)


def _start_process_group_owner(
    command: Sequence[str],
    cwd: Path,
    stdout_log: BinaryIO,
    stderr_log: BinaryIO,
) -> _ProcessGroupOwner:
    if os.name != "posix":
        return _ProcessGroupOwner(
            subprocess.Popen(
                command,
                cwd=cwd,
                stdout=stdout_log,
                stderr=stderr_log,
            )
        )

    result_read, result_write = os.pipe()
    try:
        release_read, release_write = os.pipe()
    except BaseException:
        os.close(result_read)
        os.close(result_write)
        raise
    try:
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _POSIX_GROUP_OWNER,
                    str(result_write),
                    str(release_read),
                    *command,
                ],
                cwd=cwd,
                stdout=stdout_log,
                stderr=stderr_log,
                start_new_session=True,
                pass_fds=(result_write, release_read),
            )
        except BaseException:
            os.close(result_read)
            os.close(release_write)
            raise
    finally:
        os.close(result_write)
        os.close(release_read)
    return _ProcessGroupOwner(process, result_read, release_write)


def _raise_if_interruption_requested(
    interruption_requested: InterruptionRequested | None,
) -> None:
    if interruption_requested is None:
        return
    signum = interruption_requested()
    if signum is not None:
        raise _SignalInterruption(signum)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _new_execution_dir(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    for _ in range(100):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        candidate = output_dir / f"tui-repeat-{stamp}-{os.getpid()}-{uuid4().hex[:8]}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError("could not allocate a unique execution directory")


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _wait_for_process_group(process_group: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while _process_group_exists(process_group):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def _signal_process_group(process_group: int, signum: int) -> None:
    try:
        os.killpg(process_group, signum)
    except (ProcessLookupError, PermissionError):
        pass


def _stop_process(process: subprocess.Popen[bytes]) -> int | None:
    if os.name == "posix":
        process_group = process.pid
        _signal_process_group(process_group, signal.SIGINT)
        if not _wait_for_process_group(process_group, 0.5):
            _signal_process_group(process_group, signal.SIGKILL)
            _wait_for_process_group(process_group, 0.5)
        # Keep the group leader unreaped until the final group signal above, so
        # its pid cannot be reused as an unrelated process group id.
        try:
            return process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return None

    if process.poll() is not None:
        return process.returncode
    process.send_signal(signal.SIGINT)
    try:
        return process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.terminate()
    try:
        return process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait()


def _run_once(
    run_dir: Path,
    run_number: int,
    total_runs: int,
    command: Sequence[str],
    cwd: Path,
    display_stdout: BinaryIO,
    display_stderr: BinaryIO,
    display_writer: _DisplayWriter,
    interruption_requested: InterruptionRequested | None = None,
) -> tuple[int, _ProcessGroupOwner]:
    run_dir.mkdir()
    started_at = _timestamp()
    _write_json(
        run_dir / "started.json",
        {
            "status": "running",
            "run": run_number,
            "total_runs": total_runs,
            "started_at": started_at,
            "command": list(command),
            "cwd": str(cwd),
        },
    )
    display_writer.write_line(
        display_stdout,
        f"TUI pytest run {run_number}/{total_runs} started: {run_dir}",
    )
    monotonic_start = time.monotonic()

    owner: _ProcessGroupOwner | None = None
    try:
        with (
            (run_dir / "stdout.log").open("wb", buffering=0) as stdout_log,
            (run_dir / "stderr.log").open("wb", buffering=0) as stderr_log,
        ):
            owner = _start_process_group_owner(
                command,
                cwd=cwd,
                stdout_log=stdout_log,
                stderr_log=stderr_log,
            )
            _raise_if_interruption_requested(interruption_requested)
            exit_code = owner.wait_for_command(interruption_requested)
        duration = time.monotonic() - monotonic_start
        _write_json(
            run_dir / "result.json",
            {
                "status": "passed" if exit_code == 0 else "failed",
                "run": run_number,
                "started_at": started_at,
                "finished_at": _timestamp(),
                "duration_seconds": round(duration, 6),
                "exit_code": exit_code,
            },
        )
        display_writer.write_line(
            display_stdout,
            f"TUI pytest run {run_number}/{total_runs} finished: "
            f"exit {exit_code}, {run_dir}",
        )
        return exit_code, owner
    except BaseException as error:
        process_exit_code = owner.stop() if owner is not None else None
        if isinstance(error, (KeyboardInterrupt, _SignalInterruption)):
            duration = time.monotonic() - monotonic_start
            _write_json(
                run_dir / "interrupted.json",
                {
                    "status": "interrupted",
                    "run": run_number,
                    "started_at": started_at,
                    "interrupted_at": _timestamp(),
                    "duration_seconds": round(duration, 6),
                    "process_exit_code": process_exit_code,
                },
            )
            display_writer.write_line(
                display_stderr,
                f"TUI pytest run {run_number}/{total_runs} interrupted: {run_dir}",
            )
        raise


def run_repetitions(
    output_dir: Path,
    runs: int,
    command: Sequence[str],
    *,
    cwd: Path = PROJECT_ROOT,
    display_stdout: BinaryIO | None = None,
    display_stderr: BinaryIO | None = None,
    _interruption_requested: InterruptionRequested | None = None,
) -> tuple[Path, int]:
    if not 1 <= runs <= 5:
        raise ValueError("runs must be between 1 and 5")
    if not command:
        raise ValueError("command must not be empty")
    execution_dir = _new_execution_dir(output_dir.resolve())
    _write_json(
        execution_dir / "execution.json",
        {
            "status": "running",
            "created_at": _timestamp(),
            "runs": runs,
            "command": list(command),
            "cwd": str(cwd.resolve()),
        },
    )
    stdout = display_stdout if display_stdout is not None else sys.stdout.buffer
    stderr = display_stderr if display_stderr is not None else sys.stderr.buffer
    display_writer = _DisplayWriter()
    display_finished = False
    exit_codes: list[int] = []
    current_owner: _ProcessGroupOwner | None = None
    try:
        for run_number in range(1, runs + 1):
            _raise_if_interruption_requested(_interruption_requested)
            exit_code, current_owner = _run_once(
                execution_dir / f"run-{run_number:02d}",
                run_number,
                runs,
                command,
                cwd.resolve(),
                stdout,
                stderr,
                display_writer,
                _interruption_requested,
            )
            exit_codes.append(exit_code)
            _raise_if_interruption_requested(_interruption_requested)
            if run_number < runs:
                owner_to_release = current_owner
                current_owner = None
                owner_to_release.release()
                _raise_if_interruption_requested(_interruption_requested)

        caller_exit_code = 0 if all(code == 0 for code in exit_codes) else 1
        _write_json(
            execution_dir / "execution.json",
            {
                "status": "passed" if caller_exit_code == 0 else "failed",
                "finished_at": _timestamp(),
                "runs_requested": runs,
                "runs_completed": len(exit_codes),
                "exit_codes": exit_codes,
                "caller_exit_code": caller_exit_code,
                "command": list(command),
                "cwd": str(cwd.resolve()),
            },
        )
        display_writer.finish()
        display_finished = True
        _raise_if_interruption_requested(_interruption_requested)
        if current_owner is not None:
            owner_to_release = current_owner
            current_owner = None
            owner_to_release.release()
    except (KeyboardInterrupt, _SignalInterruption):
        if current_owner is not None:
            current_owner.stop()
            current_owner = None
        _write_json(
            execution_dir / "execution.json",
            {
                "status": "interrupted",
                "finished_at": _timestamp(),
                "runs_requested": runs,
                "runs_completed": len(exit_codes),
                "exit_codes": exit_codes,
                "command": list(command),
                "cwd": str(cwd.resolve()),
            },
        )
        raise
    finally:
        if current_owner is not None:
            current_owner.stop()
        if not display_finished:
            display_writer.finish()
    return execution_dir, caller_exit_code


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repeat tests/test_tui.py and retain each pytest exit code and output.",
    )
    parser.add_argument(
        "output_dir",
        type=Path,
        help="directory that will contain a unique execution folder",
    )
    parser.add_argument("--runs", type=int, choices=range(1, 6), default=3, metavar="N")
    args, pytest_args = parser.parse_known_args(argv)
    args.pytest_args = pytest_args
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    pytest_args = args.pytest_args
    if pytest_args[:1] == ["--"]:
        pytest_args = pytest_args[1:]
    command = [sys.executable, "-m", "pytest", *(pytest_args or DEFAULT_PYTEST_ARGS)]

    received_signal: int | None = None

    def record_signal(signum: int, _frame: object) -> None:
        nonlocal received_signal
        if received_signal is None:
            received_signal = signum

    previous_sigint = signal.signal(signal.SIGINT, record_signal)
    try:
        previous_sigterm = signal.signal(signal.SIGTERM, record_signal)
        try:
            try:
                _, exit_code = run_repetitions(
                    args.output_dir,
                    args.runs,
                    command,
                    _interruption_requested=lambda: received_signal,
                )
            except KeyboardInterrupt:
                return 130
            except _SignalInterruption as interruption:
                return 128 + interruption.signum
            return exit_code
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)
    finally:
        signal.signal(signal.SIGINT, previous_sigint)


if __name__ == "__main__":
    raise SystemExit(main())
