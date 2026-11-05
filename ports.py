"""포트 관리 — Codex 세션이 띄우는 데모·개발 서버에 빈 포트를 골라 주고, 띄운 프로세스를 보고 끈다.

표준 라이브러리만 쓴다(macOS·Linux 공통, 별도 도구 없음). 에이전트(Codex·Claude Code) 쪽에서는 `bin/lens-port` 가 이 API 를 부른다.

- 빈 포트: 대역(CODEX_LENS_PORT_RANGE, 기본 20000-20999) 안에서 아무도 듣지 않고(127.0.0.1·::1 에 접속 안 됨)
  묶을 수 있으며(0.0.0.0·:: bind 성공) 이 앱이 이미 준·예약한 포트가 아닌 것.
- 띄우기: 새 프로세스 그룹(start_new_session)으로 띄워 앱을 다시 띄워도 살아 있고, 끌 때는 그룹 전체에
  SIGTERM → 5초 뒤에도 살아 있으면 SIGKILL. 출력은 `<홈>/logs/ports/<ID>.log`.
- 상태: starting(포트가 아직 안 열림) → listening → exited(스스로 끝남) / stopped(끔).
  앱을 다시 띄우면 pid·시작 시각으로 같은 프로세스인지 확인하고 이어서 본다.
- 환경변수는 저장하지 않는다(토큰이 섞일 수 있다) — 명령·작업 폴더만 남긴다.
- 바깥에 내보내기(선택): CODEX_LENS_PORT_EXPOSE 가 있으면 포트가 열린 뒤 그 명령을 돌려 다른 기기에서 열 주소를 붙이고
  (예: tailscale serve --bg --https={public} http://127.0.0.1:{port}), 끄거나 끝나면 CODEX_LENS_PORT_UNEXPOSE 를 돌린다.
  {public} 은 대역에서 따로 고른 빈 포트다. 화면·CLI 가 보여 줄 주소는 CODEX_LENS_PUBLIC_URL(예: https://호스트:{public}/).
  CODEX_LENS_PORT_EXPOSE 가 아예 없고 이 기기에 tailscale 이 로그인돼 있으면 위 tailscale serve 내보내기를 기본으로 쓴다
  (빈 값이면 끈다). 앱을 어떤 환경변수로 띄웠든 다른 기기·HTTPS 화면에서 세션 서버가 열리게 하려는 것이다.
"""

import errno
import os
import random
import secrets
import shlex
import signal
import socket
import subprocess
import threading
import time

import applog
import weblink
from store import JsonStore

RUNNING = ("starting", "listening")
STOP_GRACE_SEC = 5
RESERVE_SEC = 120
HOOK_TIMEOUT_SEC = 20
KEEP_DONE = 60          # 끝난 기록은 최근 이만큼만 남긴다
LOG_TAIL = 8000


def parse_range(s, default=(20000, 20999)):
    try:
        a, b = (int(x) for x in (s or "").split("-", 1))
        if 1024 <= a <= b <= 65535:
            return a, b
    except ValueError:
        pass
    return default


def is_loopback(host):
    return (host or "").strip("[]") in ("127.0.0.1", "localhost", "::1") or (host or "").startswith("127.")


def listening(port, timeout=0.25):
    """127.0.0.1 또는 ::1 에서 누가 듣고 있나 — node 의 localhost 는 ::1 에만 묶이기도 한다."""
    for fam, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(fam, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                if s.connect_ex((addr, port)) == 0:
                    return True
        except OSError:
            pass
    return False


def bindable(port):
    """0.0.0.0 과 :: 에 묶을 수 있나. IPv6 가 없는 기기는 v4 만 본다.
    SO_REUSEADDR 를 켠다 — 대부분의 서버(node·python)가 켜고 묶으므로, 방금 끈 서버의 TIME_WAIT 는 빈 것으로 본다
    (듣고 있는 소켓은 그래도 막히고, 루프백에서 듣는 것은 listening() 이 따로 잡는다)."""
    for fam, addr in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        try:
            s = socket.socket(fam, socket.SOCK_STREAM)
        except OSError:
            if fam == socket.AF_INET6:
                continue
            return False
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if fam == socket.AF_INET6:
                try:
                    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                except OSError:
                    pass
            s.bind((addr, port))
        except OSError as e:
            if fam == socket.AF_INET6 and e.errno in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL):
                continue
            return False
        finally:
            s.close()
    return True


def port_free(port):
    return not listening(port, 0.15) and bindable(port)


def proc_start_token(pid):
    """pid 재사용을 가려내는 시작 시각 표지 — Linux 는 /proc, 그 밖은 ps."""
    try:
        with open("/proc/%d/stat" % pid) as f:
            return "proc:" + f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        pass
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=3)
        s = out.stdout.strip()
        return "ps:" + s if s else None
    except (OSError, subprocess.SubprocessError):
        return None


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def tailscale_expose():
    """이 기기에 tailscale 이 로그인돼 있으면 serve 로 내보내는 기본 명령·주소, 없으면 None."""
    ts = weblink.tailscale_bin()
    if not ts:
        return None
    serve, me = weblink.tailscale_info()
    dns = (me or {}).get("dns") or ""
    if serve is None or not dns:
        return None
    q = shlex.quote(ts)
    return {"expose": q + " serve --bg --https={public} http://127.0.0.1:{port}",
            "unexpose": q + " serve --https={public} off",
            "public_url": "https://%s:{public}/" % dns}


def serve_ports():
    """tailscale serve 가 지금 쥐고 있는 포트(HTTPS·TCP) — 이 앱이 붙이지 않은 매핑도 포함."""
    weblink.forget_cache()
    serve, _ = weblink.tailscale_info()
    out = set()
    for k in list(((serve or {}).get("TCP") or {}).keys()) + list(((serve or {}).get("Web") or {}).keys()):
        try:
            out.add(int(str(k).rsplit(":", 1)[-1]))
        except ValueError:
            pass
    return out


class Ports:
    def __init__(self, home, bind_host="127.0.0.1", port_range=None, public_url=None, notify=None):
        self.home = home
        self.range = port_range or parse_range(os.environ.get("CODEX_LENS_PORT_RANGE"))
        self.bind = "127.0.0.1" if is_loopback(bind_host) else "0.0.0.0"
        self.public_url = public_url if public_url is not None else os.environ.get("CODEX_LENS_PUBLIC_URL", "")
        self.expose_cmd = os.environ.get("CODEX_LENS_PORT_EXPOSE", "")
        self.unexpose_cmd = os.environ.get("CODEX_LENS_PORT_UNEXPOSE", "")
        self.ts_auto = False      # tailscale serve 기본 내보내기를 쓰는가 — 다른 도구의 매핑 포트를 피한다
        if "CODEX_LENS_PORT_EXPOSE" not in os.environ:
            auto = tailscale_expose()
            if auto:
                self.expose_cmd, self.unexpose_cmd = auto["expose"], auto["unexpose"]
                if public_url is None and not self.public_url:
                    self.public_url = auto["public_url"]
                self.ts_auto = True
        self.notify = notify or (lambda thread_id=None: None)
        self.log_dir = os.path.join(home, "logs", "ports")
        os.makedirs(self.log_dir, exist_ok=True)
        self.store = JsonStore(os.path.join(home, "ports.json"), {"items": []})
        self.lock = threading.RLock()
        self.procs = {}       # id -> Popen (이 앱이 띄운 것 — 끝나면 거둬야 좀비가 안 남는다)
        self.reserved = {}    # port -> 만료 시각 (free 로 주었거나 띄우는 중)
        self._reconcile()
        self.stop_evt = threading.Event()
        threading.Thread(target=self._monitor, daemon=True, name="ports-monitor").start()

    # --- 조회 -----------------------------------------------------------
    def info(self):
        return {"range": list(self.range), "bind": self.bind, "public_url": self.public_url, "expose": bool(self.expose_cmd),
                "expose_auto": self.ts_auto}

    def url_of(self, e):
        """다른 기기에서 열 주소 — 템플릿이 없거나 내보내기가 아직이면 None(화면이 지금 호스트로 만든다)."""
        t = self.public_url
        if not t or ("{public}" in t and not e.get("public")):
            return None
        return t.replace("{port}", str(e["port"])).replace("{public}", str(e.get("public") or ""))

    def list(self, thread_id=None):
        with self.lock:
            items = [dict(e) for e in self.store.read()["items"]]
        if thread_id:
            items = [e for e in items if e.get("thread_id") == thread_id]
        items.sort(key=lambda e: (e.get("state") not in RUNNING, -(e.get("started_at") or 0)))
        return items

    def get(self, pid_or_port):
        key = str(pid_or_port)
        with self.lock:
            items = self.store.read()["items"]
            for e in items:
                if e["id"] == key:
                    return dict(e)
            # 포트 번호로 찾으면 지금 살아 있는 것을 먼저
            hits = [e for e in items if str(e.get("port")) == key]
            hits.sort(key=lambda e: (e.get("state") not in RUNNING, -(e.get("started_at") or 0)))
            if hits:
                return dict(hits[0])
        raise KeyError(key)

    def log_tail(self, pid, n=LOG_TAIL):
        e = self.get(pid)
        try:
            with open(e["log"], "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - n))
                data = f.read()
        except OSError:
            return {"id": e["id"], "text": "", "size": 0}
        text = data.decode("utf-8", "replace")
        if size > n:
            text = text.split("\n", 1)[-1]
        return {"id": e["id"], "text": text, "size": size}

    # --- 빈 포트 -------------------------------------------------------
    def _taken(self):
        now = time.time()
        self.reserved = {p: t for p, t in self.reserved.items() if t > now}
        used = set()
        for e in self.store.read()["items"]:
            if e.get("state") in RUNNING:
                used.add(e["port"])
                if e.get("public"):
                    used.add(e["public"])
        return used | set(self.reserved)

    def free_port(self, reserve=RESERVE_SEC, avoid=()):
        """대역 안의 빈 포트 하나. 대역 안에서 시작점을 섞어 여러 세션이 동시에 골라도 덜 부딪친다."""
        lo, hi = self.range
        with self.lock:
            taken = self._taken() | set(avoid)
            n = hi - lo + 1
            start = random.randrange(n)
            for i in range(n):
                p = lo + (start + i) % n
                if p in taken:
                    continue
                if port_free(p):
                    self.reserved[p] = time.time() + reserve
                    return p
        raise ValueError("대역 %d-%d 에 빈 포트가 없습니다 — 쓰지 않는 서버를 끄세요" % (lo, hi))

    # --- 띄우기 ---------------------------------------------------------
    def run(self, argv=None, sh=None, cwd=None, env=None, name=None, thread_id=None, port=None, replace=True):
        if sh:
            argv = ["/bin/sh", "-c", sh]
        if not argv or not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise ValueError("띄울 명령이 필요합니다")
        cwd = os.path.abspath(os.path.expanduser(cwd or "~"))
        if not os.path.isdir(cwd):
            raise ValueError("작업 폴더가 없습니다: %s" % cwd)
        first = argv[0]
        if sh:
            try:
                first = (shlex.split(sh) or ["sh"])[0]
            except ValueError:
                first = sh.split()[0] if sh.split() else "sh"
        name = (name or "").strip()[:40] or os.path.basename(first)[:40]
        # 같은 세션·같은 이름으로 다시 띄우면 이전 것을 끄고 그 포트를 다시 쓴다(포트가 쌓이지 않게)
        prev = None
        if replace and thread_id:
            for e in self.list(thread_id):
                if e.get("name") == name and e.get("state") in RUNNING:
                    prev = e
                    self.stop(e["id"], why="replaced")
        with self.lock:
            if port:
                port = int(port)
                used = {e["port"] for e in self.store.read()["items"] if e.get("state") in RUNNING}
                if port in used:
                    raise ValueError("포트 %d 는 이미 이 앱이 쓰고 있습니다" % port)
            elif prev and self._wait_free(prev["port"]):
                port = prev["port"]
            else:
                port = self.free_port()
            self.reserved[port] = time.time() + RESERVE_SEC
        if not port_free(port):
            raise ValueError("포트 %d 를 이미 다른 프로세스가 쓰고 있습니다" % port)
        sub = {"{port}": str(port), "{host}": self.bind}
        real = []
        for a in argv:
            for k, v in sub.items():
                a = a.replace(k, v)
            real.append(a)
        pid_ = "p" + secrets.token_hex(4)
        log_path = os.path.join(self.log_dir, pid_ + ".log")
        penv = dict(env) if isinstance(env, dict) else dict(os.environ)
        penv = {str(k): str(v) for k, v in penv.items()}
        penv.update({"PORT": str(port), "CODEX_LENS_PORT_ID": pid_, "CODEX_LENS_BIND": self.bind})
        t0 = time.time()
        logf = open(log_path, "ab")
        try:
            logf.write(("# codex-lens %s · 포트 %d · %s\n# $ %s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), port, cwd, shlex.join(real))).encode("utf-8"))
            logf.flush()
            proc = subprocess.Popen(real, cwd=cwd, env=penv, stdin=subprocess.DEVNULL, stdout=logf,
                                    stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        except OSError as e:
            with self.lock:
                self.reserved.pop(port, None)
            raise ValueError("띄우지 못했습니다: %s" % e)
        finally:
            logf.close()
        entry = {"id": pid_, "name": name, "port": port, "bind": self.bind, "pid": proc.pid, "pgid": proc.pid,
                 "token": proc_start_token(proc.pid), "argv": real, "cmd": sh or shlex.join(real), "cwd": cwd,
                 "thread_id": thread_id or None, "state": "starting", "started_at": t0, "listen_at": None,
                 "ended_at": None, "exit_code": None, "log": log_path}
        with self.lock:
            self.procs[pid_] = proc
            self.reserved.pop(port, None)

            def add(d):
                d["items"].append(entry)
                self._prune(d)
            self.store.update(add)
        applog.log("port_started", id=pid_, port=port, name=name, thread=thread_id, replaced=prev and prev["id"],
                   ms=int((time.time() - t0) * 1000))
        self.notify(thread_id)
        return dict(entry)

    def _wait_free(self, port, sec=3.0):
        end = time.time() + sec
        while time.time() < end:
            if port_free(port):
                return True
            time.sleep(0.2)
        return False

    def _prune(self, d):
        done = [e for e in d["items"] if e.get("state") not in RUNNING]
        if len(done) <= KEEP_DONE:
            return
        done.sort(key=lambda e: e.get("ended_at") or e.get("started_at") or 0)
        drop = {e["id"] for e in done[:len(done) - KEEP_DONE]}
        for e in done[:len(done) - KEEP_DONE]:
            self._unlink_log(e)
        d["items"] = [e for e in d["items"] if e["id"] not in drop]

    @staticmethod
    def _unlink_log(e):
        try:
            os.unlink(e.get("log") or "")
        except OSError:
            pass

    # --- 끄기 · 지우기 ----------------------------------------------------
    def stop(self, pid, why="user"):
        e = self.get(pid)
        if e.get("state") not in RUNNING:
            return e
        t0 = time.time()
        pgid = e.get("pgid") or e.get("pid")
        killed = False
        if self._alive(e):
            try:
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            end = time.time() + STOP_GRACE_SEC
            while time.time() < end and self._alive(e):
                time.sleep(0.1)
            if self._alive(e):
                killed = True
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                end = time.time() + 2
                while time.time() < end and self._alive(e):
                    time.sleep(0.1)
        code = self._reap(e["id"])
        out = self._set(e["id"], state="stopped", ended_at=time.time(), exit_code=code, stop_reason=why)
        self._unexpose(e)
        applog.log("port_stopped", id=e["id"], port=e["port"], why=why, killed=killed, ms=int((time.time() - t0) * 1000))
        self.notify(e.get("thread_id"))
        return out

    def remove(self, pid):
        e = self.get(pid)
        if e.get("state") in RUNNING:
            raise ValueError("실행 중입니다 — 먼저 끄세요")
        with self.lock:
            self.store.update(lambda d: d.__setitem__("items", [x for x in d["items"] if x["id"] != e["id"]]))
        self._unlink_log(e)
        self.notify()
        return {"ok": True}

    def clear_done(self):
        with self.lock:
            done = [e for e in self.store.read()["items"] if e.get("state") not in RUNNING]
            self.store.update(lambda d: d.__setitem__("items", [x for x in d["items"] if x.get("state") in RUNNING]))
        for e in done:
            self._unlink_log(e)
        self.notify()
        return {"removed": len(done)}

    # --- 상태 따라가기 ----------------------------------------------------
    def _alive(self, e):
        """프로세스 그룹이 살아 있나. 리더가 끝나도 그룹의 자식(예: npm 이 띄운 node)이 포트를 쥐고 있을 수 있다.
        이 앱이 띄운 것은 먼저 거둬(poll) 좀비가 살아 있는 것으로 보이지 않게 하고,
        앱을 다시 띄운 뒤 이어 보는 것은 pid 가 재사용됐는지 시작 시각으로 가린다."""
        pid = e.get("pid")
        if not pid:
            return False
        pgid = e.get("pgid") or pid
        p = self.procs.get(e["id"])
        if p is not None:
            p.poll()
            return group_alive(pgid)
        if pid_alive(pid):
            tok = e.get("token")
            return tok is None or proc_start_token(pid) == tok
        return group_alive(pgid)

    def _reap(self, pid):
        p = self.procs.pop(pid, None)
        if p is None:
            return None
        try:
            return p.wait(1)
        except subprocess.TimeoutExpired:
            return None

    def _set(self, pid, if_running=False, **fields):
        with self.lock:
            def fn(d):
                for x in d["items"]:
                    if x["id"] == pid:
                        if if_running and x.get("state") not in RUNNING:
                            return None   # 그 사이 끈 것을 점검이 덮어쓰지 않게
                        x.update(fields)
                        return dict(x)
                return None
            return self.store.update(fn)

    def _reconcile(self):
        """앱을 다시 띄웠을 때 — 기록상 실행 중인데 프로세스가 없으면 끝난 것으로 적는다."""
        changed = False
        with self.lock:
            d = self.store.read()
            for e in d["items"]:
                if e.get("state") not in RUNNING:
                    continue
                if self._alive(e):
                    continue
                e.update(state="exited", ended_at=time.time(), exit_code=None)
                changed = True
            if changed:
                self.store.save()

    def check(self):
        """실행 중인 것들의 포트·프로세스를 본다. 바뀐 기록의 세션 ID 집합을 돌려준다(세션 없는 것은 None)."""
        changed = set()
        for e in self.list():
            if e.get("state") not in RUNNING:
                continue
            alive = self._alive(e)
            p = self.procs.get(e["id"])
            code = p.returncode if p is not None else None
            up = listening(e["port"])
            if not alive:
                self.procs.pop(e["id"], None)
                self._set(e["id"], if_running=True, state="exited", ended_at=time.time(), exit_code=code)
                applog.log("port_exited", level="warn" if code else "info", id=e["id"], port=e["port"], code=code,
                           listened=bool(e.get("listen_at")), ms=int((time.time() - e["started_at"]) * 1000))
                self._unexpose(e)
                changed.add(e.get("thread_id"))
            elif up and e["state"] == "starting":
                self._set(e["id"], if_running=True, state="listening", listen_at=time.time())
                applog.log("port_listening", id=e["id"], port=e["port"], ms=int((time.time() - e["started_at"]) * 1000))
                self._expose_later(e)
                changed.add(e.get("thread_id"))
            elif up and not e.get("expose"):
                self._expose_later(e)   # 앱을 다시 띄웠거나 내보내기를 나중에 켰을 때 — 열려 있는 것도 내보낸다
            elif not up and e["state"] == "listening":
                # 서버가 포트를 닫았다(다시 여는 중일 수 있다) — 프로세스는 살아 있으니 starting 으로 돌린다
                self._set(e["id"], if_running=True, state="starting")
                changed.add(e.get("thread_id"))
        return changed

    # --- 바깥에 내보내기(선택) ------------------------------------------
    def _hook(self, template, e, public):
        cmd = template.replace("{port}", str(e["port"])).replace("{public}", str(public or ""))
        t0 = time.time()
        try:
            out = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=HOOK_TIMEOUT_SEC,
                                 stdin=subprocess.DEVNULL)
            ok, err = out.returncode == 0, (out.stderr or out.stdout or "").strip()[-300:]
        except (OSError, ValueError, subprocess.SubprocessError) as ex:
            ok, err = False, str(ex)[:300]
        return ok, err, int((time.time() - t0) * 1000)

    def _expose_later(self, e):
        if self.expose_cmd and not e.get("expose"):
            self._set(e["id"], expose="pending")
            threading.Thread(target=self._expose, args=(e,), daemon=True, name="ports-expose").start()

    def _expose(self, e):
        try:
            # macOS 의 tailscale serve 매핑은 소켓으로 안 보인다 — 기본 내보내기면 지금 매핑된 포트를 직접 피한다
            public = self.free_port(avoid=serve_ports() if self.ts_auto else ())
        except ValueError as ex:
            self._set(e["id"], expose="failed", expose_error=str(ex))
            return
        ok, err, ms = self._hook(self.expose_cmd, e, public)
        with self.lock:
            self.reserved.pop(public, None)
        cur = self._set(e["id"], if_running=True, public=public if ok else None, expose="ok" if ok else "failed",
                        expose_error=None if ok else err)
        applog.log("port_exposed", level="info" if ok else "warn", id=e["id"], port=e["port"], public=public, ok=ok, ms=ms,
                   error=None if ok else err)
        if ok and cur is None and self.unexpose_cmd:      # 내보내는 사이 꺼졌다 — 바로 거둔다
            self._hook(self.unexpose_cmd, e, public)
        self.notify(e.get("thread_id"))

    def _unexpose(self, e):
        cur = None
        try:
            cur = self.get(e["id"])
        except KeyError:
            return
        if not (self.unexpose_cmd and cur.get("public") and cur.get("expose") == "ok"):
            return
        ok, err, ms = self._hook(self.unexpose_cmd, cur, cur["public"])
        applog.log("port_unexposed", level="info" if ok else "warn", id=e["id"], public=cur["public"], ok=ok, ms=ms,
                   error=None if ok else err)
        self._set(e["id"], expose="removed" if ok else "remove_failed", expose_error=None if ok else err)

    def _monitor(self):
        while not self.stop_evt.wait(1.0):
            try:
                for tid in self.check():
                    self.notify(tid)
            except Exception as ex:  # 점검이 실패해도 다음 점검은 한다
                applog.log("ports_monitor_error", level="error", error=repr(ex)[:300])

    def close(self):
        self.stop_evt.set()
