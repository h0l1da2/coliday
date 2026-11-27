"""팀장 지시문과 서브에이전트 정의.

팀원 구성과 프롬프트는 `~/.coliday/team.json` (`coliday team`) 으로 역할별로 바꾼다: 바꾼 역할만 적고
나머지는 기본값을 따른다. 팀장 지시문도 같은 파일에서 바꾼다 (`Custom`, `merge_agents`, `lead_prompt`).
`~/.coliday/agents.json` 이 있으면 기본 팀원 정의(`DEFAULT_AGENTS`) 대신 그 파일을 바탕으로 쓴다
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
from dataclasses import dataclass, field, replace
from pathlib import Path

from coliday.guard import EnvGuard, codex_profile

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
TEAM_PLACEHOLDER = "{team}"  # 팀장 지시문에서 팀원 목록(Codex 팀장은 맡기는 방법)이 들어갈 자리
DEFAULT_MAX_ATTEMPTS = 5
_N = "{max_attempts}"  # 지시문 안의 최대 시도 횟수 자리 (COLIDAY_MAX_ATTEMPTS)

_LEAD_TEMPLATE = """\
# coliday 팀장 모드
너는 개발팀 팀장이다. 사용자 요청을 서브에이전트에게 나눠 맡기고 결과를 검토해 최종 보고한다.
이 세션은 사용자와 대화할 수 없는 비대화형 실행이므로 질문하지 말고 합리적인 기본값으로 진행한 뒤
가정한 내용을 최종 보고에 적는다.

{team}

## 직접 처리하는 일
질문 답변, 문서·코드 찾기, 조사, 현재 상태 확인처럼 코드를 바꾸지 않는 일은 팀원에게 맡기지 말고
직접 파일을 읽고 검색해서 바로 답한다. 이때는 아래 절차를 따르지 않는다.

## 절차 (코드를 바꾸는 일)
팀원 목록의 설명을 보고 각 단계에 맞는 팀원을 고른다. 목록에 없는 이름에게는 맡기지 않는다.
1. 아주 작은 수정이 아니면 설계 담당에게 먼저 설계를 맡긴다.
2. 구현을 독립 단위로 나눠 구현 담당에게 맡긴다. 구현 담당이 여럿이면 독립 단위를 나눠 병렬로 맡긴다.
   같은 파일을 두 팀원이 동시에 고치게 하지 않는다.
3. 구현이 끝나면 리뷰 담당과 검증 담당을 실행한다. 그 밖의 팀원은 각자의 설명에 적힌 때에 쓴다.
4. [P0]~[P2] 문제나 검사 실패가 있으면 담당 구현자에게 재작업을 맡긴다. 리뷰 지적의 재작업은 "리뷰 반복 상한"을 따른다.
   {max_attempts}번 안에 해결되지 않으면 더 시도하지 말고 "실패한 작업 정리"를 따른다.
5. 직접 코드를 수정하지 말고 위임한다. 단, 팀원 결과를 확인하려고 파일을 읽는 것은 괜찮다.
   어떤 단계를 맡을 팀원이 목록에 없으면 그 단계만 직접 한다.
6. git commit, push, merge 는 하지 않는다. 변경은 작업 디렉토리에 남겨 사용자가 검토하게 한다.
7. 모든 서브에이전트가 끝난 뒤 최종 보고한다.

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


_FAILURE_HEADING = "## 실패한 작업 정리"
_REVIEW_HEADING = "## 리뷰 반복 상한"
MAX_REVIEW_ROUNDS = 2  # 리뷰 지적으로 재작업을 맡기는 최대 차례 수 (요청 하나마다)
MAX_REVIEW_CALLS = MAX_REVIEW_ROUNDS + 1  # 리뷰 담당을 부를 수 있는 횟수: 첫 리뷰 + 재리뷰 (review_gate 가 센다)

# 팀장 지시문 뒤에 늘 붙는다. 재리뷰가 매번 새 문제를 찾아 리뷰·재작업이 끝없이 도는 것을 막는다.
_LEAD_REVIEW_RULE = f"""\

{_REVIEW_HEADING}
리뷰 지적으로 재작업을 맡기는 것은 요청 하나(이어서 시킨 요청은 그 요청 안에서 새로 센다)에서 최대 {MAX_REVIEW_ROUNDS}차례다.
여러 팀원에게 동시에 맡긴 재작업은 한 차례로 센다. 재리뷰가 새 문제를 찾아도 이 횟수는 늘지 않는다.
- {MAX_REVIEW_ROUNDS}번째 재작업 뒤의 리뷰가 마지막 리뷰다. 거기서 나온 문제는 재작업을 맡기지 않고 최종 보고의 "남은 문제"에 심각도와 함께 적는다.
- [P0]·[P1] 이 남았으면 최종 보고 맨 위에 "차단 문제 남음"이라고 쓰고, 이어서 고칠지는 사용자가 정하게 한다.
- 빌드·테스트 같은 검사 실패를 고치는 재시도는 이 상한이 아니라 "실패한 작업 정리"의 횟수를 따른다.
- 리뷰 담당(이름에 review 가 든 팀원)은 요청 하나에서 최대 {MAX_REVIEW_CALLS}번만 부를 수 있고, 넘으면 coliday 가 그 호출을 막는다.
  리뷰할 범위가 여럿이어도 한 차례에는 리뷰 담당을 한 번만 불러 한꺼번에 맡긴다.
"""

# 팀장 지시문 뒤에 늘 붙는다 (사용자가 지시문을 고쳐 써도 빠지지 않는다. 팀원의 '실패했을 때' 규칙과 짝이다).
_LEAD_FAILURE_RULE = f"""\

{_FAILURE_HEADING}
같은 작업은 처음 시도를 포함해 최대 {{max_attempts}}번까지만 시도한다 (팀원에게 다시 맡기는 것과 직접 다시 해 보는 것 모두 센다).
방법을 바꿔도 목표가 같으면 같은 작업이다 (예: 테스트가 실행되지 않아 옵션·환경·우회 스크립트를 바꿔 가며 다시 돌리는 것).
어떤 작업이 {{max_attempts}}번 시도해도 실패하거나 팀원이 "최대 시도 횟수 도달"로 보고하면, 같은 일을 다시 맡기지 말고 그 작업을 멈춘다.
1. 원인을 직접 찾는다: 오류 메시지와 로그를 읽고, 코드 문제인지 실행 환경 문제(권한 거부·샌드박스·도구나 런타임 초기화 실패·외부 서비스)인지 가린다.
2. 원인을 알았고 이 작업 안에서 고칠 수 있으면, 그 원인을 고치는 일로 한 번만 더 맡긴다. 그래도 실패하면 3으로 간다.
3. 원인을 모르겠거나 이 작업 안에서 고칠 수 없으면 작업 전체를 거기서 끝내고 사용자에게 넘긴다. 남은 단계(리뷰·검증 등)를 이어가지 않는다.
   최종 보고 맨 위에 "진행 중단"이라고 쓰고, 무엇이 왜 안 되는지(모르면 모른다고), 확인한 사실, 사용자가 해 줄 일을 적는다.
끝내기 전에 정리한다:
- 반쯤 고친 코드가 빌드·테스트를 깨뜨리는지 확인하고, 깨뜨리면 그 작업의 변경만 되돌리거나 동작하는 상태로 맞춘다
  (다른 작업의 변경이나 사용자가 원래 가지고 있던 변경은 건드리지 않는다).
- 최종 보고에 그 작업을 "실패"로 적고, 시도한 방법과 횟수, 마지막 오류, 지금 코드 상태, 사용자가 할 수 있는 다음 선택지를 쓴다.
- 실패한 작업과 상관없이 이미 끝난 다른 작업의 결과는 그대로 두고 보고에 적는다.
"""


def lead_prompt(
    max_attempts: int = DEFAULT_MAX_ATTEMPTS, artifacts: str | None = None,
    agents: dict[str, dict] | None = None, template: str | None = None,
) -> str:
    """Claude 팀장 지시문. 팀원 목록은 agents 의 이름과 설명으로 만든다 (팀원을 바꾸면 지시문도 따라 바뀐다).

    template 은 사용자가 바꾼 지시문 (없으면 기본). artifacts 를 주면 결과물을 그 폴더에 남기라는 절이 붙는다.
    """
    agents = DEFAULT_AGENTS if agents is None else agents
    section = (
        f"{_TEAM_HEADING}\n{_roles(agents)}\n\n"
        "## 위임 결과 대기\n"
        "- 다음 단계가 팀원의 결과에 의존하면 기본적으로 Agent 도구를 포그라운드로 실행하고, "
        "결과를 받은 뒤 다음 단계로 넘어간다.\n"
        "- 독립 작업을 병렬로 맡겼으면 모든 팀원의 결과를 받은 뒤에만 팀장 턴을 끝낸다. "
        "백그라운드로 맡긴 작업도 결과를 받을 때까지 기다린다."
    )
    return _render_lead(template, section, max_attempts, artifacts)


def _roles(agents: dict[str, dict]) -> str:
    return "\n".join(f"- {name}: {spec.get('description') or ''}".rstrip() for name, spec in agents.items())


def _render_lead(template: str | None, section: str, max_attempts: int, artifacts: str | None) -> str:
    text = (template or _LEAD_TEMPLATE).rstrip() + "\n"
    if TEAM_PLACEHOLDER not in text:  # 자리를 지운 지시문에도 팀원 목록은 넣는다 (없으면 맡길 방법을 모른다)
        text += f"\n{TEAM_PLACEHOLDER}\n"
    # 예전 기본 지시문을 고쳐 쓴 것에는 옛 규칙이 들어 있다: 빼고 지금 규칙을 붙인다
    text = _without_section(_without_section(text, _REVIEW_HEADING), _FAILURE_HEADING)
    text += _LEAD_REVIEW_RULE + _LEAD_FAILURE_RULE
    text = text.replace(_N, str(max_attempts)).replace(TEAM_PLACEHOLDER, section.strip())
    return text + _LEAD_ARTIFACTS_RULE.replace("{artifacts}", artifacts) if artifacts else text


def _without_section(text: str, heading: str) -> str:
    """마크다운에서 heading 으로 시작하는 절(다음 `## ` 제목 앞까지)을 뺀다."""
    out, skipping = [], False
    for line in text.split("\n"):
        if line.strip() == heading:
            skipping = True
        elif skipping and line.startswith("## "):
            skipping = False
        if not skipping:
            out.append(line)
    return "\n".join(out).rstrip() + "\n"

_MEMBER_RETRY_RULE = """\

## 실패했을 때
명령·빌드·테스트 등 맡은 작업이 실패하면 원인을 고쳐 다시 시도하되, 같은 작업은 처음 시도를 포함해 최대 {max_attempts}번까지만 시도한다.
방법을 바꿔도 목표가 같으면 같은 작업으로 센다: 테스트나 빌드가 실행되지 않아 옵션·환경 변수·우회 스크립트를 바꿔 가며 다시 돌리는 것은
모두 한 작업의 시도다. 오래 걸리는 명령을 기다리며 결과를 거듭 확인하는 것으로 횟수를 피하지 않는다.
{max_attempts}번 해도 안 되면 더 시도하지 말고 멈춘 뒤, "최대 시도 횟수 도달"이라고 밝히고 팀장에게 정리해서 보고한다:
시도한 방법과 횟수, 마지막 오류 메시지, 지금 코드 상태(무엇을 바꿨고 무엇이 깨져 있는지). 성공한 것처럼 쓰지 않는다.
코드가 아니라 실행 환경 때문에 막힌 것 같으면(권한 거부, Operation not permitted, 도구·런타임 초기화 실패 등) 그렇게 적는다:
막힌 명령과 오류 원문을 그대로 붙인다. 그래야 팀장과 사용자가 원인을 찾는다.
"""


ARTIFACTS_PLACEHOLDER = "<작업별 결과물 폴더>"

_MEMBER_ARTIFACTS_RULE = """\

## 화면 작업의 결과물
웹 화면·UI 를 만들거나 바꾸는 작업이면 실제로 실행해서 눈으로 확인할 수 있는 결과물을 남긴다. 화면과 무관한 작업이면 이 절은 무시한다.
- 스크린샷(.png)이나 녹화 영상(.webm, .mp4)을 이 폴더에 저장한다: {artifacts}
- 프로젝트에 Playwright 같은 브라우저 자동화 도구가 있으면 그것을 쓴다. 없으면 `npx playwright` 처럼 임시로 실행하되
  프로젝트 의존성(package.json 등)에는 추가하지 않는다.
- 하위 폴더를 만들지 말고 모든 파일을 이 폴더 바로 아래에 둔다 (사용자가 폴더를 열면 한눈에 다 보이게).
- 파일 이름은 순서와 내용을 알 수 있게 번호를 붙여 짓는다 (예: 01-login-page.png, 02-order-cancel-flow.webm).
- 이 폴더는 사용자가 열어 보는 곳이다. 작업 공간으로 쓰지 않는다: 찍는 데 쓰는 스크립트·HTML·로그는 임시 폴더(`mktemp -d`)에 두고,
  사용자가 볼 파일만 여기에 남긴다. 같은 화면을 여러 번 찍었으면 최종본만 남긴다.
- 저장한 뒤 `ls -la` 로 파일이 실제로 있고 크기가 0 이 아닌지 확인한다.
- 개발 서버를 띄웠으면 끝나고 반드시 종료한다. 포트가 사용 중이면 다른 포트를 쓴다 (다른 작업이 동시에 돌고 있을 수 있다).
- 로그인이나 백엔드가 필요해서 찍지 못했으면 찍은 것처럼 쓰지 말고 "찍지 못함"과 이유를 보고한다.
- 보고에 저장한 파일 이름과 각 파일이 무엇을 보여 주는지 적는다.

## 사용자가 확인할 파일
사용자가 직접 열어 봐야 하는 파일(보고서·정리 문서·내보낸 데이터·로그 발췌 등)을 만들었을 때만 따른다.
- 프로젝트에 넣을 파일이 아니면 이 폴더 바로 아래에 저장한다: {artifacts}
- 프로젝트에 속하는 파일(코드·프로젝트 문서)은 원래 자리에 둔다.
- 어느 쪽이든 보고에 그 파일의 전체 경로를 적는다 (사용자가 경로를 눌러 폴더를 연다).
- 경로는 기억이나 짐작으로 적지 않는다. `ls -d <경로>` 나 `realpath <경로>` 로 실제로 있는지 확인하고, 그 출력을 그대로 옮겨 적는다
  (중간 폴더 하나만 빠져도 사용자는 파일을 찾지 못한다).
"""

_LEAD_ARTIFACTS_RULE = """\

## 화면 작업의 결과물
웹 화면·UI 를 만들거나 바꾸는 작업이면 구현 담당이나 검증 담당에게 실제로 실행해 스크린샷이나 녹화 영상을 남기게 한다.
- 결과물 폴더: {artifacts}
- 최종 보고에 저장된 파일 이름과 각 파일이 무엇을 보여 주는지 적는다. 찍지 못했으면 이유를 적는다.
- 화면과 무관한 작업이면 이 절은 무시한다.

## 사용자가 확인할 파일
사용자가 직접 열어 봐야 하는 파일(보고서·정리 문서·내보낸 데이터 등)을 만들었으면 최종 보고에 그 파일의 전체 경로를 적는다
(사용자가 경로를 눌러 폴더를 연다). 프로젝트에 넣을 파일이 아니면 결과물 폴더 바로 아래에 둔다.
보고에 적는 파일·폴더 경로는 기억이나 짐작으로 적지 않는다: `ls -d <경로>` 로 실제로 있는지 확인한 경로만, 중간 폴더를 빼지 않고 그대로 적는다.
팀원이 보고한 경로도 옮겨 적기 전에 확인한다.
"""


LEAD_PROMPT = lead_prompt()  # 기본 팀원·기본 시도 횟수로 만든 지시문


def base_agents(override: Path) -> dict[str, dict]:
    """사용자가 역할별로 바꾸기 전의 팀원 정의: agents.json 이 있으면 그 파일, 없으면 기본값."""
    if override.is_file():
        data = json.loads(override.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data:
            raise ValueError(f"{override}: 에이전트 정의는 비어 있지 않은 JSON 객체여야 합니다")
        return data
    return DEFAULT_AGENTS


@dataclass(frozen=True)
class Custom:
    """사용자가 바꾼 팀 구성 (`team.json`). 바꾼 것만 담는다.

    agents 의 값: 바탕 정의에 있는 역할이면 바꾼 항목만(값이 None 인 항목은 그 항목을 뺀다),
    새 역할이면 정의 전체, None 이면 그 역할을 팀에서 뺀다.
    """

    lead_prompt: str | None = None  # 팀장 지시문 ({team}, {max_attempts} 자리를 쓸 수 있다)
    agents: dict[str, dict | None] = field(default_factory=dict)


_ROLE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}")  # 셸 명령과 화면에 그대로 쓰인다
_CUSTOM_FORMAT = (
    '{"lead": {"prompt": "지시문"}, "agents": {"역할": {"description": "설명", "prompt": "프롬프트", '
    '"tools": ["Read", "Bash"]}, "뺄 역할": null}} 형식이어야 합니다 (prompt 는 줄 단위 배열도 된다)'
)


def load_custom(path: Path) -> Custom:
    if not path.is_file():
        return Custom()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) - {"lead", "agents"}:
        raise ValueError(f"{path}: {_CUSTOM_FORMAT}")
    lead, agents = data.get("lead"), data.get("agents") or {}
    if not (lead is None or (isinstance(lead, dict) and set(lead) <= {"prompt"})) or not isinstance(agents, dict):
        raise ValueError(f"{path}: {_CUSTOM_FORMAT}")
    try:
        template = _text(lead.get("prompt")) if lead else None
        if template is not None and not template.strip():
            raise ValueError("팀장 지시문이 비어 있습니다")
        out: dict[str, dict | None] = {}
        for name, spec in agents.items():
            if spec is not None and not isinstance(spec, dict):
                raise ValueError(f"{name}: 정의는 객체이거나 null(팀에서 뺌)이어야 합니다")
            out[name] = None if spec is None else {**spec, **({"prompt": _text(spec["prompt"])} if "prompt" in spec else {})}
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc
    return Custom(lead_prompt=template, agents=out)


def _text(value: object) -> str | None:
    """프롬프트 값: 문자열 또는 줄 단위 배열 (JSON 에서는 여러 줄 문자열을 쓰기 불편하다)."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(line, str) for line in value):
        return "\n".join(value)
    raise ValueError("prompt 는 문자열이나 문자열 배열이어야 합니다")


def save_custom(path: Path, custom: Custom) -> None:
    """바꾼 것이 없으면 파일을 지운다. 프롬프트는 손으로 고치기 쉽게 줄 단위 배열로 쓴다."""
    if custom == Custom():
        path.unlink(missing_ok=True)
        return
    data: dict = {}
    if custom.lead_prompt is not None:
        data["lead"] = {"prompt": custom.lead_prompt.split("\n")}
    if custom.agents:
        data["agents"] = {
            name: spec if spec is None
            else {k: v.split("\n") if k == "prompt" and isinstance(v, str) else v for k, v in spec.items()}
            for name, spec in custom.agents.items()
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")  # 실행 중인 worker 가 반쯤 쓴 파일을 읽지 않게 교체한다
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def merge_agents(base: dict[str, dict], custom: Custom) -> dict[str, dict]:
    """바탕 정의에 사용자가 바꾼 것을 얹은 실제 팀원 정의. 바꾼 역할은 실행 전에 검사한다."""
    out = {name: dict(spec) if isinstance(spec, dict) else {} for name, spec in base.items()}
    for name, changed in custom.agents.items():
        if changed is None:
            out.pop(name, None)
            continue
        if name not in base and (name in (LEAD_ROLE, DEFAULT_ROLE) or not _ROLE_RE.fullmatch(name)):
            raise ValueError(f"역할 이름이 올바르지 않습니다: {name!r} (영문자로 시작, 영문·숫자·-·_ 40자 이내, lead/default 제외)")
        spec = {k: v for k, v in {**out.get(name, {}), **changed}.items() if v is not None}
        _check_spec(name, spec, new=name not in base)
        out[name] = spec
    if not out:
        raise ValueError("팀원이 하나는 있어야 합니다")
    return out


def _check_spec(name: str, spec: dict, *, new: bool) -> None:
    prompt, description, tools = spec.get("prompt"), spec.get("description"), spec.get("tools")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(f"{name}: 프롬프트(prompt)가 비어 있습니다")
    if not isinstance(description, str if new else (str, type(None))) or (new and not description.strip()):
        raise ValueError(f"{name}: 설명(description)이 비어 있습니다. 팀장은 이 설명을 보고 언제 맡길지 정합니다")
    if tools is not None and not (
        isinstance(tools, list) and tools and all(isinstance(t, str) and re.fullmatch(r"[A-Za-z0-9_()*:.-]+", t) for t in tools)
    ):
        raise ValueError(f"{name}: 도구(tools)는 이름 목록이어야 합니다 (예: Read, Grep, Bash). 모든 도구를 주려면 비웁니다")
    model = spec.get("model")
    if model is not None and (not _valid_model(model) or mismatch(model)):
        raise ValueError(f"{name}: 모델이 올바르지 않습니다: {model!r}")
    if spec.get("codex_sandbox") not in (None, "read-only", "workspace-write"):
        raise ValueError(f"{name}: codex_sandbox 는 read-only 또는 workspace-write 여야 합니다")


def load_agents(override: Path, custom: Custom | None = None) -> dict[str, dict]:
    """실행에 쓰는 팀원 정의. custom 을 주면 역할별로 바꾼 것까지 반영한다."""
    base = base_agents(override)
    return merge_agents(base, custom) if custom is not None else base


KEEP: object = object()  # with_member 의 tools: 바꾸지 않음 (None 은 '모든 도구')


def with_member(
    custom: Custom, base: dict[str, dict], name: str, *,
    description: str | None = None, prompt: str | None = None, tools: object = KEEP,
) -> Custom:
    """팀원 name 을 고치거나 새로 넣은 설정. 주지 않은 항목은 지금 값을 그대로 둔다."""
    if name in (LEAD_ROLE, DEFAULT_ROLE):
        raise ValueError(f"{name} 은(는) 팀원 이름으로 쓸 수 없습니다")
    current = merge_agents(base, custom)
    spec = dict(current.get(name) or base.get(name) or {})  # 뺐던 기본 역할을 다시 넣으면 기본 정의에서 시작한다
    if description is not None:
        spec["description"] = description.strip()
    if prompt is not None:
        spec["prompt"] = prompt.strip()
    if tools is not KEEP:
        spec.pop("tools", None)
        if tools is not None:
            spec["tools"] = list(tools)  # type: ignore[call-overload]
    if name in base and isinstance(base[name], dict):
        original = base[name]
        changed: dict = {k: v for k, v in spec.items() if _plain(original.get(k)) != _plain(v)}
        changed.update({k: None for k in original if k not in spec})
    else:
        changed = spec
    agents = {k: v for k, v in custom.agents.items() if k != name}
    if changed or name not in base:
        agents[name] = changed
    updated = replace(custom, agents=agents)
    merge_agents(base, updated)  # 저장하기 전에 검사한다
    return updated


def _plain(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


def without_member(custom: Custom, base: dict[str, dict], name: str) -> Custom:
    """팀원 name 을 팀에서 뺀 설정. 마지막 팀원은 뺄 수 없다."""
    if name not in merge_agents(base, custom):
        raise ValueError(f"알 수 없는 역할: {name}")
    agents = {k: v for k, v in custom.agents.items() if k != name}
    if name in base:
        agents[name] = None
    updated = replace(custom, agents=agents)
    merge_agents(base, updated)
    return updated


def reset_role(custom: Custom, base: dict[str, dict], role: str | None) -> Custom:
    """role 을 기본값으로 되돌린 설정 (lead 는 팀장 지시문, None 은 전부). 새로 넣은 역할은 없어진다."""
    if role is None:
        return Custom()
    if role == LEAD_ROLE:
        return replace(custom, lead_prompt=None)
    if role not in custom.agents:
        raise ValueError(f"바꾼 적이 없는 역할입니다: {role}")
    updated = replace(custom, agents={k: v for k, v in custom.agents.items() if k != role})
    merge_agents(base, updated)
    return updated


def with_lead_prompt(custom: Custom, text: str | None) -> Custom:
    if text is not None and not text.strip():
        raise ValueError("팀장 지시문이 비어 있습니다")
    if text is not None and text.strip() == _LEAD_TEMPLATE.strip():
        text = None  # 기본과 같으면 저장하지 않는다 (기본 지시문이 나중에 바뀌면 따라간다)
    return replace(custom, lead_prompt=text.strip() if text is not None else None)


def lead_template(custom: Custom) -> str:
    """고칠 수 있는 팀장 지시문 원문 ({team}, {max_attempts} 자리가 그대로 있다)."""
    return custom.lead_prompt or _LEAD_TEMPLATE


STATE_DEFAULT, STATE_CHANGED, STATE_ADDED, STATE_REMOVED = "기본값", "고침", "추가함", "뺌"


def role_states(base: dict[str, dict], custom: Custom) -> list[tuple[str, str]]:
    """(역할, 상태) 목록. 첫 줄은 팀장이고, 뺀 기본 역할도 되돌릴 수 있게 보여 준다."""
    rows = [(LEAD_ROLE, STATE_CHANGED if custom.lead_prompt is not None else STATE_DEFAULT)]
    for name in base:
        if name not in custom.agents:
            rows.append((name, STATE_DEFAULT))
        else:
            rows.append((name, STATE_REMOVED if custom.agents[name] is None else STATE_CHANGED))
    rows += [(name, STATE_ADDED) for name, spec in custom.agents.items() if name not in base and spec is not None]
    return rows


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
    if model is not None and (problem := mismatch(model)):
        raise ValueError(problem)
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


_CLAUDE_NAMES = ("opus", "sonnet", "haiku", "fable", "claude")
_CODEX_NAMES = ("gpt", "codex", "o1", "o3", "o4")


def mismatch(spec: str | None) -> str | None:
    """에이전트와 모델이 서로 안 맞으면(codex:opus, claude:gpt-6.1-sol) 설명을, 아니면 None.

    모델 칸에 다른 에이전트의 모델 이름이 남은 채 에이전트만 바꾸는 실수를 실행 전에 잡는다.
    이름으로만 판단하므로 모르는 이름은 통과시킨다 (실제로 있는 모델인지는 각 CLI 가 확인한다).
    """
    if spec is None:
        return None
    if is_codex(spec):
        model = (codex_model(spec) or "").lower()
        if model.startswith(_CLAUDE_NAMES):
            return f"{model} 은(는) Claude 모델이라 Codex 에서 쓸 수 없습니다. codex 만 쓰거나 codex:gpt-… 형태로 쓰세요."
        return None
    model = (canonical(spec) or "").lower()
    if model.startswith(_CODEX_NAMES):
        return f"{model} 은(는) Codex 모델이라 Claude 에서 쓸 수 없습니다. codex:{model} 로 쓰거나 Claude 모델을 쓰세요."
    return None


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
    agents: dict[str, dict], models: Models, max_attempts: int = DEFAULT_MAX_ATTEMPTS, artifacts: str | None = None,
) -> dict[str, dict]:
    """실행에 쓰는 팀원 정의 사본: 모델 설정을 덮어쓰고 지침에 최대 시도 횟수 규칙을 붙인다.

    artifacts 를 주면 파일을 쓸 수 있는 팀원(구현·검증)에게 화면 작업의 결과물을 그 폴더에 남기라는 절도 붙인다.

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
        if artifacts and codex_sandbox(spec) == "workspace-write":  # 설계·리뷰처럼 읽기만 하는 역할은 뺀다
            spec["prompt"] += _MEMBER_ARTIFACTS_RULE.replace("{artifacts}", artifacts)
        out[name] = spec
    return out


def claude_agents(
    resolved: dict[str, dict], runners: dict[str, str] | None = None, tasks_dir: str = "",
) -> dict[str, dict]:
    """Claude 팀장의 `claude --agents` 에 넘길 정의: Codex 팀원은 래퍼로 바꾼다.

    runners 는 {역할: 그 역할을 Codex 로 돌리는 실행 파일 경로} 다 (`codex_runners` 로 만든 파일을 둔 곳).
    tasks_dir 은 래퍼가 작업 명세를 파일로 써 둘 폴더다 (`claude --add-dir` 로 열어 둔다).
    """
    out: dict[str, dict] = {}
    for name, spec in resolved.items():
        spec = {k: v for k, v in spec.items() if k != "codex_sandbox"}  # coliday 전용 키
        if is_codex(spec.get("model")):
            spec = _codex_wrapper(spec, (runners or {})[name], f"{tasks_dir}/{Path((runners or {})[name]).name}")
        out[name] = spec
    return out


def codex_runners(
    resolved: dict[str, dict], codex_bin: str = CODEX, artifacts: str | None = None, *, local: bool = True,
    guard: EnvGuard | None = None, network: bool = False,
) -> dict[str, tuple[str, str]]:
    """Codex 로 일할 팀원마다 {역할: (파일 이름, sh 스크립트)}. 스크립트는 작업 명세 파일을 받아 Codex 의 답을 출력한다.

    Claude 래퍼가 `codex exec` 를 직접 부르면 auto 권한 모드의 분류기가 "허가받지 않은 외부 전송"으로 보고 막는다.
    그래서 실행 파일 하나로 묶어 두고 그 파일만 허용 규칙(`runner_rule`)으로 연다.
    명세를 heredoc 으로 넘기면 본문의 글자([P0]~[P3] 같은 대괄호)에 따라 규칙이 맞지 않아, 파일 경로만 인자로 받는다.
    local=False 는 서버에서 실행할 때다: artifacts 가 서버의 경로라 이 컴퓨터의 파일 시스템으로 풀지 않는다.
    guard 를 주면 Codex 가 .env 파일을 읽지 못하게 막는다 (`guard.py`).
    network 는 서버의 프로젝트를 다루는 작업이다: Codex 가 실행하는 명령이 ssh 로 서버에 닿을 수 있게 한다.
    """
    out: dict[str, tuple[str, str]] = {}
    for name, spec in resolved.items():
        if is_codex(spec.get("model")):
            file_name = f"{len(out) + 1}-{re.sub(r'[^A-Za-z0-9_-]', '_', name)}"  # 규칙·명령에 그대로 쓸 수 있는 이름
            out[name] = (file_name, _codex_runner(spec, codex_sandbox(spec, network), codex_bin, artifacts, local, guard, network))
    return out


def runner_command(path: str) -> str:
    """래퍼가 실행할 명령의 앞부분. 허용 규칙과 글자 그대로 맞아야 한다."""
    return shlex.quote(path)


def runner_rule(path: str) -> str:
    """`claude --allowedTools` 에 넘길 규칙: 이 실행 파일만 분류기를 거치지 않고 실행된다."""
    return f"Bash({runner_command(path)}:*)"


def codex_access(
    sandbox: str, *writable: str | Path | None, local: bool = True, guard: EnvGuard | None = None, network: bool = False,
) -> list[str]:
    """Codex 의 접근 범위 옵션: 샌드박스와, 작업 디렉토리 말고도 쓸 수 있는 폴더. guard 가 있으면 .env 읽기도 막는다.

    network 는 서버의 프로젝트를 다룰 때다: 명령이 ssh 로 서버에 닿아야 한다.
    """
    if guard is not None or network:
        # 권한 프로필은 -s 와 함께 쓸 수 없다: 샌드박스와 쓰기 폴더를 프로필 하나로 준다.
        roots = [str(Path(p).resolve()) if local else str(p) for p in writable if p]
        if guard is not None:
            return guard.codex_args(sandbox, roots, network=network)
        return codex_profile(sandbox, roots, network=network)
    roots = codex_writable_roots(*writable, local=local) if sandbox == "workspace-write" else []
    return ["-c", f'sandbox_mode="{sandbox}"', *roots]


def _codex_runner(
    spec: dict, sandbox: str, codex_bin: str, artifacts: str | None, local: bool, guard: EnvGuard | None = None,
    network: bool = False,
) -> str:
    model = codex_model(spec["model"])
    command = f'{shlex.quote(codex_bin)} exec -C "$(pwd -P)" --skip-git-repo-check'
    command += "".join(f" {shlex.quote(part)}" for part in codex_access(sandbox, artifacts, local=local, guard=guard, network=network))
    if model:
        command += f" -m {shlex.quote(model)}"
    command += ' -o "$OUT" -'
    prompt = str(spec.get("prompt") or "").strip()
    mark = "COLIDAY_ROLE_PROMPT"
    while mark in prompt:  # 역할 지침에 같은 줄이 있어도 heredoc 이 중간에 끊기지 않게
        mark += "_"
    return (
        "#!/bin/sh\n"
        "# coliday 가 만든 파일. 이 역할을 Codex CLI 로 실행한다: 작업 명세 파일(없으면 stdin)을 받아 Codex 의 답을 출력한다.\n"
        'if [ "$#" -gt 0 ] && [ ! -s "$1" ]; then echo "작업 명세 파일이 없거나 비어 있습니다: $1" >&2; exit 2; fi\n'
        'OUT=$(mktemp) && LOG=$(mktemp) || exit 1\n'
        "trap 'rm -f \"$OUT\" \"$LOG\"' EXIT\n"
        # 명세 파일로 불렀으면 끝날 때 <명세>.out(답·실패 내용)과 <명세>.done(종료 코드)을 남긴다.
        # 명령이 백그라운드로 넘어가도 래퍼가 프로세스를 찾지 않고 이 파일로 끝났는지 안다.
        'RES=/dev/null; DONE=""\n'
        'if [ "$#" -gt 0 ]; then RES="$1.out"; DONE="$1.done"; rm -f "$RES" "$DONE"; fi\n'
        "{\n"
        f"cat <<'{mark}'\n{prompt}\n{mark}\n"
        "printf '\\n# 작업 명세\\n'\n"
        'if [ "$#" -gt 0 ]; then cat "$1"; else cat; fi\n'
        f'}} | {command} > "$LOG" 2>&1\n'
        "code=$?\n"
        'if [ "$code" -ne 0 ]; then\n'
        '  { echo "codex 실행 실패 (종료 코드 $code)"; tail -n 40 "$LOG"; } | tee "$RES" >&2\n'
        "else\n"
        '  tee "$RES" < "$OUT"\n'
        "fi\n"
        'if [ -n "$DONE" ]; then echo "$code" > "$DONE"; fi\n'
        'exit "$code"\n'
    )


def without_codex(resolved: dict[str, dict]) -> tuple[dict[str, dict], list[str]]:
    """Codex 로 일할 팀원을 Claude 로 돌린 정의와 그 팀원 이름들. Codex CLI 가 없는 곳에서 쓴다.

    모델을 빼면 팀장과 같은 Claude 모델로 일한다. 역할 지침과 도구는 그대로다.
    """
    names = [name for name, spec in resolved.items() if is_codex(spec.get("model"))]
    return {n: {k: v for k, v in s.items() if not (n in names and k == "model")} for n, s in resolved.items()}, names


def with_note(resolved: dict[str, dict], note: str) -> dict[str, dict]:
    """팀원 지침 뒤에 이 작업에만 해당하는 안내(note)를 덧붙인다."""
    if not note:
        return resolved
    return {name: {**spec, "prompt": f"{spec.get('prompt') or ''}\n{note}"} for name, spec in resolved.items()}


def for_server(resolved: dict[str, dict], note: str) -> dict[str, dict]:
    """서버의 프로젝트를 다루는 작업의 팀원 정의: 지침에 다루는 방법(note)을 덧붙이고, 그 명령을 실행할 Bash 를 준다."""
    out: dict[str, dict] = {}
    for name, spec in resolved.items():
        spec = {**spec, "prompt": f"{spec.get('prompt') or ''}\n{note}"}
        tools = spec.get("tools")
        if isinstance(tools, list) and "Bash" not in tools:
            spec["tools"] = [*tools, "Bash"]
        out[name] = spec
    return out


def codex_writable_roots(*paths: str | Path | None, local: bool = True) -> list[str]:
    """Codex 샌드박스(workspace-write)에서 작업 디렉토리 말고도 쓸 수 있게 열 폴더를 주는 옵션."""
    # Codex 는 심볼릭 링크가 낀 쓰기 폴더를 거부하고 모든 명령 실행을 막는다 (macOS 의 임시 폴더 /var → /private/var 도 링크다).
    # 서버의 경로(local=False)는 이 컴퓨터에서 풀면 엉뚱한 경로가 된다 (/var/lib/… → /private/var/lib/…).
    roots = [str(Path(p).resolve()) if local else str(p) for p in paths if p]
    return ["-c", f"sandbox_workspace_write.writable_roots={json.dumps(roots)}"] if roots else []


def codex_sandbox(spec: dict, remote: bool = False) -> str:
    """이 역할을 Codex 로 돌릴 때의 샌드박스. 수정 도구가 없는 역할(설계·리뷰)은 파일을 못 고치게 한다.

    서버의 프로젝트(remote)면 누구든 작업 폴더에 쓸 수 있다: 그 폴더는 서버의 파일을 가져다 보는 곳일 뿐이다.
    """
    if remote:
        return "workspace-write"
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
    artifacts: str | None = None, template: str | None = None,
) -> str:
    """Codex 팀장 지시문: Claude 용과 같되, 팀원에게 맡기는 방법만 셸 명령으로 바꾼다."""
    how = (
        "## 팀원에게 맡기는 방법\n"
        "서브에이전트 도구 대신 아래 셸 명령으로 맡긴다. <역할> 자리에 팀원 이름을 쓰고, 작업 명세는 stdin 으로 준다:\n\n"
        f"{agent_command} <역할> <<'COLIDAY_TASK'\n<작업 명세>\nCOLIDAY_TASK\n\n"
        "- 명령은 팀원이 일을 끝낼 때까지 기다렸다가 그 팀원의 보고를 출력한다. 몇 분 이상 걸릴 수 있으니\n"
        "  명령 실행 시간 제한을 60분(3600000ms)으로 지정하고, 끝나기 전에 다시 실행하지 않는다.\n"
        "- 실행 도구가 명령이 끝나기 전에 세션 ID를 반환하면 그 ID로 기존 세션을 계속 기다리고, "
        "종료 상태와 팀원 보고를 확인한다. 새 명령을 시작하지 않는다.\n"
        "- 팀원은 이 대화를 볼 수 없다. 명세에 필요한 맥락(목표, 관련 파일, 제약)을 모두 적는다.\n"
        "- 이 명령 말고 다른 방법으로 팀원을 흉내 내지 않는다.\n\n"
        f"### 팀원\n{_roles(resolved)}\n"
    )
    return _render_lead(template, how, max_attempts, artifacts)


def _codex_wrapper(spec: dict, runner: str, task_prefix: str) -> dict:
    """팀원에게 온 작업 명세를 그대로 Codex 실행 파일(`codex_runners`)에 넘기는 Claude 래퍼 정의."""
    prompt = (
        "너는 Codex CLI 를 실행해 주는 얇은 래퍼다. 맡은 일을 직접 하지 않는다 (파일을 고치거나 직접 조사해 답하지 않는다).\n"
        f"1. Write 도구로 받은 작업 명세 전문을 고치지 말고 새 파일에 쓴다. 경로는 {task_prefix}-<임의의 숫자 6자리>.md 로 한다 "
        "(같은 역할이 동시에 불려도 겹치지 않게). Write 는 이 파일을 쓰는 데만 쓴다.\n"
        "2. 다음 명령을 실행한다. 다른 명령과 묶지 않고(&&, ;, |, 리다이렉션 금지) 이 명령 하나만 그대로 실행한다. "
        "오래 걸릴 수 있으니 실행 시간 제한을 최대로 지정하고 끝날 때까지 기다린다:\n"
        f"{runner_command(runner)} <1에서 쓴 파일의 경로>\n"
        "   명령이 백그라운드로 넘어갔으면 완료 알림을 기다린다. 명령은 끝날 때 <1에서 쓴 파일의 경로>.done(종료 코드)과 "
        ".out(Codex 의 답)을 남기니, 확인이 필요하면 `cat <경로>.done <경로>.out` 처럼 그 두 파일만 읽는다. "
        "pgrep·ps 로 프로세스를 찾거나 sleep 반복문으로 기다리지 않는다 "
        "(이 경로는 팀장과 그 반복문 자신의 명령줄에도 들어 있어 늘 찾아지므로 반복문이 끝나지 않는다).\n"
        "3. 끝나면 `git status --short`, `git diff --stat` 을 읽는다.\n"
        "4. 보고: 명령이 출력한 Codex 의 답을 빠짐없이 전하고, 변경 파일 목록과 Codex 가 보고한 검사 결과를 적는다 "
        "(직접 확인하지 않은 것은 그렇게 표시). 명령이 실패했거나 실행이 거부됐으면 오류 메시지를 그대로 보고하고 대신 일하지 않는다."
    )
    return {"description": spec.get("description", ""), "prompt": prompt, "tools": ["Bash", "Read", "Write"], "model": "haiku"}


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
    template: str | None = None,
) -> list[tuple[str, str]]:
    """(제목, 프롬프트) 목록. 실제 실행에 쓰이는 내용 그대로다. role 을 주면 그 역할만."""
    if role is not None and role != LEAD_ROLE and role not in agents:
        raise ValueError(f"알 수 없는 역할: {role} (가능: {', '.join([LEAD_ROLE, *agents])})")
    out: list[tuple[str, str]] = []
    resolved = resolve_agents(agents, models, max_attempts, ARTIFACTS_PLACEHOLDER)
    for name, model, _ in model_rows(agents, models):
        if role not in (None, name) or name == DEFAULT_ROLE:
            continue
        if name == LEAD_ROLE:
            prompt = lead_prompt(max_attempts, ARTIFACTS_PLACEHOLDER, resolved, template)
            if is_codex(lead_spec(models)):
                prompt = codex_lead_prompt(
                    resolved, "coliday _agent <작업별 임시 폴더>", max_attempts, ARTIFACTS_PLACEHOLDER, template
                )
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
