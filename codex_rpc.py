"""Codex app-server(JSON-RPC over stdio) 연결.

앱 전체가 app-server 프로세스 하나를 상주시켜 세션 목록·읽기·실행·포크를 모두 이 연결로 처리한다.
app-server 는 중계기(`codex_broker.py`)가 새 세션으로 쥐고 있고 이 앱은 유닉스 소켓으로 붙는다 — 앱을 다시 띄워도
돌던 턴이 끊기지 않고, 다시 붙으면 그동안의 알림을 이어 받는다. `socket_path` 를 주지 않거나
`CODEX_LENS_BROKER=0` 이면 예전처럼 app-server 를 자식으로 띄운다(앱과 함께 죽는다).
연결이 끊기면 다음 요청 때 다시 붙고(중계기가 없으면 띄우고), 그때 올라와 있던 스레드 목록(`loaded`)은
중계기가 알려 준 것으로 바꾼다 — 새 app-server 면 비어 있어 다시 쓰려면 thread/resume 이 필요하다.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time

import applog
import codex_broker

HERE = os.path.dirname(os.path.abspath(__file__))


class _ProcConn:
    """app-server 를 자식으로 띄운 연결(예전 방식)."""

    def __init__(self, argv, env, stderr_path):
        err = open(stderr_path, "ab") if stderr_path else subprocess.DEVNULL
        try:
            self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, env=env, bufsize=0)
        finally:
            if err is not subprocess.DEVNULL:
                err.close()
        self.pid = self.p.pid
        self.info = {}

    def alive(self):
        return self.p.poll() is None

    def write(self, data):
        self.p.stdin.write(data)
        self.p.stdin.flush()

    def lines(self):
        return self.p.stdout

    def code(self):
        return self.p.poll()

    def close(self):
        p = self.p
        if p.poll() is None:
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


class _BrokerConn:
    """중계기 소켓 연결. 닫아도 중계기와 app-server 는 남는다."""

    def __init__(self, sock, info):
        self.s = sock
        self.f = sock.makefile("rb")
        self.info = info
        self.pid = info.get("appServerPid")
        self.closed = False

    def alive(self):
        return not self.closed

    def write(self, data):
        try:
            self.s.sendall(data)
        except OSError:
            self.closed = True
            raise

    def lines(self):
        try:
            for raw in self.f:
                yield raw
        except (OSError, ValueError):
            pass
        self.closed = True

    def code(self):
        return None

    def close(self):
        self.closed = True
        try:
            self.s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.s.close()
        except OSError:
            pass


class RpcError(Exception):
    def __init__(self, message, code=None, data=None):
        super().__init__(message)
        self.code = code
        self.data = data


class CodexClient:
    def __init__(self, argv=None, env=None, stderr_path=None, client_name="codex-lens", version="0", socket_path=None,
                 broker_log=None):
        # CODEX_LENS_CODEX_ARGV(JSON 목록)는 가짜 app-server 를 끼울 때 쓴다(scripts/run_fake_demo.py).
        env_argv = os.environ.get("CODEX_LENS_CODEX_ARGV")
        self.argv = argv or (json.loads(env_argv) if env_argv else
                             [os.environ.get("CODEX_LENS_CODEX_BIN", "codex"), "app-server"])
        self.env = env
        self.stderr_path = stderr_path
        self.client_name = client_name
        self.version = version
        use_broker = os.environ.get("CODEX_LENS_BROKER", "1") not in ("0", "false", "no")
        self.socket_path = socket_path if use_broker else None
        self.broker_log = broker_log
        self.broker_idle = float(os.environ.get("CODEX_LENS_BROKER_IDLE_SEC") or 600)
        self.proc = None            # 지금 연결(_ProcConn | _BrokerConn)
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.pending = {}           # id -> {"event", "result", "error"}
        self.next_id = 0
        self.listeners = []         # fn(method, params)
        self.request_handlers = []  # fn(method, params) -> result | None (None 이면 다음 처리기)
        self.loaded = set()         # 이 app-server 에 올라와 있는 스레드
        self.generation = 0
        self.init_info = {}
        self.started_at = 0
        self.reattached = False     # 앱을 다시 띄우기 전부터 돌던 app-server 에 다시 붙었는지

    # --- 생명 주기 -----------------------------------------------------
    def ensure(self):
        with self.lock:
            if self.proc and self.proc.alive():
                return
            self._start()

    def _connect_broker(self):
        """중계기에 붙는다. 없거나 물러났으면 새로 띄운다."""
        env = os.environ if self.env is None else self.env
        fp = codex_broker.fingerprint(self.argv, env)
        spawned = None
        deadline = time.time() + 20
        while True:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.connect(self.socket_path)
                s.sendall((json.dumps({"jsonrpc": "2.0", "id": "hello", "method": "_broker/hello",
                                       "params": {"fingerprint": fp, "client": self.client_name}}) + "\n").encode("utf-8"))
                s.settimeout(10)
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk:
                        raise ConnectionError("중계기가 연결을 닫았습니다")
                    buf += chunk
                s.settimeout(None)
                info = (json.loads(buf) or {}).get("result") or {}
                if info.get("action") == "attach":
                    return _BrokerConn(s, info)
                applog.log("codex_broker_retired", pid=info.get("pid"))
                s.close()
                time.sleep(0.2)
            except (OSError, ValueError):
                s.close()
                if spawned is None:
                    spawned = self._spawn_broker(fp)
                elif spawned.poll() is not None:
                    raise RpcError("codex app-server 중계기가 시작하자마자 끝났습니다(코드 %s) — %s 를 확인하세요"
                                   % (spawned.returncode, self.stderr_path or "stderr"))
                if time.time() > deadline:
                    raise RpcError("codex app-server 중계기에 연결하지 못했습니다(%s)" % self.socket_path)
                time.sleep(0.1)

    def _spawn_broker(self, fp):
        if not codex_broker.shutil.which(self.argv[0], path=(self.env or os.environ).get("PATH")):
            raise RpcError("codex 실행 파일을 찾지 못했습니다(%s). Codex CLI 를 설치하고 로그인하세요." % self.argv[0])
        cmd = [sys.executable, os.path.join(HERE, "codex_broker.py"), "--socket", self.socket_path,
               "--fingerprint", fp, "--idle", str(self.broker_idle)]
        if self.broker_log:
            cmd += ["--log", self.broker_log]
        err = open(self.stderr_path, "ab") if self.stderr_path else subprocess.DEVNULL
        try:
            # 새 세션 — 앱의 프로세스 그룹을 통째로 끄는 재기동(프로세스 관리자·systemd 등)에 휩쓸리지 않게
            p = subprocess.Popen(cmd + ["--"] + self.argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=err, env=self.env, start_new_session=True, close_fds=True,
                                 cwd=os.path.dirname(self.socket_path) or None)
        finally:
            if err is not subprocess.DEVNULL:
                err.close()
        applog.log("codex_broker_spawned", pid=p.pid, fingerprint=fp)
        return p

    def _start(self):
        t0 = time.time()
        if self.socket_path:
            conn = self._connect_broker()
        else:
            try:
                conn = _ProcConn(self.argv, self.env, self.stderr_path)
            except FileNotFoundError as e:
                raise RpcError("codex 실행 파일을 찾지 못했습니다(%s). Codex CLI 를 설치하고 로그인하세요." % self.argv[0]) from e
        self.proc = conn
        self.generation += 1
        self.loaded = set()
        self.started_at = time.time()
        gen = self.generation
        threading.Thread(target=self._reader, args=(conn, gen), daemon=True, name="codex-reader").start()
        info = self._request_raw("initialize", {
            "clientInfo": {"name": self.client_name, "title": "Codex Lens", "version": self.version},
            "capabilities": {"experimentalApi": True}}, 60) or {}
        self._send({"jsonrpc": "2.0", "method": "initialized"})
        broker = info.pop("codexLensBroker", None) or {}
        self.reattached = bool(broker.get("reattached"))
        if self.reattached:
            self.loaded = set(broker.get("loaded") or [])
        self.init_info = info
        applog.log("codex_started", pid=conn.pid, ms=int((time.time() - t0) * 1000), codex_home=info.get("codexHome"),
                   broker_pid=conn.info.get("pid"), reattached=self.reattached, loaded=len(self.loaded),
                   stale=conn.info.get("stale"))

    def close(self):
        """연결을 닫는다. 중계기를 쓰면 app-server 와 돌던 턴은 남는다."""
        with self.lock:
            p = self.proc
            self.proc = None
        if p:
            p.close()

    def shutdown_backend(self):
        """중계기와 app-server 까지 내린다(시험 정리용)."""
        if self.socket_path and self.proc and self.proc.alive():
            try:
                self._request_raw("_broker/shutdown", {}, 5)
            except RpcError:
                pass
        self.close()

    # --- 입출력 -------------------------------------------------------
    def _send(self, obj):
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self.write_lock:
            p = self.proc
            if not p or not p.alive():
                raise RpcError("codex app-server 가 실행 중이 아닙니다")
            try:
                p.write(data)
            except OSError as e:
                raise RpcError("codex app-server 에 보내지 못했습니다: %s" % e) from e

    def _reader(self, proc, gen):
        for raw in proc.lines():
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            try:
                self._dispatch(obj)
            except Exception as e:  # 처리기 오류가 읽기 루프를 끊지 않게
                applog.log("codex_dispatch_error", level="error", error=repr(e)[:300])
        code = proc.code()
        applog.log("codex_exited", level="warn", code=code, generation=gen)
        # 기다리던 요청은 모두 실패로 끝낸다.
        with self.lock:
            stale = [k for k, v in self.pending.items() if v.get("gen") == gen]
        for k in stale:
            slot = self.pending.pop(k, None)
            if slot:
                slot["error"] = RpcError("codex app-server 가 종료됐습니다")
                slot["event"].set()
        for fn in list(self.listeners):
            try:
                fn("_process/exited", {"generation": gen})
            except Exception:
                pass

    def _dispatch(self, obj):
        method = obj.get("method")
        if method and "id" in obj:
            threading.Thread(target=self._serve_request, args=(obj,), daemon=True).start()
            return
        if method:
            params = obj.get("params") or {}
            if method == "thread/closed":
                self.loaded.discard(params.get("threadId"))
            for fn in list(self.listeners):
                try:
                    fn(method, params)
                except Exception as e:
                    applog.log("listener_error", level="error", method=method, error=repr(e)[:300])
            return
        slot = self.pending.pop(obj.get("id"), None)
        if not slot:
            return
        if "error" in obj:
            e = obj["error"] or {}
            slot["error"] = RpcError(e.get("message") or "요청이 실패했습니다", e.get("code"), e.get("data"))
        else:
            slot["result"] = obj.get("result")
        slot["event"].set()

    def _serve_request(self, obj):
        method, params = obj.get("method"), obj.get("params") or {}
        result = None
        for fn in list(self.request_handlers):
            try:
                result = fn(method, params)
            except Exception as e:
                applog.log("server_request_error", level="error", method=method, error=repr(e)[:300])
            if result is not None:
                break
        try:
            if result is None:
                self._send({"jsonrpc": "2.0", "id": obj["id"],
                            "error": {"code": -32601, "message": "codex-lens 는 %s 요청을 처리하지 않습니다" % method}})
            else:
                self._send({"jsonrpc": "2.0", "id": obj["id"], "result": result})
        except RpcError:
            pass

    def _request_raw(self, method, params, timeout):
        with self.lock:
            self.next_id += 1
            rid = self.next_id
            slot = {"event": threading.Event(), "gen": self.generation}
            self.pending[rid] = slot
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not slot["event"].wait(timeout):
            self.pending.pop(rid, None)
            raise RpcError("%s 응답이 %d초 안에 오지 않았습니다" % (method, timeout))
        if slot.get("error"):
            raise slot["error"]
        return slot.get("result")

    def request(self, method, params=None, timeout=60):
        self.ensure()
        t0 = time.time()
        try:
            return self._request_raw(method, params or {}, timeout)
        finally:
            ms = int((time.time() - t0) * 1000)
            if ms > 1500:
                applog.log("codex_slow_request", level="warn", method=method, ms=ms)

    # --- 편의 ---------------------------------------------------------
    def ensure_loaded(self, thread_id, **overrides):
        """스레드가 이 프로세스에 올라와 있지 않으면 thread/resume 으로 올린다."""
        self.ensure()
        if thread_id in self.loaded:
            return None
        params = {"threadId": thread_id, "excludeTurns": True}
        params.update({k: v for k, v in overrides.items() if v is not None})
        res = self.request("thread/resume", params, 120)
        self.loaded.add(thread_id)
        return res
