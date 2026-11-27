"""테스트용 가짜 claude. 인자를 기록하고 fixture 이벤트를 출력한다."""
import json, os, sys, time
from pathlib import Path

if sys.argv[1:] == ["--version"]:  # 버전 확인은 기록하지 않는다
    print(os.environ.get("FAKE_CLAUDE_VERSION", "2.1.331 (Claude Code)"))
    sys.exit(0)

stdin_prompt = sys.stdin.read()
if "--tools" in sys.argv and sys.argv[sys.argv.index("--output-format") + 1] == "json":
    # 작업을 묶는 짧은 호출 (topics.py). 팀장 실행 기록(FAKE_CLAUDE_ARGS)을 덮어쓰지 않게 따로 남긴다.
    if os.environ.get("FAKE_CLAUDE_TOPIC_ARGS"):
        with open(os.environ["FAKE_CLAUDE_TOPIC_ARGS"], "a") as f:
            f.write(json.dumps({"argv": sys.argv[1:], "stdin": stdin_prompt, "cwd": os.getcwd()}, ensure_ascii=False) + "\n")
    time.sleep(float(os.environ.get("FAKE_CLAUDE_TOPIC_DELAY", "0")))  # 묶기가 도는 동안의 상태를 보는 테스트용
    answer = os.environ.get("FAKE_CLAUDE_TOPIC", json.dumps({
        "turns": [{"turn": 1, "bundle": None, "name": "주문 취소", "line": "취소 API 추가"}],
        "summaries": [{"bundle": None, "name": "주문 취소", "summary": "주문 취소 API 를 만들었다."}],
    }, ensure_ascii=False))
    if answer == "fail":
        sys.stderr.write("topic boom\n")
        sys.exit(1)
    print(json.dumps({"type": "result", "is_error": False, "result": answer}, ensure_ascii=False))
    sys.exit(0)
codex_login = None
if os.environ.get("FAKE_CLAUDE_CODEX_LOGIN") is not None and os.environ.get("CODEX_HOME"):
    login_file = Path(os.environ["CODEX_HOME"]) / "auth.json"  # 넘겨받은 Codex 로그인. 값이 있으면 그걸로 갱신한 것처럼 바꾼다
    codex_login = {"text": login_file.read_text(), "mode": oct(login_file.stat().st_mode & 0o777)}
    if os.environ["FAKE_CLAUDE_CODEX_LOGIN"]:
        login_file.write_text(os.environ["FAKE_CLAUDE_CODEX_LOGIN"])
record = os.environ.get("FAKE_CLAUDE_ARGS")
if record:
    Path(record).write_text(json.dumps(
        {"argv": sys.argv[1:], "stdin": stdin_prompt, "cwd": os.getcwd(), "pid": os.getpid(),
         "env": {k: os.environ.get(k) for k in os.environ.get("FAKE_CLAUDE_ENV", "").split(",") if k},
         "codex_login": codex_login,
         "auth": {k: os.environ[k] for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY") if k in os.environ}},
        ensure_ascii=False,
    ))
if os.environ.get("FAKE_CLAUDE_DISPATCH") and "--settings" in sys.argv:
    # 팀장이 Agent 도구로 팀원을 차례로 부른 것처럼 --settings 의 PreToolUse 훅을 실제로 실행하고 종료 코드를 남긴다
    import subprocess
    hooks = json.loads(sys.argv[sys.argv.index("--settings") + 1]).get("hooks", {}).get("PreToolUse", [])
    results = []
    for role in os.environ["FAKE_CLAUDE_DISPATCH"].split(","):
        payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Agent",
                              "tool_input": {"subagent_type": role, "description": "x", "prompt": "y"}})
        for entry in hooks:
            for hook in entry["hooks"]:
                done = subprocess.run(hook["command"], shell=True, input=payload, capture_output=True, text=True)
                results.append({"role": role, "code": done.returncode, "stderr": done.stderr})
    Path(os.environ["FAKE_CLAUDE_HOOK_LOG"]).write_text(json.dumps(results, ensure_ascii=False))
if os.environ.get("FAKE_CLAUDE_SLEEP"):
    time.sleep(float(os.environ["FAKE_CLAUDE_SLEEP"]))
if os.environ.get("FAKE_CLAUDE_ARTIFACT"):  # 결과물 폴더(--add-dir 로 받은 곳)에 스크린샷을 남긴 것처럼 한다
    target = Path(sys.argv[sys.argv.index("--add-dir") + 1])
    for name in os.environ["FAKE_CLAUDE_ARTIFACT"].split(","):
        (target / name).parent.mkdir(parents=True, exist_ok=True)
        (target / name).write_bytes(b"fake")
if os.environ.get("FAKE_CLAUDE_TOUCH"):
    Path(os.environ["FAKE_CLAUDE_TOUCH"]).write_text("new file\n")
sys.stdout.write((Path(__file__).parent / "stream.jsonl").read_text())
sys.stdout.write("not json line\n")
if os.environ.get("FAKE_CLAUDE_RESULT"):  # 마지막 보고를 바꾼다 (예: .env 허용을 기다리며 멈춘 보고)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": "s-1",
                      "result": os.environ["FAKE_CLAUDE_RESULT"]}, ensure_ascii=False))
code = int(os.environ.get("FAKE_CLAUDE_EXIT", "0"))
if code:
    sys.stderr.write("boom: something failed\n")
sys.exit(code)
