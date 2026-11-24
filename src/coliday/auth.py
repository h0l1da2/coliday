"""서버의 에이전트 CLI(claude, codex)에 넘겨 줄 인증. 이 컴퓨터에 한 번 등록해 둔다.

서버에서는 로그인하지 않는다: 서버의 작업을 시작할 때 등록한 값을 그 서버의 claude 프로세스에 환경변수로 넘긴다
(codex 팀원은 그 프로세스가 실행하므로 같은 환경을 물려받는다). 값은 ssh 의 표준 입력으로 보내고
(명령 인자에는 넣지 않는다: `ps` 에 보인다) 서버의 디스크에는 쓰지 않는다.

- Claude (필수): `claude setup-token` 이 주는 토큰(sk-ant-oat…)은 `CLAUDE_CODE_OAUTH_TOKEN`,
  Anthropic API 키(sk-ant-api…)는 `ANTHROPIC_API_KEY` 로 넘긴다.
- Codex: 기본은 이 컴퓨터의 Codex 로그인(`~/.codex/auth.json`)이다. Codex 에는 환경변수로 넘길 수 있는 구독 토큰이 없어
  그 파일을 서버의 작업 폴더에 잠깐 복사해 쓰고 실행이 끝나면 지운다 (`codex_login`, `restore_codex_login`).
  OpenAI API 키를 등록했으면 그 대신 `CODEX_API_KEY` 로 넘긴다 (`codex exec` 가 읽는다). 둘 다 없으면 서버의 codex 로그인을 쓴다.

저장 파일은 `이름=값` 줄로 된 env 파일이고 이름이 `.env` 로 시작한다: .env 읽기 차단(`guard.py`)이 에이전트에게서 이 파일도 가린다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from coliday.config import Config

CLAUDE, CODEX = "claude", "codex"
KINDS = (CLAUDE, CODEX)
LABEL = {CLAUDE: "Claude 토큰", CODEX: "Codex(OpenAI) API 키"}
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
API_KEY_ENV = "ANTHROPIC_API_KEY"
CODEX_ENV = "CODEX_API_KEY"
_NAMES = {CLAUDE: (TOKEN_ENV, API_KEY_ENV), CODEX: (CODEX_ENV,)}
SETUP = "이 컴퓨터의 터미널에서 `claude setup-token` 을 실행하면 토큰을 준다"
HOW_TO = (
    "이 컴퓨터의 터미널에서 `claude setup-token` 으로 토큰을 받아 등록하세요: "
    "TUI 는 프로젝트 추가 창(Ctrl+G)의 토큰 칸, CLI 는 `coliday auth set`"
)
# 서버의 에이전트가 인증에 실패했을 때 남기는 말
_FAILURE_SIGNS = (
    "Failed to authenticate", "OAuth session expired", "Invalid API key", "Please run /login", "authentication_error",
)


class AuthError(ValueError):
    pass


def load(cfg: Config) -> dict[str, str]:
    """등록한 값 전부: {환경변수 이름: 값}. 없거나 읽을 수 없으면 비어 있다."""
    try:
        lines = cfg.auth_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    known = {name for names in _NAMES.values() for name in names}
    found: dict[str, str] = {}
    for line in lines:
        name, sep, value = line.partition("=")
        if sep and name in known and _valid(value):
            found[name] = value
    return found


def get(cfg: Config, kind: str) -> str | None:
    """그 CLI 에 등록한 값."""
    values = load(cfg)
    return next((values[name] for name in _NAMES[kind] if name in values), None)


def save(cfg: Config, kind: str, secret: str) -> None:
    """등록한다 (그 CLI 의 이전 값을 바꾼다). 본인만 읽을 수 있는 파일로 저장한다."""
    secret = secret.strip()
    if not secret:
        raise AuthError("값이 비어 있습니다.")
    if not _valid(secret):
        raise AuthError(f"{LABEL[kind]}에는 공백이나 줄바꿈이 들어갈 수 없습니다. 한 줄로 된 값만 붙여 넣으세요.")
    values = {name: value for name, value in load(cfg).items() if name not in _NAMES[kind]}
    values[env_name(kind, secret)] = secret
    _write(cfg, values)


def clear(cfg: Config, kind: str | None = None) -> bool:
    """등록한 값을 지운다 (kind 가 없으면 전부). 지울 것이 있었으면 True."""
    values = load(cfg)
    kept = {} if kind is None else {name: value for name, value in values.items() if name not in _NAMES[kind]}
    if kept:
        _write(cfg, kept)
    else:
        cfg.auth_path.unlink(missing_ok=True)
    return len(kept) != len(values)


def env_name(kind: str, secret: str) -> str:
    if kind == CODEX:
        return CODEX_ENV
    return API_KEY_ENV if secret.startswith("sk-ant-api") else TOKEN_ENV


def describe(kind: str, secret: str) -> str:
    """값을 드러내지 않고 무엇이 등록됐는지 말한다."""
    label = "Anthropic API 키" if env_name(kind, secret) == API_KEY_ENV else LABEL[kind]
    return f"{label} (…{secret[-4:]})" if len(secret) >= 20 else label


def codex_login_path() -> Path:
    """이 컴퓨터의 Codex 로그인 파일."""
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"


def codex_login() -> str | None:
    """이 컴퓨터의 Codex 로그인 (파일 내용 그대로). 로그인돼 있지 않거나 읽을 수 없으면 None."""
    try:
        text = codex_login_path().read_text(encoding="utf-8")
    except OSError:
        return None
    return text if _codex_tokens(text) else None


def restore_codex_login(sent: str, returned: str) -> bool:
    """서버에서 쓰는 동안 갱신된 Codex 로그인을 이 컴퓨터에 되돌려 놓는다. 바꿨으면 True.

    Codex 는 로그인을 갱신할 때 이전 갱신 토큰을 못 쓰게 만든다. 서버 쪽이 갱신했는데 되돌려 놓지 않으면
    이 컴퓨터의 codex 가 다시 로그인하라고 한다. 그 사이 이 컴퓨터의 파일이 바뀌었으면(여기서도 갱신했다) 건드리지 않는다.
    """
    fresh, before = _codex_tokens(returned), _codex_tokens(sent)
    if not fresh or fresh == before:
        return False
    target = codex_login_path()
    try:
        if _codex_tokens(target.read_text(encoding="utf-8")) != before:
            return False
        tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(returned)
            os.replace(tmp, target)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
    except OSError:
        return False
    return True


def _codex_tokens(text: str) -> dict | None:
    """로그인 파일의 토큰 묶음. ChatGPT 로그인이 아니면(없거나 깨졌으면) None."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict) or not tokens.get("access_token") or not tokens.get("refresh_token"):
        return None
    return tokens


def is_failure(text: str | None) -> bool:
    """에이전트가 남긴 말이 인증 실패인지."""
    return any(sign in (text or "") for sign in _FAILURE_SIGNS)


def _write(cfg: Config, values: dict[str, str]) -> None:
    target = cfg.auth_path
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # 처음부터 0600 으로 만든다
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(f"{name}={value}\n" for name, value in values.items())
        os.replace(tmp, target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _valid(secret: str) -> bool:
    return bool(secret) and secret.isascii() and secret.isprintable() and not any(ch.isspace() for ch in secret)
