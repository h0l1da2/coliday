# coliday

팀장 에이전트에게 일을 시키고, 팀원(architect / coder / codex-coder / reviewer / verifier)이
일하는 과정을 터미널에서 추적하는 CLI. 역할마다 Claude Code 또는 Codex CLI 를 골라 쓸 수 있다.
Agent SDK 없이 `claude -p` 와 `codex exec` 를 직접 실행한다.

## 쓰기 전에

- [Claude Code](https://claude.com/claude-code) CLI(`claude`)가 설치돼 있고 로그인돼 있어야 한다. 사용량과 비용은 각자의 계정에 청구된다.
- 팀장은 사람의 권한 확인 없이 실행된다 (`--permission-mode auto`, 확인이 필요한 동작은 자동 거부).
  파일을 고치고 명령을 실행하므로 신뢰하는 레포에서만 쓴다.
- Python 3.11 이상. macOS 에서만 테스트했다. Linux 는 확인하지 않았고 Windows 는 지원하지 않는다.
- 팀장과 팀원은 역할마다 Claude 또는 [Codex CLI](https://github.com/openai/codex)(`codex`)로 바꿀 수 있다. Codex 를 쓰는 역할이 있으면
  `codex` 가 설치돼 있고 로그인돼 있어야 한다 (기본 설정에서는 `codex-coder` 만 Codex 를 쓴다).
- Anthropic 과 무관한 개인 프로젝트다.

## 설치

```bash
git clone https://github.com/h0l1da2/coliday.git
brew install uv            # 처음 한 번
uv tool install --editable ./coliday
```

`uv` 없이 쓰려면 venv 를 만들고 그 안의 실행 파일을 PATH 에 링크한다:

```bash
cd coliday && python3 -m venv .venv && .venv/bin/pip install -e .
ln -s "$PWD/.venv/bin/coliday" ~/.local/bin/coliday
```

## TUI

터미널에서 인자 없이 `coliday` 를 실행하면 열린다. 열었던 디렉토리가 git 저장소면 프로젝트 목록에 연결되고 선택된다.

```text
┌ 프로젝트 · 새 작업 위치: shop-be ┐
│ shop-be   ~/work_dir/shop-be     │
│ shop-fe   ~/work_dir/shop-fe     │
┌ 작업 목록 ───────────────────┐┌ 변경 사항 #12 ───────┐
│ ● 12 주문 취소 API   coder: Edit… ││ +public Order cancel │
├ 타임라인 #12 ────────────────┤│ ...                  │
│ 10:03 팀장   coder에게 분배      ││                      │
│ 10:07 coder  Edit OrderService  ││                      │
└──────────────────────────────┘└──────────────────────┘
│ 작업 #12 에 이어서 시킬 일 (Enter)  │
 아래 단축키는 Ctrl 키를 누른 채로 입력합니다 (예: Ctrl+N 새 작업)
 N 새 작업  O 프로젝트  G 프로젝트 추가  T 모델  P 프롬프트  S 작업 취소  R 작업 삭제  C 종료
```

- 타임라인 아래 입력창이 채팅 입력이다. 어디에 포커스가 있든 글자를 치면 입력창으로 들어가고, Enter 를 누르면
  선택한 작업에 이어서 시킨다 (같은 세션·같은 worktree, 타임라인도 이어진다). 작업이 하나도 없으면 고른 프로젝트에서
  새 작업으로 시작한다. Esc 로 작업 목록으로 돌아간다.
- 작업이 실행 중일 때 보낸 채팅은 대기했다가(타임라인에 ⏳ 로 표시) 지금 실행이 끝나는 대로 같은 세션에 이어서 전달된다.
  여러 개를 보내면 한 번에 묶어서 전달한다. 실행 도중에 끼어드는 것은 아니다. 작업을 취소하면 대기한 요청은 버려진다.
- 글자 키는 채팅에 쓰이므로 단축키는 모두 ctrl 조합이다: `ctrl+n` 새 작업(새 세션·새 worktree), `ctrl+o` 프로젝트 목록,
  `ctrl+g` 프로젝트 추가, `ctrl+t` 모델, `ctrl+p` 프롬프트, `ctrl+f` 결과물 폴더, `ctrl+s` 작업 취소, `ctrl+r` 작업 삭제, `ctrl+c` 종료.
- 처음 열면 프로젝트 목록에서 시작한다. ↑/↓ 로 작업 위치를 고르고 Enter 를 누르면 작업 목록으로 넘어간다.
  작업 목록에서 Enter 를 누르면 채팅 입력으로 간다.
- 맨 위는 연결된 프로젝트 디렉토리 목록이다. `ctrl+g` 로 디렉토리와 이름(비우면 폴더 이름)을 입력해 추가한다.
  목록에 포커스가 있을 때 `ctrl+e` 로 이름을 바꾸고 `ctrl+x` 로 목록에서 뺀다 (디렉토리와 작업은 그대로).
  작업을 시작한 디렉토리는 자동으로 연결된다.
- 목록에서 ↑/↓ 로 작업을 고르면 타임라인과 diff 가 바뀐다. 1초마다 갱신되고, 실행 중인 작업의 diff 는 2초마다 다시 계산한다.
- `ctrl+t` 로 기본 에이전트와 팀장·팀원의 에이전트·모델을 바꾼다 (아래 "Claude 와 Codex 골라 쓰기").
- `ctrl+p` 로 팀장·팀원에게 주는 프롬프트를 본다.
- 이어서 시킨 작업의 타임라인에는 이전 요청·진행 과정·팀장 보고가 모두 남는다. 마우스 휠이나 PgUp/PgDn(또는 alt+↑/↓)로
  올려 본다. 채팅 입력 중에도 PgUp/PgDn 은 타임라인을 넘긴다.
- 타임라인 아래에 선택한 작업의 결과물 폴더 경로가 항상 한 줄로 나온다 (사진·영상이 있으면 개수도 표시).
  그 줄을 클릭하거나 `ctrl+f` 를 누르면 폴더가 Finder 로 열려 사진·영상을 바로 볼 수 있다 (터미널 안에 그리지는 않는다).
  아직 비어 있어도 열 수 있고, 몇 번이든 다시 누를 수 있다.
- 타임라인과 변경 사항은 마우스로 드래그하면 선택한 부분이 바로 클립보드에 복사된다.
- `ctrl+c` 로 TUI 를 닫아도 작업은 백그라운드에서 계속된다. 다른 터미널에서 시킨 작업도 같은 목록에 보인다.

## CLI

```bash
cd ~/work_dir/some-project
coliday run "주문 취소 API 추가"      # 실행하며 진행 상황을 실시간 표시 (Ctrl+C 해도 작업은 계속)
coliday run -d "README 정리"          # 백그라운드로 시작만
coliday continue 3 "테스트도 추가해줘" # 작업 3 에 이어서 시키기 (같은 세션·같은 worktree, -d 가능). 실행 중이면 끝난 뒤 전달
coliday project                       # 연결된 프로젝트 디렉토리 목록
coliday project add ~/work_dir/shop --name 쇼핑몰   # 연결 (디렉토리 생략 시 현재 디렉토리, --name 생략 시 폴더 이름)
coliday project rm ~/work_dir/shop    # 목록에서 빼기
coliday model                         # 기본 에이전트와 팀장·팀원의 에이전트·모델 보기
coliday model default codex           # 기본 에이전트 바꾸기 (claude / codex)
coliday model coder claude:sonnet     # 역할 하나만 바꾸기 (--reset 으로 기본을 따르게)
coliday prompt                        # 팀장·팀원 프롬프트 보기 (coliday prompt coder 로 하나만)
coliday ls                            # 작업 목록 (텍스트)
coliday show 3 -f                     # 작업 3 타임라인 따라보기
coliday diff 3                        # 작업 3 이 만든 코드 변경
coliday open 3                        # 작업 3 이 남긴 결과물(스크린샷·영상) 폴더 열기
coliday cancel 3                      # 취소
coliday rm 3                          # 끝난 작업 삭제: 목록·기록과 worktree·브랜치 (변경이 남아 있으면 --force 필요)
```

- 작업마다 `~/.coliday/worktrees/<repo>-<id>` worktree 와 `coliday/<id>` 브랜치를 만들어 그 안에서 일한다.
  그래서 여러 터미널에서 동시에 시켜도 서로 파일을 덮어쓰지 않는다. 커밋하지 않은 변경은 worktree 에 포함되지 않는다.
- `ctrl+r`(TUI) / `rm` 으로 지운 작업은 목록에서 바로 사라지고 worktree·브랜치·로그도 함께 지워진다. 되돌릴 수 없으니
  남길 변경은 먼저 병합한다. worktree 에 변경이 남아 있으면 한 번 더 확인한다 (CLI 는 `--force`).
- git 저장소가 아니면 `--no-worktree` 로 현재 디렉토리에서 직접 작업한다 (이때는 동시 작업이 충돌할 수 있다).
- 팀장은 commit/push/merge 를 하지 않는다. 결과는 `diff` 로 확인한 뒤 직접 병합한다.

## 설정

| 환경변수 | 기본값 | 설명 |
| --- | --- | --- |
| `COLIDAY_HOME` | `~/.coliday` | DB, 로그, worktree 위치 |
| `COLIDAY_CLAUDE_BIN` | `claude` | claude 실행 파일 |
| `COLIDAY_PERMISSION_MODE` | `auto` | `claude --permission-mode` 값. 권한 확인이 필요한 동작은 자동 거부된다 |
| `COLIDAY_CODEX_BIN` | `codex` | codex 실행 파일 |
| `COLIDAY_OPEN_BIN` | `open` (macOS) / `xdg-open` | 결과물 폴더를 여는 명령 |
| `COLIDAY_MAX_ATTEMPTS` | `5` | 실패한 작업을 처음 포함해 몇 번까지 시도할지 (1 이상의 정수) |

어떤 작업(빌드, 테스트, 재작업 등)이 실패하면 팀원과 팀장은 같은 작업을 최대 `COLIDAY_MAX_ATTEMPTS` 번까지만 시도한다.
그래도 안 되면 팀원은 멈추고 시도 내용과 마지막 오류를 팀장에게 넘기고, 팀장은 코드를 동작하는 상태로 정리한 뒤
최종 보고에 그 작업을 "실패"로 적는다. 이 값은 팀장·팀원에게 주는 지침에 들어가는 것이라 `coliday prompt` 로 확인할 수 있다
(에이전트가 따르는 규칙이지, 프로그램이 횟수를 세어 강제로 끊는 것은 아니다). 바꾼 값은 다음 실행부터 적용된다.

모델 설정은 `~/.coliday/models.json` 에 저장되고 다음 실행(새 작업·이어서 시키기)부터 적용된다. 실행 중인 작업은 바뀌지 않는다.
팀장은 `run --model` / `continue --model` 로 작업별로 덮어쓸 수 있고 (예: `--model codex`, `--model claude:opus`),
따로 고르지 않았으면 기본 에이전트를 따른다.
모델 이름이 실제로 있는지는 실행할 때 claude / codex 가 확인한다 (틀리면 그 작업이 실패로 표시된다).

### Claude 와 Codex 골라 쓰기

기본 에이전트(`claude` 또는 `codex`)를 하나 고르고, 필요한 역할만 따로 바꾼다. 팀장과 팀원 모두 가능하다.

| 값 | 뜻 |
| --- | --- |
| `claude` / `codex` | 그 에이전트의 기본 모델 |
| `claude:opus` / `codex:gpt-6.1-sol` | 그 에이전트의 특정 모델 |

모델 이름은 모두 소문자로, 띄어쓰기 없이 하이픈으로 쓴다 (`gpt-6.1-sol` ○, `GPT-6.1 Sol` ✗).

```bash
coliday model                             # 현재 설정 보기
coliday model default codex               # 기본 에이전트를 Codex 로: 따로 고르지 않은 역할이 모두 Codex
coliday model lead claude:opus            # 팀장만 Claude 의 opus 로
coliday model reviewer codex:gpt-6.1-sol  # 리뷰어만 Codex 의 특정 모델로
coliday model reviewer --reset            # 그 역할을 다시 기본 에이전트를 따르게
```

TUI 에서는 `ctrl+t` 창 맨 위에서 기본 에이전트를 고르고, 역할마다 에이전트(기본을 따름 / claude / codex)와 모델을 고른다.

- 기본 에이전트가 `claude` 면 팀원 정의에 적힌 Claude 모델(architect·reviewer 는 opus)을 쓰고, `codex-coder` 는 Codex 로 일한다.
  기본 에이전트가 `codex` 면 따로 고르지 않은 역할은 모두 Codex 기본 모델로 일한다.
- Claude 팀장 아래의 Codex 팀원은 `codex exec` 를 대신 실행해 주는 작은 Claude(haiku) 에이전트를 거쳐 일한다.
- Codex 팀장은 샌드박스(workspace-write) 안에서 실행되고, 팀원에게는 셸 명령으로 일을 맡긴다. 팀원은 coliday 가 샌드박스 밖에서 실행한다.
- Codex 로 실행하는 역할은 수정 도구(Edit/Write)가 없으면 읽기 전용 샌드박스로 돈다 (architect, reviewer).
  팀원 정의에 `"codex_sandbox": "workspace-write"` 를 두면 쓰기를 허용한다 (verifier 는 빌드 산출물 때문에 기본으로 허용).
- Codex 는 비용을 알려 주지 않아, Codex 가 쓴 몫은 작업 비용에 포함되지 않는다.
- 이어서 시킬 때 팀장을 Claude↔Codex 로 바꾸면 이전 대화는 이어지지 않고 새 세션으로 시작한다 (작업 디렉토리는 그대로).

팀원 정의를 바꾸려면 `~/.coliday/agents.json` 에 `claude --agents` 형식 JSON 을 둔다
(기본값은 `src/coliday/team.py` 의 `DEFAULT_AGENTS`). 프로젝트별 규칙은 각 레포의 `AGENTS.md` / `CLAUDE.md` 를 따른다.

## 화면 작업의 결과물

웹 화면·UI 를 만들거나 바꾸는 작업이면 팀원(구현·검증 담당)이 실제로 실행해서 스크린샷(.png)이나 녹화 영상(.webm, .mp4)을
`~/.coliday/jobs/<id>/artifacts/` 에 남기고, 팀장은 최종 보고에 파일 이름과 내용을 적는다.

- 찍는 도구는 프로젝트에 있는 것(Playwright 등)을 쓰고, 없으면 `npx playwright` 처럼 임시로 실행한다 (의존성에 추가하지 않는다).
- 사진과 영상은 하위 폴더 없이 한 폴더에 번호를 붙여 늘어놓는다 (`01-login-page.png`, `02-order-flow.webm`).
  실행이 끝나면 coliday 가 폴더를 정리한다: 하위 폴더에 있던 사진·영상은 맨 위로 꺼내고(`verify/ok.png` → `verify-ok.png`),
  찍는 데 쓴 HTML·스크립트·로그 같은 나머지 파일은 지우지 않고 `_other/` 로 모은다. 그래서 폴더를 열면 사진·영상만 한눈에 보인다.
  실행 중에는 팀원이 쓰고 있을 수 있어 정리하지 않는다.
- 로그인이나 백엔드가 필요해 찍지 못하면 "찍지 못함"과 이유를 보고한다. 그때는 폴더가 비어 있다.
- 이어서 시키면 같은 폴더에 쌓이고, 작업을 삭제하면 결과물도 함께 지워진다. 남길 파일은 먼저 옮긴다.
- 실제로 찍을지는 팀장·팀원이 작업 내용을 보고 판단한다 (지침으로 주는 것이라 `coliday prompt` 에서 볼 수 있다).

## 저장 위치

- `~/.coliday/jobs.db`: 작업·이벤트 (SQLite)
- `~/.coliday/projects.json`: 연결된 프로젝트 디렉토리, `models.json`: 모델 설정
- `~/.coliday/jobs/<id>/stream.jsonl`: 팀장 원본 이벤트, `stderr.log`, `worker.log`, `artifacts/`(결과물)

## 개발

```bash
.venv/bin/python -m pytest -q
```

## 라이선스

[MIT](LICENSE)
