"""테스트용 가짜 codex. 호출을 기록하고 `codex exec --json` 형태의 이벤트를 출력한다.

FAKE_CODEX_DELEGATE=<역할> 이면 팀장으로 불렸을 때 지시문에 적힌 명령으로 그 팀원에게 일을 맡긴다.
"""
import json, os, re, subprocess, sys
from pathlib import Path

stdin = sys.stdin.read()
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
role = os.environ.get("FAKE_CODEX_DELEGATE")
if is_lead and role:
    delegate = re.search(r"^(\S+ -m coliday _agent \S+) <역할>", stdin, re.M).group(1)
    command(f"{delegate} {role}")
    done = subprocess.run(delegate.split() + [role], input="로그인 구현\n세부 명세", capture_output=True, text=True)
    emit(type="item.completed", item={"type": "agent_message",
                                      "text": f"팀원 보고({done.returncode}): {(done.stdout + done.stderr).strip()}"})
command("ls -la")
emit(type="item.completed", item={"type": "file_change", "changes": [{"path": "/repo/src/a.py", "kind": "update"}]})
emit(type="item.completed", item={"type": "agent_message", "text": "코덱스 최종 보고\n둘째 줄" if is_lead else "코덱스 팀원 보고"})
code = int(os.environ.get("FAKE_CODEX_EXIT", "0"))
if code:
    emit(type="turn.failed", error={"message": "codex boom"})
    sys.stderr.write("codex stderr tail\n")
else:
    emit(type="turn.completed", usage={"input_tokens": 1, "output_tokens": 1})
sys.exit(code)
