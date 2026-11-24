"""테스트용 가짜 claude. 인자를 기록하고 fixture 이벤트를 출력한다."""
import json, os, sys, time
from pathlib import Path

if sys.argv[1:] == ["--version"]:  # 버전 확인은 기록하지 않는다
    print(os.environ.get("FAKE_CLAUDE_VERSION", "2.1.331 (Claude Code)"))
    sys.exit(0)

stdin_prompt = sys.stdin.read()
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
code = int(os.environ.get("FAKE_CLAUDE_EXIT", "0"))
if code:
    sys.stderr.write("boom: something failed\n")
sys.exit(code)
