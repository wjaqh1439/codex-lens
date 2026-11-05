"""Claude Code 를 Codex app-server 와 같은 RPC 면으로 감싼다(프로세스 안 어댑터).

lens·forks·docs·refine 은 Codex app-server 의 메서드·알림만 안다. 이 어댑터가 같은 이름의 메서드
(thread/list·read·start·fork·resume·unsubscribe, turn/start·steer·interrupt, model/list)를 받아
Claude Code 로 처리하고, 실행 중 이벤트를 Codex 알림 모양(turn/started, item/*, turn/completed …)으로 내보낸다.

- 기록: `<CLAUDE_CONFIG_DIR 또는 ~/.claude>/projects/*/<세션 ID>.jsonl` 을 읽는다(claude_history).
- 실행: 턴마다 `claude -p --input-format stream-json --output-format stream-json` 을 그 세션의 작업 폴더에서 띄운다.
  첫 턴은 `--session-id`(세션 ID 를 이 앱이 정한다), 다음 턴부터 `--resume`. 요청 메시지의 uuid = 턴 ID.
- 끼워 넣기(turn/steer): 돌고 있는 프로세스의 stdin 에 다음 사용자 메시지를 쓴다 — Claude 가 도구 결과 다음에 읽는다.
- 멈춤(turn/interrupt): stream-json 제어 요청 `interrupt`, 8초 안에 안 끝나면 프로세스를 끝낸다.
- 포크(thread/fork lastTurnId): 기록을 그 턴 끝까지 잘라 새 세션 ID 로 복사하고 `--resume` 한다. 앞부분이 같아
  원본의 프롬프트 캐시를 읽는다(2026-11-05 실측: 잘라 복사한 포크 40.9k/43.8k 캐시 읽음). 일회용 포크는 끝나면 지운다.
- 읽기 전용(sandbox read-only): `--permission-mode dontAsk` + 읽기 도구만 허락 — 도구 정의는 그대로라 캐시가 깨지지 않는다.
"""

import collections
import glob
import json
import os
import re
import subprocess
import threading
import time
import uuid

import applog
import claude_history as ch
from codex_rpc import RpcError

PERMISSION_MODE = os.environ.get("CODEX_LENS_CLAUDE_PERMISSION_MODE", "bypassPermissions")
READ_ONLY_TOOLS = ["Read", "Grep", "Glob", "WebSearch", "WebFetch", "Bash(git log:*)", "Bash(git show:*)",
                   "Bash(git diff:*)", "Bash(git status:*)", "Bash(git blame:*)", "Bash(ls:*)", "Bash(cat:*)",
                   "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)", "Bash(rg:*)", "Bash(grep:*)"]
INTERRUPT_GRACE_SEC = 8
MISS_CACHE_SEC = 15
READ_ONLY = "하위 에이전트 기록은 읽기만 됩니다 — 이어서 시키려면 상위 세션에서 말하세요"
_ID = re.compile(r"^[0-9A-Za-z-]{8,64}$")

# 모델 목록의 단일 출처. effort 는 Claude Code CLI 의 --effort 선택지(low·medium·high·xhigh·max).
# CODEX_LENS_CLAUDE_MODELS(JSON 목록, 같은 키)로 바꿀 수 있다.
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
MODELS = [
    {"id": "claude-fable-5-1", "alias": "fable", "label": "Claude Fable 5.1", "description": "가장 뛰어난 모델", "efforts": EFFORTS},
    {"id": "claude-opus-5-5", "alias": "opus", "label": "Claude Opus 5.5", "description": "깊은 추론 · 복잡한 작업", "efforts": EFFORTS},
    {"id": "claude-sonnet-5-5", "alias": "sonnet", "label": "Claude Sonnet 5.5", "description": "균형 — 일반 구현", "efforts": EFFORTS},
    {"id": "claude-haiku-5-5", "alias": "haiku", "label": "Claude Haiku 5.5", "description": "빠르고 가벼운 작업", "efforts": EFFORTS},
]


def _now():
    return time.time()


def _blocks(inputs):
    """Codex 입력 항목(text·image·localImage) → Claude 메시지 블록."""
    out = []
    for it in inputs or []:
        t = it.get("type")
        if t == "text" and it.get("text"):
            out.append({"type": "text", "text": it["text"]})
        elif t == "localImage" and it.get("path"):
            out.append({"type": "text", "text": "[첨부 이미지: %s]" % it["path"]})
        elif t == "image" and it.get("url"):
            m = re.match(r"^data:(image/[\w.+-]+);base64,(.*)$", it["url"], re.S)
            if m:   # 붙여넣은 이미지(data URL) → Claude 이미지 블록
                out.append({"type": "image", "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)}})
            else:
                out.append({"type": "image", "source": {"type": "url", "url": it["url"]}})
    return out or [{"type": "text", "text": " "}]


def _text_of(inputs):
    return "\n".join(it.get("text") or "" for it in inputs or [] if it.get("type") == "text" and it.get("text"))


def _images_of(inputs):
    """실행 중 화면에 보일 이미지 — 보낸 입력 항목(image·localImage)을 그대로 쓴다."""
    return [it for it in inputs or [] if it.get("type") in ("image", "localImage")]


def _user_line(uid, inputs):
    return {"type": "user", "uuid": uid, "session_id": "", "parent_tool_use_id": None,
            "message": {"role": "user", "content": _blocks(inputs)}}


class Run:
    """턴 하나 = claude -p 프로세스 하나. stdout(stream-json)을 Codex 알림으로 바꿔 내보낸다."""

    def __init__(self, client, th, inputs, client_id=None, model=None, effort=None):
        self.client = client
        self.th = th
        self.tid = th["id"]
        self.turn_id = str(uuid.uuid4())
        self.inputs = inputs
        self.client_id = client_id
        self.model = model
        self.effort = effort
        self.proc = None
        self.write_lock = threading.Lock()
        self.pending = {}          # 끼워 넣은 말 uuid -> (client id, 입력)
        self.tools = {}            # tool_use id -> 진행 중 항목
        self.answers = set()       # StructuredOutput 처럼 답으로 바꾼 도구 id
        self.sub_signal = 0        # 하위 에이전트 소식을 마지막으로 부모 항목에 알린 시각
        self.block = {}            # 메시지 id -> 지금 스트림 중인 블록 번호
        self.count = {}            # 메시지 id -> 다음 블록 번호(스트림 이벤트가 없을 때)
        self.cur_msg = None
        self.done = False
        self.interrupted = False
        self.stdin_open = True
        self.stderr = collections.deque(maxlen=40)
        self.started_at = _now()
        self.context_last = None
        self.context_model = None
        self.context_window = None

    # --- 띄우기 ---------------------------------------------------------
    def argv(self):
        c, th = self.client, self.th
        a = list(c.argv) + ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                            "--include-partial-messages", "--replay-user-messages"]
        if th.get("path") or c.find(self.tid):
            a += ["--resume", self.tid]
        else:
            a += ["--session-id", self.tid]
            if th.get("ephemeral"):
                a.append("--no-session-persistence")
        model = c.model_arg(self.model or th.get("model"))
        if model:
            a += ["--model", model]
        if self.effort:
            a += ["--effort", self.effort]
        extra = c.instructions_for(th)
        if extra:
            a += ["--append-system-prompt", extra]
        if th.get("sandbox") == "read-only":
            a += ["--permission-mode", "dontAsk", "--allowedTools", ",".join(READ_ONLY_TOOLS)]
        else:
            a += ["--permission-mode", PERMISSION_MODE]
        return a

    def start(self):
        cwd = self.th.get("cwd")
        if not cwd or not os.path.isdir(cwd):
            raise RpcError("작업 폴더가 없습니다: %s" % cwd)
        argv = self.argv()
        try:
            self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         cwd=cwd, env=self.client.run_env(self.tid), bufsize=0)
        except FileNotFoundError as e:
            raise RpcError("claude 실행 파일을 찾지 못했습니다(%s). Claude Code 를 설치하고 로그인하세요." % argv[0]) from e
        applog.log("claude_turn_spawn", thread=self.tid, turn=self.turn_id, pid=self.proc.pid,
                   resume="--resume" in argv, model=self.model or self.th.get("model"), effort=self.effort,
                   sandbox=self.th.get("sandbox"))
        self._write(_user_line(self.turn_id, self.inputs))
        self.err_thread = threading.Thread(target=self._read_err, daemon=True, name="claude-stderr")
        self.err_thread.start()
        threading.Thread(target=self._read, daemon=True, name="claude-turn").start()

    def _write(self, obj):
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self.write_lock:
            if not self.stdin_open or not self.proc or self.proc.poll() is not None:
                raise RpcError("Claude Code 가 입력을 받지 않습니다(턴이 끝나는 중)")
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise RpcError("Claude Code 에 보내지 못했습니다: %s" % e) from e

    def _close_stdin(self):
        with self.write_lock:
            if self.stdin_open:
                self.stdin_open = False
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass

    # --- 끼워 넣기 · 멈춤 -------------------------------------------------
    def steer(self, inputs, client_id):
        if self.done or self.interrupted:
            raise RpcError("진행 중인 턴이 끝나 끼워 넣지 못했습니다")
        uid = str(uuid.uuid4())
        self.pending[uid] = (client_id, inputs)
        try:
            self._write(_user_line(uid, inputs))
        except RpcError:
            self.pending.pop(uid, None)
            raise
        return uid

    def interrupt(self):
        if self.done:
            return
        self.interrupted = True
        try:
            self._write({"type": "control_request", "request_id": "lens-" + uuid.uuid4().hex[:8],
                         "request": {"subtype": "interrupt"}})
        except RpcError:
            pass

        def hard():
            if self.done or not self.proc:
                return
            try:
                self.proc.terminate()
                self.proc.wait(5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        threading.Timer(INTERRUPT_GRACE_SEC, hard).start()

    def kill(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass

    # --- 읽기 ----------------------------------------------------------
    def emit(self, method, params):
        self.client.emit(method, dict(params, threadId=self.tid))

    def _read_err(self):
        path = self.client.stderr_path
        f = open(path, "ab") if path else None
        try:
            for raw in self.proc.stderr:
                self.stderr.append(raw.decode("utf-8", "replace").rstrip())
                if f:
                    f.write(raw)
                    f.flush()
        except (ValueError, OSError):
            pass                       # 끝난 뒤 파이프가 닫혔다
        finally:
            if f:
                f.close()

    def _read(self):
        self.emit("turn/started", {"turn": {"id": self.turn_id, "status": "inProgress", "items": []}})
        self.emit("item/completed", {"item": ch.user_item(self.turn_id, _text_of(self.inputs), _images_of(self.inputs),
                                                          self.client_id)})
        try:
            for raw in self.proc.stdout:
                o = ch._loads(raw)
                if o is None:
                    continue
                try:
                    self._on(o)
                except Exception as e:   # 한 이벤트의 오류가 턴 전체를 끊지 않게
                    applog.log("claude_event_error", level="error", thread=self.tid, error=repr(e)[:300],
                               type=o.get("type"))
                if self.done:
                    break
        finally:
            code = self.proc.wait()
            self.err_thread.join(2)        # 마지막 오류 줄까지 읽고 나서 닫는다
            for f in (self.proc.stdout, self.proc.stderr):
                try:
                    f.close()
                except Exception:
                    pass
            if not self.done:
                err = "\n".join(list(self.stderr)[-6:]).strip()
                if self.interrupted:
                    self._finish("interrupted", None)
                else:
                    self._finish("failed", err or "Claude Code 가 답 없이 끝났습니다(종료 코드 %s)" % code)

    def _on_sub(self, o):
        """하위 에이전트의 소식 → 부모 Agent 항목의 activity(지금 돌리는 도구)를 고쳐 알린다.

        알림이 곧 신호다 — 이게 없으면 하위 에이전트가 일하는 내내 부모 턴은 조용해 '지연'으로 보인다.
        스트림 조각마다 알리지 않도록 도구가 바뀔 때와 5초마다만 알린다."""
        parent = self.tools.get(o.get("parent_tool_use_id"))
        if parent is None:
            return
        changed = False
        for b in ch._content_blocks(o.get("message")):
            if o.get("type") == "assistant" and b.get("type") == "tool_use":
                parent["activity"] = ch.strip_private(dict(ch.tool_item(b, self.th.get("cwd")), startedAt=_now()))
                changed = True
            elif o.get("type") == "user" and b.get("type") == "tool_result" and parent.get("activity") \
                    and parent["activity"].get("id") == b.get("tool_use_id"):
                parent["activity"] = None
                changed = True
        now = _now()
        if changed or now - self.sub_signal >= 5:
            self.sub_signal = now
            if not parent.get("agentThreadId"):
                # 그 하위 에이전트 기록(meta 의 toolUseId)을 찾으면 화면이 그 기록을 세션으로 연다
                path = self.client.find(self.tid)
                sub = ch.subagent_threads(path).get(parent.get("id")) if path else None
                if sub:
                    parent["agentThreadId"] = sub
            self.emit("item/updated", {"item": ch.strip_private(parent)})

    def _item_id(self, mid):
        idx = self.block.get(mid)
        if idx is None:
            idx = self.count.get(mid, 0)
        self.count[mid] = idx + 1
        return "%s:%d" % (mid, idx)

    def _on(self, o):
        t = o.get("type")
        if o.get("parent_tool_use_id"):
            self._on_sub(o)            # 하위 에이전트 안의 일 — 부모 턴의 도구 항목으로만 보인다
            return
        if t == "stream_event":
            ev = o.get("event") or {}
            et = ev.get("type")
            if et == "message_start":
                self.cur_msg = (ev.get("message") or {}).get("id")
            elif et == "content_block_start" and self.cur_msg:
                idx = ev.get("index", 0)
                self.block[self.cur_msg] = idx
                cb = ev.get("content_block") or {}
                iid = "%s:%d" % (self.cur_msg, idx)
                if cb.get("type") == "text":
                    self.emit("item/started", {"item": ch.text_item(iid, "")})
                elif cb.get("type") in ("thinking", "redacted_thinking"):
                    self.emit("item/started", {"item": ch.thinking_item(iid, "")})
            elif et == "content_block_delta" and self.cur_msg:
                d = ev.get("delta") or {}
                iid = "%s:%d" % (self.cur_msg, ev.get("index", 0))
                if d.get("type") == "text_delta" and d.get("text"):
                    self.emit("item/agentMessage/delta", {"itemId": iid, "delta": d["text"]})
                elif d.get("type") == "thinking_delta" and d.get("thinking"):
                    self.emit("item/reasoning/summaryTextDelta", {"itemId": iid, "delta": d["thinking"], "summaryIndex": 0})
            return
        if t == "assistant":
            msg = o.get("message") or {}
            if msg.get("model") and msg["model"] != "<synthetic>":
                self.client.seen_model(self.tid, msg["model"])
                self.context_model = msg["model"]
                if msg.get("usage"):
                    u = msg["usage"]
                    self.context_last = {"totalTokens": sum(u.get(k) or 0 for k in (
                        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"))}
                    self._emit_context()
            mid = msg.get("id") or o.get("uuid")
            for b in ch._content_blocks(msg):
                bt = b.get("type")
                iid = self._item_id(mid)
                if bt == "text":
                    self.emit("item/completed", {"item": ch.text_item(iid, b.get("text"))})
                elif bt in ("thinking", "redacted_thinking"):
                    self.emit("item/completed", {"item": ch.thinking_item(iid, b.get("thinking") or "")})
                elif bt == "tool_use":
                    ans = ch.structured_answer(b)
                    if ans is not None:
                        self.answers.add(b.get("id"))
                        self.emit("item/completed", {"item": dict(ch.text_item(b.get("id"), ans), phase="final_answer")})
                        continue
                    it = dict(ch.tool_item(b, self.th.get("cwd")), startedAt=_now())
                    self.tools[b.get("id")] = it
                    self.emit("item/started", {"item": ch.strip_private(it)})
            if o.get("error") and not self.interrupted:
                self.emit("error", {"error": {"message": str(o.get("error"))}})
            return
        if t == "user":
            if o.get("isReplay"):
                uid = o.get("uuid")
                hit = self.pending.pop(uid, None)
                if hit:
                    cid, inputs = hit
                    self.emit("item/completed", {"item": ch.user_item(uid, _text_of(inputs), _images_of(inputs), cid)})
                return
            for b in ch._content_blocks(o.get("message")):
                if b.get("type") != "tool_result":
                    continue
                it = self.tools.pop(b.get("tool_use_id"), None)
                if it is None:
                    continue
                done = ch.apply_result(it, b, o.get("tool_use_result"))
                self.emit("item/completed", {"item": ch.strip_private(done)})
            return
        if t == "system":
            st = o.get("subtype")
            if st == "init" and o.get("model"):
                self.client.seen_model(self.tid, o["model"])
            elif st == "compact_boundary":
                self.emit("item/completed", {"item": {"type": "contextCompaction", "id": o.get("uuid") or uuid.uuid4().hex}})
            return
        if t == "result":
            # result.usage는 턴 전체 누적이다. 컨텍스트는 마지막 assistant 호출 하나만 쓴다.
            mu = (o.get("modelUsage") or {}).get(self.context_model) or {}
            self.context_window = mu.get("contextWindow") or self.context_window
            self._emit_context()
            u = o.get("usage") or {}
            cached = u.get("cache_read_input_tokens") or 0
            total_in = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + cached
            last = {"inputTokens": total_in, "cachedInputTokens": cached, "outputTokens": u.get("output_tokens"),
                    "reasoningOutputTokens": (u.get("output_tokens_details") or {}).get("thinking_tokens")}
            self.emit("thread/tokenUsage/updated", {"turnId": self.turn_id, "tokenUsage": {"last": last, "total": last}})
            if self.pending and not self.interrupted:
                return                 # 끼워 넣은 말을 아직 읽지 않았다 — 같은 턴으로 이어서 받는다
            reason = str(o.get("terminal_reason") or "")
            if self.interrupted or reason.startswith("aborted"):
                self._finish("interrupted", None)
            elif o.get("subtype") == "success" and not o.get("is_error"):
                self._finish("completed", None)
            else:
                errs = o.get("errors")
                msg = o.get("result") or ("; ".join(map(str, errs)) if isinstance(errs, list) and errs else "") \
                    or o.get("subtype") or "실패"
                self._finish("failed", str(msg)[:2000])

    def _emit_context(self):
        if self.context_last is not None:
            self.emit("thread/context/updated", {"turnId": self.turn_id, "tokenUsage": {
                "last": self.context_last, "modelContextWindow": self.context_window}})

    def _finish(self, status, error):
        if self.done:
            return
        self.done = True
        self._close_stdin()
        for it in list(self.tools.values()):
            self.emit("item/completed", {"item": dict(ch.strip_private(it), status="interrupted" if status == "interrupted" else "failed")})
        self.tools.clear()
        self.client.run_ended(self, status)
        self.emit("turn/completed", {"turn": {"id": self.turn_id, "status": status,
                                              "error": {"message": error} if error else None}})
        applog.log("claude_turn_end", thread=self.tid, turn=self.turn_id, status=status,
                   ms=int((_now() - self.started_at) * 1000), error=(error or "")[:200] or None)


class ClaudeClient:
    """CodexClient 와 같은 모양(request·listeners·loaded·ensure_loaded·close·init_info)의 Claude Code 어댑터."""

    agent = "claude"

    def __init__(self, root=None, argv=None, env=None, stderr_path=None, list_filter=None, instructions=None):
        env_argv = os.environ.get("CODEX_LENS_CLAUDE_ARGV")
        self.argv = argv or (json.loads(env_argv) if env_argv else [os.environ.get("CODEX_LENS_CLAUDE_BIN", "claude")])
        self.env = env
        src = env if env is not None else os.environ
        self.root = os.path.abspath(os.path.expanduser(root or src.get("CLAUDE_CONFIG_DIR") or "~/.claude"))
        self.stderr_path = stderr_path
        self.list_filter = list_filter      # fn(id) -> bool — 데모가 설정 폴더를 같이 쓸 때 데모 세션만 보인다
        # fn(id) -> 덧붙일 시스템 지침(포트 관리) 또는 None. 포크는 원본 세션 기준으로 물어 원본과 같은 시스템
        # 프롬프트로 돌아야 캐시를 읽는다 — 그래서 지침은 이 앱이 만든 세션에만 주고(Lens), 턴마다 같은 값을 준다
        self.instructions = instructions
        self.listeners = []
        self.request_handlers = []
        self.loaded = set()
        self.init_info = {"claudeHome": self.root}
        self.lock = threading.RLock()
        self.threads = {}      # 이 앱이 만들었거나 다룬 세션: id -> {cwd, model, sandbox, ephemeral, path, …}
        self.runs = {}         # id -> 돌고 있는 Run
        self.where = {}        # id -> 기록 파일
        self.miss = {}         # id -> 마지막으로 못 찾은 시각
        self.metas = {}        # 파일 -> 목록 메타 (mtime·크기가 같으면 다시 쓴다)
        self.reads = {}        # 파일 -> (활동 시각, 크기, live, 프로세스, 턴, 정보)
        self._models = None

    def instructions_for(self, th):
        if not self.instructions:
            return None
        try:
            return self.instructions(th.get("forked_from") or th["id"])
        except Exception:
            return None

    def run_env(self, tid):
        """세션이 돌리는 명령(bin/lens-port)이 어느 세션인지 알게 Codex 와 같은 CODEX_THREAD_ID 를 넣는다."""
        env = dict(self.env if self.env is not None else os.environ)
        env["CODEX_THREAD_ID"] = tid
        return env

    # --- CodexClient 와 같은 면 -----------------------------------------
    def ensure(self):
        return None

    def close(self):
        with self.lock:
            runs = list(self.runs.values())
        for r in runs:
            r.kill()

    def ensure_loaded(self, tid, **overrides):
        th = self._thread(tid)
        for k in ("sandbox", "model"):
            if overrides.get(k):
                th[k] = overrides[k]
        return None

    def emit(self, method, params):
        for fn in list(self.listeners):
            try:
                fn(method, params)
            except Exception as e:
                applog.log("listener_error", level="error", method=method, error=repr(e)[:300])

    def request(self, method, params=None, timeout=60):
        fn = getattr(self, "_m_" + method.replace("/", "_"), None)
        if fn is None:
            raise RpcError("Claude Code 는 %s 를 지원하지 않습니다" % method)
        t0 = _now()
        try:
            return fn(params or {})
        finally:
            ms = int((_now() - t0) * 1000)
            if ms > 1500:
                applog.log("claude_slow_request", level="warn", method=method, ms=ms)

    # --- 세션 찾기 ------------------------------------------------------
    def projects(self):
        return os.path.join(self.root, "projects")

    def find(self, tid):
        if not tid or not _ID.match(str(tid)):
            return None
        p = self.where.get(tid)
        if p and os.path.exists(p):
            return p
        if ch.is_subagent_id(tid):
            # 하위 에이전트 기록 — <프로젝트>/<부모 세션 ID>/subagents/agent-<ID>.jsonl
            hits = glob.glob(os.path.join(glob.escape(self.projects()), "*", "*", "subagents", tid + ".jsonl"))
        else:
            hits = glob.glob(os.path.join(glob.escape(self.projects()), "*", tid + ".jsonl"))
        if hits:
            self.where[tid] = hits[0]
            self.miss.pop(tid, None)
            return hits[0]
        return None

    def owns(self, tid):
        if not tid:
            return False
        with self.lock:
            if tid in self.threads:
                return True
        if tid in self.where:
            return True
        m = self.miss.get(tid)
        if m and _now() - m < MISS_CACHE_SEC:
            return False
        if self.find(tid):
            return True
        self.miss[tid] = _now()
        return False

    def _meta(self, path):
        try:
            st = os.stat(path)
        except OSError:
            return None
        m = self.metas.get(path)
        if m and m["mtime"] == st.st_mtime and m["size"] == st.st_size:
            return m
        try:
            m = ch.scan_meta(path)
        except OSError:
            return None
        self.metas[path] = m
        return m

    def _thread(self, tid):
        with self.lock:
            th = self.threads.get(tid)
            if th:
                return th
        path = self.find(tid)
        if not path:
            raise RpcError("Claude 세션을 찾지 못했습니다: %s" % tid)
        m = self._meta(path) or {}
        th = {"id": tid, "cwd": m.get("cwd"), "model": m.get("model"), "sandbox": None, "ephemeral": False,
              "path": path, "created": m.get("created") or _now(), "external": True}
        with self.lock:
            return self.threads.setdefault(tid, th)

    def seen_model(self, tid, model):
        with self.lock:
            th = self.threads.get(tid)
            if th and th.get("external") and model:
                th["model"] = model

    def run_ended(self, run, status):
        with self.lock:
            if self.runs.get(run.tid) is run:
                self.runs.pop(run.tid, None)
            th = run.th
            p = self.find(run.tid)
            if p:
                th["path"] = p
            th["updated"] = _now()

    def running(self, tid):
        with self.lock:
            r = self.runs.get(tid)
            return r if r and not r.done else None

    # --- 모델 -----------------------------------------------------------
    def default_model(self):
        """Claude Code 설정의 기본 모델 id(별칭이면 풀어서). 모르면 None."""
        val = os.environ.get("ANTHROPIC_MODEL")
        if not val:
            try:
                with open(os.path.join(self.root, "settings.json")) as f:
                    val = (json.load(f) or {}).get("model")
            except (OSError, ValueError):
                val = None
        if not val:
            return None
        base = re.sub(r"\[.*\]$", "", str(val)).strip()
        for m in self.model_table():
            if base in (m["id"], m.get("alias")):
                return m["id"]
        return base

    def model_arg(self, model):
        """--model 로 넘길 값. 설정의 기본 모델과 같으면 넘기지 않는다 — 기본값에 붙은 옵션(예: opus[1m] 의 긴 맥락)과
        원본 세션의 조건을 그대로 둬 캐시와 맥락 한도가 바뀌지 않게 한다."""
        if not model:
            return None
        return None if model == self.default_model() else model

    def model_table(self):
        if self._models is None:
            raw = os.environ.get("CODEX_LENS_CLAUDE_MODELS")
            try:
                self._models = json.loads(raw) if raw else MODELS
            except ValueError:
                self._models = MODELS
        return self._models

    def _m_model_list(self, p):
        default = self.default_model()
        return {"data": [{"id": m["id"], "displayName": m.get("label") or m["id"], "description": m.get("description") or "",
                          "isDefault": m["id"] == default, "hidden": False,
                          "supportedReasoningEfforts": [{"reasoningEffort": e} for e in m.get("efforts") or []],
                          "defaultReasoningEffort": None} for m in self.model_table()]}

    # --- 목록 · 읽기 ---------------------------------------------------------
    def _obj(self, tid, m=None, th=None):
        m = m or {}
        th = th or self.threads.get(tid) or {}
        created = m.get("created") or th.get("created") or _now()
        updated = m.get("mtime") or th.get("updated") or created
        path = m.get("path") or th.get("path")
        if path and _now() - updated < ch.LIVE_GRACE_SEC * 2:
            # 하위 에이전트가 일하는 동안 부모 기록은 멈춰 있다 — 그쪽 기록의 변경도 갱신으로 본다
            updated = ch.activity_mtime(path, updated)
        name = m.get("name")
        extra = {}
        if path and ch.is_subagent_path(path):
            # 하위 에이전트 기록: 부모는 기록이 든 폴더의 세션, 이름은 meta 의 종류·설명
            sm = ch.subagent_meta(path)
            kind = sm.get("agentType") or "에이전트"
            name = name or ("하위 에이전트 · %s" % kind + (" · %s" % sm["description"] if sm.get("description") else ""))
            extra = {"parentThreadId": os.path.basename(os.path.dirname(os.path.dirname(path))),
                     "agentNickname": kind, "readOnly": True}
        return dict({"id": tid, "name": name, "preview": m.get("preview") or th.get("preview") or "",
                "cwd": m.get("cwd") or th.get("cwd"), "model": m.get("model") or th.get("model"),
                "reasoningEffort": m.get("effort"), "createdAt": int(created), "updatedAt": int(updated),
                "source": m.get("entrypoint") or "claude", "status": {"type": "active" if self.running(tid) else "idle"},
                "forkedFromId": th.get("forked_from"), "path": m.get("path") or th.get("path"),
                "cliVersion": m.get("version"), "gitInfo": {"branch": m.get("branch")} if m.get("branch") else None,
                "agent": "claude"}, **extra)

    def iter_threads(self, search=None, cwd=None, exclude=()):
        """갱신 시각이 늦은 것부터 Thread 모양을 하나씩 낸다(검색어가 없으면 필요한 만큼만 메타를 읽는다)."""
        exclude = set(exclude or ())
        files = []
        for path in glob.glob(os.path.join(glob.escape(self.projects()), "*", "*.jsonl")):
            try:
                files.append((os.path.getmtime(path), path))
            except OSError:
                pass
        seen = set()
        with self.lock:
            fresh = [(th.get("created") or 0, th) for th in self.threads.values()
                     if not th.get("path") and not th.get("ephemeral") and not th.get("sandbox")]
        rows = [(mt, "f", p) for mt, p in files] + [(c, "t", th) for c, th in fresh]
        rows.sort(key=lambda r: r[0], reverse=True)
        q = (search or "").lower().strip()
        for mt, kind, x in rows:
            if kind == "f":
                tid = os.path.basename(x)[:-len(".jsonl")]
                if tid in exclude or tid in seen or (self.list_filter and not self.list_filter(tid)):
                    continue
                m = self._meta(x)
                if not m or (not m["preview"] and not m["name"]):
                    continue                 # 요청이 하나도 없는 빈 기록
                self.where.setdefault(tid, x)
                obj = self._obj(tid, m)
            else:
                tid = x["id"]
                if tid in exclude or self.find(tid) or (self.list_filter and not self.list_filter(tid)):
                    continue
                obj = self._obj(tid, None, x)
            if cwd and obj["cwd"] != cwd:
                continue
            if q and q not in ("%s\n%s\n%s" % (obj["name"] or "", obj["preview"], obj["cwd"] or "")).lower():
                continue
            seen.add(tid)
            yield obj

    def _m_thread_list(self, p):
        limit = max(1, min(int(p.get("limit") or 40), 200))
        after = p.get("cursor")
        out, nxt = [], None
        skipping = bool(after)
        for obj in self.iter_threads(p.get("searchTerm"), p.get("cwd"), p.get("excludedThreadIds")):
            if skipping:
                if obj["id"] == after:
                    skipping = False
                continue
            if len(out) >= limit:
                nxt = out[-1]["id"]
                break
            out.append(obj)
        return {"data": out, "nextCursor": nxt}

    def _m_thread_read(self, p):
        tid = p.get("threadId")
        path = self.find(tid)
        with self.lock:
            th = self.threads.get(tid)
        if not path:
            if th:
                return {"thread": dict(self._obj(tid, None, th), turns=[])}
            raise RpcError("Claude 세션을 찾지 못했습니다: %s" % tid)
        m = self._meta(path) or {}
        obj = self._obj(tid, m, th)
        if not p.get("includeTurns"):
            return {"thread": dict(obj, turns=[])}
        st = os.stat(path)
        # 앞 하위 에이전트가 도는 동안은 본 기록이 조용하다 — 하위 에이전트 기록까지 본 시각으로 진행·캐시를 정한다
        act = ch.activity_mtime(path, st.st_mtime)
        live = bool(self.running(tid))
        # 이 앱 밖에서 도는(또는 앱 재기동으로 손을 놓친) 세션: Claude 프로세스 명부로 살아 있는지 본다
        side = ch.is_subagent_path(path)
        if live:
            proc = None
        elif side:
            proc = ch.subagent_proc(ch.proc_registry(self.root), path, ch.subagent_meta(path), m.get("last_version"), act)
        else:
            proc = ch.proc_state(ch.proc_registry(self.root), tid, m.get("last_version"), act)
        hit = self.reads.get(path)
        if hit and hit[:4] == (act, st.st_size, live, proc) and not (
                hit[5].get("grace_until") and _now() >= hit[5]["grace_until"]):
            turns, info = hit[4], hit[5]
        else:
            turns, info = ch.build_turns(ch.read_records(path), m.get("cwd"), act, live, proc,
                                         lambda ids: ch.subagent_activity(path, ids), sidechain=side)
            # 하위 에이전트 항목 → 그 기록을 세션으로 여는 ID(진행 중이라 결과가 아직 없어도 meta 로 잇는다)
            turns = ch.with_subagent_threads(turns, path)
            self.reads[path] = (act, st.st_size, live, proc, turns, info)
            if len(self.reads) > 64:
                self.reads.pop(next(iter(self.reads)))
        if turns and turns[-1].get("status") == "inProgress":
            turns = turns[:-1] + [self._with_subagents(path, turns[-1])]
        obj["updatedAt"] = max(obj["updatedAt"], int(act))
        obj["model"] = info.get("model") or obj["model"]
        obj["reasoningEffort"] = info.get("effort") or obj["reasoningEffort"]
        return {"thread": dict(obj, turns=turns)}

    @staticmethod
    def _with_subagents(path, turn):
        """진행 중 턴의 아직 안 끝난 하위 에이전트 항목에 그 에이전트의 진행(마지막 단계·시각)을 붙인다."""
        ids = {i.get("id") for i in turn.get("items") or []
               if i.get("type") == "subAgentActivity" and i.get("status") == "inProgress"}
        prog = ch.subagent_progress(path, ids) if ids else {}
        if not prog:
            return turn
        items = [dict(i, progress=prog[i["id"]]) if i.get("id") in prog else i for i in turn["items"]]
        return dict(turn, items=items)

    # --- 만들기 · 포크 ------------------------------------------------------
    def _m_thread_start(self, p):
        cwd = p.get("cwd")
        if not cwd or not os.path.isdir(cwd):
            raise RpcError("작업 폴더가 없습니다: %s" % cwd)
        tid = str(uuid.uuid4())
        th = {"id": tid, "cwd": os.path.realpath(cwd), "model": p.get("model"), "sandbox": p.get("sandbox") if p.get("sandbox") == "read-only" else None,
              "ephemeral": bool(p.get("ephemeral")), "path": None, "created": _now()}
        with self.lock:
            self.threads[tid] = th
        return {"thread": dict(self._obj(tid, None, th), turns=[]), "model": p.get("model")}

    def _m_thread_fork(self, p):
        src = p.get("threadId")
        if ch.is_subagent_id(src):
            raise RpcError(READ_ONLY)
        last = p.get("lastTurnId")
        path = self.find(src)
        if not path:
            raise RpcError("포크할 Claude 세션을 찾지 못했습니다: %s" % src)
        run = self.running(src)
        if run and last == run.turn_id:
            raise RpcError("turn is in progress — 진행 중인 턴은 포크할 수 없습니다")
        new = str(uuid.uuid4())
        try:
            lines = ch.fork_lines(path, last, new)
        except ValueError as e:
            raise RpcError(str(e)) from e
        dst = os.path.join(os.path.dirname(path), new + ".jsonl")
        with open(dst, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        m = self._meta(path) or {}
        th = {"id": new, "cwd": m.get("cwd"), "model": p.get("model") or m.get("model"),
              "sandbox": p.get("sandbox") if p.get("sandbox") == "read-only" else None,
              "ephemeral": bool(p.get("ephemeral")), "path": dst, "created": _now(), "forked_from": src}
        with self.lock:
            self.threads[new] = th
        self.where[new] = dst
        applog.log("claude_fork", thread=src, fork=new, turn=last, ephemeral=th["ephemeral"], lines=len(lines))
        return {"thread": dict(self._obj(new, None, th), turns=[])}

    def _m_thread_resume(self, p):
        self._thread(p.get("threadId"))
        return {}

    def _m_thread_unsubscribe(self, p):
        tid = p.get("threadId")
        with self.lock:
            th = self.threads.get(tid)
            if th and th.get("ephemeral") and not self.running(tid):
                self.threads.pop(tid, None)
            else:
                th = None
        if th and th.get("path"):
            # 일회용 포크는 기록을 남기지 않는다(Codex 의 ephemeral 과 같게)
            for x in (th["path"], th["path"][:-len(".jsonl")]):
                try:
                    if os.path.isdir(x):
                        import shutil
                        shutil.rmtree(x, ignore_errors=True)
                    elif os.path.exists(x):
                        os.remove(x)
                except OSError:
                    pass
            self.where.pop(tid, None)
            self.metas.pop(th["path"], None)
        return {}

    # --- 턴 -------------------------------------------------------------
    def _m_turn_start(self, p):
        tid = p.get("threadId")
        if ch.is_subagent_id(tid):
            raise RpcError(READ_ONLY)
        th = self._thread(tid)
        with self.lock:
            if self.running(tid):
                raise RpcError("이 Claude 세션은 이미 응답하는 중입니다")
            run = Run(self, th, p.get("input"), p.get("clientUserMessageId"), p.get("model"), p.get("effort"))
            self.runs[tid] = run
        try:
            run.start()
        except Exception:
            with self.lock:
                self.runs.pop(tid, None)
            raise
        return {"turn": {"id": run.turn_id, "status": "inProgress", "items": []}}

    def _m_turn_steer(self, p):
        tid = p.get("threadId")
        run = self.running(tid)
        if not run:
            raise RpcError("진행 중인 턴이 없어 끼워 넣지 못했습니다")
        if p.get("expectedTurnId") and p["expectedTurnId"] != run.turn_id:
            raise RpcError("진행 중인 턴이 바뀌어 끼워 넣지 못했습니다")
        run.steer(p.get("input"), p.get("clientUserMessageId"))
        return {"turnId": run.turn_id}

    def _m_turn_interrupt(self, p):
        run = self.running(p.get("threadId"))
        if not run:
            raise RpcError("실행 중인 턴이 없습니다")
        run.interrupt()
        return {}
