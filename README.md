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
  새 작업으로 시작한다. 실행 중인 작업에는 보낼 수 없고, 쓴 내용은 입력창에 남는다. Esc 로 작업 목록으로 돌아간다.
- 글자 키는 채팅에 쓰이므로 단축키는 모두 ctrl 조합이다: `ctrl+n` 새 작업(새 세션·새 worktree), `ctrl+o` 프로젝트 목록,
  `ctrl+g` 프로젝트 추가, `ctrl+t` 모델, `ctrl+p` 프롬프트, `ctrl+s` 작업 취소, `ctrl+r` 작업 삭제, `ctrl+c` 종료.
- 처음 열면 프로젝트 목록에서 시작한다. ↑/↓ 로 작업 위치를 고르고 Enter 를 누르면 작업 목록으로 넘어간다.
  작업 목록에서 Enter 를 누르면 채팅 입력으로 간다.
- 맨 위는 연결된 프로젝트 디렉토리 목록이다. `ctrl+g` 로 디렉토리와 이름(비우면 폴더 이름)을 입력해 추가한다.
  목록에 포커스가 있을 때 `ctrl+e` 로 이름을 바꾸고 `ctrl+x` 로 목록에서 뺀다 (디렉토리와 작업은 그대로).
  작업을 시작한 디렉토리는 자동으로 연결된다.
- 목록에서 ↑/↓ 로 작업을 고르면 타임라인과 diff 가 바뀐다. 1초마다 갱신되고, 실행 중인 작업의 diff 는 2초마다 다시 계산한다.
- `ctrl+t` 로 팀장·팀원 모델을 바꾼다. 칸을 비우면 기본값으로 돌아간다.
- `ctrl+p` 로 팀장·팀원에게 주는 프롬프트를 본다.
- 이어서 시킨 작업의 타임라인에는 이전 요청·진행 과정·팀장 보고가 모두 남는다. 마우스 휠이나 PgUp/PgDn(또는 alt+↑/↓)로
  올려 본다. 채팅 입력 중에도 PgUp/PgDn 은 타임라인을 넘긴다.
- 타임라인과 변경 사항은 마우스로 드래그하면 선택한 부분이 바로 클립보드에 복사된다.
- `ctrl+c` 로 TUI 를 닫아도 작업은 백그라운드에서 계속된다. 다른 터미널에서 시킨 작업도 같은 목록에 보인다.

## CLI

```bash
cd ~/work_dir/some-project
coliday run "주문 취소 API 추가"      # 실행하며 진행 상황을 실시간 표시 (Ctrl+C 해도 작업은 계속)
coliday run -d "README 정리"          # 백그라운드로 시작만
coliday continue 3 "테스트도 추가해줘" # 끝난 작업 3 에 이어서 시키기 (같은 세션·같은 worktree, -d 가능)
coliday project                       # 연결된 프로젝트 디렉토리 목록
coliday project add ~/work_dir/shop --name 쇼핑몰   # 연결 (디렉토리 생략 시 현재 디렉토리, --name 생략 시 폴더 이름)
coliday project rm ~/work_dir/shop    # 목록에서 빼기
coliday model                         # 팀장·팀원 모델 보기
coliday model lead opus               # 팀장 모델 바꾸기
coliday model coder sonnet            # 팀원 모델 바꾸기 (--reset 으로 기본값 복원)
coliday prompt                        # 팀장·팀원 프롬프트 보기 (coliday prompt coder 로 하나만)
coliday ls                            # 작업 목록 (텍스트)
coliday show 3 -f                     # 작업 3 타임라인 따라보기
coliday diff 3                        # 작업 3 이 만든 코드 변경
coliday cancel 3                      # 취소
coliday rm 3                          # 끝난 작업 삭제: 목록·기록과 worktree·브랜치 (변경이 남아 있으면 --force 필요)
```

- 작업마다 `~/.coliday/worktrees/<repo>-<id>` worktree 와 `coliday/<id>` 브랜치를 만들어 그 안에서 일한다.
  그래서 여러 터미널에서 동시에 시켜도 서로 파일을 덮어쓰지 않는다. 커밋하지 않은 변경은 worktree 에 포함되지 않는다.
- 이어서 시키기는 끝난 작업(성공·실패·취소)에만 할 수 있다.
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

모델 설정은 `~/.coliday/models.json` 에 저장되고 다음 실행(새 작업·이어서 시키기)부터 적용된다. 실행 중인 작업은 바뀌지 않는다.
팀장 모델은 `run --model` / `continue --model` 로 작업별로 덮어쓸 수 있고, 설정이 없으면 claude 기본 모델을 쓴다.
모델 이름은 `opus`, `sonnet`, `haiku` 같은 별칭이나 전체 모델 id 를 쓴다 (이름이 맞는지는 실행할 때 claude 가 확인한다).

### Claude 와 Codex 섞어 쓰기

모델 자리에 `codex`(Codex 기본 모델) 또는 `codex:<모델>` 을 쓰면 그 역할은 Codex CLI 로 일한다. 팀장과 팀원 모두 가능하다.

```bash
coliday model lead codex              # 팀장을 Codex 로
coliday model reviewer codex:gpt-5    # 리뷰어만 Codex 의 특정 모델로
coliday model codex-coder sonnet      # 기본이 Codex 인 팀원을 Claude 로
coliday model lead --reset            # 되돌리기
```

- Claude 팀장 아래의 Codex 팀원은 `codex exec` 를 대신 실행해 주는 작은 Claude(haiku) 에이전트를 거쳐 일한다.
- Codex 팀장은 샌드박스(workspace-write) 안에서 실행되고, 팀원에게는 셸 명령으로 일을 맡긴다. 팀원은 coliday 가 샌드박스 밖에서 실행한다.
- Codex 로 실행하는 역할은 수정 도구(Edit/Write)가 없으면 읽기 전용 샌드박스로 돈다 (architect, reviewer).
  팀원 정의에 `"codex_sandbox": "workspace-write"` 를 두면 쓰기를 허용한다 (verifier 는 빌드 산출물 때문에 기본으로 허용).
- Codex 는 비용을 알려 주지 않아, Codex 가 쓴 몫은 작업 비용에 포함되지 않는다.
- 이어서 시킬 때 팀장을 Claude↔Codex 로 바꾸면 이전 대화는 이어지지 않고 새 세션으로 시작한다 (작업 디렉토리는 그대로).

팀원 정의를 바꾸려면 `~/.coliday/agents.json` 에 `claude --agents` 형식 JSON 을 둔다
(기본값은 `src/coliday/team.py` 의 `DEFAULT_AGENTS`). 프로젝트별 규칙은 각 레포의 `AGENTS.md` / `CLAUDE.md` 를 따른다.

## 저장 위치

- `~/.coliday/jobs.db`: 작업·이벤트 (SQLite)
- `~/.coliday/projects.json`: 연결된 프로젝트 디렉토리, `models.json`: 모델 설정
- `~/.coliday/jobs/<id>/stream.jsonl`: claude 원본 이벤트, `stderr.log`, `worker.log`

## 개발

```bash
.venv/bin/python -m pytest -q
```

## 라이선스

[MIT](LICENSE)
