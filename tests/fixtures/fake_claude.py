"""테스트용 가짜 claude. 인자를 기록하고 fixture 이벤트를 출력한다."""
import json, os, sys, time
from pathlib import Path

if sys.argv[1:] == ["--version"]:  # 버전 확인은 기록하지 않는다
    print(os.environ.get("FAKE_CLAUDE_VERSION", "2.1.331 (Claude Code)"))
    sys.exit(0)

stdin_prompt = sys.stdin.read()
record = os.environ.get("FAKE_CLAUDE_ARGS")
if record:
    Path(record).write_text(json.dumps(
        {"argv": sys.argv[1:], "stdin": stdin_prompt, "cwd": os.getcwd(), "pid": os.getpid()},
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
