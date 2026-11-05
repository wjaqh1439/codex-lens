"""codex app-server 를 codex-lens 프로세스 밖에 붙잡아 두는 중계기.

codex-lens 가 app-server 를 자식으로 두면 앱을 다시 띄울 때(업데이트·재기동) 같은 프로세스 그룹이라 함께 죽고,
돌던 턴도 끊긴다. 중계기는 새 세션(setsid)으로 떠서 app-server(stdio)를 쥐고, codex-lens 는 유닉스 소켓으로
붙었다 떨어진다. 앱이 내려가 있는 동안에도 턴은 계속 돌고, 다시 붙으면 그동안 쌓인 알림을 이어 받는다.

규약(줄 단위 JSON, app-server 와 같다)
- 연결하면 맨 먼저 `{"method": "_broker/hello", "params": {"fingerprint": ...}}` 를 보낸다. 답의 `action` 이
  `attach` 면 그대로 쓰고, `retire` 면 중계기가 물러난 것이니(지문이 달라졌고 돌던 일이 없다) 새로 띄운다.
- `initialize` 는 처음 한 번만 app-server 로 간다. 다시 붙은 연결에는 그때 답을 돌려주고 `codexLensBroker`
  (`reattached`, `loaded` — 이 app-server 에 올라와 있는 스레드)를 붙인다. 이어서 진행 중이거나 끊긴 동안 끝난
  턴의 알림, 답을 못 받은 서버 요청을 받은 순서대로 다시 보낸다.
- 요청 id 는 중계기가 바꿔 달아 연결이 바뀌어도 겹치지 않게 한다. 떠난 연결의 늦은 응답은 버린다.
- `_broker/shutdown` 요청이면 app-server 를 내리고 끝낸다(시험 정리용).

끝나는 때: app-server 가 죽었을 때, 붙은 연결이 없고 돌던 턴·답 못 받은 서버 요청이 없는 채로 IDLE 초가 지났을 때.
"""

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_LINES_PER_TURN = 50000


def fingerprint(argv, env=None):
    """같은 중계기를 다시 써도 되는지 가르는 값 — 실행 파일·인자·CODEX_HOME·이 파일이 바뀌면 달라진다."""
    env = os.environ if env is None else env
    parts = [json.dumps(argv), env.get("CODEX_HOME") or ""]
    exe = shutil.which(argv[0], path=env.get("PATH")) if argv else None
    for p in (exe, os.path.abspath(__file__)):
        if not p:
            continue
        rp = os.path.realpath(p)
        try:
            parts.append("%s:%d" % (rp, int(os.stat(rp).st_mtime)))
        except OSError:
            parts.append(rp)
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def socket_path(home):
    """유닉스 소켓 경로 — 너무 길면(macOS 104바이트 한도) 임시 폴더에 홈 경로 해시로 둔다."""
    p = os.path.join(home, "codex.sock")
    if len(p.encode("utf-8")) < 100:
        return p
    h = hashlib.sha1(os.path.abspath(home).encode("utf-8")).hexdigest()[:12]
    return os.path.join("/tmp", "codex-lens-%s.sock" % h)


def _thread_of(params):
    return params.get("threadId") or (params.get("thread") or {}).get("id")


def _is_delta(method):
    return method.endswith("Delta") or method.endswith("/delta")


class Broker:
    def __init__(self, argv, sock, fp, idle_sec, log_path=None):
        self.argv = argv
        self.sock = sock
        self.fp = fp
        self.idle_sec = idle_sec
        self.log_path = log_path
        self.lock = threading.RLock()
        self.up_lock = threading.Lock()
        self.proc = None
        self.srv = None
        self.sock_ino = None
        self.client = None          # 지금 붙은 연결(socket)
        self.ready = False          # 그 연결이 initialize 를 마치고 다시 보내기까지 받았는지
        self.conn_gen = 0
        self.next_id = 0
        self.inflight = {}          # 중계기 id -> (conn_gen, 원래 id, method, thread id)
        self.init_result = None
        self.initialized_sent = False
        self.loaded = set()
        self.turns = OrderedDict()  # thread id -> {"turn", "done", "lines": [(method, item id, raw)]} — 진행 중·끊긴 동안 끝난 턴
        self.held = OrderedDict()   # 답을 못 받은 서버 요청 id(json) -> raw
        self.last_client = time.time()
        self.stopping = threading.Event()
        self.started_at = time.time()

    # --- 기록 ---------------------------------------------------------
    def log(self, event, **kv):
        if not self.log_path:
            return
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z", "event": event, "pid": os.getpid()}
        rec.update(kv)
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # --- 시작·끝 -------------------------------------------------------
    def start(self):
        self.proc = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
        if os.path.exists(self.sock):
            try:
                os.unlink(self.sock)
            except OSError:
                pass
        old = os.umask(0o077)
        try:
            self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.srv.bind(self.sock)
        finally:
            os.umask(old)
        self.sock_ino = os.stat(self.sock).st_ino
        self.srv.listen(4)
        self.log("broker_start", app_server_pid=self.proc.pid, fingerprint=self.fp, idle_sec=self.idle_sec)
        threading.Thread(target=self._upstream, daemon=True, name="upstream").start()
        threading.Thread(target=self._idle_watch, daemon=True, name="idle").start()
        threading.Thread(target=self._accept, daemon=True, name="accept").start()

    def _unlink(self):
        try:
            if self.sock_ino is not None and os.stat(self.sock).st_ino == self.sock_ino:
                os.unlink(self.sock)
        except OSError:
            pass
        self.sock_ino = None

    def shutdown(self, why):
        if self.stopping.is_set():
            return
        self.stopping.set()
        self._unlink()
        self.log("broker_stop", why=why, uptime_sec=int(time.time() - self.started_at))
        with self.lock:
            c, self.client = self.client, None
        if c:
            try:
                c.close()
            except OSError:
                pass
        p = self.proc
        if p and p.poll() is None:
            try:
                p.stdin.close()
            except Exception:
                pass
            try:
                p.terminate()
                p.wait(5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    def busy(self):
        """돌던 턴, 답을 기다리는 turn/start(턴 시작 알림 전), 답 못 받은 서버 요청이 있으면 참."""
        return (bool(self.held) or any(not t["done"] for t in self.turns.values())
                or any(v[2] == "turn/start" for v in self.inflight.values()))

    def undelivered(self):
        """끊긴 동안 끝나 아직 아무 연결도 받지 못한 턴이 있으면 참."""
        return any(t["done"] for t in self.turns.values())

    def _idle_watch(self):
        while not self.stopping.wait(2):
            with self.lock:
                idle = self.client is None and not self.busy() and time.time() - self.last_client >= self.idle_sec
            if idle:
                self.shutdown("idle")
                return

    # --- app-server 쪽 -------------------------------------------------
    def _write_up(self, raw):
        with self.up_lock:
            p = self.proc
            if not p or p.poll() is not None:
                raise OSError("app-server 가 실행 중이 아닙니다")
            p.stdin.write(raw if raw.endswith(b"\n") else raw + b"\n")
            p.stdin.flush()

    def _upstream(self):
        for raw in self.proc.stdout:
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            try:
                self._from_server(obj, raw)
            except Exception as e:  # 중계 오류가 읽기 루프를 끊지 않게
                self.log("broker_dispatch_error", error=repr(e)[:300])
        code = self.proc.poll()
        self.log("app_server_exited", code=code)
        self.shutdown("app_server_exited")

    def _from_server(self, obj, raw):
        method = obj.get("method")
        with self.lock:
            if method and "id" in obj:              # 서버 요청 — 답이 올 때까지 쥐고 있는다
                self.held[json.dumps(obj["id"])] = raw
                if self.ready:
                    self._send_client(raw)
                return
            if method:                              # 알림
                self._buffer(method, obj.get("params") or {}, raw)
                if self.ready:
                    self._send_client(raw)
                return
            slot = self.inflight.pop(obj.get("id"), None)
            if not slot:
                return
            gen, orig, req_method, req_tid = slot
            res = obj.get("result")
            if isinstance(res, dict):
                turn = res.get("turn") or {}
                if req_method == "turn/start" and req_tid and turn.get("id"):
                    t = self.turns.get(req_tid)
                    if not t or t["turn"] != turn["id"]:
                        # 턴 시작 알림보다 응답이 먼저 왔다 — 다시 보낼 때 턴이 보이게 시작 알림을 만들어 둔다
                        note = {"jsonrpc": "2.0", "method": "turn/started",
                                "params": {"threadId": req_tid, "turn": dict(turn, status=turn.get("status") or "inProgress")}}
                        self._start_turn(req_tid, turn["id"], (json.dumps(note, ensure_ascii=False) + "\n").encode("utf-8"))
                if req_method in ("thread/start", "thread/resume", "thread/fork"):
                    tid = (res.get("thread") or {}).get("id")
                    if tid:
                        self.loaded.add(tid)
                if req_method == "initialize" and "error" not in obj:
                    self.init_result = res
            if gen != self.conn_gen or self.client is None:
                return                              # 떠난 연결의 응답
            obj["id"] = orig
            self._send_client((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            if req_method == "initialize" and not self.ready:
                self._replay()

    def _buffer(self, method, params, raw):
        tid = _thread_of(params)
        if method == "thread/closed":
            self.loaded.discard(tid)
        if not tid:
            return
        if method == "turn/started":
            turn = (params.get("turn") or {}).get("id")
            t = self.turns.get(tid)
            if not (t and t["turn"] == turn and not t["done"]):   # 응답으로 먼저 만들어 둔 같은 턴이면 그대로
                self._start_turn(tid, turn, raw)
            return
        t = self.turns.get(tid)
        if not t or t["done"]:
            return
        item_id = params.get("itemId")
        if method == "item/completed":
            # 완성본이 오면 그 항목의 조각(delta)은 다시 보낼 필요가 없다
            done_id = (params.get("item") or {}).get("id")
            if done_id:
                t["lines"] = [x for x in t["lines"] if not (x[1] == done_id and _is_delta(x[0]))]
        if _is_delta(method) and len(t["lines"]) >= MAX_LINES_PER_TURN:
            return
        t["lines"].append((method, item_id, raw))
        if method == "turn/completed":
            t["done"] = True
            if self.ready:
                # 지금 붙은 연결이 실시간으로 받았다 — 다시 보낼 것 없다
                self.turns.pop(tid, None)

    def _start_turn(self, tid, turn, raw):
        self.turns[tid] = {"turn": turn, "done": False, "lines": [("turn/started", None, raw)]}
        self.turns.move_to_end(tid)

    # --- codex-lens 쪽 -------------------------------------------------
    def _send_client(self, raw):
        c = self.client
        if not c:
            return
        try:
            c.sendall(raw if raw.endswith(b"\n") else raw + b"\n")
        except OSError:
            self._drop(c)

    def _drop(self, c):
        with self.lock:
            if self.client is c:
                self.client = None
                self.ready = False
                self.last_client = time.time()
                self.log("client_detached", busy=self.busy(), loaded=len(self.loaded))
        try:
            c.close()
        except OSError:
            pass

    def _replay(self):
        """다시 붙은 연결에 진행 중·끊긴 동안 끝난 턴의 알림과 답 못 받은 서버 요청을 다시 보낸다(잠금 안에서)."""
        n = 0
        for tid in list(self.turns):
            t = self.turns[tid]
            for _, _, raw in t["lines"]:
                self._send_client(raw)
                n += 1
            if t["done"]:
                self.turns.pop(tid, None)
        for raw in self.held.values():
            self._send_client(raw)
            n += 1
        self.ready = self.client is not None
        if n:
            self.log("replayed", lines=n, turns=len(self.turns), held=len(self.held))

    def _accept(self):
        while not self.stopping.is_set():
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True, name="client").start()

    def _reply(self, c, rid, result=None, error=None):
        obj = {"jsonrpc": "2.0", "id": rid}
        if error:
            obj["error"] = {"code": -32000, "message": error}
        else:
            obj["result"] = result
        c.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))

    def _serve(self, c):
        f = c.makefile("rb")
        try:
            hello = json.loads(f.readline() or b"{}")
        except ValueError:
            hello = {}
        if hello.get("method") != "_broker/hello":
            c.close()
            return
        their_fp = (hello.get("params") or {}).get("fingerprint")
        with self.lock:
            stale = their_fp and their_fp != self.fp
            if stale and not self.busy() and not self.undelivered():
                self._reply(c, hello.get("id"), {"action": "retire", "pid": os.getpid()})
                c.close()
                self.log("broker_retire", fingerprint=self.fp, wanted=their_fp)
                threading.Thread(target=self.shutdown, args=("retired",), daemon=True).start()
                return
            old = self.client
            self.client = c
            self.ready = False
            self.conn_gen += 1
            gen = self.conn_gen
            self._reply(c, hello.get("id"), {"action": "attach", "pid": os.getpid(), "appServerPid": self.proc.pid,
                                             "fingerprint": self.fp, "stale": bool(stale), "busy": self.busy(),
                                             "startedAt": self.started_at})
            self.log("client_attached", gen=gen, stale=bool(stale), busy=self.busy(), loaded=len(self.loaded),
                     turns=len(self.turns), held=len(self.held))
        if old:
            try:
                old.close()
            except OSError:
                pass
        try:
            for raw in f:
                if self.client is not c:
                    break
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                self._from_client(c, gen, obj, raw)
        except OSError:
            pass
        self._drop(c)

    def _from_client(self, c, gen, obj, raw):
        method = obj.get("method")
        if method == "_broker/shutdown":
            self._reply(c, obj.get("id"), {"ok": True})
            self.shutdown("requested")
            return
        if method and "id" in obj:
            with self.lock:
                if method == "initialize" and self.init_result is not None:
                    res = dict(self.init_result, codexLensBroker={"reattached": True, "loaded": sorted(self.loaded)})
                    self._reply(c, obj["id"], res)
                    self._replay()
                    return
                self.next_id += 1
                bid = self.next_id
                self.inflight[bid] = (gen, obj["id"], method, (obj.get("params") or {}).get("threadId"))
            obj["id"] = bid
            self._up_or_fail(c, (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"), bid)
            return
        if method:
            if method == "initialized":
                with self.lock:
                    if self.initialized_sent:
                        return
                    self.initialized_sent = True
            self._up_or_fail(c, raw, None)
            return
        if "id" in obj:                             # 서버 요청에 대한 답
            with self.lock:
                self.held.pop(json.dumps(obj["id"]), None)
            self._up_or_fail(c, raw, None)

    def _up_or_fail(self, c, raw, bid):
        try:
            self._write_up(raw)
        except OSError as e:
            if bid is not None:
                with self.lock:
                    slot = self.inflight.pop(bid, None)
                if slot:
                    try:
                        self._reply(c, slot[1], error=str(e))
                    except OSError:
                        pass


def main():
    ap = argparse.ArgumentParser(description="codex app-server 중계기(codex-lens)")
    ap.add_argument("--socket", required=True)
    ap.add_argument("--fingerprint", required=True)
    ap.add_argument("--idle", type=float, default=600)
    ap.add_argument("--log")
    ap.add_argument("argv", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    argv = a.argv[1:] if a.argv and a.argv[0] == "--" else a.argv
    if not argv:
        ap.error("app-server 명령이 필요합니다")
    b = Broker(argv, a.socket, a.fingerprint, a.idle, a.log)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: b.shutdown("signal"))
    try:
        b.start()
    except FileNotFoundError as e:
        print("codex 실행 파일을 찾지 못했습니다(%s): %s" % (argv[0], e), file=sys.stderr, flush=True)
        sys.exit(127)
    b.stopping.wait()
    time.sleep(0.2)


if __name__ == "__main__":
    main()
