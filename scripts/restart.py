#!/usr/bin/env python3
"""codex-lens 를 띄우고·끄고·다시 띄운다 — 진행 중인 Codex 세션은 끊지 않는다.

    python3 scripts/restart.py            # = restart (안 떠 있으면 띄우기만)
    python3 scripts/restart.py start | stop | status

설정은 앱과 같은 환경변수(CODEX_LENS_HOME·CODEX_LENS_DEMO·CODEX_LENS_HOST·CODEX_LENS_PORT …)를 그대로 넘긴다.
끄는 것은 이 체크아웃·데이터 홈의 앱 프로세스 하나뿐이다 — codex app-server 를 쥔 중계기(codex_broker.py)는
남겨 두어 돌던 턴이 이어지고, 다시 뜬 앱이 거기 다시 붙는다. 그래서 `pkill -f "codex app-server"` 같은 이름 검색으로
끄지 않는다(중계기 명령줄에도 그 글자가 있다).

앱 찾기: <데이터 홈>/server.json 의 pid(앱이 뜰 때 적는다). 없거나 맞지 않으면(옛 버전) 명령줄이 `python… app.py` 이고
작업 폴더가 이 체크아웃(지운 뒤 다시 받은 폴더 포함)이며 데이터 홈이 같은 프로세스를 찾는다.
앱 출력은 <데이터 홈>/logs/server.log 에 덧붙인다. 표준 라이브러리만 쓴다(macOS·Linux).
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import codex_broker  # noqa: E402  (소켓 경로 규칙을 앱과 같이 쓴다)
APP = os.path.join(ROOT, "app.py")
DEMO = os.environ.get("CODEX_LENS_DEMO") == "1"
HOME = os.path.abspath(os.path.expanduser(os.environ.get("CODEX_LENS_HOME")
                                          or ("~/.codex-lens-demo" if DEMO else "~/.codex-lens")))
import local_env  # noqa: E402

local_env.load(HOME)   # 앱과 같은 <데이터 홈>/env 를 읽어 주소·포트를 맞춘다
HOST = os.environ.get("CODEX_LENS_HOST", "127.0.0.1")
PORT = int(os.environ.get("CODEX_LENS_PORT") or os.environ.get("PORT") or (18761 if DEMO else 8761))
START_TIMEOUT = float(os.environ.get("CODEX_LENS_START_TIMEOUT_SEC") or 30)


def url():
    h = HOST.strip("[]")
    h = "127.0.0.1" if h in ("", "0.0.0.0") else ("::1" if h == "::" else h)
    return "http://%s:%d/" % ("[%s]" % h if ":" in h else h, PORT)


def say(msg):
    print(msg, flush=True)


# --- 프로세스 살피기 -------------------------------------------------------
def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # 좀비(부모가 거두지 않음)는 끝난 것으로 본다
    st = _ps(pid, "stat")
    return not st.startswith("Z")


def _ps(pid, field):
    try:
        return subprocess.run(["ps", "-o", "%s=" % field, "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def _args(pid):
    """명령줄 한 줄(리눅스는 /proc, 그 밖은 ps)."""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            return " ".join(a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a)
    except OSError:
        return _ps(pid, "args")


def _cwd(pid):
    try:
        p = os.readlink("/proc/%d/cwd" % pid)
    except OSError:
        try:
            out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True).stdout
        except OSError:
            return ""
        p = next((ln[1:] for ln in out.splitlines() if ln.startswith("n")), "")
    return p[:-len(" (deleted)")] if p.endswith(" (deleted)") else p


ENV_RE = re.compile(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=(.*?)(?=\s[A-Za-z_][A-Za-z0-9_]*=|$)")


def _env(pid):
    try:
        with open("/proc/%d/environ" % pid, "rb") as f:
            pairs = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
    except OSError:
        try:
            out = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
        except OSError:
            return {}
        # macOS: 명령줄 뒤에 KEY=값 이 공백으로 이어진다 — 값 안의 공백(Application Support 등)은 다음 KEY= 까지 살린다
        return {m.group(1): m.group(2) for m in ENV_RE.finditer(out.strip())}
    return dict(p.split("=", 1) for p in pairs if "=" in p)


APP_RE = re.compile(r"(^|/)python[\w.]*\s+(-\S+\s+)*(?P<script>(\S.*/)?app\.py)(\s|$)", re.I)


def _is_app(args):
    """`python… app.py` 또는 `python… /경로/app.py` 꼴인지 (macOS ps 는 실행 파일을 …/Python 으로 보인다)."""
    return bool(APP_RE.search(args or ""))


def _home_of(env):
    demo = env.get("CODEX_LENS_DEMO") == "1"
    h = env.get("CODEX_LENS_HOME") or ("~/.codex-lens-demo" if demo else "~/.codex-lens")
    if h.startswith("~"):
        h = (env.get("HOME") or os.path.expanduser("~")) + h[1:]
    return os.path.abspath(h)


def _same_root(pid, args):
    if (ROOT + "/app.py") in args:
        return True
    m = APP_RE.search(args)
    if m and m.group("script") == "app.py":     # 상대 경로 — 작업 폴더로 가린다
        return os.path.realpath(_cwd(pid) or "/nonexistent") == os.path.realpath(ROOT)
    return False


def find_app():
    """이 체크아웃·데이터 홈의 앱 pid (없으면 None)."""
    try:
        with open(os.path.join(HOME, "server.json")) as f:
            pid = int(json.load(f).get("pid") or 0)
        if pid and _alive(pid) and _is_app(_args(pid)):
            return pid
    except (OSError, ValueError, TypeError):
        pass
    # 옛 버전은 server.json 이 없다 — 명령줄로 먼저 거르고, 작업 폴더·데이터 홈으로 가린다
    try:
        out = subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True, text=True).stdout
    except OSError:
        return None
    me = os.getpid()
    for line in out.splitlines():
        tok = line.strip().split(None, 1)
        if len(tok) < 2 or not tok[0].isdigit() or int(tok[0]) == me or not _is_app(tok[1]):
            continue
        pid = int(tok[0])
        args = _args(pid) or tok[1]
        if _alive(pid) and _same_root(pid, args) and _home_of(_env(pid)) == HOME:
            return pid
    return None


def broker_info():
    sock = codex_broker.socket_path(HOME)
    log = os.path.join(HOME, "logs", "broker.jsonl")
    last = None
    try:
        with open(log, encoding="utf-8") as f:
            for line in f:
                last = line
        last = json.loads(last) if last else None
    except (OSError, ValueError):
        last = None
    return {"socket": os.path.exists(sock), "last": last}


# --- 명령 ----------------------------------------------------------------
def stop(quiet=False):
    pid = find_app()
    if not pid:
        if not quiet:
            say("떠 있는 앱이 없습니다 (%s)" % HOME)
        return True
    os.kill(pid, signal.SIGTERM)
    for _ in range(100):
        if not _alive(pid):
            say("앱을 껐습니다 (pid %d) — 중계기와 진행 중인 세션은 그대로입니다" % pid)
            return True
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    time.sleep(0.3)
    say("앱이 10초 안에 끝나지 않아 강제로 껐습니다 (pid %d)" % pid)
    return not _alive(pid)


def _up(timeout=1.5):
    try:
        with urllib.request.urlopen(url(), timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def start():
    pid = find_app()
    if pid:
        say("이미 떠 있습니다 (pid %d) — %s" % (pid, url()))
        return 0
    os.makedirs(os.path.join(HOME, "logs"), exist_ok=True)
    log_path = os.path.join(HOME, "logs", "server.log")
    log = open(log_path, "ab")
    try:
        # 새 세션 — 이 스크립트(·셸·SSH)가 끝나도 앱이 남게
        p = subprocess.Popen([sys.executable, APP], cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True, close_fds=True)
    finally:
        log.close()
    t0 = time.time()
    while time.time() - t0 < START_TIMEOUT:
        if p.poll() is not None:
            say("앱이 시작하자마자 끝났습니다(코드 %s). 로그 끝(%s):" % (p.returncode, log_path))
            say(_tail(log_path))
            return 1
        if _up():
            ver = open(os.path.join(ROOT, "VERSION")).read().strip()
            b = broker_info()
            say("%sCodex Lens %s — %s (pid %d, 로그 %s)" % ("[데모] " if DEMO else "", ver, url(), p.pid, log_path))
            if b["socket"]:
                say("중계기에 다시 붙습니다 — 진행 중이던 세션은 이어집니다")
            return 0
        time.sleep(0.2)
    say("%d초 안에 %s 이 응답하지 않았습니다 — 로그 %s" % (START_TIMEOUT, url(), log_path))
    return 2


def _tail(path, n=20):
    try:
        with open(path, "rb") as f:
            return b"".join(f.readlines()[-n:]).decode("utf-8", "replace")
    except OSError:
        return ""


def status():
    pid = find_app()
    b = broker_info()
    say("앱: %s" % ("pid %d — %s (%s)" % (pid, url(), "응답함" if _up() else "응답 없음") if pid else "꺼짐"))
    last = b["last"] or {}
    say("중계기: %s%s" % ("소켓 있음" if b["socket"] else "없음",
                        " — 마지막 기록 %s %s" % (last.get("ts"), last.get("event")) if last else ""))
    say("데이터 홈: %s" % HOME)
    return 0 if pid else 3


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "restart"
    if cmd in ("-h", "--help", "help"):
        say(__doc__.strip())
        return 0
    if cmd == "start":
        return start()
    if cmd == "stop":
        return 0 if stop() else 1
    if cmd == "status":
        return status()
    if cmd == "restart":
        if not stop(quiet=True):
            return 1
        return start()
    say("모르는 명령입니다: %s (start | stop | restart | status)" % cmd)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv))
