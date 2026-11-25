"""테스트용 가짜 ssh. 받은 명령을 이 컴퓨터의 셸에서 실행한다 (서버의 홈은 FAKE_SSH_HOME).

FAKE_SSH_LOG 에 (호스트, 명령)을 한 줄씩 남긴다. FAKE_SSH_FAIL 이 있으면 접속 실패(255)를 흉내 낸다.
FAKE_SSH_PASSWORD 가 있으면 인증을 흉내 낸다: 키 접속은 서버의 authorized_keys 가 있어야 되고,
비밀번호 접속(PubkeyAuthentication=no)은 SSH_ASKPASS 가 그 비밀번호를 답해야 된다.
"""
import json, os, subprocess, sys

args = sys.argv[1:]
options = []
while args and args[0].startswith("-"):
    width = 1 if args[0] == "-N" else 2  # -N(명령 없이 연결만 열어 둔다) 말고는 `-o 값` 꼴 옵션만 쓴다
    options += args[:width]
    args = args[width:]
host, command = args[0], " ".join(args[1:])
log = os.environ.get("FAKE_SSH_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"host": host, "command": command, "options": options}, ensure_ascii=False) + "\n")
if os.environ.get("FAKE_SSH_FAIL"):
    sys.stderr.write("Warning: something\nholiday@nowhere: Permission denied (publickey).\n")
    sys.exit(255)
expected = os.environ.get("FAKE_SSH_PASSWORD")
if expected:
    if "PubkeyAuthentication=no" in options:
        answer = subprocess.run([os.environ["SSH_ASKPASS"], "password:"], capture_output=True, text=True).stdout
        ok = os.environ.get("SSH_ASKPASS_REQUIRE") == "force" and answer == expected + "\n"
    else:
        ok = os.path.exists(os.path.join(os.environ["FAKE_SSH_HOME"], ".ssh", "authorized_keys"))
    if not ok:
        sys.stderr.write("dev@server: Permission denied (publickey,password).\n")
        sys.exit(255)
env = dict(os.environ, HOME=os.environ["FAKE_SSH_HOME"], SHELL="/bin/sh")
env.pop("COLIDAY_SSH_PASSWORD", None)
os.execve("/bin/sh", ["/bin/sh", "-c", command], env)
