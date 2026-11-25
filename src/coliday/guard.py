"""에이전트가 `.env` 파일을 읽지 못하게 막는다. 사람이 작업을 시작할 때 허용한 작업에서만 푼다.

에이전트는 백그라운드에서 돌아 읽을 때마다 물어볼 수 없다. 그래서 기본은 막고, 사람이 그 작업에 대해
허용했을 때만(`coliday run --allow-env`, 새 작업 창의 체크박스) 막지 않는다.

- Claude: 읽기 금지 규칙(Read 도구)만으로는 `grep -r` 이나 `python -c open(...)` 으로 샌다. 그래서 Bash 샌드박스를
  함께 켠다 (OS 가 막는다). 샌드박스는 네트워크·쓰기도 제한하므로 빌드가 돌 만큼은 열어 둔다.
- Codex: 권한 프로필(`permissions.<이름>.filesystem`)에서 `.env*` 를 "none" 으로 둔다.

둘 다 실제 CLI 로 확인한 버전 이상에서만 쓴다: 그보다 낮으면 설정이 조용히 무시될 수 있어 작업을 시작하지 않는다.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MIN_CLAUDE = (2, 1, 325)
MIN_CODEX = (0, 171, 0)
PATTERN = ".env*"  # .env, .env.local, .env.261116 …
_PROFILE = "coliday"
_GLOB_DEPTH = 8
HOW_TO_ALLOW = "이 작업에서 .env 읽기를 허용하려면 새 작업 창(Ctrl+N)의 체크박스나 --allow-env 를 쓰세요"
# 작업 도중에 필요해지면: 에이전트는 멈추고 이 문구로 알리고, 사람이 그 작업에만 허용한다 (TUI 의 Ctrl+L, `coliday env`).
NEED_MARKER = ".env 읽기 허용이 필요합니다"
LEAD_RULE = (
    "\n## .env 파일\n"
    "이 작업에서는 이름이 .env 로 시작하는 파일을 읽을 수 없다 (사람이 허용하지 않았다). 팀원도 마찬가지다.\n"
    "- 다른 방법으로 그 내용을 알아내려 하지 않는다 (다른 명령, 복사본, 프로세스 환경, 로그 등으로 우회하지 않는다).\n"
    "- 그 내용 없이 할 수 있는 일은 그대로 진행한다.\n"
    f"- 그 내용 없이는 더 진행할 수 없으면 거기서 멈추고, 보고 맨 위에 \"{NEED_MARKER}\" 라고 쓴 뒤 "
    "어떤 파일의 무엇이 왜 필요한지 적는다. 사람이 허용하면 이어서 진행하라는 요청이 온다.\n"
)
LOCAL_ONLY_RULE = (
    "\n## .env 파일\n"
    "이 컴퓨터에 있는 이름이 .env 로 시작하는 파일은 읽을 수 없다 (사람이 허용하지 않았다). 서버의 프로젝트와 상관없는 "
    "이 컴퓨터의 파일이니 읽으려 하지 않는다. 서버의 .env 를 가져와(server-pull) 읽으려 해도 막히므로, "
    "꼭 필요하면 서버에서 직접 본다.\n"
)
ALLOWED_NOTE = "사람이 이 작업에서 .env 파일 읽기를 허용했습니다. .env 를 읽지 못해 멈췄거나 건너뛴 일이 있으면 이어서 진행하세요."
_BLOCKED_SIGNS = ("Operation not permitted", "denied by your permission settings", "Permission denied")


def blocked_read(raw: dict) -> bool:
    """에이전트 출력 한 줄이 '.env 를 읽으려다 막혔다'는 도구 결과인지 (Claude 의 tool_result, Codex 의 명령 결과)."""
    if raw.get("type") not in ("user", "item.completed"):
        return False
    text = json.dumps(raw, ensure_ascii=False)
    return ".env" in text and any(sign in text for sign in _BLOCKED_SIGNS)


def parse_version(text: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(part) for part in match.groups()) if match else None


def local_version(binary: str) -> tuple[int, ...] | None:
    """이 컴퓨터에 있는 CLI 의 버전. 실행하지 못했으면 None."""
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_version(proc.stdout) if proc.returncode == 0 else None


def show(version: tuple[int, ...] | None) -> str:
    return ".".join(map(str, version)) if version else "알 수 없음"


@dataclass(frozen=True)
class EnvGuard:
    """작업 하나에 적용할 차단 범위. roots 는 절대 경로다 (작업 디렉토리, 원래 레포, 홈)."""

    roots: tuple[str, ...]
    everywhere: bool = False  # 이 컴퓨터(macOS): 디스크 어디에 있든 막는다. 서버는 roots 아래만 (전체를 훑으면 느리다)

    @property
    def globs(self) -> list[str]:
        return [f"{root.rstrip('/')}/**/{PATTERN}" for root in self.roots]

    def claude_settings(
        self, *, writable: list[str] | tuple[str, ...] = (), protected: list[str] | tuple[str, ...] = (),
        excluded: list[str] | tuple[str, ...] = (),
    ) -> str:
        """`claude --settings` 에 넘길 JSON.

        writable 은 작업 디렉토리·홈·임시 폴더 말고도 쓸 수 있어야 하는 곳, protected 는 홈 아래지만 쓰면 안 되는 곳
        (작업 기록 DB: 에이전트가 허용 여부를 스스로 바꾸지 못하게), excluded 는 샌드박스 밖에서 실행할 명령이다.
        """
        sandbox: dict = {
            "enabled": True,
            "allowUnsandboxedCommands": False,  # 막힌 명령을 샌드박스 밖에서 다시 실행하지 못하게
            "failIfUnavailable": True,  # 샌드박스를 못 켜면 막지 않은 채 돌지 말고 시작하지 않는다
            # 네트워크는 막지 않는다 (빌드가 의존성을 받는다). 로컬 바인딩·소켓은 Gradle 데몬 같은 도구가 쓴다.
            "network": {"allowedDomains": ["*"], "allowLocalBinding": True, "allowAllUnixSockets": True},
            "filesystem": {
                "denyRead": [f"/**/{PATTERN}"] if self.everywhere else self.globs,
                "allowWrite": ["~/", "/tmp", "/private/tmp", *writable],
                "denyWrite": list(protected),
            },
        }
        if excluded:
            sandbox["excludedCommands"] = list(excluded)
        return json.dumps(
            # 앞의 것은 작업 디렉토리 아래, 뒤의 것은 그 밖의 절대 경로 (Read·Grep 도구와 cat 같은 명령)
            {"permissions": {"deny": [f"Read(**/{PATTERN})", f"Read(//**/{PATTERN})"]}, "sandbox": sandbox},
            ensure_ascii=False,
        )

    def codex_args(self, sandbox: str, writable: list[str] | tuple[str, ...] = (), *, network: bool = False) -> list[str]:
        """`codex exec` 에 줄 권한 프로필. `-s <샌드박스>` 와 쓰기 폴더 옵션을 대신한다."""
        return codex_profile(sandbox, writable, deny=self.globs, network=network)


def codex_profile(
    sandbox: str, writable: list[str] | tuple[str, ...] = (), *, deny: list[str] | tuple[str, ...] = (),
    network: bool = False,
) -> list[str]:
    """Codex 권한 프로필 옵션. deny 는 읽지 못하게 할 경로(glob), network 는 명령이 네트워크를 쓸 수 있게 한다
    (서버의 프로젝트를 ssh 로 다룰 때: `-s workspace-write` 로는 읽기 전용 역할에 네트워크만 열 수 없다)."""
    roots: dict = {".": "write" if sandbox == "workspace-write" else "read"}
    if deny:
        roots[f"**/{PATTERN}"] = "none"
    filesystem: dict = {":root": "read", ":tmpdir": "write", ":project_roots": roots}
    if sandbox == "workspace-write":
        filesystem.update({path: "write" for path in writable})
    filesystem.update({glob: "none" for glob in deny})
    if deny:
        filesystem["glob_scan_max_depth"] = _GLOB_DEPTH
    args = ["-c", f'default_permissions="{_PROFILE}"', "-c", f"permissions.{_PROFILE}.filesystem={_toml(filesystem)}"]
    if network:
        args += ["-c", f"permissions.{_PROFILE}.network={_toml({'enabled': True})}"]
    return args


def for_job(workdir: str, repo: str, home: str | None, *, local: bool) -> EnvGuard:
    """작업 디렉토리, 원래 레포(worktree 로 일해도 .env 는 거기 있다), 홈 아래의 .env 를 막는다."""
    roots = [workdir, repo]
    if local:
        roots = [str(Path(root).resolve()) for root in roots]  # 심볼릭 링크가 낀 경로는 샌드박스가 놓친다
        if home:
            roots.append(str(Path(home).resolve()))
    unique = tuple(dict.fromkeys(root for root in roots if root))
    return EnvGuard(unique, everywhere=local and sys.platform == "darwin")


def _toml(value: object) -> str:
    """TOML 인라인 값. 문자열은 JSON 과 같은 꼴(기본 문자열)로 쓴다."""
    if isinstance(value, dict):
        return "{" + ",".join(f"{json.dumps(str(k), ensure_ascii=False)}={_toml(v)}" for k, v in value.items()) + "}"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)
