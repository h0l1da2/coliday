"""테스트용 가짜 codex. 호출을 기록하고 `codex exec --json` 형태의 이벤트를 출력한다.

FAKE_CODEX_DELEGATE=<역할>[,<역할>…] 이면 팀장으로 불렸을 때 지시문에 적힌 명령으로 그 팀원에게 일을 맡긴다.
"""
import json, os, re, subprocess, sys
from pathlib import Path

if sys.argv[1:] == ["--version"]:  # 버전 확인은 기록하지 않는다
    print(os.environ.get("FAKE_CODEX_VERSION", "codex-cli 0.171.0"))
    sys.exit(0)

stdin = sys.stdin.read()
if "--ephemeral" in sys.argv:
    # 작업을 묶는 짧은 호출 (topics.py). 팀장·팀원 실행 기록(FAKE_CODEX_ARGS)에 섞이지 않게 따로 남긴다.
    if os.environ.get("FAKE_CODEX_TOPIC_ARGS"):
        with open(os.environ["FAKE_CODEX_TOPIC_ARGS"], "a") as f:
            f.write(json.dumps({"argv": sys.argv[1:], "stdin": stdin, "cwd": os.getcwd()}, ensure_ascii=False) + "\n")
    Path(sys.argv[sys.argv.index("-o") + 1]).write_text(
        os.environ.get("FAKE_CODEX_TOPIC", '{"turns": [{"turn": 1, "bundle": null, "name": "주문 취소", "line": "한 줄"}], '
                       '"summaries": [{"bundle": null, "name": "주문 취소", "summary": "요약"}]}')
    )
    sys.exit(0)
record = os.environ.get("FAKE_CODEX_ARGS")
if record:
    with open(record, "a") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "stdin": stdin, "cwd": os.getcwd()}, ensure_ascii=False) + "\n")


def emit(**event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


def command(text):
    item = {"id": "c", "type": "command_execution", "command": f"/bin/zsh -lc '{text}'", "status": "in_progress"}
    emit(type="item.started", item=item)
    emit(type="item.completed", item={**item, "status": "completed", "exit_code": 0})


emit(type="thread.started", thread_id="t-1")
emit(type="turn.started")
is_lead = "# 사용자 요청" in stdin
roles = [r for r in os.environ.get("FAKE_CODEX_DELEGATE", "").split(",") if r]  # 쉼표로 여러 번 차례로 맡긴다
if is_lead and roles:
    delegate = re.search(r"^(\S+ -m coliday _agent \S+) <역할>", stdin, re.M).group(1)
    for role in roles:
        command(f"{delegate} {role}")
        done = subprocess.run(delegate.split() + [role], input="로그인 구현\n세부 명세", capture_output=True, text=True)
        emit(type="item.completed", item={"type": "agent_message",
                                          "text": f"팀원 보고({done.returncode}): {(done.stdout + done.stderr).strip()}"})
command("ls -la")
emit(type="item.completed", item={"type": "file_change", "changes": [{"path": "/repo/src/a.py", "kind": "update"}]})
emit(type="item.completed", item={"type": "agent_message", "text": "코덱스 최종 보고\n둘째 줄" if is_lead else "코덱스 팀원 보고"})
if "-o" in sys.argv:  # codex exec -o <파일>: 마지막 답을 파일에도 남긴다
    Path(sys.argv[sys.argv.index("-o") + 1]).write_text("코덱스 팀원 보고\n")
code = int(os.environ.get("FAKE_CODEX_EXIT", "0"))
if code:
    emit(type="turn.failed", error={"message": "codex boom"})
    sys.stderr.write("codex stderr tail\n")
else:
    emit(type="turn.completed", usage={"input_tokens": 1, "output_tokens": 1})
sys.exit(code)
