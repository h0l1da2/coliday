"""테스트용 가짜 ssh. 받은 명령을 이 컴퓨터의 셸에서 실행한다 (서버의 홈은 FAKE_SSH_HOME).

FAKE_SSH_LOG 에 (호스트, 명령)을 한 줄씩 남긴다. FAKE_SSH_FAIL 이 있으면 접속 실패(255)를 흉내 낸다.
"""
import json, os, sys

args = sys.argv[1:]
options = []
while args and args[0].startswith("-"):
    options += args[:2]
    args = args[2:]  # 테스트 대상은 `-o 값` 꼴 옵션만 쓴다
host, command = args[0], " ".join(args[1:])
log = os.environ.get("FAKE_SSH_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"host": host, "command": command, "options": options}, ensure_ascii=False) + "\n")
if os.environ.get("FAKE_SSH_FAIL"):
    sys.stderr.write("Warning: something\nholiday@nowhere: Permission denied (publickey).\n")
    sys.exit(255)
env = dict(os.environ, HOME=os.environ["FAKE_SSH_HOME"], SHELL="/bin/sh")
os.execve("/bin/sh", ["/bin/sh", "-c", command], env)
