"""팀장 지시문과 서브에이전트 정의.

`~/.coliday/agents.json` 이 있으면 기본 에이전트 정의 대신 그 파일을 쓴다
(형식은 `claude --agents` 와 같다). 모델만 바꿀 때는 `~/.coliday/models.json` (`coliday model`) 을 쓴다.

모델이 `codex` 또는 `codex:<모델>` 인 역할은 Codex CLI 로 일한다. 팀장과 팀원 어느 쪽이든 섞어 쓸 수 있다.

- Claude 팀장 + Codex 팀원: Claude Code 서브에이전트는 Claude 모델만 쓸 수 있으므로, 그 팀원을
  `codex exec` 를 대신 돌려 주는 얇은 Claude 래퍼로 바꾼다 (`claude_agents`).
- Codex 팀장: Codex 에는 서브에이전트 도구가 없으므로 셸 명령(`coliday _agent`)으로 팀원에게 맡긴다
  (`codex_lead_prompt`, 실행은 `members.py`).
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

LEAD_ROLE = "lead"
DEFAULT_ROLE = "default"  # 기본 에이전트 설정을 가리키는 이름 (팀원 이름이 아니다)
CLAUDE = "claude"
CODEX = "codex"
_CODEX_MODEL_RE = re.compile(r"[A-Za-z0-9._/-]+")  # 셸 명령에 그대로 들어간다

_PROJECT_CONTEXT = """\
작업 전에 프로젝트 컨텍스트를 파악한다:
1. 작업 디렉토리의 AGENTS.md, CLAUDE.md 가 있으면 읽고 그 규칙을 최우선으로 따른다.
2. 빌드 도구를 감지한다 (build.gradle → ./gradlew, package.json → npm/pnpm/yarn,
   pyproject.toml → uv/pytest, go.mod → go 등).
3. 검사 명령은 AGENTS.md 에 적힌 것을 쓰고, 없으면 감지 결과로 추론하되 보고에 "추론함"이라고 적는다.
"""

_TEAM_HEADING = "## 팀원 (Agent 도구의 subagent_type)"
DEFAULT_MAX_ATTEMPTS = 5
_N = "{max_attempts}"  # 지시문 안의 최대 시도 횟수 자리 (COLIDAY_MAX_ATTEMPTS)

_LEAD_TEMPLATE = """\
# coliday 팀장 모드
너는 개발팀 팀장이다. 사용자 요청을 서브에이전트에게 나눠 맡기고 결과를 검토해 최종 보고한다.
이 세션은 사용자와 대화할 수 없는 비대화형 실행이므로 질문하지 말고 합리적인 기본값으로 진행한 뒤
가정한 내용을 최종 보고에 적는다.

""" + _TEAM_HEADING + """
- architect: 설계. 코드를 수정하지 않는다.
- coder: Claude 구현 담당.
- codex-coder: 두 번째 구현 담당 (기본은 Codex CLI). 독립적인 구현 단위를 병렬로 맡기거나 대안 구현이 필요할 때 쓴다.
- reviewer: diff 리뷰. 수정하지 않는다.
- verifier: 빌드·테스트·정적 검사 실행.

## 직접 처리하는 일
질문 답변, 문서·코드 찾기, 조사, 현재 상태 확인처럼 코드를 바꾸지 않는 일은 팀원에게 맡기지 말고
직접 파일을 읽고 검색해서 바로 답한다. 이때는 아래 절차를 따르지 않는다.

## 절차 (코드를 바꾸는 일)
1. 아주 작은 수정이 아니면 architect 에게 먼저 설계를 맡긴다.
2. 구현을 독립 단위로 나눠 coder / codex-coder 에게 맡긴다. 같은 파일을 두 팀원이 동시에 고치게 하지 않는다.
3. 구현이 끝나면 reviewer 와 verifier 를 실행한다.
4. [P0]~[P2] 문제나 검사 실패가 있으면 담당 구현자에게 재작업을 맡긴다.
   같은 작업은 처음 시도를 포함해 최대 {max_attempts}번까지만 시도한다 (팀원에게 다시 맡기는 것과 직접 다시 해 보는 것 모두 센다).
   {max_attempts}번 안에 해결되지 않으면 더 시도하지 말고 아래 "실패한 작업 정리"로 넘어간다.
5. 직접 코드를 수정하지 말고 위임한다. 단, 팀원 결과를 확인하려고 파일을 읽는 것은 괜찮다.
6. git commit, push, merge 는 하지 않는다. 변경은 작업 디렉토리에 남겨 사용자가 검토하게 한다.
7. 모든 서브에이전트가 끝난 뒤 최종 보고한다.

## 실패한 작업 정리
어떤 작업이 {max_attempts}번 시도해도 실패하거나 팀원이 "최대 시도 횟수 도달"로 보고하면, 그 작업을 멈추고 직접 정리한다:
- 반쯤 고친 코드가 빌드·테스트를 깨뜨리는지 확인하고, 깨뜨리면 그 작업의 변경만 되돌리거나 동작하는 상태로 맞춘다
  (다른 작업의 변경이나 사용자가 원래 가지고 있던 변경은 건드리지 않는다).
- 최종 보고에 그 작업을 "실패"로 적고, 시도한 방법과 횟수, 마지막 오류, 지금 코드 상태, 사용자가 할 수 있는 다음 선택지를 쓴다.
- 나머지 작업은 가능한 범위에서 계속 진행한다.

## 최종 보고 (한국어)
- 변경 요약 (파일별)
- 실행한 검사와 실제 결과 (실행하지 않은 검사를 통과했다고 쓰지 않는다)
- 남은 문제, 가정한 내용
"""

DEFAULT_AGENTS: dict[str, dict] = {
    "architect": {
        "description": "요구사항을 받아 프로젝트 구조에 맞는 설계안을 만든다. 구현 전에 사용.",
        "prompt": "너는 소프트웨어 아키텍트다. 코드를 수정하지 않는다.\n"
        + _PROJECT_CONTEXT
        + "\n산출물: 1) 설계 개요 2) 생성·수정할 파일 목록과 각 파일의 역할 "
        "3) 구현 단위 분할 제안(병렬 가능 여부 포함) 4) 테스트 계획 5) 위험 요소",
        "tools": ["Read", "Grep", "Glob", "Bash"],
        "model": "opus",
    },
    "coder": {
        "description": "설계나 작업 명세를 받아 코드를 구현하고 관련 테스트를 작성한다.",
        "prompt": "너는 시니어 개발자다.\n"
        + _PROJECT_CONTEXT
        + "\n명세 범위만 구현하고 관련 테스트를 작성·실행한다. git commit 은 하지 않는다.\n"
        "보고: 변경 파일 목록, 실행한 테스트와 실제 결과, 남은 문제.",
        "tools": ["Read", "Grep", "Glob", "Edit", "Write", "Bash"],
    },
    "codex-coder": {
        "description": "Codex CLI 로 구현하는 두 번째 구현 담당. 독립 구현 단위나 대안 구현에 사용.",
        "prompt": "너는 시니어 개발자다.\n"
        + _PROJECT_CONTEXT
        + "\n명세 범위만 구현하고 관련 테스트를 작성·실행한다. git commit 은 하지 않는다.\n"
        "보고: 변경 파일 목록, 실행한 테스트와 실제 결과, 남은 문제.",
        "tools": ["Read", "Grep", "Glob", "Edit", "Write", "Bash"],
        "model": "codex",
    },
    "reviewer": {
        "description": "구현 후 변경 사항을 리뷰한다. 코드 변경 뒤에는 항상 사용.",
        "prompt": "너는 코드 리뷰어다. 코드를 수정하지 않는다.\n"
        + _PROJECT_CONTEXT
        + "\n`git diff` 와 `git status` 로 변경을 확인하고 호출부·데이터 흐름·테스트까지 추적한다.\n"
        "발견사항은 한 항목에 한 문제만, 심각도([P0] 보안·데이터 손실, [P1] 주요 기능 실패, "
        "[P2] 특정 조건 오류·회귀, [P3] 개선)와 파일:라인, 발생 조건, 영향, 수정 방향을 한국어로 적는다.\n"
        "확실하지 않으면 질문으로 구분한다. 문제가 없으면 검토 범위를 요약한다.",
        "tools": ["Read", "Grep", "Glob", "Bash"],
        "model": "opus",
    },
    "verifier": {
        "description": "빌드, 테스트, 정적 검사를 실행하고 결과를 보고한다.",
        "prompt": "너는 검증 담당이다. 코드를 수정하지 않는다.\n"
        + _PROJECT_CONTEXT
        + "\n프로젝트의 포매터 검사, 빌드, 테스트를 실행한다. Docker 등 환경 문제로 실행하지 못한 검사는 "
        "'미실행'으로 보고하고 절대 통과로 쓰지 않는다.\n"
        "보고: 실행한 명령, 성공/실패, 실패 시 핵심 오류 메시지.",
        "tools": ["Read", "Grep", "Glob", "Bash"],
        "codex_sandbox": "workspace-write",  # 수정 도구는 없지만 빌드·테스트가 산출물을 쓴다
    },
}


def lead_prompt(max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> str:
    return _LEAD_TEMPLATE.replace(_N, str(max_attempts))


LEAD_PROMPT = lead_prompt()  # 기본 시도 횟수로 만든 지시문

_MEMBER_RETRY_RULE = """\

## 실패했을 때
명령·빌드·테스트 등 맡은 작업이 실패하면 원인을 고쳐 다시 시도하되, 같은 작업은 처음 시도를 포함해 최대 {max_attempts}번까지만 시도한다.
{max_attempts}번 해도 안 되면 더 시도하지 말고 멈춘 뒤, "최대 시도 횟수 도달"이라고 밝히고 팀장에게 정리해서 보고한다:
시도한 방법과 횟수, 마지막 오류 메시지, 지금 코드 상태(무엇을 바꿨고 무엇이 깨져 있는지). 성공한 것처럼 쓰지 않는다.
"""


def load_agents(override: Path) -> dict[str, dict]:
    if override.is_file():
        data = json.loads(override.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data:
            raise ValueError(f"{override}: 에이전트 정의는 비어 있지 않은 JSON 객체여야 합니다")
        return data
    return DEFAULT_AGENTS


@dataclass(frozen=True)
class Models:
    """사용자가 고른 에이전트·모델.

    값은 `claude`, `codex`(각 CLI 의 기본 모델), `claude:<모델>`, `codex:<모델>` 이다.
    에이전트 없이 모델만 쓴 값(`opus`)은 Claude 모델로 본다.
    default 는 따로 고르지 않은 역할이 쓰는 기본 에이전트다.
    """

    lead: str | None = None
    agents: dict[str, str] = field(default_factory=dict)
    default: str = "claude"


def load_models(path: Path) -> Models:
    if not path.is_file():
        return Models()
    data = json.loads(path.read_text(encoding="utf-8"))
    lead = data.get("lead") if isinstance(data, dict) else None
    agents = data.get("agents", {}) if isinstance(data, dict) else None
    default = data.get("default", CLAUDE) if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or not (lead is None or _valid_model(lead))
        or not _valid_model(default)
        or not isinstance(agents, dict)
        or not all(isinstance(k, str) and _valid_model(v) for k, v in agents.items())
    ):
        raise ValueError(
            f'{path}: {{"default": "claude|codex", "lead": "모델", "agents": {{"팀원": "모델"}}}} 형식이어야 합니다'
        )
    return Models(lead=lead, agents=dict(agents), default=default)


def save_models(path: Path, models: Models) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")  # 실행 중인 worker 가 반쯤 쓴 파일을 읽지 않게 교체한다
    data = {"default": models.default, "lead": models.lead, "agents": models.agents}
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def with_model(models: Models, agents: dict[str, dict], role: str, model: str | None) -> Models:
    """role 의 모델을 바꾼 새 설정. model 이 None 이면 기본값으로 되돌린다."""
    if model is not None and not _valid_model(model):
        raise ValueError(f"모델 이름이 올바르지 않습니다: {model!r}")
    if role == DEFAULT_ROLE:
        return Models(lead=models.lead, agents=models.agents, default=model or CLAUDE)
    if role == LEAD_ROLE:
        return Models(lead=model, agents=models.agents, default=models.default)
    if role not in agents:
        raise ValueError(f"알 수 없는 역할: {role} (가능: {', '.join([DEFAULT_ROLE, LEAD_ROLE, *agents])})")
    changed = {k: v for k, v in models.agents.items() if k != role}
    if model is not None:
        changed[role] = model
    return Models(lead=models.lead, agents=changed, default=models.default)


def canonical(spec: str | None) -> str | None:
    """실행에 쓰는 형태: Codex 는 `codex[:모델]` 그대로, Claude 는 모델 이름만 (기본 모델이면 None)."""
    if spec is None or spec == CLAUDE:
        return None
    if spec.startswith(CLAUDE + ":"):
        return spec.partition(":")[2]
    return spec


def lead_spec(models: Models) -> str:
    """팀장에 적용되는 설정 값 (따로 고르지 않았으면 기본 에이전트)."""
    return models.lead or models.default


def member_model(models: Models, name: str, defined: object) -> str | None:
    """팀원이 실제로 쓰는 모델 (canonical 형태). defined 는 팀원 정의의 model 값.

    따로 고른 값 > 정의가 Codex 로 못 박은 팀원 > 기본 에이전트 > 정의의 Claude 모델 순이다.
    기본 에이전트가 codex 면 정의의 Claude 모델(opus 등)은 쓸 수 없으므로 기본 에이전트를 따른다.
    """
    defined = defined if isinstance(defined, str) and defined else None
    if name in models.agents:
        return canonical(models.agents[name])
    if is_codex(defined):
        return defined
    if is_codex(models.default):
        return models.default
    return canonical(models.default) or canonical(defined)


def is_codex(model: object) -> bool:
    return isinstance(model, str) and (model == CODEX or model.startswith(CODEX + ":"))


def resolve_agents(
    agents: dict[str, dict], models: Models, max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict[str, dict]:
    """실행에 쓰는 팀원 정의 사본: 모델 설정을 덮어쓰고 지침에 최대 시도 횟수 규칙을 붙인다.

    정의에 없는 팀원의 설정은 무시한다.
    """
    out: dict[str, dict] = {}
    for name, spec in agents.items():
        spec = dict(spec) if isinstance(spec, dict) else {}
        model = member_model(models, name, spec.get("model"))
        if model:
            spec["model"] = model
        else:
            spec.pop("model", None)  # Claude 기본: 팀장 모델을 따른다
        spec["prompt"] = str(spec.get("prompt") or "").rstrip() + "\n" + _MEMBER_RETRY_RULE.replace(_N, str(max_attempts))
        out[name] = spec
    return out


def claude_agents(resolved: dict[str, dict], codex_bin: str = CODEX) -> dict[str, dict]:
    """Claude 팀장의 `claude --agents` 에 넘길 정의: Codex 팀원은 래퍼로 바꾼다."""
    out: dict[str, dict] = {}
    for name, spec in resolved.items():
        spec = {k: v for k, v in spec.items() if k != "codex_sandbox"}  # coliday 전용 키
        out[name] = _codex_wrapper(spec, codex_sandbox(resolved[name]), codex_bin) if is_codex(spec.get("model")) else spec
    return out


def apply_models(agents: dict[str, dict], models: Models, codex_bin: str = CODEX) -> dict[str, dict]:
    return claude_agents(resolve_agents(agents, models), codex_bin)


def codex_sandbox(spec: dict) -> str:
    """이 역할을 Codex 로 돌릴 때의 샌드박스. 수정 도구가 없는 역할(설계·리뷰)은 파일을 못 고치게 한다."""
    if spec.get("codex_sandbox") in ("read-only", "workspace-write"):
        return spec["codex_sandbox"]
    tools = spec.get("tools")
    writes = not isinstance(tools, list) or bool({"Edit", "Write"} & set(tools))
    return "workspace-write" if writes else "read-only"


def codex_model(model: str) -> str | None:
    """`codex:<모델>` 의 모델 부분. `codex` 만 쓰면 Codex 기본 모델(None)."""
    return model.partition(":")[2] or None


def codex_lead_prompt(
    resolved: dict[str, dict], agent_command: str, max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> str:
    """Codex 팀장 지시문: Claude 용과 같되, 팀원에게 맡기는 방법만 셸 명령으로 바꾼다."""
    head, _, rest = lead_prompt(max_attempts).partition(_TEAM_HEADING)
    _, _, tail = rest.partition("\n## ")
    roles = "\n".join(f"- {name}: {spec.get('description') or ''}".rstrip() for name, spec in resolved.items())
    how = (
        "## 팀원에게 맡기는 방법\n"
        "서브에이전트 도구 대신 아래 셸 명령으로 맡긴다. <역할> 자리에 팀원 이름을 쓰고, 작업 명세는 stdin 으로 준다:\n\n"
        f"{agent_command} <역할> <<'COLIDAY_TASK'\n<작업 명세>\nCOLIDAY_TASK\n\n"
        "- 명령은 팀원이 일을 끝낼 때까지 기다렸다가 그 팀원의 보고를 출력한다. 몇 분 이상 걸릴 수 있으니\n"
        "  명령 실행 시간 제한을 60분(3600000ms)으로 지정하고, 끝나기 전에 다시 실행하지 않는다.\n"
        "- 팀원은 이 대화를 볼 수 없다. 명세에 필요한 맥락(목표, 관련 파일, 제약)을 모두 적는다.\n"
        "- 이 명령 말고 다른 방법으로 팀원을 흉내 내지 않는다.\n\n"
        f"### 팀원\n{roles}\n"
    )
    return f"{head}{how}\n## {tail}"


def _codex_wrapper(spec: dict, sandbox: str, codex_bin: str) -> dict:
    """팀원의 역할 지침을 그대로 Codex 에 넘기는 Claude 래퍼 정의."""
    model = spec["model"].partition(":")[2]
    command = f'{shlex.quote(codex_bin)} exec -C "$PWD" -s {sandbox} --skip-git-repo-check'
    if model:
        command += f" -m {shlex.quote(model)}"
    command += ' -o "$OUT" - < "$PROMPT"'
    prompt = (
        "너는 Codex CLI 를 실행해 주는 얇은 래퍼다. 맡은 일을 직접 하지 않는다 (파일을 고치거나 직접 조사해 답하지 않는다).\n"
        "1. `OUT=$(mktemp); PROMPT=$(mktemp)` 로 임시 파일을 만들고, $PROMPT 에 아래 [역할 지침] 전문과 "
        "받은 작업 명세 전문을 고치지 말고 차례로 쓴다 (따옴표가 깨지지 않게 heredoc 을 쓴다).\n"
        f"2. 다음 명령을 그대로 실행한다. 오래 걸릴 수 있으니 끝날 때까지 기다린다:\n   {command}\n"
        "3. 끝나면 $OUT 내용과 `git status --short`, `git diff --stat` 을 읽는다.\n"
        "4. 보고: Codex 의 답을 빠짐없이 전하고, 변경 파일 목록과 Codex 가 보고한 검사 결과를 적는다 "
        "(직접 확인하지 않은 것은 그렇게 표시). 명령이 실패했으면 오류 메시지를 그대로 보고하고 대신 일하지 않는다.\n\n"
        "[역할 지침]\n" + str(spec.get("prompt") or "").strip()
    )
    wrapper = {"description": spec.get("description", ""), "prompt": prompt, "tools": ["Bash", "Read"], "model": "haiku"}
    return wrapper


def model_rows(agents: dict[str, dict], models: Models) -> list[tuple[str, str, bool]]:
    """(역할, 쓰이는 에이전트·모델, 사용자가 바꿨는지) 목록. 첫 줄은 기본 에이전트, 둘째 줄은 팀장."""
    lead = canonical(lead_spec(models))
    rows = [
        (DEFAULT_ROLE, models.default, models.default != CLAUDE),
        (LEAD_ROLE, display(lead), models.lead is not None),
    ]
    for name, spec in resolve_agents(agents, models).items():
        model = spec.get("model")
        text = display(model) if model or is_codex(lead) else "claude (팀장과 같은 모델)"
        rows.append((name, text, name in models.agents))
    return rows


def display(model: str | None) -> str:
    """canonical 모델을 에이전트가 드러나게 보여 준다: claude, claude:opus, codex, codex:gpt-x."""
    if model is None:
        return CLAUDE
    return model if is_codex(model) else f"{CLAUDE}:{model}"


def role_prompts(
    agents: dict[str, dict], models: Models, role: str | None = None, max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> list[tuple[str, str]]:
    """(제목, 프롬프트) 목록. 실제 실행에 쓰이는 내용 그대로다. role 을 주면 그 역할만."""
    if role is not None and role != LEAD_ROLE and role not in agents:
        raise ValueError(f"알 수 없는 역할: {role} (가능: {', '.join([LEAD_ROLE, *agents])})")
    out: list[tuple[str, str]] = []
    resolved = resolve_agents(agents, models, max_attempts)
    for name, model, _ in model_rows(agents, models):
        if role not in (None, name) or name == DEFAULT_ROLE:
            continue
        if name == LEAD_ROLE:
            prompt = lead_prompt(max_attempts)
            if is_codex(lead_spec(models)):
                prompt = codex_lead_prompt(resolved, "coliday _agent <작업별 임시 폴더>", max_attempts)
            out.append((f"lead (팀장) · 모델: {model}", prompt.strip()))
            continue
        spec = resolved[name]
        title = f"{name} · 모델: {model}"
        if isinstance(spec.get("tools"), list):
            title += f" · 도구: {', '.join(map(str, spec['tools']))}"
        body = str(spec.get("prompt") or "(프롬프트 없음)").strip()
        if spec.get("description"):
            body = f"[팀장이 보는 설명] {spec['description']}\n\n{body}"
        out.append((title, body))
    return out


def _valid_model(value: object) -> bool:
    if not isinstance(value, str) or not value or any(ch.isspace() for ch in value) or value.startswith("-"):
        return False
    if is_codex(value):
        return value == CODEX or _CODEX_MODEL_RE.fullmatch(value.partition(":")[2]) is not None
    if value.startswith(CLAUDE + ":"):
        return bool(value.partition(":")[2])
    return True
