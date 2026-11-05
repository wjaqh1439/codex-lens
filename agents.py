"""에이전트 라우터 — Codex app-server 와 Claude Code 어댑터를 CodexClient 하나처럼 보이게 묶는다.

- 스레드 ID 가 Claude 기록(또는 이 앱이 만든 Claude 세션)이면 Claude 로, 아니면 Codex 로 보낸다.
- thread/start·model/list 는 인자의 `agent`("codex" 기본 | "claude")로 가른다.
- thread/list 는 두 쪽을 갱신 시각 순으로 합친다. Codex 커서는 불투명해서, 페이지에 못 실은 Codex 항목을
  서버 쪽 커서 상태에 남겨 두고 다음 페이지에서 이어 합친다(앱을 다시 띄우면 처음부터).
- 한쪽 실행 파일이 없거나 죽어 있어도 다른 쪽 목록은 보인다.
"""

import collections
import itertools
import threading
import uuid

import applog
from codex_rpc import RpcError

AGENTS = ("codex", "claude")
CURSOR_KEEP = 200


class AgentRouter:
    def __init__(self, codex, claude):
        self.codex = codex
        self.claude = claude
        self.listeners = []
        self.request_handlers = []
        # 두 쪽이 같은 처리기 목록을 본다 — Lens 가 여기에 붙인다
        codex.listeners = self.listeners
        codex.request_handlers = self.request_handlers
        claude.listeners = self.listeners
        claude.request_handlers = self.request_handlers
        self.cursors = collections.OrderedDict()
        self.lock = threading.Lock()

    # --- CodexClient 와 같은 면 -----------------------------------------
    @property
    def loaded(self):
        return self.codex.loaded

    def __getattr__(self, name):
        # 그 밖의 Codex 전용 면(중계기 socket_path·shutdown_backend·proc …)은 Codex 클라이언트 것을 그대로 쓴다
        if name in ("codex", "claude"):
            raise AttributeError(name)
        return getattr(self.codex, name)

    @property
    def init_info(self):
        return self.codex.init_info

    def ensure(self):
        self.codex.ensure()

    def close(self):
        for c in (self.codex, self.claude):
            try:
                c.close()
            except Exception:
                pass

    def agent_of(self, tid):
        return "claude" if self.claude.owns(tid) else "codex"

    def backend(self, agent):
        return self.claude if agent == "claude" else self.codex

    def ensure_loaded(self, tid, **overrides):
        return self.backend(self.agent_of(tid)).ensure_loaded(tid, **overrides)

    def request(self, method, params=None, timeout=60):
        p = dict(params or {})
        agent = p.pop("agent", None)
        if method == "thread/list":
            return self._list(p, agent, timeout)
        if method in ("thread/start", "model/list"):
            return self.backend(agent).request(method, p, timeout)
        return self.backend(self.agent_of(p.get("threadId"))).request(method, p, timeout)

    # --- 목록 합치기 ------------------------------------------------------
    def _list(self, p, agent, timeout):
        if agent in AGENTS:
            out = self.backend(agent).request("thread/list", p, timeout)
            return out
        limit = max(1, min(int(p.get("limit") or 40), 100))
        token = p.pop("cursor", None)
        with self.lock:
            st = self.cursors.pop(token, None) if token else None
        if st is None:
            st = {"codex_cursor": None, "codex_done": False, "buf": collections.deque(), "claude_skip": 0}
        # Claude 쪽은 처음부터 걸러 낸 순서대로 읽고 이미 낸 개수만큼 건너뛴다(로컬 파일이라 싸다)
        claude_it = itertools.islice(self.claude.iter_threads(p.get("searchTerm"), p.get("cwd"),
                                                              p.get("excludedThreadIds")), st["claude_skip"], None)
        claude_head = next(claude_it, None)
        errors = []
        out = []
        while len(out) < limit:
            if not st["buf"] and not st["codex_done"]:
                cp = dict(p, limit=limit)
                if st["codex_cursor"]:
                    cp["cursor"] = st["codex_cursor"]
                try:
                    res = self.codex.request("thread/list", cp, timeout)
                except RpcError as e:
                    if "excludedThreadIds" in cp and "exclud" in str(e).lower():
                        cp.pop("excludedThreadIds")
                        try:
                            res = self.codex.request("thread/list", cp, timeout)
                        except RpcError as e2:
                            res, e = None, e2
                    else:
                        res = None
                    if res is None:
                        errors.append(str(e))
                        applog.log("list_backend_failed", level="warn", agent="codex", error=str(e)[:300])
                        st["codex_done"] = True
                if res is not None:
                    st["buf"].extend(res.get("data") or [])
                    st["codex_cursor"] = res.get("nextCursor")
                    st["codex_done"] = not st["codex_cursor"]
            c = st["buf"][0] if st["buf"] else None
            if c is None and claude_head is None:
                break
            if claude_head is None or (c is not None and (c.get("updatedAt") or 0) >= (claude_head.get("updatedAt") or 0)):
                out.append(st["buf"].popleft())
            else:
                out.append(claude_head)
                st["claude_skip"] += 1
                claude_head = next(claude_it, None)
        nxt = None
        if st["buf"] or not st["codex_done"] or claude_head is not None:
            nxt = uuid.uuid4().hex
            with self.lock:
                self.cursors[nxt] = st
                while len(self.cursors) > CURSOR_KEEP:
                    self.cursors.popitem(last=False)
        if not out and errors:
            raise RpcError(errors[0])
        return {"data": out, "nextCursor": nxt}
