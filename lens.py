"""앱의 중심 — 세션 목록·읽기·실행, 실행 중 턴 스트림, 코멘트, 멘션.

과정 요약과 인라인 질문(포크 작업)은 forks.py 의 JobManager 가 맡는다.
"""

import os
import base64
import binascii
import queue
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import applog
import codex_broker
import refine
import worktree
from agents import AgentRouter
from claude_rpc import ClaudeClient
from codex_rpc import CodexClient, RpcError
from forks import JobManager
from session_search import CodexIndex, matches, session_title, terms
from session_tree import Launches, group, thread_parent
from store import JsonStore
from stream_speed import StreamSpeed
from codex_speed import RolloutRates, summarize as summarize_rates
from context_usage import context_usage
from turns import normalize_turn, quiet_until, subagent_title, unwrap_output

# 목록에 보일 세션 출처. 세션이 띄운 하위 에이전트(subAgentThreadSpawn)는 부모 아래에 묶어 보이고,
# 검토·압축 같은 내부 하위 에이전트(subAgentReview·subAgentCompact·subAgentOther)는 뺀다.
SOURCE_KINDS = ["cli", "vscode", "exec", "appServer", "subAgentThreadSpawn", "unknown"]
LIST_PAGES_KEEP = 200
SANDBOX = os.environ.get("CODEX_LENS_SANDBOX", "danger-full-access")
MENTION_MARK = "[codex-lens · 멘션 맥락]"
LIVE_KEEP_SEC = 60
# 다른 프로세스(Codex 앱·CLI·다른 도구)에서 돌고 있는 턴을 이 app-server 가 읽으면 status 가 'interrupted' 이고
# completedAt 이 비어 있다(사용자가 멈춘 턴은 completedAt 이 있다). 프로세스가 죽어도 똑같이 보이므로
# 기록(updatedAt)이 이 시간 안에 바뀐 세션만 진행 중으로, 지나면 '끝나지 않음'으로 본다.
EXTERNAL_ACTIVE_SEC = 600
# 다른 곳에서 도는 턴 때문에 대기열에 둔 메시지를 서버가 확인하는 간격(초)과, 끝난 것으로 보기 위한 연속 확인 횟수.
EXTERNAL_WATCH_SEC = float(os.environ.get("CODEX_LENS_EXTERNAL_WATCH_SEC") or 3)
EXTERNAL_CLEAR_CHECKS = 2
# 목록에 마지막 턴 상태(멈춤·실패·끝나지 않음)를 보일 세션 — 이보다 오래 바뀌지 않은 세션은 묻지 않는다(완료로 본다).
LIST_STATE_SEC = 86400
FILE_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".gradle", "build", "dist", ".next", ".cache",
                  "target", ".idea", ".mypy_cache", ".pytest_cache"}
FILE_LIMIT = 20000
APPROVE = {
    "item/commandExecution/requestApproval": {"decision": "accept"},
    "item/fileChange/requestApproval": {"decision": "accept"},
    "execCommandApproval": {"decision": "approved"},
    "applyPatchApproval": {"decision": "approved"},
}
DECLINE = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "execCommandApproval": {"decision": "denied"},
    "applyPatchApproval": {"decision": "denied"},
}


class Busy(Exception):
    pass


class Bus:
    """세션(스레드)별 구독자에게 이벤트를 나눠 준다(SSE)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.subs = {}

    def subscribe(self, key):
        q = queue.Queue(1000)
        with self.lock:
            self.subs.setdefault(key, set()).add(q)
        return q

    def unsubscribe(self, key, q):
        with self.lock:
            s = self.subs.get(key)
            if s:
                s.discard(q)
                if not s:
                    self.subs.pop(key, None)

    def publish(self, key, event):
        with self.lock:
            targets = list(self.subs.get(key, ()))
        for q in targets:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass


class LiveTurn:
    """이 앱의 app-server 에서 돌고 있는 턴 — 알림으로 항목을 모은다."""

    def __init__(self, thread_id, rate_fn=None):
        self.thread_id = thread_id
        # Codex: 기록 파일에서 응답별 실측 속도를 읽는 함수(turn_id → 요약). 있으면 수신량 추정은 쓰지 않는다.
        self.rate_fn = rate_fn
        self.turn_id = None
        self.status = "starting"
        self.items = OrderedDict()
        self.item_started = {}   # 명령 항목 ID → 시작 시각(세션을 띄운 명령 찾기)
        self.started_at = time.time()
        self.last_signal = time.time()
        self.finished_at = None
        self.error = None
        self.dirty = True
        self.speed = StreamSpeed()

    def item(self, iid, default_type):
        it = self.items.get(iid)
        if it is None:
            it = {"type": default_type, "id": iid}
            self.items[iid] = it
        return it

    def snapshot(self):
        turn = {"id": self.turn_id, "status": "inProgress" if self.status in ("starting", "inProgress") else self.status,
                "items": list(self.items.values()), "startedAt": int(self.started_at),
                "error": self.error}
        out = normalize_turn(turn)
        out["live"] = {"since": self.started_at, "last_signal": self.last_signal, "status": self.status}
        if self.rate_fn:
            rate = self.rate_fn(self.turn_id) if self.turn_id else None
            if rate:
                out["rate"] = rate
        elif self.status in ("starting", "inProgress"):
            out["live"]["speed"] = self.speed.snapshot(_now())
        return out


def unfinished(raw_turn):
    st = raw_turn.get("status")
    return st == "inProgress" or (st == "interrupted" and not raw_turn.get("completedAt"))


def turn_state(raw_turn, updated, now):
    """기록의 마지막 턴 → 목록 상태. None 이면 표시 없음(완료)."""
    if not raw_turn:
        return None
    if raw_turn.get("cut"):
        return {"kind": "unfinished"}
    if raw_turn.get("alive"):   # Claude 프로세스가 살아 있다 — 오래 조용해도 돌고 있다
        return {"kind": "running", "external": True, "alive": True,
                "last_signal": max(updated or 0, raw_turn.get("signal") or 0)}
    if unfinished(raw_turn):
        quiet = raw_turn.get("quietUntil")
        updated = max(updated or 0, raw_turn.get("signal") or 0)
        if now - max(updated, quiet or 0) <= EXTERNAL_ACTIVE_SEC:
            return {"kind": "running", "external": True, "last_signal": updated, "quiet_until": quiet}
        return {"kind": "unfinished"}
    st = raw_turn.get("status")
    if st in ("interrupted", "failed"):
        return {"kind": st}
    return None


def _now():
    return time.time()


# 속도(serviceTier) — 모델이 내놓는 등급(Fast = "priority") 말고 표준 속도를 고를 때 쓰는 값.
# turn/start 의 serviceTier 는 이 턴과 이후 턴에 남는다(Codex 0.165 스키마).
STANDARD_TIER = "default"
AGENT_IDS = ("codex", "claude")
DEFAULT_KEYS = ("model", "effort", "tier")
# 입력창의 "최근" 칩 — 에이전트마다 보낼 때 쓴 {model, effort, tier} 를 최근 것부터 이만큼 남긴다(기기 공통)
RECENT_MAX = 6
DEFAULT_SETTINGS = {"hidden": [], "defaults": {}, "fork_efforts": {}}
# 포크 작업(과정 요약 · 인라인 질문)의 기본 effort — 모델 관리에서 에이전트마다 정하고, 정하지 않으면 환경변수(기본 low).
FORK_KINDS = ("summary", "question")
FORK_EFFORT_ENV = {"summary": os.environ.get("CODEX_LENS_SUMMARY_EFFORT", "low"),
                   "question": os.environ.get("CODEX_LENS_QUESTION_EFFORT", "low")}
# effort 의 세기 순서 — 모델이 받지 않는 값을 가장 가까운 값으로 맞출 때 쓴다(같은 거리면 약한 쪽)
EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def fit_effort(effort, efforts):
    """모델이 받는 effort 목록(efforts) 안에서 effort 와 가장 가까운 값. 목록이 비었거나 이미 들어 있으면 그대로."""
    if not effort or not efforts or effort in efforts:
        return effort
    rank = {e: i for i, e in enumerate(EFFORT_ORDER)}
    if effort not in rank:
        return effort
    known = [e for e in efforts if e in rank]
    if not known:
        return effort
    return min(known, key=lambda e: (abs(rank[e] - rank[effort]), rank[e]))


def _history_not_ready(error):
    message = str(error).lower()
    return ("rollout" in message and "is empty" in message) or "not materialized yet" in message


class Lens:
    def __init__(self, home, version, demo=False, codex_env=None, codex_argv=None, claude_env=None, claude_argv=None,
                 claude_mine_only=False):
        self.home = home
        self.launches = Launches(os.path.join(home, "launches.json"))   # 이 앱에서 돈 명령이 띄운 세션
        self.list_pages = OrderedDict()   # 목록 다음 쪽 커서 → (안쪽 커서, 이미 낸 세션 ID)
        self.parent_misses = {}           # 읽지 못한 부모 ID → 시각(잠깐 다시 읽지 않는다)
        self.version = version
        self.demo = demo
        os.makedirs(os.path.join(home, "logs"), exist_ok=True)
        self.registry = JsonStore(os.path.join(home, "registry.json"), {"mine": {}, "forks": {}, "archived": {}})
        self.contexts = JsonStore(os.path.join(home, "context.json"), {"threads": {}})
        # app-server 는 중계기가 쥐고 있어 앱을 다시 띄워도 돌던 턴이 이어진다(codex_broker.py)
        codex = CodexClient(argv=codex_argv, env=codex_env, version=version,
                            stderr_path=os.path.join(home, "logs", "codex-stderr.log"),
                            socket_path=codex_broker.socket_path(home),
                            broker_log=os.path.join(home, "logs", "broker.jsonl"))
        # claude_mine_only: 데모가 Claude 설정 폴더를 운영과 같이 쓸 때 — 이 앱(데모)이 만든 세션만 목록에 보인다
        mine = lambda tid: tid in self.registry.read()["mine"]  # noqa: E731
        claude = ClaudeClient(argv=claude_argv, env=claude_env, list_filter=mine if claude_mine_only else None,
                              stderr_path=os.path.join(home, "logs", "claude-stderr.log"),
                              instructions=lambda tid: self.dev_instructions if mine(tid) else None)
        # 세션마다 Codex 또는 Claude Code 로 보낸다 — 아래 코드는 Codex app-server 의 메서드·알림 모양만 쓴다
        self.client = AgentRouter(codex, claude)
        self.client.listeners.append(self._on_note)
        # 검색용 Codex 목록 사본 — app-server 의 searchTerm 대신 쓴다(session_search.py)
        self._search_warmed = False
        self.codex_index = CodexIndex(lambda p: self.client.codex.request("thread/list", p, 60), SOURCE_KINDS)
        self.client.request_handlers.append(self._on_server_request)
        self.bus = Bus()
        self.comments = JsonStore(os.path.join(home, "comments.json"), {"items": []})
        self.md_comments = JsonStore(os.path.join(home, "md-comments.json"), {"items": []})
        # "턴 끝나고" 보낼 메시지 — 스레드별 목록. 앱을 다시 띄워도 남는다(그때는 화면에서 직접 보낸다).
        self.queues = JsonStore(os.path.join(home, "queue.json"), {"threads": {}})
        # 모델 관리 — 새 세션 기본값(모델 · effort · 속도)과 고르기 목록에서 숨길 모델
        self.settings = JsonStore(os.path.join(home, "settings.json"), dict(DEFAULT_SETTINGS))
        self.recents = JsonStore(os.path.join(home, "recent-picks.json"), {})
        # 입력창 다듬기의 목적별 다듬기 지시 — 사용자가 고친 것만
        self.refine_prompts = refine.PromptStore(os.path.join(home, "refine-prompts.json"))
        self.lock = threading.RLock()
        self.live = {}           # thread_id -> LiveTurn
        self.client_ids = {}     # clientUserMessageId -> {"thread", "turn"}
        self.meta_cache = {}     # thread_id -> 세션 메타(모델·cwd)
        self.rollout_rates = RolloutRates()   # Codex 기록 파일 → 응답별 실측 생성 속도(codex_speed.py)
        self.state_cache = {}    # thread_id -> (updatedAt, 마지막 턴 기록) — 목록 상태
        self.starting_threads = {}  # 첫 기록이 쓰이기 전 thread/start 응답과 읽기 대기 여부
        self.models_cache = (0, None)
        self.files_cache = {}
        self.dev_instructions = None  # 세션에 줄 개발자 지침(포트 관리) — App 이 채운다. Claude 는 이 앱이 만든 세션에만 시스템 지침으로 덧붙인다
        self.turn_listeners = []  # fn(thread_id, turn_id, status, error) — 이 앱에서 돌린 턴이 끝날 때
        self.jobs = JobManager(self)
        self.stop = threading.Event()
        self.ext_wait = {}       # thread_id -> 다른 곳의 턴이 끝나기를 기다리는 대기열의 연속 '끝남' 확인 횟수
        self.draining = set()    # 대기열을 잇는 중인 세션 — 같은 세션에서 두 번 돌지 않게
        threading.Thread(target=self._flusher, daemon=True, name="live-flusher").start()
        threading.Thread(target=self._external_watcher, daemon=True, name="external-queue-watch").start()
        threading.Thread(target=self._reattach, daemon=True, name="codex-reattach").start()

    def _reattach(self):
        """앱을 다시 띄웠을 때 남아 있는 중계기에 바로 붙는다 — 끊긴 동안 끝난 턴의 알림(대기열 잇기·문서 다듬기
        마무리)을 화면을 열기 전에도 받는다. 중계기가 없으면 첫 요청 때 띄운다."""
        if not (self.client.socket_path and os.path.exists(self.client.socket_path)):
            return
        try:
            self.client.ensure()
        except Exception as e:
            applog.log("codex_reattach_failed", level="warn", error=repr(e)[:300])

    # --- 알림 ---------------------------------------------------------
    def _thread_of(self, params):
        return params.get("threadId") or (params.get("thread") or {}).get("id")

    def _on_note(self, method, params):
        if method == "_process/exited":   # Codex app-server 가 죽었다 — Claude 턴은 저마다 프로세스라 상관없다
            with self.lock:
                for lt in self.live.values():
                    if lt.status in ("starting", "inProgress") and self.client.agent_of(lt.thread_id) == "codex":
                        lt.status = "failed"
                        lt.error = "codex app-server 가 종료됐습니다"
                        lt.finished_at = _now()
                        lt.dirty = True
            self.jobs.on_process_exit()
            return
        tid = self._thread_of(params)
        if not tid:
            return
        if self.jobs.owns(tid):
            self.jobs.on_note(tid, method, params)
            return
        # resume 직후처럼 실행 중 턴이 없어도 세션의 사용량을 받는다.
        if method in ("thread/tokenUsage/updated", "thread/context/updated"):
            # Claude 의 tokenUsage.last 는 턴 누적(포크 비용용)이다. 별도 호출 단위 알림만 쓴다.
            if method == "thread/tokenUsage/updated" and self.client.agent_of(tid) == "claude":
                return
            ctx = context_usage(params.get("tokenUsage") or {})
            if ctx is not None:
                with self.lock:
                    lt = self.live.get(tid)
                    if lt:
                        lt.last_signal = _now()
                        lt.dirty = True
                    if self.contexts.read()["threads"].get(tid) == ctx:
                        return
                    self.contexts.update(lambda d: d["threads"].__setitem__(tid, ctx))
                    if tid in self.meta_cache:
                        self.meta_cache[tid]["context"] = ctx
                self.bus.publish(tid, {"type": "context", "context": ctx})
            return
        with self.lock:
            lt = self.live.get(tid)
            if lt is None:
                if method != "turn/started":
                    return
                lt = self.live[tid] = self._live_turn(tid)
            self._apply(lt, method, params)

    def _apply(self, lt, method, params):
        lt.last_signal = _now()
        lt.dirty = True
        if method == "turn/started":
            turn = params.get("turn") or {}
            if lt.turn_id and turn.get("id") and turn["id"] != lt.turn_id and lt.status not in ("starting", "inProgress"):
                # 같은 스레드의 다음 턴 — 새로 모은다
                lt.items.clear()
                lt.started_at = _now()
                lt.speed.clear()
            lt.turn_id = turn.get("id") or lt.turn_id
            lt.status = "inProgress"
        elif method in ("item/started", "item/completed", "item/updated"):
            item = params.get("item") or {}
            iid = item.get("id")
            if not iid:
                return
            if item.get("type") == "userMessage" and iid not in lt.items:
                # 보낸 글의 자리표시(턴 요청 _req, 끼워 넣은 말 _steer:<client id>)를 그 자리에서 진짜 항목으로 바꾼다.
                # 턴 요청의 항목이 오기 전에 끼워 넣은 말이 먼저 받아질 수 있어 client id 로 짝을 찾고, 순서를 지킨다.
                cid = item.get("clientId")
                key = next((k for k, v in lt.items.items() if k.startswith("_") and cid and v.get("clientId") == cid), None)
                if key is None:
                    key = "_req" if "_req" in lt.items else next((k for k in lt.items if k.startswith("_steer:")), None)
                if key:
                    lt.items = OrderedDict((iid, item) if k == key else (k, v) for k, v in lt.items.items())
            prev = lt.items.get(iid)
            if item.get("type") == "commandExecution":
                if method == "item/started":
                    lt.item_started.setdefault(iid, _now())
                else:
                    self._observe_launch(lt, item, lt.item_started.pop(iid, lt.started_at))
            if prev and method == "item/started":
                return
            if method == "item/updated" and not prev:
                return                 # 진행 중 항목의 덧붙임(하위 에이전트 소식) — 없는 항목은 만들지 않는다
            # 도구·대기 구간과 완성본 수신은 텍스트 생성 속도에 섞지 않는다.
            lt.speed.clear()
            # 완성본이 오면 바꿔 끼우되 스트리밍으로 모은 글이 더 길면 남긴다(완성본에 글이 빠진 경우)
            if prev and prev.get("text") and not item.get("text"):
                item = dict(item, text=prev["text"])
            lt.items[iid] = item
        elif method == "item/agentMessage/delta":
            lt.speed.add((method, params.get("itemId")), params.get("delta") or "", _now())
            it = lt.item(params.get("itemId"), "agentMessage")
            it["text"] = (it.get("text") or "") + (params.get("delta") or "")
        elif method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            lt.speed.add((method, params.get("itemId"), params.get("summaryIndex", params.get("contentIndex", 0))),
                         params.get("delta") or "", _now())
            it = lt.item(params.get("itemId"), "reasoning")
            key = "summary" if method.endswith("summaryTextDelta") else "content"
            arr = it.setdefault(key, [])
            idx = params.get("summaryIndex", params.get("contentIndex", 0)) or 0
            while len(arr) <= idx:
                arr.append("")
            arr[idx] += params.get("delta") or ""
        elif method == "item/reasoning/summaryPartAdded":
            it = lt.item(params.get("itemId"), "reasoning")
            it.setdefault("summary", []).append("")
        elif method == "item/commandExecution/outputDelta":
            lt.speed.clear()
            it = lt.item(params.get("itemId"), "commandExecution")
            out = (it.get("aggregatedOutput") or "") + (params.get("delta") or "")
            it["aggregatedOutput"] = out[-20000:]
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            lt.status = turn.get("status") or "completed"
            err = turn.get("error")
            lt.error = (err.get("message") if isinstance(err, dict) else err) or None
            lt.finished_at = _now()
            applog.log("turn_end", thread=lt.thread_id, turn=lt.turn_id, status=lt.status,
                       ms=int((lt.finished_at - lt.started_at) * 1000))
            self.bus.publish(lt.thread_id, {"type": "live", "turn": lt.snapshot()})
            self.bus.publish(lt.thread_id, {"type": "turn_done", "turn_id": lt.turn_id, "status": lt.status})
            self.bus.publish("*", {"type": "sessions"})
            lt.dirty = False
            for fn in self.turn_listeners:
                threading.Thread(target=fn, args=(lt.thread_id, lt.turn_id, lt.status, lt.error), daemon=True).start()
            # 끝까지 마친 턴만 대기열을 잇는다 — 멈춤·실패 뒤에는 기다리는 메시지를 사용자가 직접 보내거나 뺀다.
            # 보내기는 RPC 응답을 기다려야 하니 알림을 읽는 스레드 밖에서 한다.
            if lt.status == "completed" and self.queue_of(lt.thread_id):
                threading.Thread(target=self._drain, args=(lt.thread_id,), daemon=True, name="queue-drain").start()
        elif method == "error":
            lt.error = (params.get("error") or {}).get("message") or lt.error

    def _observe_launch(self, lt, item, start):
        """끝난 명령의 출력에 그 명령이 도는 동안 생긴 세션 ID 가 있으면 이 세션이 띄운 것이다(session_tree.Launches)."""
        try:
            n = self.launches.observe(lt.thread_id, "%s\n%s" % (item.get("command") or "", item.get("aggregatedOutput") or ""), start, _now())
            if n:
                applog.log("launch_observed", thread=lt.thread_id, ids=n)
        except Exception as e:   # noqa: BLE001 — 기록 실패가 턴 표시를 막지 않게
            applog.log("launch_observe_failed", level="warn", thread=lt.thread_id, error=repr(e)[:200])

    def _flusher(self):
        while not self.stop.wait(0.25):
            now = _now()
            with self.lock:
                out = []
                for tid, lt in list(self.live.items()):
                    if lt.dirty:
                        lt.dirty = False
                        out.append((tid, lt.snapshot()))
                    if lt.finished_at and now - lt.finished_at > LIVE_KEEP_SEC:
                        self.live.pop(tid, None)
                        self.starting_threads.pop(tid, None)
            for tid, snap in out:
                self.bus.publish(tid, {"type": "live", "turn": snap})
            self.jobs.flush()

    def _on_server_request(self, method, params):
        tid = self._thread_of(params)
        if tid and self.jobs.owns(tid):
            return self.jobs.on_server_request(tid, method, params)
        if method in APPROVE:
            return APPROVE[method]
        if method == "item/permissions/requestApproval":
            return {"permissions": params.get("permissions") or {}, "scope": "turn"}
        if method == "item/tool/requestUserInput":
            return {"answers": {}}
        return None

    # --- 생성 속도(Codex 실측) -------------------------------------------
    def _live_turn(self, tid):
        if self.client.agent_of(tid) == "claude":
            return LiveTurn(tid)
        return LiveTurn(tid, rate_fn=lambda turn_id: self._turn_rate(tid, turn_id))

    def _turn_rate(self, tid, turn_id, path=None):
        """Codex 턴의 응답별 실측 속도 요약 — 기록 파일(rollout)의 실제 출력 토큰과 응답 시각으로 센다."""
        if not path:
            meta = self.meta_cache.get(tid) or {}
            path = meta.get("path") or ((self.starting_threads.get(tid) or {}).get("thread") or {}).get("path")
        try:
            return summarize_rates(self.rollout_rates.turn(path, turn_id))
        except Exception as e:   # noqa: BLE001 — 속도 표시가 턴 표시를 막지 않게
            applog.log("turn_rate_failed", level="warn", thread=tid, error=repr(e)[:200])
            return None

    # --- 세션 목록 ------------------------------------------------------
    def _meta(self, th):
        registry = self.registry.read()
        mine = registry["mine"]
        preview = (th.get("preview") or "").strip()
        title = session_title(th) or subagent_title(th) or "(빈 세션)"
        status = (th.get("status") or {}).get("type") if isinstance(th.get("status"), dict) else th.get("status")
        src = th.get("source")
        if isinstance(src, dict):
            src = next(iter(src.keys()), "unknown")
        meta = {
            "id": th.get("id"), "title": title, "preview": preview[:300], "cwd": th.get("cwd"),
            "model": th.get("model"), "effort": th.get("reasoningEffort"),
            "created_at": th.get("createdAt"), "updated_at": th.get("updatedAt"), "source": src,
            "status": status, "forked_from": th.get("forkedFromId"), "path": th.get("path"),
            "cli_version": th.get("cliVersion"), "branch": (th.get("gitInfo") or {}).get("branch"),
            "archived": th.get("id") in registry.get("archived", {}),
            "mine": th.get("id") in mine, "agent": th.get("agent") or "codex",
            "context": self.contexts.read()["threads"].get(th.get("id")),
        }
        if th.get("readOnly"):
            meta["readonly"] = True     # Claude 하위 에이전트 기록 — 이어 시키기·포크는 부모 세션에서
        parent, nick = thread_parent(th)
        parent = parent or self._launched_by(th)
        if parent and parent != th.get("id"):
            meta["parent"] = parent
            if nick:
                meta["agent_name"] = nick
        wt = (mine.get(th.get("id")) or {}).get("worktree")
        if wt:
            meta["worktree"] = {"branch": wt.get("branch"), "repo": wt.get("repo"), "origin_cwd": wt.get("origin_cwd")}
        with self.lock:
            lt = self.live.get(meta["id"])
            if lt and lt.status in ("starting", "inProgress"):
                meta["status"] = "running"
        return meta

    def _launched_by(self, th):
        tid, created = th.get("id"), th.get("createdAt")
        return self.launches.parent_of(tid, created)

    def hidden_ids(self):
        return set(self.registry.read()["forks"].keys())

    def set_archived(self, tid, archived):
        """이 앱의 목록에서만 보관한다. Codex 기록·실행·대기열은 그대로 둔다."""
        t0 = _now()
        if type(archived) is not bool:
            raise ValueError("archived 는 true 또는 false 여야 합니다")
        if tid in self.hidden_ids():
            raise KeyError(tid)
        # 없는 세션을 보관 목록에 만들지 않는다. 캐시 대신 실제 존재 여부를 확인한다.
        try:
            th = self.client.request("thread/read", {"threadId": tid, "includeTurns": False}, 60).get("thread") or {}
        except RpcError as e:
            if not _history_not_ready(e):
                raise
            # 첫 기록이 쓰이기 전의 진행 중 세션도 읽기 화면과 같은 초기 메타로 확인한다.
            th, _, _ = self._raw_turns(tid)
        if th.get("id") != tid:
            raise KeyError(tid)
        def update(d):
            entries = d.setdefault("archived", {})
            if archived:
                entries.setdefault(tid, _now())
            else:
                entries.pop(tid, None)
        with self.registry.lock:
            previous = dict(self.registry.read().get("archived", {}))
            try:
                self.registry.update(update)
            except Exception:
                # 디스크 저장이 실패했으면 메모리에서도 보관 전 상태를 유지한다.
                self.registry.read()["archived"] = previous
                raise
        meta = self._meta(th)
        self.meta_cache[tid] = meta
        applog.log("session_archive", thread=tid, archived=archived, ms=int((_now() - t0) * 1000))
        self.bus.publish(tid, {"type": "session_changed"})
        return {"session": meta}

    def list_sessions(self, cursor=None, limit=40, search=None, cwd=None, archived=False):
        """세션 목록 한 쪽. 세션 안에서 만든 세션은 부모의 `children` 으로 묶는다(session_tree.group).
        다음 쪽 커서는 이 앱이 내는 토큰이다 — 앞 쪽에서 낸 세션(끌어올린 부모 포함)을 다시 내지 않으려고
        안쪽 커서와 함께 낸 ID 를 서버 메모리에 둔다(앱을 다시 띄우면 처음 쪽부터)."""
        with self.lock:
            st = self.list_pages.get(cursor) if cursor else None
        inner, seen = (st["inner"], set(st["seen"])) if st else (cursor, set())
        if terms(search):
            res = self.search_sessions(search, cursor=inner, limit=limit, cwd=cwd, archived=archived)
        else:
            res = self._list_page(inner, limit, search, cwd, archived)
        items = group(res["sessions"], seen, lambda p: self._parent_meta(p, archived, rows=True))
        nxt = None
        if res["next_cursor"]:
            nxt = "p" + uuid.uuid4().hex
            with self.lock:
                self.list_pages[nxt] = {"inner": res["next_cursor"], "seen": seen}
                while len(self.list_pages) > LIST_PAGES_KEEP:
                    self.list_pages.popitem(last=False)
        return {"sessions": items, "next_cursor": nxt}

    def _parent_meta(self, tid, archived=None, rows=False):
        """끌어올릴 부모 세션의 메타. 숨긴(포크) 세션·못 읽는 세션은 None.
        archived 가 False 면 보관한 부모는 끌어올리지 않고(보관 목록이 아닌 쪽), True 면 보관한 부모만 끌어올린다.
        None 이면 보관 여부를 보지 않는다(세션 머리의 부모 링크). rows 면 목록 행처럼 상태(`state`)를 붙인 사본을 준다."""
        if tid in self.hidden_ids():
            return None
        if archived is not None and (tid in self.registry.read().get("archived", {})) != bool(archived):
            return None
        m = self.meta_cache.get(tid)
        if not m:
            miss = self.parent_misses.get(tid)
            if miss and time.monotonic() - miss < 60:
                return None
            try:
                res = self.client.request("thread/read", {"threadId": tid, "includeTurns": False}, 20)
            except (RpcError, OSError) as e:
                self.parent_misses[tid] = time.monotonic()
                applog.log("parent_read_failed", level="warn", thread=tid, error=str(e)[:200])
                return None
            th = res.get("thread") or {}
            if not th.get("id"):
                return None
            m = self._meta(th)
            self.meta_cache[tid] = m
        if rows:
            m = {k: v for k, v in m.items() if k not in ("children", "parent_title")}
            self._annotate([m])
        return m

    def _list_page(self, cursor, limit, search, cwd, archived=False):
        self._warm_search()
        forks = self.hidden_ids()
        archived_ids = set(self.registry.read().get("archived", {}))
        if archived and not archived_ids:
            return {"sessions": [], "next_cursor": None}
        hidden = forks | (set() if archived else archived_ids)
        recent_hidden = sorted(hidden, reverse=True)[:100]
        params = {"limit": max(1, min(int(limit or 40), 100)), "sourceKinds": SOURCE_KINDS, "sortKey": "updated_at"}
        if recent_hidden:
            params["excludedThreadIds"] = recent_hidden
        if cursor:
            params["cursor"] = cursor
        if search:
            params["searchTerm"] = search
        if cwd:
            params["cwd"] = cwd
        # 제외 목록은 100개까지만 보낼 수 있다. 숨긴 항목으로 한 쪽이 비어도 다음 쪽을 읽는다.
        items, seen = [], set()
        target = params["limit"]
        while True:
            params["limit"] = target - len(items)
            try:
                res = self.client.request("thread/list", params, 60)
            except RpcError as e:
                if "excludedThreadIds" in params and "exclud" in str(e).lower():
                    params.pop("excludedThreadIds")
                    res = self.client.request("thread/list", params, 60)
                else:
                    raise
            self.codex_index.remember(th for th in res.get("data") or [] if th.get("agent") != "claude")
            items.extend(self._meta(th) for th in res.get("data") or []
                         if th.get("id") not in hidden and (not archived or th.get("id") in archived_ids))
            cursor = res.get("nextCursor")
            if not cursor or len(items) >= target or cursor in seen:
                break
            seen.add(cursor)
            params["cursor"] = cursor
        self._annotate(items)
        return {"sessions": items, "next_cursor": cursor}

    def _annotate(self, items):
        """목록 행마다 상태(`state`)를 붙인다 — 실행 중(이 앱·밖)·멈춤·실패·끝나지 않음·시스템 오류. 없으면 완료."""
        t0 = _now()

        def one(m):
            try:
                return self._list_state(m)
            except Exception as e:   # noqa: BLE001 — 한 행을 못 읽어도 목록은 보인다
                applog.log("list_state_failed", level="warn", thread=m["id"], error=repr(e)[:200])
                return None, False
        for m in items:
            self.meta_cache[m["id"]] = m
        # 처음 묻는 행은 기록을 읽어야 해서(Claude 는 파일 전체) 나눠 읽는다 — 다음부터는 updatedAt 캐시
        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(one, items))
        asked = 0
        for m, (state, q) in zip(items, results):
            m["state"] = state
            asked += q
            if state and state["kind"] == "running":
                m["status"] = "running"
        if asked:
            applog.log("list_state", asked=asked, rows=len(items), ms=int((_now() - t0) * 1000))

    def _list_state(self, m):
        """(상태, 기록을 물었는지)."""
        tid, upd = m["id"], m.get("updated_at") or 0
        with self.lock:
            lt = self.live.get(tid)
            if lt and lt.status in ("starting", "inProgress"):
                return {"kind": "running", "last_signal": lt.last_signal,
                        "quiet_until": quiet_until(list(lt.items.values()))}, False
        if m.get("status") == "systemError":
            return {"kind": "error"}, False
        if m.get("status") == "active":   # 다른 클라이언트가 이 app-server 로 돌리는 중
            return {"kind": "running", "last_signal": upd}, False
        now = _now()
        if now - upd > LIST_STATE_SEC:
            return None, False
        hit = self.state_cache.get(tid)
        asked = False
        # Claude 의 '진행 중'은 기록이 그대로여도 시간이 지나면 끝남으로 바뀐다 — 끝나지 않은 턴은 다시 묻는다(읽기는 캐시된다)
        if hit and hit[0] == upd and not (m.get("agent") == "claude" and unfinished(hit[1])):
            last = hit[1]
        else:
            last, asked = self._last_raw_turn(tid, m.get("agent")), True
            self.state_cache[tid] = (upd, last)
            if len(self.state_cache) > 2000:
                self.state_cache.pop(next(iter(self.state_cache)))
        return turn_state(last, upd, now), asked

    def _last_raw_turn(self, tid, agent):
        if agent == "claude":
            # Claude 기록은 파일을 읽어 턴을 만든다(파일 mtime 으로 캐시) — 다른 곳에서 도는 턴은 inProgress 로 온다
            th = self.client.request("thread/read", {"threadId": tid, "includeTurns": True}, 30).get("thread") or {}
            turns = th.get("turns") or []
        else:
            th = {}
            turns = self.client.request("thread/turns/list", {"threadId": tid, "limit": 1, "sortDirection": "desc",
                                                              "itemsView": "notLoaded"}, 20).get("data") or []
            turns = turns[:1]
        if not turns:
            return None
        t = turns[-1]
        out = {k: t.get(k) for k in ("id", "status", "completedAt")}
        out["quietUntil"] = quiet_until(t.get("items")) if t.get("status") == "inProgress" else None
        if agent == "claude" and out["status"] == "interrupted" and not out["completedAt"]:
            out["cut"] = True   # Claude 기록 읽기가 이미 '끝 기록 없이 끊김'으로 판정했다(진행 중은 inProgress 로 온다)
        if agent == "claude" and out["status"] == "inProgress" and t.get("alive"):
            out["alive"] = True
        if agent == "claude" and th.get("updatedAt"):
            out["signal"] = th["updatedAt"]   # 하위 에이전트 기록까지 본 마지막 활동(목록 updatedAt 은 본 기록만 본다)
        return out

    def _external_busy(self, tid):
        """화면·목록에서 '다른 곳에서 진행 중'으로 본 세션인지 — 그동안은 이 앱에서 새 턴을 열지 않는다."""
        st = (self.meta_cache.get(tid) or {}).get("state") or {}
        return bool(st.get("external")) and (bool(st.get("alive")) or
                                             _now() - max(st.get("last_signal") or 0, st.get("quiet_until") or 0) <= EXTERNAL_ACTIVE_SEC)

    def _warm_search(self):
        """처음 목록을 열 때 검색에 쓸 목록을 뒤에서 미리 읽는다 — Codex 는 끝까지(1300여 개에 9초 남짓),
        Claude 는 기록마다 메타를 한 번(1400여 개에 3초 남짓). 첫 검색이 그만큼 기다리지 않게."""
        if self._search_warmed:
            return
        self._search_warmed = True
        self.codex_index.warm()

        def claude():
            try:
                for _ in self.client.claude.iter_threads(None):
                    pass
            except Exception as e:   # noqa: BLE001
                applog.log("search_warm_failed", level="warn", agent="claude", error=repr(e)[:300])
        threading.Thread(target=claude, daemon=True, name="claude-index").start()

    def search_sessions(self, search, cursor=None, limit=40, cwd=None, archived=False):
        """제목·폴더 이름에 낱말이 모두 든 세션을 갱신 시각 순으로. 다음 쪽 커서는 `q<건너뛸 개수>`."""
        t0 = time.monotonic()
        words = terms(search)
        limit = max(1, min(int(limit or 40), 100))
        try:
            skip = int(cursor[1:]) if cursor and cursor.startswith("q") else 0
        except ValueError:
            skip = 0
        archived_ids = set(self.registry.read().get("archived", {}))
        hidden = self.hidden_ids() | (set() if archived else archived_ids)
        found, errors = [], []
        try:
            found.extend(th for th in self.codex_index.snapshot() if matches(th, words))
        except Exception as e:   # noqa: BLE001 — Codex 쪽이 안 돼도 Claude 결과는 보여 준다
            errors.append(str(e) or repr(e))
            applog.log("search_backend_failed", level="warn", agent="codex", error=errors[-1][:300])
        try:
            found.extend(th for th in self.client.claude.iter_threads(None, cwd) if matches(th, words))
        except Exception as e:   # noqa: BLE001
            errors.append(str(e) or repr(e))
            applog.log("search_backend_failed", level="warn", agent="claude", error=errors[-1][:300])
        found = [th for th in found if th.get("id") not in hidden and (not archived or th.get("id") in archived_ids)
                 and (not cwd or th.get("cwd") == cwd)]
        if not found and errors:
            raise RpcError(errors[0])
        found.sort(key=lambda th: th.get("updatedAt") or 0, reverse=True)
        page = found[skip:skip + limit]
        items = [self._meta(th) for th in page]
        self._annotate(items)
        nxt = "q%d" % (skip + limit) if len(found) > skip + limit else None
        applog.log("session_search", words=len(words), count=len(found), ms=int((time.monotonic() - t0) * 1000))
        return {"sessions": items, "next_cursor": nxt}

    def recent_cwds(self):
        try:
            res = self.list_sessions(limit=60)
        except RpcError:
            return []
        seen, out = set(), []
        for s in res["sessions"]:
            # 워크트리에서 돈 세션은 원래 고른 폴더를 최근 폴더로 친다
            c = (s.get("worktree") or {}).get("origin_cwd") or s.get("cwd")
            if c and c not in seen and os.path.isdir(c):
                seen.add(c)
                out.append(c)
        return out[:12]

    # --- 세션 읽기 ------------------------------------------------------
    def _raw_turns(self, tid):
        for attempt in range(4):
            try:
                th, turns = self._stored_turns(tid)
            except RpcError as e:
                if not _history_not_ready(e):
                    raise
                with self.lock:
                    initial = self.starting_threads.get(tid)
                    lt = self.live.get(tid)
                    if initial and lt and lt.turn_id and lt.turn_id == initial.get("turn_id"):
                        if not initial["pending"]:
                            applog.log("session_history_pending", thread=tid, source="live")
                            initial["pending"] = True
                        return initial["thread"], [], True
                # 다른 클라이언트에서 막 만든 세션도 잠깐 기다린다. 영구 오류는 숨기지 않는다.
                if attempt == 3:
                    raise
                time.sleep(0.05 * 2 ** attempt)
            else:
                with self.lock:
                    initial = self.starting_threads.pop(tid, None)
                if initial and initial["pending"]:
                    applog.log("session_history_ready", thread=tid)
                return th, turns, False

    def _stored_turns(self, tid):
        try:
            res = self.client.request("thread/read", {"threadId": tid, "includeTurns": True}, 90)
            th = res.get("thread") or {}
            return th, th.get("turns") or []
        except RpcError as e:
            if _history_not_ready(e):
                raise
            # 전체 기록 읽기를 거절하는 판(paginated 기록)은 턴을 쪽마다 읽는다.
            applog.log("read_fallback", level="warn", thread=tid, error=str(e)[:200])
            res = self.client.request("thread/read", {"threadId": tid, "includeTurns": False}, 60)
            th = res.get("thread") or {}
            turns, cursor = [], None
            for _ in range(200):
                p = {"threadId": tid, "limit": 50, "sortDirection": "asc", "itemsView": "full"}
                if cursor:
                    p["cursor"] = cursor
                page = self.client.request("thread/turns/list", p, 90)
                turns += page.get("data") or []
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            return th, turns

    def read_session(self, tid):
        t0 = _now()
        th, raw, pending = self._raw_turns(tid)
        read_ms = int((_now() - t0) * 1000)   # 에이전트 쪽 기록 읽기(Codex app-server · Claude 기록 파일)
        meta = self._meta(th)
        meta["location"] = worktree.location(meta.get("cwd"))
        self.meta_cache[tid] = meta
        if meta.get("parent"):
            pm = self._parent_meta(meta["parent"])
            meta = dict(meta, parent_title=pm["title"]) if pm else meta
        turns = [normalize_turn(t, i + 1) for i, t in enumerate(raw)]
        # 이 앱에 올라오지 않은 Codex 세션에서 끝 기록이 없는 턴 — 최근이면 다른 곳에서 진행 중, 아니면 끊긴 턴
        foreign = meta["status"] in ("notLoaded", None) and meta.get("agent") != "claude"
        upd = th.get("updatedAt") or 0
        meta["state"] = None
        for i, (r, t) in enumerate(zip(raw, turns)):
            if foreign and unfinished(r):
                if i == len(raw) - 1 and _now() - upd <= EXTERNAL_ACTIVE_SEC:
                    t["status"], t["external"] = "inProgress", {"last_signal": upd}
                else:
                    t["status"] = "unfinished"
            elif meta.get("agent") == "claude" and r.get("status") == "interrupted" and not r.get("completedAt"):
                t["status"] = "unfinished"   # claude_history 가 '끝 기록 없이 끊김'으로 본 턴(프로세스가 꺼짐)
                t["cut"] = r.get("cut")      # process: 프로세스가 꺼짐 · quiet: 5분 넘게 기록 없음
        if turns and turns[-1].get("external"):
            meta["state"] = {"kind": "running", "external": True, "last_signal": upd}
        elif turns and meta.get("agent") == "claude" and turns[-1]["status"] == "inProgress" and not self._running(tid):
            # Claude 기록 읽기가 '다른 곳(CLI)에서 진행 중'으로 본 턴
            t = turns[-1]
            t["external"] = {"last_signal": upd}
            meta["state"] = {"kind": "running", "external": True, "last_signal": upd, "quiet_until": t.get("quiet_until")}
            if raw and raw[-1].get("alive"):
                t["external"]["alive"] = meta["state"]["alive"] = True   # 프로세스가 살아 있음(명부로 확인)
        with self.lock:
            lt = self.live.get(tid)
            snap = lt.snapshot() if lt and lt.turn_id else None
        if snap:
            for i, t in enumerate(turns):
                if t["id"] == snap["id"]:
                    # 기록이 아직 덜 쓰였으면 실행 중 모은 쪽을 쓴다
                    if t["status"] == "inProgress" or len(snap["process"]) > len(t["process"]) or (snap["answer"] and not t["answer"]):
                        snap["index"] = t["index"]
                        turns[i] = snap
                    break
            else:
                snap["index"] = len(turns) + 1
                turns.append(snap)
        if meta.get("agent") != "claude" and meta.get("path"):
            for t in turns:
                if not t.get("rate"):
                    rate = self._turn_rate(tid, t["id"], meta["path"])
                    if rate:
                        t["rate"] = rate
        ms = int((_now() - t0) * 1000)
        applog.log("session_read", thread=tid, turns=len(turns), ms=ms, read_ms=read_ms, agent=meta.get("agent") or "codex")
        # 화면의 읽기 과정이 단계별 시간을 보인다 — 서버 안에서 에이전트 읽기와 정리에 쓴 시간
        return {"session": meta, "turns": turns, "jobs": self.jobs.for_thread(tid),
                "comments": self.list_comments(tid), "queue": self.queue_of(tid), "history_pending": pending,
                "timing": {"read_ms": read_ms, "build_ms": max(0, ms - read_ms)}}

    def session_meta(self, tid):
        m = self.meta_cache.get(tid)
        if m:
            return m
        res = self.client.request("thread/read", {"threadId": tid, "includeTurns": False}, 60)
        m = self._meta(res.get("thread") or {})
        self.meta_cache[tid] = m
        return m

    # --- 실행 -----------------------------------------------------------
    def git_info(self, cwd):
        gi = worktree.info(cwd)
        if not gi:
            return {"git": False}
        return {"git": True, "root": gi["root"], "branch": gi["branch"], "dirty": gi["dirty"], "has_head": bool(gi["head"])}

    def create_session(self, cwd, text, mentions=None, model=None, effort=None, client_id=None, use_worktree=False, images=None,
                       agent=None, tier=None):
        cwd = os.path.abspath(os.path.expanduser((cwd or "").strip() or "~"))
        if not os.path.isdir(cwd):
            raise ValueError("작업 폴더가 없습니다: %s" % cwd)
        images = self.validate_images(images)
        if not (text or "").strip() and not images:
            raise ValueError("첫 메시지를 적어 주세요")
        if client_id and client_id in self.client_ids:
            return self.client_ids[client_id]
        wt = None
        if use_worktree and worktree.info(cwd):
            t0 = _now()
            wt = worktree.create(cwd, os.path.join(self.home, "worktrees"))
            applog.log("worktree_created", path=wt["path"], branch=wt["branch"], repo=wt["repo"],
                       base_branch=wt["base_branch"], dirty=wt["dirty"], ms=int((_now() - t0) * 1000))
            cwd = wt["cwd"]
            # 원래 저장소 안 파일을 멘션했으면 워크트리 안 같은 파일을 가리키게 한다 — 원본을 고치지 않게
            mentions = [dict(m, path=worktree.remap(m.get("path"), wt["repo"], wt["path"]))
                        if m.get("kind") == "file" and m.get("path") else m for m in mentions or []]
        agent = "claude" if agent == "claude" else "codex"
        params = {"cwd": cwd, "approvalPolicy": "never", "sandbox": SANDBOX, "agent": agent}
        model, effort, tier = self.apply_defaults(model, effort, tier, agent)
        if agent == "claude":
            tier = None   # Claude Code 에는 속도 등급이 없다
        if model:
            params["model"] = model
        if tier:
            params["serviceTier"] = tier
        if self.dev_instructions:
            params["developerInstructions"] = self.dev_instructions
        try:
            res = self.client.request("thread/start", params, 120)
        except Exception:
            if wt:
                worktree.remove(wt)
            raise
        th = res.get("thread") or {}
        tid = th["id"]
        self.client.loaded.add(tid)
        entry = {"created_at": _now(), "cwd": cwd, "model": model, "effort": effort, "agent": agent, "tier": tier}
        if wt:
            entry["worktree"] = {k: wt[k] for k in ("path", "branch", "repo", "origin_cwd", "base_branch", "base")}
        self.registry.update(lambda d: d["mine"].__setitem__(tid, entry))
        # thread/read 는 저장 파일을 읽는다. 첫 flush 전에는 파일이 비어 있어도 턴은 이미 실행 중이다.
        initial = dict(th, model=res.get("model") or model or th.get("model"),
                       reasoningEffort=effort or res.get("reasoningEffort") or th.get("reasoningEffort"),
                       preview=text[:300])
        initial_entry = {"thread": initial, "pending": False}
        with self.lock:
            self.starting_threads[tid] = initial_entry
        applog.log("session_created", thread=tid, agent=agent, model=res.get("model") or model, effort=effort, tier=tier,
                   worktree=wt["branch"] if wt else None)
        try:
            out = self.send_turn(tid, text, mentions, model=model, effort=effort, client_id=client_id, images=images, tier=tier)
            with self.lock:
                initial_entry["turn_id"] = out["turn_id"]
        except Exception:
            with self.lock:
                self.starting_threads.pop(tid, None)
            raise
        self.bus.publish("*", {"type": "sessions"})
        return out

    def _running(self, tid):
        lt = self.live.get(tid)
        return lt if lt and lt.status in ("starting", "inProgress") else None

    def send_turn(self, tid, text, mentions=None, model=None, effort=None, client_id=None, mode=None, images=None, tier=None):
        """새 턴을 연다. 응답 중이면 mode 로 가른다 — steer: 진행 중인 턴에 끼워 넣기, queue: 턴이 끝나면 보내기."""
        images = self.validate_images(images)
        if not (text or "").strip() and not images:
            raise ValueError("보낼 내용이 없습니다")
        with self.lock:
            if client_id and client_id in self.client_ids:
                return self.client_ids[client_id]
            running = bool(self._running(tid))
        # 잠금을 쥔 채 RPC 를 기다리면 알림을 읽는 스레드가 막혀 응답도 못 받는다 — 잠금 밖에서 보낸다
        if running:
            if mode == "steer":
                return self.steer(tid, text, mentions, model, effort, client_id, images=images)
            if mode == "queue":
                return self.enqueue(tid, text, mentions, model, effort, client_id, images=images, tier=tier)
            raise Busy("응답하는 중입니다 — 끝나거나 멈춘 뒤 보내세요")
        with self.lock:
            ours = tid in self.live   # 이 앱이 방금 돌린 세션은 이 앱이 상태를 안다 — 기록을 다시 읽어 되묻지 않는다
        if not ours and "state" not in (self.meta_cache.get(tid) or {}):
            # 화면·목록이 아직 이 세션의 상태를 확인하지 않았다(이 앱의 턴 뒤 캐시를 비웠거나 API 로 바로 보냄) — 새로 확인한다
            try:
                self._probe_external(tid)
            except Exception as e:   # noqa: BLE001 — 확인하지 못하면 예전처럼 그대로 보낸다
                applog.log("external_probe_failed", level="warn", thread=tid, error=repr(e)[:300])
        if self._external_busy(tid):
            # 다른 프로세스가 돌리는 턴에는 끼워 넣을 수 없다 — 바로 넣기도 턴이 끝난 뒤로 미룬다(화면은 같은 두 선택지만 보인다)
            if mode in ("steer", "queue"):
                why = "다른 곳에서 진행 중인 턴" + (" — 끼워 넣을 수 없음" if mode == "steer" else "")
                return self.enqueue(tid, text, mentions, model, effort, client_id, images=images, tier=tier, why=why, external=True)
            raise Busy("다른 곳(Codex 앱·CLI 등)에서 진행 중입니다 — 그 턴이 끝난 뒤 보내세요")
        with self.lock:
            if self._running(tid):
                raise Busy("응답하는 중입니다 — 끝나거나 멈춘 뒤 보내세요")
        self.client.ensure_loaded(tid, developerInstructions=self.dev_instructions)
        model = self._model_for(tid, model)
        inputs = self.build_input(text, mentions, tid, images=images)
        with self.lock:
            lt = self._live_turn(tid)
            # 보낸 글을 바로 요청으로 보인다 — 진짜 userMessage 항목이 오면 바꿔 끼운다
            lt.items["_req"] = {"type": "userMessage", "id": "_req", "clientId": client_id, "content": inputs}
            self.live[tid] = lt
        params = {"threadId": tid, "input": inputs}
        if client_id:
            params["clientUserMessageId"] = client_id
        if model:
            params["model"] = model
        if effort:
            params["effort"] = effort
        if tier and self.client.agent_of(tid) != "claude":   # 속도 등급은 Codex 만 받는다
            params["serviceTier"] = tier
        t0 = _now()
        try:
            res = self.client.request("turn/start", params, 120)
        except Exception as e:
            with self.lock:
                if self.live.get(tid) is lt and not lt.turn_id:
                    self.live.pop(tid, None)
            raise e
        turn = res.get("turn") or {}
        with self.lock:
            lt.turn_id = lt.turn_id or turn.get("id")
            if lt.status == "starting":
                lt.status = "inProgress"
            lt.dirty = True
        out = {"id": tid, "turn_id": lt.turn_id}
        if client_id:
            self.client_ids[client_id] = out
        self.meta_cache.pop(tid, None)
        applog.log("turn_start", thread=tid, turn=lt.turn_id, ms=int((_now() - t0) * 1000), model=model, effort=effort, tier=tier,
                   chars=len(text or ""), mentions=len(mentions or []), image_count=len(images or []))
        return out

    def _model_for(self, tid, model):
        """다른 에이전트의 모델이면 넘기지 않는다(예: 문서 다듬기에서 연결된 Claude 세션에 Codex 모델을 고른 경우)."""
        if not model:
            return model
        try:
            known = {m["id"]: m.get("agent") for m in self.models()}
        except RpcError:
            return model
        agent = known.get(model)
        if agent and agent != self.client.agent_of(tid):
            applog.log("model_dropped", level="warn", thread=tid, model=model, model_agent=agent)
            return None
        return model

    # --- 바로 넣기 · 턴 끝나고 ----------------------------------------------
    def steer(self, tid, text, mentions=None, model=None, effort=None, client_id=None, front=False, images=None):
        """진행 중인 턴에 끼워 넣는다(turn/steer). 턴 id 를 아직 모르거나 Codex 가 거절하면 대기열로 보낸다
        (front=True 면 맨 앞 — 대기열에서 꺼낸 메시지를 되돌릴 때)."""
        client_id = client_id or uuid.uuid4().hex
        inputs = self.build_input(text, mentions, tid, images=images)
        key = "_steer:" + client_id
        with self.lock:
            lt = self._running(tid)
            turn_id = lt.turn_id if lt else None
            if not turn_id:
                return self.enqueue(tid, text, mentions, model, effort, client_id, front=front, images=images, why="턴이 막 시작돼 끼워 넣을 수 없음")
            lt.items[key] = {"type": "userMessage", "id": key, "clientId": client_id, "content": inputs}
            lt.dirty = True
        t0 = _now()
        try:
            self.client.request("turn/steer", {"threadId": tid, "input": inputs, "expectedTurnId": turn_id,
                                               "clientUserMessageId": client_id}, 60)
        except RpcError as e:
            with self.lock:
                lt.items.pop(key, None)
                lt.dirty = True
            applog.log("turn_steer_rejected", level="warn", thread=tid, turn=turn_id, error=str(e)[:300])
            out = self.enqueue(tid, text, mentions, model, effort, client_id, front=front, images=images, why="에이전트가 끼워 넣기를 거절함")
            with self.lock:
                idle = not self._running(tid)
            if idle:   # 거절 사이에 턴이 끝났으면 기다릴 턴이 없다 — 바로 다음 턴으로 보낸다
                threading.Thread(target=self._drain, args=(tid,), daemon=True, name="queue-drain").start()
            return out
        out = {"id": tid, "turn_id": turn_id, "steered": True}
        self.client_ids[client_id] = out
        applog.log("turn_steer", thread=tid, turn=turn_id, ms=int((_now() - t0) * 1000), chars=len(text or ""),
                   mentions=len(mentions or []))
        return out

    def queue_of(self, tid):
        return list(self.queues.read()["threads"].get(tid) or [])

    def _queue_changed(self, tid):
        self.bus.publish(tid, {"type": "queue", "queue": self.queue_of(tid)})

    def enqueue(self, tid, text, mentions=None, model=None, effort=None, client_id=None, front=False, why=None, images=None, tier=None,
                external=False):
        entry = {"id": uuid.uuid4().hex[:12], "text": text, "mentions": mentions or [], "model": model, "effort": effort,
                 "tier": tier,
                 "client_id": client_id or uuid.uuid4().hex, "created_at": _now(), "images": self.validate_images(images)}

        def add(d):
            q = d["threads"].setdefault(tid, [])
            q.insert(0, entry) if front else q.append(entry)
        self.queues.update(add)
        out = {"id": tid, "queued": True, "queue_id": entry["id"]}
        self.client_ids[entry["client_id"]] = out
        applog.log("turn_queued", thread=tid, queue=entry["id"], front=front, image_count=len(images or []), why=why, chars=len(text or ""),
                   external=external)
        if external:
            self._watch_external(tid, entry["id"])
        self._queue_changed(tid)
        return out

    def _queue_pop(self, tid, qid=None):
        def pop(d):
            q = d["threads"].get(tid) or []
            for i, e in enumerate(q):
                if qid is None or e["id"] == qid:
                    q.pop(i)
                    if not q:
                        d["threads"].pop(tid, None)
                    return e
            return None
        e = self.queues.update(pop)
        if e:
            self.client_ids.pop(e.get("client_id"), None)   # 이제 실제로 보낸다 — 보낸 결과로 다시 적힌다
            self._queue_changed(tid)
        return e

    def queue_remove(self, tid, qid):
        e = self._queue_pop(tid, qid)
        if not e:
            raise KeyError(qid)
        applog.log("turn_unqueued", thread=tid, queue=qid)
        return {"ok": True, "queue": self.queue_of(tid)}

    def queue_send(self, tid, qid):
        """기다리는 메시지 하나를 지금 보낸다 — 응답 중이면 그 턴에 끼워 넣고, 아니면 새 턴으로."""
        with self.lock:
            running = bool(self._running(tid))
        if not running and self._external_busy(tid):
            # 다른 곳의 턴에는 끼워 넣을 수 없다 — 대기열에 그대로 두고 그 턴이 끝나면 보낸다
            if not any(e["id"] == qid for e in self.queue_of(tid)):
                raise KeyError(qid)
            self._watch_external(tid, qid)
            return {"id": tid, "queued": True, "queue_id": qid}
        e = self._queue_pop(tid, qid)
        if not e:
            raise KeyError(qid)
        return self._send_entry(tid, e, mode="steer")

    def _send_entry(self, tid, e, mode):
        try:
            with self.lock:
                running = bool(self._running(tid))
            if running and mode == "steer":
                return self.steer(tid, e["text"], e.get("mentions"), e.get("model"), e.get("effort"), e.get("client_id"),
                                  front=True, images=e.get("images"))
            return self.send_turn(tid, e["text"], e.get("mentions"), e.get("model"), e.get("effort"), e.get("client_id"), images=e.get("images"),
                                  tier=e.get("tier"))
        except Exception:
            # 못 보냈으면 잃지 않고 제자리(맨 앞)로 돌려놓는다
            self.queues.update(lambda d: d["threads"].setdefault(tid, []).insert(0, e))
            self._queue_changed(tid)
            raise

    def _drain(self, tid):
        with self.lock:
            if tid in self.draining:   # 같은 세션의 대기열을 두 곳에서 동시에 잇지 않는다
                return
            self.draining.add(tid)
        try:
            e = self._queue_pop(tid)
            if not e:
                return
            try:
                out = self._send_entry(tid, e, mode="turn")
                applog.log("turn_dequeued", thread=tid, queue=e["id"], turn=out.get("turn_id"), external=bool(e.get("external")))
            except Exception as ex:
                applog.log("queue_drain_failed", level="warn", thread=tid, queue=e["id"], error=repr(ex)[:300])
        finally:
            with self.lock:
                self.draining.discard(tid)

    # --- 다른 곳에서 도는 턴 뒤에 보내기 ---------------------------------------
    def _watch_external(self, tid, qid=None):
        """다른 곳(agent-hub 밖의 CLI·Codex 앱)에서 도는 턴 때문에 대기열에 둔 세션을 감시 대상에 올린다."""
        with self.lock:
            fresh = tid not in self.ext_wait
            self.ext_wait[tid] = 0
        if fresh:
            applog.log("external_queue_watch", thread=tid, queue=qid)

    def _probe_external(self, tid):
        """기록을 새로 읽어 그 세션의 마지막 턴 상태를 돌려준다(목록 상태와 같은 모양, 없으면 완료). 읽은 상태는 캐시에도 적는다."""
        th = self.client.request("thread/read", {"threadId": tid, "includeTurns": False}, 30).get("thread") or {}
        meta = self._meta(th)
        state, _ = self._list_state(meta)
        meta["state"] = state
        cur = self.meta_cache.get(tid)
        if cur is not None:
            cur["state"] = state
        else:
            self.meta_cache[tid] = meta
        return state

    def _external_watcher(self):
        while not self.stop.wait(EXTERNAL_WATCH_SEC):
            with self.lock:
                tids = list(self.ext_wait)
            for tid in tids:
                if self.stop.is_set():
                    return
                try:
                    self._external_tick(tid)
                except Exception as e:   # noqa: BLE001 — 한 세션을 못 읽어도 다른 세션은 계속 본다
                    applog.log("external_queue_probe_failed", level="warn", thread=tid, error=repr(e)[:300])

    def _external_tick(self, tid):
        """다른 곳의 턴이 끝나 이 세션이 쉬면 대기열 맨 앞 하나를 보낸다. 나머지는 그 턴이 끝날 때 이어진다.
        끝까지 마친 턴 뒤에만 보낸다 — 멈춤·실패·끊김은 사용자가 직접 보내거나 뺀다(이 앱이 돌린 턴과 같은 규칙)."""
        if not self.queue_of(tid):
            with self.lock:
                self.ext_wait.pop(tid, None)
            return
        with self.lock:
            if self._running(tid) or tid in self.draining:
                return   # 이 앱의 턴이 돌고 있으면 그 턴이 끝날 때 잇는다
        state = self._probe_external(tid)
        if state and state["kind"] == "running":
            with self.lock:
                if tid in self.ext_wait:
                    self.ext_wait[tid] = 0   # 다시 시작했거나 계속 도는 중
            return
        if state is not None:
            # 멈춤·실패·끊김(600초 넘게 기록이 없음)·시스템 오류 — 보내지 않고 대기열에 둔다
            with self.lock:
                self.ext_wait.pop(tid, None)
            applog.log("external_queue_held", thread=tid, state=state["kind"], queued=len(self.queue_of(tid)))
            return
        with self.lock:
            if tid not in self.ext_wait:
                return
            self.ext_wait[tid] += 1
            if self.ext_wait[tid] < EXTERNAL_CLEAR_CHECKS:
                return   # 한 번 끝난 듯 보인 것만으로는 보내지 않는다 — 한 번 더 확인
            self.ext_wait.pop(tid, None)
        applog.log("external_queue_send", thread=tid, queued=len(self.queue_of(tid)))
        self._drain(tid)
        # 보내려는 사이 다른 곳에서 새 턴이 시작돼 못 보냈으면(맨 앞으로 돌아와 있다) 그 턴이 끝나기를 다시 기다린다
        if self.queue_of(tid) and self._external_busy(tid):
            self._watch_external(tid)

    def interrupt(self, tid):
        with self.lock:
            lt = self.live.get(tid)
            turn_id = lt.turn_id if lt else None
        if not turn_id:
            raise ValueError("실행 중인 턴이 없습니다")
        self.client.request("turn/interrupt", {"threadId": tid, "turnId": turn_id}, 30)
        applog.log("turn_interrupt", thread=tid, turn=turn_id)
        return {"ok": True}

    def models(self, fresh=False):
        """두 에이전트의 모델 — 항목마다 agent 를 단다. 한쪽을 못 읽어도 다른 쪽은 돌려준다.
        사용자 설정(숨김·기본)은 model_settings() 가 따로 준다."""
        ts, data = self.models_cache
        if data and not fresh and _now() - ts < 600:
            return data
        out, errors = [], []
        for agent in ("codex", "claude"):
            try:
                res = self.client.request("model/list", {"agent": agent}, 60)
            except RpcError as e:
                errors.append(e)
                applog.log("models_failed", level="warn", agent=agent, error=str(e)[:300])
                continue
            for m in res.get("data") or []:
                if m.get("hidden"):
                    continue
                out.append({"id": m.get("id") or m.get("model"), "label": m.get("displayName") or m.get("id"),
                            "description": m.get("description") or "", "default": bool(m.get("isDefault")),
                            "efforts": [e.get("reasoningEffort") for e in m.get("supportedReasoningEfforts") or []],
                            "default_effort": m.get("defaultReasoningEffort"), "agent": agent,
                            "tiers": [{"id": t.get("id"), "name": t.get("name") or t.get("id"),
                                       "description": t.get("description") or ""}
                                      for t in m.get("serviceTiers") or [] if t.get("id")],
                            "default_tier": m.get("defaultServiceTier")})
        if not out and errors:
            raise errors[0]
        if not errors:
            self.models_cache = (_now(), out)
        return out

    def default_pick(self, agent="codex"):
        """모델·effort 를 넘기지 않고 새 세션을 열 때 실제로 쓰이는 값. 모델 관리의 기본값 → 에이전트 설정(Codex 는 config.toml)
        → 모델 목록의 기본 순이다. Claude 는 effort 를 CLI 가 정해 알 수 없으면 비운다."""
        agent = "claude" if agent == "claude" else "codex"
        df = self.model_settings()["defaults"][agent]
        ms = [m for m in self.models() if (m.get("agent") or "codex") == agent]
        cfg = {}
        if agent == "codex":
            try:
                cfg = (self.client.request("config/read", {}, 30) or {}).get("config") or {}
            except RpcError as e:
                applog.log("config_read", level="warn", error=str(e)[:200])
        model = df["model"] or cfg.get("model") or next((m["id"] for m in ms if m["default"]), None)
        m = next((m for m in ms if m["id"] == model), None)
        effort = next((e for e in (df["effort"], cfg.get("model_reasoning_effort")) if e and m and e in m["efforts"]), None) \
            or (m and m["default_effort"]) or None
        return {"model": model, "effort": effort}

    def model_settings(self):
        """{"hidden": [모델 ID], "defaults": {"codex": {"model","effort","tier"}, "claude": {...}},
        "fork_efforts": {"codex": {"summary","question"}, "claude": {...}}} — 기본값은 에이전트마다.
        fork_efforts 는 실제로 쓰일 값(정하지 않았으면 환경변수 값)이다."""
        d = self.settings.read()
        dd = d.get("defaults") or {}
        return {"hidden": list(d.get("hidden") or []),
                "defaults": {a: {k: str((dd.get(a) or {}).get(k) or "") for k in DEFAULT_KEYS} for a in AGENT_IDS},
                "fork_efforts": {a: {k: v or FORK_EFFORT_ENV[k] for k, v in self._fork_efforts_raw(d)[a].items()}
                                 for a in AGENT_IDS}}

    @staticmethod
    def _fork_efforts_raw(d):
        fe = d.get("fork_efforts") or {}
        return {a: {k: str((fe.get(a) or {}).get(k) or "") for k in FORK_KINDS} for a in AGENT_IDS}

    def fork_effort(self, kind, agent, model):
        """포크 작업의 기본 effort — 모델 관리 값(없으면 환경변수)을 세션 모델이 받는 값 안으로 맞춘다."""
        agent = "claude" if agent == "claude" else "codex"
        effort = self.model_settings()["fork_efforts"][agent][kind]
        try:
            m = next((x for x in self.models() if x["id"] == model and (x.get("agent") or "codex") == agent), None)
        except Exception:
            m = None
        return fit_effort(effort, m["efforts"] if m else [])

    def set_model_settings(self, body):
        """모델 관리 화면이 고친 값만 바꾼다 — hidden, 또는 agent 의 model·effort·tier.
        기본 모델은 숨길 수 없고, effort·속도는 그 모델이 받는 값만 둔다."""
        cur = self.model_settings()
        fork_raw = self._fork_efforts_raw(self.settings.read())
        agent = "claude" if body.get("agent") == "claude" else "codex"
        if "hidden" in body:
            cur["hidden"] = [str(x) for x in body.get("hidden") or [] if x]
        df = cur["defaults"][agent]
        for k in DEFAULT_KEYS:
            if k in body:
                df[k] = str(body[k] or "")
        mine = [m for m in self.models() if (m.get("agent") or "codex") == agent]
        ids = {m["id"]: m for m in mine}
        if df["model"] and df["model"] not in ids:
            raise ValueError("%s 모델이 아닙니다: %s" % ("Claude Code" if agent == "claude" else "Codex", df["model"]))
        m = ids.get(df["model"]) or next((x for x in mine if x["default"]), None)
        if df["effort"] and m and df["effort"] not in m["efforts"]:
            raise ValueError("%s 은(는) effort %s 를 받지 않습니다" % (m["label"], df["effort"]))
        if df["tier"] and df["tier"] not in self._tiers_of(m):
            raise ValueError("%s 은(는) 속도 %s 를 받지 않습니다" % (m["label"] if m else "기본 모델", df["tier"]))
        fe = body.get("fork_efforts")
        if isinstance(fe, dict):
            offered = {e for x in mine for e in x["efforts"]}
            for k in FORK_KINDS:
                if k in fe:
                    v = str(fe[k] or "")
                    if v and v not in offered:
                        raise ValueError("%s 모델은 effort %s 를 받지 않습니다" % ("Claude Code" if agent == "claude" else "Codex", v))
                    fork_raw[agent][k] = v
        keep = {d["model"] for d in cur["defaults"].values() if d["model"]}
        cur["hidden"] = sorted(set(h for h in cur["hidden"] if h not in keep))
        saved = {"hidden": cur["hidden"], "defaults": cur["defaults"], "fork_efforts": fork_raw}
        self.settings.update(lambda d: (d.clear(), d.update(saved)))
        applog.log("model_settings", agent=agent, default_model=df["model"], default_effort=df["effort"],
                   default_tier=df["tier"], hidden=len(cur["hidden"]),
                   summary_effort=fork_raw[agent]["summary"], question_effort=fork_raw[agent]["question"])
        return self.model_settings()

    def recent_picks(self):
        """{"codex": [{"model","effort","tier","at"}], "claude": [...]} — 최근 것부터."""
        d = self.recents.read()
        return {a: [dict(e) for e in (d.get(a) or []) if isinstance(e, dict)][:RECENT_MAX] for a in AGENT_IDS}

    def note_recent(self, pick):
        """보내기에 실제로 쓰인 {agent, model, effort, tier} 를 맨 앞에 둔다(같은 조합은 하나만). 모델이 없으면 무시한다."""
        if not isinstance(pick, dict):
            return None
        agent = "claude" if pick.get("agent") == "claude" else "codex"
        entry = {k: str(pick.get(k) or "")[:120] for k in DEFAULT_KEYS}
        if not entry["model"]:
            return None
        key = tuple(entry[k] for k in DEFAULT_KEYS)
        entry["at"] = time.time()

        def upd(d):
            rest = [e for e in (d.get(agent) or [])
                    if isinstance(e, dict) and tuple(str(e.get(k) or "") for k in DEFAULT_KEYS) != key]
            d[agent] = [entry] + rest[:RECENT_MAX - 1]
        self.recents.update(upd)
        return entry

    @staticmethod
    def _tiers_of(m):
        return [STANDARD_TIER] + [t["id"] for t in m["tiers"]] if m and m.get("tiers") else []

    def apply_defaults(self, model, effort, tier, agent="codex"):
        """새 세션에서 "기본"(값 없음)으로 둔 칸을 그 에이전트의 모델 관리 기본값으로 채운다. 모델을 직접 골랐으면
        기본 effort·속도는 그 모델이 받을 때만 쓴다."""
        df = self.model_settings()["defaults"]["claude" if agent == "claude" else "codex"]
        model = model or df["model"] or None
        if not effort and df["effort"] or not tier and df["tier"]:
            try:
                mine = [m for m in self.models() if (m.get("agent") or "codex") == agent]
            except Exception:
                mine = []
            m = next((x for x in mine if x["id"] == model), None) if model else next((x for x in mine if x["default"]), None)
            if not effort and df["effort"] and m and df["effort"] in m["efforts"]:
                effort = df["effort"]
            if not tier and df["tier"] and df["tier"] in self._tiers_of(m):
                tier = df["tier"]
        return model, effort or None, tier or None

    # --- 멘션 -----------------------------------------------------------
    @staticmethod
    def validate_images(images):
        if images is None:
            return []
        if not isinstance(images, list) or len(images) > 8:
            raise ValueError("이미지는 최대 8장까지 보낼 수 있습니다")
        total = 0
        for im in images:
            url = im.get("url", "") if isinstance(im, dict) else ""
            if not isinstance(url, str) or not url.startswith(("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/webp;base64,", "data:image/gif;base64,")):
                raise ValueError("PNG·JPEG·WebP·GIF 이미지만 보낼 수 있습니다")
            if len(url) > 2800000:
                raise ValueError("이미지는 합계 2MB까지 보낼 수 있습니다")
            try:
                raw = base64.b64decode(url.split(",", 1)[1], validate=True)
            except (ValueError, binascii.Error):
                raise ValueError("이미지 데이터가 올바르지 않습니다")
            if not raw:
                raise ValueError("빈 이미지는 보낼 수 없습니다")
            total += len(raw)
        if total > 2 * 1024 * 1024:
            raise ValueError("이미지는 합계 2MB까지 보낼 수 있습니다")
        return [{"url": im["url"]} for im in images]

    def build_input(self, text, mentions, tid=None, images=None):
        items = [{"type": "text", "text": text or "", "text_elements": []}]
        blocks = []
        files = [m for m in mentions or [] if m.get("kind") == "file" and m.get("path")]
        if files:
            lines = ["[멘션한 파일] — 필요하면 직접 열어 읽어라."]
            for m in files:
                lines.append("- %s" % m["path"])
            blocks.append("\n".join(lines))
        for m in mentions or []:
            if m.get("kind") == "session" and m.get("id") and m.get("id") != tid:
                blocks.append(self.session_digest(m["id"]))
        if blocks:
            items.append({"type": "text", "text": "\n\n" + MENTION_MARK + "\n" + "\n\n".join(blocks), "text_elements": []})
        items.extend({"type": "image", "url": im["url"]} for im in self.validate_images(images))
        return items

    def session_digest(self, tid, turns_n=4, clip=1500):
        try:
            data = self.read_session(tid)
        except RpcError as e:
            return "[멘션한 세션 %s] 읽지 못했다: %s" % (tid, e)
        s = data["session"]
        lines = ["[멘션한 세션] %s" % s["title"], "- 세션 ID: %s" % tid, "- 작업 폴더: %s" % s.get("cwd")]
        if s.get("path"):
            lines.append("- 기록 파일: %s" % s["path"])
        turns = [t for t in data["turns"] if t["request"]["text"] or t["answer"]]
        lines.append("- 턴 %d개 중 마지막 %d개:" % (len(turns), min(turns_n, len(turns))))
        for t in turns[-turns_n:]:
            req = t["request"]["text"].split(MENTION_MARK)[0].strip()
            ans = (t["answer"] or {}).get("text") or {"interrupted": "(사용자가 중간에 멈춰 답 없음)",
                                                       "failed": "(실패해 답 없음)"}.get(t["status"], "(답 없음)")
            lines.append("  ## 턴 %s 요청\n  %s" % (t["index"], req[:clip]))
            lines.append("  ## 턴 %s 답변\n  %s" % (t["index"], ans[:clip] + ("…" if len(ans) > clip else "")))
        return "\n".join(lines)

    def list_files(self, cwd, q="", limit=30):
        cwd = os.path.abspath(os.path.expanduser(cwd or "~"))
        if not os.path.isdir(cwd):
            return {"cwd": cwd, "files": []}
        files = self.project_files(cwd)
        ql = (q or "").lower()
        scored = []
        for f in files:
            s = _fuzzy(ql, f.lower())
            if s is not None:
                scored.append((s, len(f), f))
        scored.sort()
        return {"cwd": cwd, "files": [{"rel": f, "path": os.path.join(cwd, f)} for _, _, f in scored[:limit]],
                "total": len(files)}

    def project_files(self, cwd, max_age=30):
        """프로젝트 파일 상대 경로 목록 — git 저장소면 무시 규칙을 따른다. 30초 동안 다시 쓴다."""
        ts, files = self.files_cache.get(cwd, (0, None))
        if files is None or _now() - ts > max_age:
            files = self._scan_files(cwd)
            self.files_cache[cwd] = (_now(), files)
        return files

    def _scan_files(self, cwd):
        try:
            out = subprocess.run(["git", "-C", cwd, "ls-files", "-co", "--exclude-standard"], capture_output=True,
                                 text=True, timeout=10)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.splitlines()[:FILE_LIMIT]
        except (OSError, subprocess.SubprocessError):
            pass
        files = []
        for root, dirs, names in os.walk(cwd):
            dirs[:] = [d for d in dirs if d not in FILE_SKIP_DIRS and not d.startswith(".")]
            for n in names:
                files.append(os.path.relpath(os.path.join(root, n), cwd))
                if len(files) >= FILE_LIMIT:
                    return files
        return files

    def read_file(self, path):
        path = os.path.abspath(os.path.expanduser(path or ""))
        if not os.path.isfile(path):
            raise FileNotFoundError("파일이 없습니다: %s" % path)
        if os.path.getsize(path) > 3 * 1024 * 1024:
            raise ValueError("3MB 가 넘는 파일은 열지 않습니다")
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        return {"path": path, "name": os.path.basename(path), "text": text, "mtime": os.path.getmtime(path)}

    # --- 코멘트 ---------------------------------------------------------
    def list_comments(self, tid):
        return [c for c in self.comments.read()["items"] if c.get("thread_id") == tid]

    def save_comment(self, tid, body):
        return _upsert(self.comments, dict(body, thread_id=tid),
                       ("turn_id", "part", "quote", "start", "text", "state", "response"))

    def patch_comment(self, store, cid, body):
        def fn(d):
            for c in d["items"]:
                if c["id"] == cid:
                    for k in ("state", "text"):
                        if k in body:
                            c[k] = body[k]
                    c["updated_at"] = _now()
                    return c
            raise KeyError(cid)
        return store.update(fn)

    def delete_comment(self, store, cid):
        def fn(d):
            n = len(d["items"])
            d["items"] = [c for c in d["items"] if c["id"] != cid]
            return n != len(d["items"])
        return store.update(fn)

    def mark_sent(self, tid, ids):
        if not ids:
            return
        def fn(d):
            for c in d["items"]:
                if c["id"] in ids and c.get("state") == "input":
                    c["state"] = "sent"
                    c["sent_to"] = tid
                    c["updated_at"] = _now()
        self.comments.update(fn)
        self.md_comments.update(fn)

    def list_md_comments(self, path):
        path = os.path.abspath(path)
        return [c for c in self.md_comments.read()["items"] if c.get("path") == path]

    def save_md_comment(self, path, body):
        return _upsert(self.md_comments, dict(body, path=os.path.abspath(path)),
                       ("quote", "start", "line", "text", "state"))


def _upsert(store, body, fields):
    cid = body.get("id") or str(uuid.uuid4())

    def fn(d):
        for c in d["items"]:
            if c["id"] == cid:      # 같은 id 의 재전송은 한 번만 저장한다
                return c
        c = {"id": cid, "created_at": _now(), "updated_at": _now()}
        for k in ("thread_id", "path") + tuple(fields):
            if k in body:
                c[k] = body[k]
        c.setdefault("state", "none")
        if isinstance(c.get("quote"), str):
            c["quote"] = c["quote"][:4000]
        if isinstance(c.get("response"), str):
            c["response"] = c["response"][:200]
        d["items"].append(c)
        return c
    return store.update(fn)


def _fuzzy(q, s):
    """q 의 글자가 s 에 차례로 나오면 점수(작을수록 좋음), 아니면 None."""
    if not q:
        return 0
    at = s.find(q)
    if at >= 0:
        base = os.path.basename(s)
        return (0 if q in base else 1) * 1000 + at
    pos, gaps = -1, 0
    for ch in q:
        nxt = s.find(ch, pos + 1)
        if nxt < 0:
            return None
        gaps += nxt - pos - 1
        pos = nxt
    return 5000 + gaps


__all__ = ["Lens", "Busy", "RpcError", "unwrap_output", "MENTION_MARK"]
