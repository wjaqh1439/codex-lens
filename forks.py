"""포크 작업 — 과정 요약·인라인 질문·요청 다듬기.

둘 다 원본 세션을 그 턴까지 포크(thread/fork lastTurnId)하고, **원본 세션과 같은 모델**로 한 턴을 돌린다.
같은 모델·같은 앞부분이라 원본이 써 둔 프롬프트 캐시를 그대로 읽는다(모델을 빼면 기본 모델로 떠서 캐시를 못 읽는다 —
2026-10-31 실측: 모델 없이 포크 0/40.8k, 모델 지정 26.4k/32.5k 캐시 읽음).

- 과정 요약: 요청마다 일회용 포크(ephemeral). 결과는 jobs.json 에 남는다.
- 인라인 질문: 저장형 포크(ephemeral:false)를 (원본 세션, 턴, 모델)마다 하나 두고 그 턴에 대한 질문을 모두 그 포크 끝에 잇는다.
  추가 질문(parent_id)도 같은 포크다. 포크 ID 는 registry.json 의 forks 에 적어 세션 목록에서 숨긴다.
- 요청 다듬기(refine.py): 입력창 글을 다듬는다. 세션이면 마지막 끝난 턴까지 일회용 포크, 새 세션이면 일회용 빈 세션
  (thread/start ephemeral, 고른 모델·작업 폴더). 원본 세션과 묶이지 않을 수 있어(thread_id 없음) 세션 이벤트로 알리지 않고
  화면이 GET /api/jobs/<id> 로 따라 읽는다. 세션 읽기의 jobs 에도 싣지 않는다.
포크는 읽기 전용 샌드박스로 띄운다(파일을 고치지 못한다). 명령 승인 요청이 오면 읽기 전용 명령만 허락한다.
"""

import os
import threading
import time
import uuid

import applog
import refine
from codex_rpc import RpcError

TIMEOUT_SEC = int(os.environ.get("CODEX_LENS_FORK_TIMEOUT_SEC") or 300)
REFINE_EFFORT = os.environ.get("CODEX_LENS_REFINE_EFFORT", "low")
STALL_SEC = 30
MAX_JOBS_PER_THREAD = 300

SUMMARY_PROMPT = """[과정 요약 요청 — codex-lens]
바로 앞 턴(사용자 요청: "{request}")에서 네가 한 '과정'을 사용자가 쉽게 이해하도록 한국어로 정리해 줘.
과정이란 그 턴에서 실행한 명령, 읽거나 찾아본 파일, 고친 파일, 중간 판단과 방향 전환이다.
도구는 쓰지 말고 이 대화에 이미 있는 내용만으로 답해. 전문 용어는 처음 나올 때 짧게 풀어 써.

형식(마크다운):
**한 줄 요약** — 무엇을 하려고 어떤 순서로 했는지 한 문장.

### 단계
1. 무엇을 했고 왜 했는지 (3~7단계, 관련된 명령·파일은 `코드`로)

### 바뀐 것
- 고친·만든 파일과 상태 변화 (없으면 "없음")

### 눈여겨볼 점
- 실패했다가 고친 것, 가정, 사용자가 확인해야 할 것 (없으면 생략)
"""

QUESTION_PROMPT = """[인라인 질문 — codex-lens 화면에서 고른 부분에 대한 짧은 질문]
선택한 부분:
{quote}

질문: {question}

이 대화 맥락으로 답할 수 있으면 도구 없이 답하고, 파일 내용처럼 확인해야 하는 사실이 있을 때만 읽기 도구를 써.
파일을 고치거나 상태를 바꾸는 명령은 쓰지 마. 쉬운 말로 짧게(대개 3~8문장) 답해.
앞서 오간 다른 인라인 질문의 답은 참고해도 되지만 사용자는 그 카드를 보고 있지 않다 — "앞서 말했듯"처럼 가리키지 말고 필요한 내용은 이 답에 직접 적어."""

FOLLOWUP_PROMPT = """[인라인 질문 — 이어서 묻기]
앞 질문: {parent_question}
앞 답(요약): {parent_answer}
{selection}
질문: {question}

같은 규칙으로 짧고 쉽게 답해."""

DEFAULT_QUESTION = "선택한 부분을 쉽게 설명해 줘."
READ_ONLY_PROGRAMS = {"cat", "head", "tail", "ls", "wc", "rg", "grep", "find", "fd", "stat", "file", "pwd", "tree", "jq",
                      "sed", "awk", "diff", "git", "nl", "sort", "uniq", "cut", "less", "more", "du", "echo"}
GIT_READ = {"log", "show", "diff", "status", "blame", "grep", "ls-files", "rev-parse", "branch", "tag"}


def _now():
    return time.time()


def readonly_command(cmd):
    """명령 문자열이 읽기만 하는지 대략 판정한다(파이프로 이은 조각마다)."""
    import shlex
    from turns import short_cmd
    cmd = short_cmd(cmd)
    if any(x in cmd for x in (">", "`", "$(", "\n")) and "2>&1" not in cmd:
        return False
    for part in cmd.replace("&&", "|").replace("||", "|").replace(";", "|").split("|"):
        try:
            words = shlex.split(part)
        except ValueError:
            return False
        if not words:
            continue
        prog = os.path.basename(words[0])
        if prog not in READ_ONLY_PROGRAMS:
            return False
        if prog == "git" and (len(words) < 2 or words[1] not in GIT_READ):
            return False
        if prog == "sed" and "-i" in words:
            return False
        if prog == "find" and any(w in ("-exec", "-delete", "-execdir") for w in words):
            return False
    return True


class JobManager:
    def __init__(self, lens):
        from store import JsonStore
        self.lens = lens
        self.client = lens.client
        self.store = JsonStore(os.path.join(lens.home, "jobs.json"), {"jobs": {}})
        self.lock = threading.RLock()
        self.by_fork = {}        # fork thread id -> job id (실행 중)
        self.waiters = {}        # job id -> threading.Event(턴 끝)
        self.dirty = set()
        self.last_flush = 0
        # 앱이 다시 시작되면 돌던 작업은 끝난 것으로 본다
        def recover(d):
            for j in d["jobs"].values():
                if j.get("status") == "running":
                    j.update(status="failed", stage="failed", error="앱이 다시 시작돼 중단됐습니다 — 다시 물어보세요")
        self.store.update(recover)

    # --- 조회 -----------------------------------------------------------
    def owns(self, tid):
        if tid in self.by_fork:
            return True
        return tid in self.lens.registry.read()["forks"]

    def _jobs(self):
        return self.store.read()["jobs"]

    def public(self, j):
        out = {k: v for k, v in j.items() if not k.startswith("_")}
        if j.get("status") == "running":
            now = _now()
            out["elapsed_sec"] = round(now - j.get("started_at", now), 1)
            out["idle_sec"] = round(now - j.get("last_signal_at", now), 1)
            out["stalled"] = out["idle_sec"] > STALL_SEC
        return out

    def for_thread(self, tid):
        with self.lock:
            jobs = [self.public(j) for j in self._jobs().values() if j.get("thread_id") == tid and j.get("kind") != "refine"]
        jobs.sort(key=lambda j: j.get("created_at", 0))
        return jobs

    def get(self, jid):
        j = self._jobs().get(jid)
        if not j:
            raise KeyError(jid)
        return j

    # --- 시작 -----------------------------------------------------------
    def start_summary(self, tid, turn_id, request_text="", client_id=None, effort=None):
        return self._start(tid, turn_id, "summary", client_id, request=request_text[:2000],
                           effort=effort or self._default_effort("summary", tid))

    def _default_effort(self, kind, tid):
        """고르지 않았을 때 — 모델 관리의 기본 effort(에이전트별)를 세션 모델이 받는 값으로 맞춘다."""
        m = self.lens.session_meta(tid)
        return self.lens.fork_effort(kind, m.get("agent") or "codex", m.get("model"))

    def start_question(self, tid, turn_id, body):
        question = (body.get("question") or "").strip() or DEFAULT_QUESTION
        quote = (body.get("quote") or "").strip()
        if not quote:
            raise ValueError("고른 글이 없습니다")
        extra = {"quote": quote[:4000], "question": question[:4000], "part": body.get("part") or "answer",
                 "start": body.get("start"), "parent_id": body.get("parent_id"),
                 "anchor": body.get("anchor") or {}, "effort": body.get("effort") or self._default_effort("question", tid)}
        if extra["parent_id"]:
            parent = self._jobs().get(extra["parent_id"])
            if not parent or parent.get("thread_id") != tid:
                raise ValueError("이어 물을 질문을 찾지 못했습니다")
            extra["group_id"] = parent.get("group_id") or parent["id"]
            extra["depth"] = (parent.get("depth") or 0) + 1
        return self._start(tid, turn_id, "question", body.get("client_id"), **extra)

    def start_refine(self, body):
        """입력창 글 다듬기. body: text, thread_id·turn_id(세션 화면 — 그 턴까지 포크) 또는 cwd·model·agent(새 세션 화면),
        mode(request 에이전트 요청 | writing 좋은 글), refine_model(다듬기에만 쓸 모델 — 없으면 세션·입력창 모델),
        effort, tier(Codex 속도), answers(추가 질문에 대한 답), draft_key, client_id.
        다듬기 지시는 시작할 때의 refine-prompts.json 값을 작업에 고정한다(다시 시도해도 같은 지시)."""
        text = (body.get("text") or "").strip()
        if not text:
            raise ValueError("다듬을 글이 없습니다")
        mode = refine.mode_of(body.get("mode"))
        tid = body.get("thread_id") or None
        turn_id = (body.get("turn_id") or None) if tid else None
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            prompt = self.lens.refine_prompts.text(mode)
        extra = {"text": text[:20000], "draft_key": str(body.get("draft_key") or "")[:200], "mode": mode,
                 "prompt": prompt.strip()[:20000],
                 "context_answers": refine.answers(body.get("answers"), answer_limit=4000) if mode == "request" else [],
                 "effort": body.get("effort") or REFINE_EFFORT, "tier": str(body.get("tier") or "")[:40] or None,
                 "refine_model": str(body.get("refine_model") or "")[:200] or None, "result": None}
        sm = self.lens.session_meta(tid) if tid else {}
        if turn_id:
            meta = dict(sm)
        else:
            cwd = os.path.abspath(os.path.expanduser((body.get("cwd") or "").strip() or "~"))
            cwd = sm.get("cwd") or cwd
            if not os.path.isdir(cwd):
                raise ValueError("작업 폴더가 없습니다: %s" % cwd)
            agent = sm.get("agent") or ("claude" if body.get("agent") == "claude" else "codex")
            model = body.get("model") or sm.get("model") or None
            if not model:   # 새 세션 화면 — 보낼 때와 같은 기본 모델(모델 관리, 에이전트별)로 다듬는다
                model = self.lens.model_settings()["defaults"][agent]["model"] or None
            meta = {"model": model, "cwd": cwd, "agent": agent}
        if extra["refine_model"]:
            agent = meta.get("agent") or "codex"
            try:
                known = [m for m in self.lens.models() if (m.get("agent") or "codex") == agent]
            except Exception:
                known = []
            if known and extra["refine_model"] not in [m["id"] for m in known]:
                raise ValueError("%s 모델이 아닙니다: %s" % ("Claude Code" if agent == "claude" else "Codex", extra["refine_model"]))
            meta["model"] = extra["refine_model"]
        if (meta.get("agent") or "codex") == "claude":
            extra["tier"] = None   # Claude 에는 속도 등급이 없다
        return self._start(tid, turn_id, "refine", body.get("client_id"), meta=meta, **extra)

    def _start(self, tid, turn_id, kind, client_id, meta=None, **extra):
        jid = client_id or uuid.uuid4().hex[:16]
        with self.lock:
            existing = self._jobs().get(jid)
            if existing:
                return self.public(existing)
            busy = [j for j in self._jobs().values()
                    if j.get("thread_id") == tid and j.get("status") == "running"]
            if len(busy) >= 4:
                raise ValueError("이 세션에서 이미 %d개를 처리하는 중입니다 — 끝난 뒤 다시 시도하세요" % len(busy))
        meta = meta or self.lens.session_meta(tid)
        model = meta.get("model")
        now = _now()
        j = {"id": jid, "kind": kind, "thread_id": tid, "turn_id": turn_id, "status": "running", "stage": "starting",
             "steps": [{"stage": "starting", "t": 0}], "answer": "", "error": None, "model": model, "usage": None,
             "created_at": now, "started_at": now, "last_signal_at": now, "version": 1, "cwd": meta.get("cwd"),
             "agent": meta.get("agent") or "codex"}
        j.update(extra)
        if kind == "question" and not j.get("group_id"):
            j["group_id"] = jid
        with self.lock:
            self.store.update(lambda d: (d["jobs"].__setitem__(jid, j), self._trim(d, tid)))
        threading.Thread(target=self._run, args=(jid,), daemon=True, name="job-" + jid).start()
        applog.log("job_start", job=jid, kind=kind, thread=tid, turn=turn_id, model=model)
        self._publish(j, now=True)
        return self.public(j)

    def _trim(self, d, tid):
        mine = sorted([j for j in d["jobs"].values() if j.get("thread_id") == tid], key=lambda j: j.get("created_at", 0))
        for j in mine[:-MAX_JOBS_PER_THREAD]:
            if j.get("status") != "running":
                d["jobs"].pop(j["id"], None)

    # --- 진행 ----------------------------------------------------------
    def _stage(self, j, stage, detail=None):
        if j.get("stage") != stage:
            j["steps"] = (j.get("steps") or [])[-29:] + [{"stage": stage, "t": round(_now() - j["started_at"], 1)}]
        j["stage"] = stage
        if detail is not None:
            j["stage_detail"] = detail[:160]
        j["version"] = j.get("version", 0) + 1

    def _publish(self, j, now=False):
        if j.get("kind") == "refine":   # 화면이 GET /api/jobs/<id> 로 따라 읽는다
            return
        if now:
            self.lens.bus.publish(j["thread_id"], {"type": "job", "job": self.public(j)})
        else:
            with self.lock:
                self.dirty.add(j["id"])

    def flush(self):
        """0.25초마다 부른다 — 진행 신호는 0.5초에 한 번만 화면에 알린다."""
        now = _now()
        if now - self.last_flush < 0.5:
            return
        self.last_flush = now
        with self.lock:
            ids, self.dirty = self.dirty, set()
            jobs = self._jobs()
            out = [jobs[i] for i in ids if i in jobs]
            # 지연 표시는 신호가 없어도 바뀌어야 한다
            for j in jobs.values():
                if j.get("status") == "running" and j["id"] not in ids:
                    out.append(j)
        for j in out:
            if j.get("kind") == "refine":
                continue
            self.lens.bus.publish(j["thread_id"], {"type": "job", "job": self.public(j)})

    def on_note(self, tid, method, params):
        with self.lock:
            jid = self.by_fork.get(tid)
            j = self._jobs().get(jid) if jid else None
            if not j or j.get("status") != "running":
                return
            j["last_signal_at"] = _now()
            if method == "item/started":
                it = params.get("item") or {}
                t = it.get("type")
                if t == "reasoning":
                    self._stage(j, "thinking")
                elif t == "commandExecution":
                    cmd = it.get("command")
                    if isinstance(cmd, list):
                        cmd = cmd[-1]
                    self._stage(j, "reading", "명령 " + str(cmd)[:120])
                    j["tools_used"] = j.get("tools_used", 0) + 1
                elif t == "contextCompaction":
                    self._stage(j, "compacting")
            elif method == "item/agentMessage/delta":
                if j.get("stage") != "answering":
                    self._stage(j, "answering")
                    j["first_token_ms"] = int((_now() - j["started_at"]) * 1000)
                j["_partial"] = j.get("_partial", "") + (params.get("delta") or "")
                j["answer"] = j["_partial"]
            elif method == "item/reasoning/summaryTextDelta":
                j["_think"] = (j.get("_think", "") + (params.get("delta") or ""))[-400:]
                head = j["_think"].strip().split("\n")[0].strip("* ")
                if head:
                    j["stage_detail"] = head[:80]
            elif method == "item/completed":
                it = params.get("item") or {}
                if it.get("type") == "agentMessage":
                    from turns import unwrap_output
                    j["_final"] = unwrap_output(it.get("text") or "")
                    j["answer"] = j["_final"]
            elif method == "thread/tokenUsage/updated":
                usage = (params.get("tokenUsage") or {}).get("last") or {}
                j["usage"] = {"input": usage.get("inputTokens"), "cached": usage.get("cachedInputTokens"),
                              "output": usage.get("outputTokens"), "reasoning": usage.get("reasoningOutputTokens")}
            elif method == "turn/started":
                j["fork_turn"] = (params.get("turn") or {}).get("id")
                if j.get("stage") in ("starting", "forking"):
                    self._stage(j, "waiting")
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                j["_turn_status"] = turn.get("status")
                err = turn.get("error")
                j["_turn_error"] = (err.get("message") if isinstance(err, dict) else err) or None
                ev = self.waiters.get(jid)
                if ev:
                    ev.set()
            j["version"] = j.get("version", 0) + 1
        self._publish(j)

    def on_server_request(self, tid, method, params):
        if method in ("item/commandExecution/requestApproval", "execCommandApproval"):
            ok = readonly_command(params.get("command") or "")
            if method == "execCommandApproval":
                return {"decision": "approved" if ok else "denied"}
            return {"decision": "accept" if ok else "decline"}
        if method in ("item/fileChange/requestApproval",):
            return {"decision": "decline"}
        if method == "applyPatchApproval":
            return {"decision": "denied"}
        if method == "item/tool/requestUserInput":
            return {"answers": {}}
        return None

    def on_process_exit(self):
        """Codex app-server 가 죽었다 — Codex 포크에서 기다리던 작업만 깨운다(Claude 포크는 저마다 프로세스다)."""
        with self.lock:
            jobs = self._jobs()
            for jid in list(self.waiters):
                if (jobs.get(jid) or {}).get("agent", "codex") == "codex":
                    self.waiters[jid].set()

    # --- 실행 ----------------------------------------------------------
    def _find_fork(self, j):
        """같은 (세션, 턴, 모델)의 질문 포크 중 지금 비어 있는 것."""
        forks = self.lens.registry.read()["forks"]
        busy = set(self.by_fork.keys())
        cands = [(info.get("created_at", 0), fid) for fid, info in forks.items()
                 if info.get("kind") == "question" and info.get("origin") == j["thread_id"]
                 and info.get("turn_id") == j["turn_id"] and info.get("model") == j.get("model") and fid not in busy]
        if j.get("parent_id"):
            parent = self._jobs().get(j["parent_id"]) or {}
            pf = parent.get("fork_thread")
            if pf in forks and pf not in busy:
                return pf
        cands.sort(reverse=True)
        return cands[0][1] if cands else None

    def _fork_params(self, j, ephemeral):
        p = {"threadId": j["thread_id"], "lastTurnId": j["turn_id"], "ephemeral": ephemeral, "excludeTurns": True,
             "sandbox": "read-only", "approvalPolicy": "on-request"}
        if j.get("model"):
            p["model"] = j["model"]
        return p

    def _prompt(self, j):
        if j["kind"] == "refine":
            return refine.build_prompt(j["text"], bool(j.get("turn_id")), j.get("cwd") or "", j.get("context_answers"),
                                       j.get("mode"), j.get("prompt"))
        if j["kind"] == "summary":
            req = (j.get("request") or "").strip().replace("\n", " ")
            if len(req) > 200:
                req = req[:200] + "…"
            return SUMMARY_PROMPT.format(request=req or "(요청 글 없음)")
        quote = "\n".join("> " + line for line in j["quote"].split("\n"))
        if j.get("parent_id"):
            parent = self._jobs().get(j["parent_id"]) or {}
            pa = (parent.get("answer") or "").strip()
            if len(pa) > 1200:
                pa = pa[:1200] + "…"
            sel = ""
            if j.get("part", "").startswith("q:"):
                sel = "앞 답에서 고른 부분:\n" + quote
            return FOLLOWUP_PROMPT.format(parent_question=parent.get("question", ""), parent_answer=pa,
                                          selection=sel, question=j["question"])
        return QUESTION_PROMPT.format(quote=quote, question=j["question"])

    def _run(self, jid):
        j = self.get(jid)
        ev = threading.Event()
        self.waiters[jid] = ev
        fork = None
        try:
            with self.lock:
                self._stage(j, "forking")
            self._publish(j, now=True)
            reused = False
            if j["kind"] == "question":
                fork = self._find_fork(j)
                if fork:
                    with self.lock:
                        self.by_fork[fork] = jid
                    self.client.ensure_loaded(fork, sandbox="read-only", approvalPolicy="on-request",
                                              model=j.get("model"))
                    reused = True
            if not fork:
                eph = j["kind"] != "question"
                if j["kind"] == "refine" and not j.get("turn_id"):
                    # 이을 턴이 없다(새 세션·턴 없는 세션) — 일회용 빈 세션
                    p = {"cwd": j.get("cwd"), "ephemeral": True, "sandbox": "read-only", "approvalPolicy": "on-request",
                         "agent": j.get("agent") or "codex"}
                    if j.get("model"):
                        p["model"] = j["model"]
                    res = self.client.request("thread/start", p, 120)
                    with self.lock:
                        j["model"] = j.get("model") or res.get("model")
                else:
                    res = self.client.request("thread/fork", self._fork_params(j, eph), 120)
                fork = (res.get("thread") or {})["id"]
                self.client.loaded.add(fork)
                with self.lock:
                    self.by_fork[fork] = jid
                if not eph:
                    self.lens.registry.update(lambda d: d["forks"].__setitem__(fork, {
                        "kind": "question", "origin": j["thread_id"], "turn_id": j["turn_id"],
                        "model": j.get("model"), "created_at": _now()}))
            with self.lock:
                j["fork_thread"] = fork
                j["fork_mode"] = "reused" if reused else "new"
                self._stage(j, "waiting")
            self._publish(j, now=True)
            params = {"threadId": fork, "input": [{"type": "text", "text": self._prompt(j), "text_elements": []}]}
            if j.get("model"):
                params["model"] = j["model"]
            if j.get("effort"):
                params["effort"] = j["effort"]
            if j.get("tier"):
                params["serviceTier"] = j["tier"]
            res = self.client.request("turn/start", params, 120)
            with self.lock:
                j["fork_turn"] = j.get("fork_turn") or (res.get("turn") or {}).get("id")
            if not ev.wait(TIMEOUT_SEC):
                try:
                    self.client.request("turn/interrupt", {"threadId": fork, "turnId": j.get("fork_turn")}, 20)
                except RpcError:
                    pass
                raise RpcError("%d초 안에 끝나지 않아 멈췄습니다" % TIMEOUT_SEC)
            status = j.get("_turn_status")
            with self.lock:
                if j.get("status") == "cancelled":
                    pass
                elif status == "completed" and (j.get("_final") or j.get("answer")):
                    j["answer"] = j.get("_final") or j.get("answer")
                    if j["kind"] == "refine":
                        try:
                            j["result"] = refine.parse(j["answer"], j["text"], j.get("mode"))
                        except ValueError as e:
                            j["status"] = "failed"
                            j["error"] = str(e)
                            self._stage(j, "failed")
                    if j["status"] == "running":
                        j["status"] = "done"
                        self._stage(j, "done")
                elif status == "interrupted":
                    j["status"] = "cancelled"
                    self._stage(j, "cancelled")
                else:
                    j["status"] = "failed"
                    j["error"] = j.get("_turn_error") or ("에이전트 프로세스가 종료됐습니다" if not status else "답을 받지 못했습니다")
                    self._stage(j, "failed")
        except Exception as e:
            with self.lock:
                if j.get("status") == "running":
                    j["status"] = "failed"
                    msg = str(e)
                    if "progress" in msg.lower() or "in progress" in msg.lower():
                        msg = "이 턴이 아직 진행 중이라 포크할 수 없습니다 — 끝난 뒤 다시 시도하세요"
                    j["error"] = msg[:500]
                    self._stage(j, "failed")
        finally:
            with self.lock:
                if fork:
                    self.by_fork.pop(fork, None)
                self.waiters.pop(jid, None)
                j["duration_ms"] = int((_now() - j["started_at"]) * 1000)
                j["finished_at"] = _now()
                for k in [k for k in j if k.startswith("_")]:
                    j.pop(k, None)
                self.store.save()
            if j["kind"] != "question" and fork:
                try:
                    self.client.request("thread/unsubscribe", {"threadId": fork}, 10)
                    self.client.loaded.discard(fork)
                except RpcError:
                    pass
            u = j.get("usage") or {}
            applog.log("job_end", job=jid, kind=j["kind"], status=j["status"], ms=j["duration_ms"],
                       first_token_ms=j.get("first_token_ms"), cached=u.get("cached"), input=u.get("input"),
                       fork_mode=j.get("fork_mode"), error=(j.get("error") or "")[:200] or None)
            self._publish(j, now=True)

    def cancel(self, jid):
        j = self.get(jid)
        if j.get("status") != "running":
            return self.public(j)
        with self.lock:
            j["status"] = "cancelled"
            self._stage(j, "cancelled")
            fork, turn = j.get("fork_thread"), j.get("fork_turn")
            ev = self.waiters.get(jid)
        if fork and turn:
            try:
                self.client.request("turn/interrupt", {"threadId": fork, "turnId": turn}, 20)
            except RpcError:
                pass
        if ev:
            ev.set()
        self._publish(j, now=True)
        return self.public(j)

    def delete(self, jid):
        j = self._jobs().get(jid)
        if not j:
            return False
        if j.get("status") == "running":
            self.cancel(jid)
        with self.lock:
            self.store.update(lambda d: d["jobs"].pop(jid, None))
        self.lens.bus.publish(j["thread_id"], {"type": "job_removed", "id": jid})
        return True

    def patch(self, jid, body):
        j = self.get(jid)
        with self.lock:
            for k in ("folded", "inserted"):
                if k in body:
                    j[k] = bool(body[k])
            j["version"] = j.get("version", 0) + 1
            self.store.save()
        self._publish(j, now=True)
        return self.public(j)
