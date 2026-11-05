#!/usr/bin/env python3
"""테스트·브라우저 회귀용 가짜 `codex app-server` — 진짜 모델·~/.codex 를 건드리지 않는다.

시드: 환경변수 FAKE_CODEX_SEED(JSON 파일, threads 목록). 호출 기록: FAKE_CODEX_LOG(JSON Lines)에 메서드·인자를 남긴다.
FAKE_CODEX_ROLLOUT_DIR 를 주면 새 세션마다 진짜와 같은 모양의 기록(rollout JSONL — 응답 항목·token_count)을 그 폴더에 쓰고
스레드의 `path` 로 알린다(응답별 실측 생성 속도 시험용).
포크에 원본과 같은 모델을 주면 tokenUsage 의 cachedInputTokens 가 0보다 크다(실제 캐시 재사용을 흉내).
"""

import json
import os
import sys
import threading
import time
import uuid

lock = threading.Lock()
out_lock = threading.Lock()
threads = {}
LOG = os.environ.get("FAKE_CODEX_LOG")
DELAY = float(os.environ.get("FAKE_CODEX_DELAY") or 0.05)
CONTEXT_TOKENS = int(os.environ.get("FAKE_CODEX_CONTEXT_TOKENS") or 1280)
ROLLOUT_DIR = os.environ.get("FAKE_CODEX_ROLLOUT_DIR")


def rollout(th, kind, payload):
    """진짜 rollout 한 줄을 흉내 낸다(시각·바깥 종류·payload)."""
    if not th.get("path"):
        if not ROLLOUT_DIR:
            return
        os.makedirs(ROLLOUT_DIR, exist_ok=True)       # 시드 세션도 첫 턴부터 기록을 남긴다
        th["path"] = os.path.join(ROLLOUT_DIR, "rollout-%s.jsonl" % th["id"])
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".%03dZ" % (int(time.time() * 1000) % 1000)
    with open(th["path"], "a") as f:
        f.write(json.dumps({"timestamp": ts, "type": kind, "payload": payload}, ensure_ascii=False) + "\n")


def rollout_usage(th, out):
    rollout(th, "event_msg", {"type": "token_count", "info": {"last_token_usage": {
        "input_tokens": CONTEXT_TOKENS - out, "cached_input_tokens": 0, "output_tokens": out, "reasoning_output_tokens": 10,
        "total_tokens": CONTEXT_TOKENS}}})


def send(obj):
    with out_lock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def note(method, params):
    send({"jsonrpc": "2.0", "method": method, "params": params})


def log(method, params):
    if LOG:
        with open(LOG, "a") as f:
            f.write(json.dumps({"method": method, "params": params}, ensure_ascii=False) + "\n")


def now():
    return int(time.time())


def tid():
    return "01a1" + uuid.uuid4().hex[:28]


def load_seed():
    path = os.environ.get("FAKE_CODEX_SEED")
    if not path or not os.path.exists(path):
        return
    with open(path) as f:
        data = json.load(f)
    for th in data.get("threads", []):
        th.setdefault("createdAt", now() - 3600)
        th.setdefault("updatedAt", th["createdAt"])
        th.setdefault("forkedFromId", None)
        th.setdefault("source", "cli")
        th.setdefault("status", {"type": "notLoaded"})
        th.setdefault("reasoningEffort", "medium")
        threads[th["id"]] = th


def external_end(th):
    """밖에서 돌던 턴(_live_external)이 끝난 것으로 보일지 — 환경변수 FAKE_CODEX_EXTERNAL_END 가 가리키는 파일의 줄 `스레드ID[:상태]`
    (상태는 completed 기본 · interrupted · failed). 파일을 지우면 다시 도는 중으로 돌아온다(시험이 끝남·재시작을 흉내 낸다)."""
    path = os.environ.get("FAKE_CODEX_EXTERNAL_END")
    if not (th.get("_live_external") and path and os.path.exists(path)):
        return None
    with open(path) as f:
        for line in f.read().split():
            tid_, _, st = line.partition(":")
            if tid_ in (th["id"], "*"):
                return st or "completed"
    return None


def settled(th):
    """끝난 것으로 보이는 밖의 턴이면 마지막 턴을 끝낸 복사본, 아니면 그대로."""
    st = external_end(th)
    if not st or not th.get("turns"):
        return th
    done = int(time.time()) - 30
    turns = list(th["turns"])
    for i in range(len(turns) - 1, -1, -1):   # 끝나지 않은(interrupted·completedAt 없음) 밖의 턴 — 이 앱이 이어 보낸 턴은 건드리지 않는다
        if turns[i].get("status") == "interrupted" and not turns[i].get("completedAt"):
            turns[i] = dict(turns[i], status=st, completedAt=done, error={"message": "모델 호출이 실패했습니다"} if st == "failed" else None)
            return dict(th, turns=turns, updatedAt=done if i == len(turns) - 1 else th["updatedAt"], _live_external=False)
    return dict(th, _live_external=False)


def public(th, turns=False):
    th = settled(th)
    if th.get("_live_external"):
        th["updatedAt"] = now() - 2
    out = {k: v for k, v in th.items() if not k.startswith("_")}
    out["turns"] = th.get("turns", []) if turns else []
    return out


def new_thread(cwd, model, forked=None, turns=None):
    t = {"id": tid(), "preview": "", "cwd": cwd, "model": model or "gpt-fake-sol", "reasoningEffort": None,
         "createdAt": now(), "updatedAt": now(), "forkedFromId": forked, "source": "vscode", "status": {"type": "idle"},
         "turns": turns or [], "name": None, "path": None, "cliVersion": "fake", "gitInfo": None}
    if ROLLOUT_DIR:
        os.makedirs(ROLLOUT_DIR, exist_ok=True)
        t["path"] = os.path.join(ROLLOUT_DIR, "rollout-%s.jsonl" % t["id"])
    threads[t["id"]] = t
    return t


def run_turn(th, turn, text, model):
    """턴 하나를 흉내 낸다 — 생각 → 명령 → 답(스트리밍)."""
    tidv = th["id"]
    try:
        time.sleep(DELAY)
        note("turn/started", {"threadId": tidv, "turn": {"id": turn["id"], "status": "inProgress", "items": []}})
        rollout(th, "event_msg", {"type": "task_started", "turn_id": turn["id"]})
        rollout(th, "response_item", {"type": "message", "id": "msg_u", "role": "user", "content": [{"type": "input_text", "text": text[:200]}]})
        note("item/started", {"threadId": tidv, "turnId": turn["id"], "item": turn["items"][0]})
        note("item/completed", {"threadId": tidv, "turnId": turn["id"], "item": turn["items"][0]})
        rs = {"type": "reasoning", "id": "rs_" + uuid.uuid4().hex[:8], "summary": ["**살펴보기**\n\n요청을 확인한다."], "content": []}
        note("item/started", {"threadId": tidv, "turnId": turn["id"], "item": dict(rs, summary=[])})
        note("item/reasoning/summaryTextDelta", {"threadId": tidv, "turnId": turn["id"], "itemId": rs["id"], "delta": "**살펴보기**", "summaryIndex": 0})
        time.sleep(DELAY)
        note("item/completed", {"threadId": tidv, "turnId": turn["id"], "item": rs})
        rollout(th, "response_item", {"type": "reasoning", "summary": []})
        items = [rs]
        is_fork = bool(th.get("forkedFromId")) or th.get("_ephemeral")
        if "중간 말" in text:
            # 진짜 Codex 처럼 하던 중에 남기는 말(phase=commentary) — 멈추면 이 말이 마지막 말로 남는다
            cm = {"type": "agentMessage", "id": "msg_" + uuid.uuid4().hex[:8], "text": "기존 상태를 확인하고 다시 띄우겠습니다.", "phase": "commentary"}
            note("item/started", {"threadId": tidv, "turnId": turn["id"], "item": dict(cm, text="")})
            note("item/completed", {"threadId": tidv, "turnId": turn["id"], "item": cm})
            items.append(cm)
        if not is_fork:
            cmd = {"type": "commandExecution", "id": "exec-" + uuid.uuid4().hex[:8], "command": "/bin/zsh -lc 'ls -la'",
                   "cwd": th["cwd"], "status": "inProgress", "aggregatedOutput": None, "exitCode": None, "durationMs": None,
                   "commandActions": [], "source": "agent", "processId": None}
            note("item/started", {"threadId": tidv, "turnId": turn["id"], "item": cmd})
            rollout(th, "response_item", {"type": "custom_tool_call", "name": "exec", "input": "ls -la"})
            note("item/commandExecution/outputDelta", {"threadId": tidv, "turnId": turn["id"], "itemId": cmd["id"], "delta": "README.md\n"})
            time.sleep(DELAY)
            cmd = dict(cmd, status="completed", aggregatedOutput="README.md\ncalc.py\n", exitCode=0, durationMs=12)
            note("item/completed", {"threadId": tidv, "turnId": turn["id"], "item": cmd})
            rollout(th, "response_item", {"type": "custom_tool_call_output", "output": "README.md"})
            rollout_usage(th, 120)
            items.append(cmd)
        # 명령 사이에 끼워 넣은 말(turn/steer)을 읽는다 — 진짜 Codex 도 다음 도구 경계에서 읽는다
        # (끼워 넣기를 시험할 틈: 요청에 "천천히"가 있으면 FAKE_CODEX_SLOW_SEC 초, 기본 1.5)
        note("thread/tokenUsage/updated", {"threadId": tidv, "turnId": turn["id"], "tokenUsage": {
            "modelContextWindow": 258400, "total": {"totalTokens": 999999},
            "last": {"totalTokens": CONTEXT_TOKENS // 2}}})
        until = time.time() + (float(os.environ.get("FAKE_CODEX_SLOW_SEC") or 1.5) if "천천히" in text else 0)
        while time.time() < until and not turn.get("_interrupt"):   # 멈춤은 기다리지 않고 바로 받는다
            time.sleep(0.1)
        with lock:
            steers = list(turn.get("_steers") or [])
            turn["_steer_closed"] = True
        items += steers
        answer = fake_answer(text, [st["content"][0].get("text", "") for st in steers])
        if turn.get("_schema"):
            answer = protocol_answer(text, answer)
        msg_id = "msg_" + uuid.uuid4().hex[:8]
        note("item/started", {"threadId": tidv, "turnId": turn["id"], "item": {"type": "agentMessage", "id": msg_id, "text": "", "phase": "final_answer"}})
        for i in range(0, len(answer), 12):
            if i == 96:
                time.sleep(float(os.environ.get('FAKE_STREAM_PAUSE_SEC') or 0))
            if turn.get("_interrupt"):
                break
            note("item/agentMessage/delta", {"threadId": tidv, "turnId": turn["id"], "itemId": msg_id, "delta": answer[i:i + 12]})
            time.sleep(float(os.environ.get('FAKE_STREAM_DELAY') or DELAY / 5))
        status = "interrupted" if turn.get("_interrupt") else "completed"
        msg = {"type": "agentMessage", "id": msg_id, "text": answer, "phase": "final_answer"}
        if status == "completed":
            note("item/completed", {"threadId": tidv, "turnId": turn["id"], "item": msg})
            items.append(msg)
        rollout(th, "response_item", {"type": "message", "id": msg_id, "role": "assistant", "content": [{"type": "output_text", "text": answer[:200]}]})
        rollout_usage(th, max(20, len(answer.encode("utf-8")) // 4))
        origin = threads.get(th.get("forkedFromId") or "") or {}
        cached = 1000 if (not is_fork or origin.get("model") == model) else 0
        note("thread/tokenUsage/updated", {"threadId": tidv, "turnId": turn["id"], "tokenUsage": {
            "modelContextWindow": 258400, "total": {"totalTokens": 999999},
            "last": {"totalTokens": CONTEXT_TOKENS, "inputTokens": CONTEXT_TOKENS - 80, "cachedInputTokens": cached, "outputTokens": 80, "reasoningOutputTokens": 10}}})
        with lock:
            turn["items"] = [turn["items"][0]] + items
            turn["status"] = status
            turn["completedAt"] = now()
            turn["durationMs"] = 300
            th["updatedAt"] = now()
            th["status"] = {"type": "idle"}
        note("turn/completed", {"threadId": tidv, "turn": {k: v for k, v in turn.items() if not k.startswith("_")}})
    except Exception as e:  # pragma: no cover
        sys.stderr.write("fake turn error %r\n" % e)


def protocol_answer(text, answer):
    """outputSchema(agent-output/1)를 받은 턴의 최종 답 — 진짜 Codex 처럼 규약 JSON 하나로 낸다(가짜 Claude 도 쓴다).
    요청에 "카드"가 있으면 할 일·정할 것·메인 반영을 채운다."""
    obj = {"protocol": "agent-output/1", "body": answer, "todos": [], "decisions": [], "merge": None}
    if "카드" in text:
        obj["todos"] = [{"id": "t1", "title": "데모 서버 내리기", "detail": "확인이 끝나면 내린다", "owner": "agent", "project": None}]
        obj["decisions"] = [{"id": "d1", "header": "적용 범위", "question": "규약을 기존 세션에도 걸까요?",
                             "options": [{"label": "새 세션만", "description": "기존 세션의 조건을 지킨다", "recommended": True},
                                         {"label": "모든 세션", "description": None, "recommended": False}],
                             "multiSelect": False, "allowOther": True}]
        obj["merge"] = {"summary": "최종 답 규약을 앱이 직접 강제한다", "bump": "minor", "version": None, "checks": ["단위 시험"]}
    return json.dumps(obj, ensure_ascii=False)


def fake_answer(text, steers=()):
    """요청 글에 맞는 가짜 답 — 가짜 Claude(fake_claude.py)도 같이 쓴다. steers: 끼워 넣은 말 글."""
    if text.startswith("[FDE 케이스 — codex-lens"):
        answer = fake_fde(text)
    elif "[codex-lens · 문서 다듬기]" in text:
        n = fake_refine(text)
        answer = "코멘트 %d개를 반영했습니다.\n\n1. 고른 부분 끝에 **다듬음** 표시를 붙였습니다." % n
    elif "[요청 다듬기" in text and '"text":"다듬은 글 전체"' in text:   # 좋은 글 목적
        src = text.split("[원문]\n", 1)[-1].split("\n\n---\n", 1)[0]
        custom = " (고친 지시)" if "고친 지시로 다듬어" in text else ""
        answer = json.dumps({"text": "다듬은 글: " + src.strip() + custom,
                             "editing_notes": [{"quote": src.strip().split("\n")[0][:40], "note": "이 말이 무엇을 가리키는지 확인하세요."}]},
                            ensure_ascii=False)
    elif "[요청 다듬기" in text:
        src = text.split("[원문]\n", 1)[-1].split("\n\n---\n", 1)[0]
        mentions = [w for w in src.split() if w.startswith("@")]
        extra = " (답변 반영)" if "[추가 질문에 대한 사용자 답변]" in text else ""
        answer = json.dumps({"request": "목표: " + src.strip().split("\n")[0][:200] + extra + "\n\n완료 기준: 테스트가 통과한다."
                             + ("\n참고: " + " ".join(mentions) if mentions else ""),
                             "missing_context": [] if extra else [{"question": "어느 파일을 고칠까요?", "purpose": "수정 범위를 정한다",
                                                                   "options": ["calc.py", "README.md"]}],
                             "opinion": "범위가 분명해 바로 진행해도 됩니다.", "answers": []}, ensure_ascii=False)
        if "망가진 답" in src:
            answer = "형식을 지키지 않은 답"
    elif "[과정 요약 요청" in text:
        answer = "**한 줄 요약** — 파일 목록을 보고 답했습니다.\n\n### 단계\n1. `ls -la` 로 폴더를 살폈습니다.\n\n### 바뀐 것\n- 없음"
    elif "[인라인 질문" in text:
        answer = "고른 부분은 **계산기 예제**를 뜻합니다. 질문: " + text.split("질문:")[-1].split("\n")[0].strip()
    else:
        answer = "작업을 마쳤습니다.\n\n- `calc.py` 의 나눗셈을 고쳤습니다.\n- README 에 사용법을 적었습니다.\n\n받은 요청: " + text.split("\n")[0][:80]
        for st in steers:
            answer += "\n\n끼워 넣은 말도 읽었습니다: " + st[:80]
        answer *= int(os.environ.get('FAKE_STREAM_REPEAT') or 1)
    return answer


def fake_fde(text):
    """FDE 케이스 단계 흉내(understand · plan · ask) — 형식에 맞는 JSON. 피드백이 있으면 요지 앞에 '피드백 반영 · '."""
    step = text.split("step=", 1)[1].split("]", 1)[0]
    fb = "피드백 반영 · " if "[사용자 피드백" in text else ""
    if "망가진 답" in text:
        return "형식을 지키지 않은 답"
    if step == "understand":
        out = {"headline": fb + "점검원마다 근무 시간 안에 맡은 설비를 모두 도는 경로를 만드는 문제",
               "summary": ["입력은 점검원 · 설비 · 이동 시간 표", "규칙은 하루 12대와 근무 시간", "좋은 경로의 기준은 아직 정해야 한다"],
               "detail": "### 문제\n- 점검원별 경로와 시간표\n### 지킬 규칙\n- 하루 최대 12대\n### 아직 모르는 것\n- 점검 한 건에 걸리는 시간",
               "sources": ["C1"],
               "choices": [{"question": "총 이동 시간과 점검원 사이 업무량 균형 중 무엇을 먼저 볼까요?", "why": "최적화 목표의 순서를 정한다",
                            "options": [{"label": "이동 시간 먼저", "effect": "이동 비용이 준다", "recommended": True},
                                        {"label": "업무량 균형 먼저", "effect": "점검원 사이 차이가 준다"}]}]}
    elif step == "plan":
        v = text.count("[계획 v")
        out = {"headline": fb + "설비 묶기 → 경로 순서 → 시간표, 핵심은 경로 구성" + (" (개선)" if v else ""),
               "summary": ["경로 구성이 결과의 질을 정한다", "검증기는 생성 코드와 분리한다"], "detail": "### 왜 이 순서인가\n묶음이 정해져야 순서를 다듬을 수 있다.",
               "sources": ["C1"],
               "modules": [{"name": "데이터 검증", "role": "입력 무결성 확인"}, {"name": "경로 구성", "role": "한도 · 근무 시간을 지키며 묶고 순서 정하기", "main": True},
                           {"name": "독립 검증기", "role": "규칙 위반 세기"}],
               "algorithm": {"name": "탐욕 묶기 + 경계 재배정" if v else "탐욕 묶기 + 지역 개선",
                             "idea": "가까운 설비부터 하루 한도까지 묶고, 2-opt 로 순서를 다듬는다." + (" 경계 설비를 이웃 경로로 옮겨 본다." if v else ""),
                             "steps": ["설비 군집", "한도까지 탐욕 배정", "2-opt 순서 개선"], "weaknesses": ["군집 경계에서 점검원이 더 필요할 수 있다"]},
               "criteria": [{"name": "규칙 위반", "metric": "violations", "why": "반드시 0"}, {"name": "총 이동 시간", "metric": "travel", "why": "이동 비용"},
                            {"name": "구현 난이도", "metric": "", "why": "시간 제한"}],
               "options": [{"name": "탐욕 + 지역 개선", "summary": "빠르고 단순", "eval": {"규칙 위반": "0 보장", "총 이동 시간": "보통", "구현 난이도": "쉬움"}},
                           {"name": "정수 계획법", "summary": "최적에 가깝지만 느림", "eval": {"규칙 위반": "0 보장", "총 이동 시간": "좋음", "구현 난이도": "어려움"}}],
               "checks": [{"name": "규칙 위반 세기", "command": "printf 'METRIC violations=0\\nMETRIC travel=%d\\n'" % (380 if v else 412)},
                          {"name": "단위 시험", "command": "true"}],
               "change": "경계 재배정을 더했다 — travel 을 줄이는 것이 목표" if v else "",
               "choices": [{"question": "어느 해결 방법으로 갈까요?", "why": "구현할 방법을 정한다",
                            "options": [{"label": "탐욕 + 지역 개선", "effect": "오늘 안에 검증까지 끝낸다", "recommended": True},
                                        {"label": "정수 계획법", "effect": "품질이 오르지만 구현 위험이 크다"}]}]}
    else:
        q = text.split("[질문]\n", 1)[-1].split("\n\n---", 1)[0].strip()
        out = {"headline": "답: " + q[:40], "summary": [], "detail": "**" + q[:200] + "** — 이해 카드의 규칙을 보면 된다.", "choices": []}
    return json.dumps(out, ensure_ascii=False)


def fake_refine(text):
    """다듬기 요청 흉내 — 코멘트가 인용한 첫 줄마다 끝에 ' (다듬음)' 을 붙인다."""
    path = next((l[len("- 문서: "):] for l in text.split("\n") if l.startswith("- 문서: ")), None)
    if not path or not os.path.isfile(path):
        return 0
    with open(path, encoding="utf-8") as f:
        doc = f.read()
    quotes, n = [], 0
    lines = text.split("\n")
    for i, l in enumerate(lines):
        if l.startswith("## 코멘트") and i + 1 < len(lines) and lines[i + 1].startswith("> "):
            quotes.append(lines[i + 1][2:])
    for q in quotes:
        if q and q in doc:
            doc = doc.replace(q, q + " (다듬음)", 1)
            n += 1
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return n


def handle(req):
    m, p, rid = req.get("method"), req.get("params") or {}, req.get("id")
    log(m, p)
    if rid is None:
        return
    def ok(result):
        send({"jsonrpc": "2.0", "id": rid, "result": result})
    def err(msg):
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": msg}})
    with lock:
        if m == "initialize":
            return ok({"userAgent": "fake", "codexHome": os.environ.get("CODEX_HOME", "/fake"), "platformFamily": "unix", "platformOs": "linux"})
        if m == "model/list":
            return ok({"data": [
                {"id": "gpt-fake-sol", "model": "gpt-fake-sol", "displayName": "GPT-Fake-Sol", "description": "기본 모델", "hidden": False,
                 "isDefault": True, "supportedReasoningEfforts": [{"reasoningEffort": e, "description": ""} for e in ("low", "medium", "high")],
                 "defaultReasoningEffort": "medium",
                 "serviceTiers": [{"id": "priority", "name": "Fast", "description": "2x speed, increased usage"}]},
                {"id": "gpt-fake-hidden", "model": "gpt-fake-hidden", "displayName": "숨은 모델", "description": "", "hidden": True,
                 "isDefault": False, "supportedReasoningEfforts": [], "defaultReasoningEffort": "low"},
                {"id": "gpt-fake-luna", "model": "gpt-fake-luna", "displayName": "GPT-Fake-Luna", "description": "가벼운 모델", "hidden": False,
                 "isDefault": False, "supportedReasoningEfforts": [{"reasoningEffort": "low", "description": ""}], "defaultReasoningEffort": "low"}],
                "nextCursor": None})
        if m == "config/read":
            return ok({"config": {"model": "gpt-fake-sol", "model_reasoning_effort": "high"}, "origins": {}})
        if m == "thread/list":
            excl = set(p.get("excludedThreadIds") or [])
            items = [t for t in threads.values() if t["id"] not in excl and not t.get("_ephemeral")]
            if p.get("searchTerm"):
                items = [t for t in items if p["searchTerm"].lower() in ((t.get("name") or "") + (t.get("preview") or "")).lower()]
            if p.get("cwd"):
                items = [t for t in items if t.get("cwd") == p["cwd"]]
            items.sort(key=lambda t: t["updatedAt"], reverse=True)
            lim = p.get("limit") or 50
            off = int(p.get("cursor") or 0)
            more = len(items) > off + lim
            return ok({"data": [public(t) for t in items[off:off + lim]], "nextCursor": str(off + lim) if more else None,
                       "backwardsCursor": None})
        if m == "thread/read":
            # 느린 기록 읽기 흉내(데모의 읽기 과정 표시용) — 전체 턴 읽기만 늦춘다
            if p.get("includeTurns") and float(os.environ.get("FAKE_CODEX_READ_SEC") or 0):
                time.sleep(float(os.environ["FAKE_CODEX_READ_SEC"]))
            t = threads.get(p.get("threadId"))
            if not t:
                return err("thread not found: %s" % p.get("threadId"))
            if time.monotonic() < t.get("_persist_at", 0):
                return err("failed to read thread: thread-store internal error: failed to read session metadata /fake/rollout.jsonl: rollout at /fake/rollout.jsonl is empty")
            return ok({"thread": public(t, p.get("includeTurns"))})
        if m == "thread/start":
            t = new_thread(p.get("cwd") or "/tmp", p.get("model"))
            t["_ephemeral"] = bool(p.get("ephemeral"))
            t["_persist_at"] = time.monotonic() + float(os.environ.get("FAKE_CODEX_PERSIST_DELAY") or 0)
            return ok({"thread": public(t), "model": t["model"]})
        if m == "thread/resume":
            t = threads.get(p.get("threadId"))
            if not t:
                return err("no rollout found for thread id %s" % p.get("threadId"))
            return ok({"thread": public(t), "model": t["model"]})
        if m == "thread/fork":
            src = threads.get(p.get("threadId"))
            if not src:
                return err("thread not found")
            turns, last = [], p.get("lastTurnId")
            for tu in src.get("turns", []):
                if tu.get("status") == "inProgress" and tu["id"] == last:
                    return err("turn is in progress")
                turns.append(tu)
                if tu["id"] == last:
                    break
            t = new_thread(src["cwd"], p.get("model") or "gpt-fake-default", forked=src["id"], turns=json.loads(json.dumps(turns)))
            t["preview"] = src.get("preview")
            t["_ephemeral"] = bool(p.get("ephemeral"))
            return ok({"thread": public(t), "model": t["model"]})
        if m == "turn/start":
            t = threads.get(p.get("threadId"))
            if not t:
                return err("thread not loaded")
            text = "\n".join(i.get("text", "") for i in p.get("input") or [] if i.get("type") == "text")
            if not t.get("preview"):
                t["preview"] = text.split("\n")[0][:200]
            if p.get("model"):
                t["model"] = p["model"]
            turn = {"id": tid(), "status": "inProgress", "startedAt": now(), "completedAt": None, "durationMs": None, "error": None,
                    "items": [{"type": "userMessage", "id": uuid.uuid4().hex[:8], "clientId": p.get("clientUserMessageId"), "content": [dict(i) for i in p.get("input") or []]}]}
            if p.get("outputSchema"):
                turn["_schema"] = True   # 최종 메시지를 규약 JSON 으로(진짜는 제약 디코딩)
            t.setdefault("turns", []).append(turn)
            t["status"] = {"type": "active", "activeFlags": []}
            threading.Thread(target=run_turn, args=(t, turn, text, p.get("model") or t["model"]), daemon=True).start()
            return ok({"turn": {k: v for k, v in turn.items() if k != "items"}})
        if m == "turn/steer":
            t = threads.get(p.get("threadId"))
            cur = next((tu for tu in (t or {}).get("turns", []) if tu.get("status") == "inProgress"), None)
            if not cur or cur.get("_steer_closed"):
                return err("no active turn to steer")
            if cur["id"] != p.get("expectedTurnId"):
                return err("expected turn %s but active turn is %s" % (p.get("expectedTurnId"), cur["id"]))
            item = {"type": "userMessage", "id": uuid.uuid4().hex[:8], "clientId": p.get("clientUserMessageId"),
                    "content": [dict(i) for i in p.get("input") or []]}
            cur.setdefault("_steers", []).append(item)
            note("item/started", {"threadId": t["id"], "turnId": cur["id"], "item": item})
            note("item/completed", {"threadId": t["id"], "turnId": cur["id"], "item": item})
            return ok({"turnId": cur["id"]})
        if m == "turn/interrupt":
            t = threads.get(p.get("threadId"))
            for tu in (t or {}).get("turns", []):
                if tu["id"] == p.get("turnId"):
                    tu["_interrupt"] = True
            return ok({})
        if m == "thread/unsubscribe":
            return ok({})
        if m == "thread/turns/list":
            t = settled(threads.get(p.get("threadId")) or {})
            turns = list(t.get("turns", []))
            if p.get("sortDirection") != "asc":
                turns.reverse()   # 진짜 app-server 처럼 기본은 최신 턴부터
            if p.get("limit"):
                turns = turns[:p["limit"]]
            if p.get("itemsView") == "notLoaded":
                turns = [dict(tu, items=[]) for tu in turns]
            return ok({"data": turns, "nextCursor": None})
        return err("fake: unsupported method %s" % m)


def main():
    load_seed()
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        slow_read = req.get("method") == "thread/read" and float(os.environ.get("FAKE_CODEX_READ_SEC") or 0)
        threading.Thread(target=handle, args=(req,), daemon=True).start() if req.get("method") == "turn/start" or slow_read else handle(req)


if __name__ == "__main__":
    main()
