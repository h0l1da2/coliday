"""Codex 팀장 아래에서 팀원을 실행한다.

Claude 팀장은 Claude Code 의 서브에이전트 도구로 팀원을 부르지만 Codex 에는 그런 도구가 없다.
그래서 Codex 팀장은 셸 명령 `coliday _agent <폴더> <역할>` 로 맡기고, 실제 팀원 실행은 worker 가 한다:

    Codex 팀장(샌드박스 안) ──요청 파일──▶ worker(샌드박스 밖) ──▶ claude -p / codex exec
                            ◀──응답 파일──

팀원(claude, 중첩된 codex)은 네트워크와 홈 디렉토리 쓰기가 필요해 Codex 샌드박스 안에서는 돌 수 없다.
파일로 주고받으면 팀장을 샌드박스에 둔 채로(workspace-write) 팀원만 밖에서 실행할 수 있다.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from coliday import team
from coliday.guard import EnvGuard
from coliday.config import Config
from coliday import review_gate
from coliday.events import LEAD, CodexTranslator, Translator
from coliday.store import Job, Store

_POLL_SEC = 0.3
MARKER = "coliday _agent"  # 팀장의 분배 명령을 타임라인에서 숨길 때 찾는 문자열


def rpc_dir(home: Path, job_id: int) -> Path:
    """작업별 요청·응답 폴더. 임시 디렉토리 아래라 Codex 샌드박스 안에서도 쓸 수 있다.

    작업 번호는 coliday 홈(COLIDAY_HOME)마다 1 부터 매겨진다. 이름에 홈 경로의 해시를 넣지 않으면
    홈이 다른 두 실행(동시에 돈 두 테스트 묶음 등)이 같은 폴더를 써서 서로의 요청을 가져가고 멈춘다.
    """
    tag = hashlib.sha256(str(Path(home).resolve()).encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"coliday-rpc-{os.getuid()}-{tag}-{job_id}"


def agent_command(home: Path, job_id: int) -> str:
    return f"{sys.executable} -m {MARKER} {rpc_dir(home, job_id)}"


# ── 팀장 쪽 (샌드박스 안에서 실행되는 클라이언트) ──────────────────────────
def request(directory: Path, role: str, task: str) -> int:
    """요청을 남기고 응답을 기다려 출력한다. DB 나 네트워크는 쓰지 않는다."""
    if not task.strip():
        print("작업 명세가 비어 있습니다. stdin 으로 명세를 주세요.", file=sys.stderr)
        return 2
    if not directory.is_dir():
        print(f"요청 폴더가 없습니다: {directory} (팀장 실행 중에만 쓸 수 있습니다)", file=sys.stderr)
        return 2
    rid = uuid.uuid4().hex
    _write_json(directory / f"{rid}.req.json", {"role": role, "task": task})
    response = directory / f"{rid}.res.json"
    while not response.exists():
        time.sleep(_POLL_SEC)
    data = json.loads(response.read_text(encoding="utf-8"))
    print(data.get("text") or "")
    return 0 if data.get("ok") else 1


# ── worker 쪽 ────────────────────────────────────────────────────────────
class Server:
    """요청 폴더를 지켜보다가 요청마다 스레드를 띄워 팀원을 실행한다."""

    def __init__(
        self, cfg: Config, job: Job, resolved: dict[str, dict], guard: EnvGuard | None = None,
        *, cwd: str | None = None, helpers: list[str] | tuple[str, ...] = (), review_counter: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.review_counter = review_counter  # 있으면 리뷰 담당 호출 수를 세어 상한을 넘으면 막는다 (review_gate)
        self.job = job
        self.resolved = resolved
        self.guard = guard  # 있으면 팀원도 .env 를 읽지 못한다
        self.cwd = cwd or job.workdir  # 팀원이 실행되는 폴더 (서버의 프로젝트면 이 컴퓨터의 작업 폴더)
        self.helpers = list(helpers)  # 서버의 프로젝트를 다루는 실행 파일 (있으면 서버 작업이다)
        self.directory = rpc_dir(cfg.home, job.id)
        self.cost_usd = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True)

    def start(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        for stale in self.directory.glob("*.json"):  # 이전 실행이 남긴 요청을 다시 실행하지 않는다
            stale.unlink(missing_ok=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _watch(self) -> None:
        seen: set[str] = set()
        while not self._stop.is_set():
            for req in sorted(self.directory.glob("*.req.json")):
                if req.name not in seen:
                    seen.add(req.name)
                    threading.Thread(target=self._handle, args=(req,), daemon=True).start()
            self._stop.wait(_POLL_SEC)

    def _handle(self, req: Path) -> None:
        store = Store(self.cfg.db_path)  # sqlite 연결은 스레드마다 따로 쓴다
        try:
            try:
                data = json.loads(req.read_text(encoding="utf-8"))
                role, task = str(data["role"]), str(data["task"])
            except (OSError, ValueError, KeyError, TypeError):
                ok, text = False, "요청 형식이 올바르지 않습니다."
            else:
                ok, text = self._run(store, role, task)
            _write_json(req.with_name(req.name.replace(".req.json", ".res.json")), {"ok": ok, "text": text})
        finally:
            store.close()

    def _run(self, store: Store, role: str, task: str) -> tuple[bool, str]:
        job_id = self.job.id
        if role not in self.resolved:
            return False, f"알 수 없는 역할: {role} (가능: {', '.join(self.resolved)})"
        if self.review_counter is not None and (reason := review_gate.take(self.review_counter, role)):
            return False, reason
        first_line = " ".join(task.strip().splitlines()[0].split())[:120]
        store.add_event(job_id, LEAD, "dispatch", f"{role}에게 분배: {first_line}")
        store.update_job(job_id, phase=f"{LEAD}: {role}에게 분배")
        ok, text, cost = run_member(
            self.cfg, store, self.job, role, self.resolved[role], task, self.guard, cwd=self.cwd, helpers=self.helpers,
        )
        with self._lock:
            self.cost_usd += cost or 0.0
        store.add_event(job_id, role, "done", "종료 (completed)" if ok else "종료 (failed)")
        store.add_event(job_id, LEAD, "phase", f"{role} 결과 검토 중")
        store.update_job(job_id, phase=f"{LEAD}: {role} 결과 검토 중")
        return ok, text


def run_member(
    cfg: Config, store: Store, job: Job, role: str, spec: dict, task: str, guard: EnvGuard | None = None,
    *, cwd: str | None = None, helpers: list[str] | tuple[str, ...] = (),
) -> tuple[bool, str, float | None]:
    """팀원 하나를 실행하고 (성공 여부, 보고, 비용)을 돌려준다. 진행 과정은 타임라인에 남긴다.

    helpers 는 서버의 프로젝트를 다루는 실행 파일이다: 있으면 그 파일을 권한 검사·샌드박스 없이 실행하게 열고 네트워크를 쓴다.
    """
    remote_job = bool(helpers)
    prompt = str(spec.get("prompt") or "")
    model = spec.get("model")
    if team.is_codex(model):
        cmd = [
            cfg.codex_bin, "exec", "--json", "--skip-git-repo-check",
            *team.codex_access(
                team.codex_sandbox(spec, remote_job), cfg.artifacts_dir(job.id), guard=guard, network=remote_job,
            ),
        ]
        if team.codex_model(model):
            cmd += ["-m", team.codex_model(model)]
        cmd.append("-")
        stdin = f"{prompt}\n\n# 작업 명세\n{task}"
        translator: Translator | CodexTranslator = CodexTranslator(lead=role)
    else:
        cmd = [
            cfg.claude_bin, "-p", "--output-format", "stream-json", "--verbose",
            "--permission-mode", cfg.permission_mode, "--permission-prompts", "none",
            "--append-system-prompt", prompt, "-n", f"coliday#{job.id}:{role}",
            "--add-dir", str(cfg.artifacts_dir(job.id)),
        ]
        if isinstance(model, str) and model:
            cmd += ["--model", model]
        if isinstance(spec.get("tools"), list):
            cmd += ["--tools", ",".join(map(str, spec["tools"]))]
        if guard is not None:
            cmd += ["--settings", guard.claude_settings(
                writable=[str(cfg.artifacts_dir(job.id))], protected=cfg.protected_paths,
                excluded=[f"{team.runner_command(path)} *" for path in helpers],
            )]
        if helpers:
            cmd += ["--allowedTools", *(team.runner_rule(path) for path in helpers)]
        stdin = task
        translator = Translator(lead=role)
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd or job.workdir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=agent_env(guard),
        )
    except OSError as exc:
        store.add_event(job.id, role, "error", f"실행 실패: {exc}")
        return False, f"{role} 실행 실패: {exc}", None
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    errors: list[str] = []
    drain = threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True)  # stderr 가 차서 멈추지 않게
    drain.start()
    proc.stdin.write(stdin)
    proc.stdin.close()
    for line in proc.stdout:
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        for ev in translator.feed(raw):
            store.add_event(job.id, ev.actor, ev.kind, ev.message)
            if ev.kind != "text":
                store.update_job(job.id, phase=f"{ev.actor}: {ev.message}")
    returncode = proc.wait()
    drain.join(timeout=2)
    ok = returncode == 0 and not translator.is_error
    text = translator.result or ""
    if not ok:
        tail = " ".join("".join(errors)[-300:].split())
        store.add_event(job.id, role, "error", f"종료 코드 {returncode}" + (f": {tail}" if tail else ""))
        text = text or f"{role} 이(가) 실패했습니다 (종료 코드 {returncode}). {tail}".strip()
    return ok, text, translator.cost_usd


_HEADLESS = "-Djava.awt.headless=true"


def agent_env(guard: EnvGuard | None) -> dict[str, str] | None:
    """에이전트 프로세스의 환경. 샌드박스 안에서 Java 가 막히는 두 가지를 미리 피한다 (macOS).

    - /usr/bin/java 가 JDK 를 찾지 못한다: 미리 찾아 JAVA_HOME 으로 준다.
    - 화면(AWT)을 초기화하는 코드가 끝없이 멈춘다 (글꼴 너비를 재는 Apache POI 테스트 등. 오류도 내지 않아 에이전트가
      하염없이 기다린다): 모든 JVM 이 읽는 JAVA_TOOL_OPTIONS 로 화면 없는 모드를 켠다. 사용자가 직접 정해 두었으면 건드리지 않는다.
    """
    if guard is None or sys.platform != "darwin":
        return None
    env = dict(os.environ)
    options = env.get("JAVA_TOOL_OPTIONS", "")
    if "java.awt.headless" not in options:
        env["JAVA_TOOL_OPTIONS"] = f"{options} {_HEADLESS}".strip()
    if not env.get("JAVA_HOME"):
        try:
            found = subprocess.run(["/usr/libexec/java_home"], capture_output=True, text=True, timeout=10, check=False)
            home = found.stdout.strip()
            if found.returncode == 0 and home.startswith("/"):
                env["JAVA_HOME"] = home
        except (OSError, subprocess.TimeoutExpired):
            pass
    return env


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")  # 반쯤 쓴 파일을 상대가 읽지 않게 교체한다
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
