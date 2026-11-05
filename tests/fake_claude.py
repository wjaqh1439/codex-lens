#!/usr/bin/env python3
"""테스트·브라우저 회귀용 가짜 `claude -p --input-format stream-json --output-format stream-json` — 진짜 모델·~/.claude 를
건드리지 않는다. 기록은 $CLAUDE_CONFIG_DIR/projects/<작업 폴더 이름>/<세션 ID>.jsonl 에 진짜와 같은 모양으로 쓴다.

턴 흉내: 생각 → Bash `ls` 도구(결과) → 답(스트리밍). 포크(읽기 전용)는 도구를 쓰지 않는다.
- 요청에 "천천히"가 있으면 도구 결과 전에 FAKE_CLAUDE_SLOW_SEC(기본 1.5)초 기다리며 끼워 넣은 말·멈춤을 받는다.
- 이어받은(--resume) 세션을 같은 모델로 돌리면 cache_read_input_tokens 가 0보다 크다(캐시 재사용 흉내).
- 호출 기록: FAKE_CLAUDE_LOG(JSON Lines)에 argv·작업 폴더를 남긴다.
"""

import glob
import json
import os
import queue
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_app_server import fake_answer, protocol_answer  # noqa: E402

ROOT = os.path.abspath(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude-fake"))
DELAY = float(os.environ.get("FAKE_CLAUDE_DELAY") or 0.03)
out_lock = threading.Lock()


def iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def send(obj):
    with out_lock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def arg(argv, name):
    if name in argv:
        i = argv.index(name)
        return argv[i + 1] if i + 1 < len(argv) else None
    return None


class Session:
    def __init__(self, sid, path, persist, model):
        self.sid, self.path, self.persist, self.model = sid, path, persist, model
        self.last = None
        self.cwd = os.path.realpath(os.getcwd())
        if path and os.path.exists(path):
            with open(path) as f:
                for line in f:
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    if o.get("uuid"):
                        self.last = o["uuid"]
                    m = (o.get("message") or {}).get("model") if o.get("type") == "assistant" else None
                    if m:
                        self.file_model = m

    def rec(self, typ, **kw):
        r = {"parentUuid": self.last, "isSidechain": False, "type": typ, "uuid": kw.pop("uuid", None) or str(uuid.uuid4()),
             "timestamp": iso(), "userType": "external", "entrypoint": "sdk-cli", "cwd": self.cwd, "sessionId": self.sid,
             "version": "fake", "gitBranch": "main"}
        r.update(kw)
        self.last = r["uuid"]
        self.write(r)
        return r

    def write(self, r):
        if not self.persist:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main():
    argv = sys.argv[1:]
    if os.environ.get("FAKE_CLAUDE_LOG"):
        with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
            f.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "thread_env": os.environ.get("CODEX_THREAD_ID")},
                               ensure_ascii=False) + "\n")
    model = arg(argv, "--model") or "claude-fake-opus"
    resume, sid = arg(argv, "--resume"), arg(argv, "--session-id")
    persist = "--no-session-persistence" not in argv
    read_only = arg(argv, "--permission-mode") == "dontAsk"
    schema = "--json-schema" in argv   # 진짜처럼 최종 답을 StructuredOutput 도구 호출로 낸다
    cwd = os.path.realpath(os.getcwd())
    pdir = os.path.join(ROOT, "projects", re.sub(r"[^A-Za-z0-9]", "-", cwd))
    if resume:
        hits = glob.glob(os.path.join(ROOT, "projects", "*", resume + ".jsonl"))
        if not hits:
            send({"type": "result", "subtype": "error_during_execution", "is_error": True,
                  "errors": ["No conversation found with session ID: %s" % resume]})
            return 1
        sid, path = resume, hits[0]
    else:
        sid = sid or str(uuid.uuid4())
        path = os.path.join(pdir, sid + ".jsonl")
    ses = Session(sid, path, persist, model)
    resumed_same_model = bool(resume) and getattr(ses, "file_model", None) == model

    inbox = queue.Queue()

    def reader():
        for line in sys.stdin:
            try:
                inbox.put(json.loads(line))
            except ValueError:
                pass
        inbox.put(None)
    threading.Thread(target=reader, daemon=True).start()

    send({"type": "system", "subtype": "init", "session_id": sid, "model": model, "cwd": cwd, "uuid": str(uuid.uuid4())})
    first = True
    while True:
        msg = inbox.get()
        if msg is None:
            return 0
        if msg.get("type") != "user":
            continue
        text = "\n".join(b.get("text") or "" for b in msg["message"]["content"] if b.get("type") == "text")
        uid = msg.get("uuid") or str(uuid.uuid4())
        send(dict(msg, isReplay=True, session_id=sid, uuid=uid))
        ses.rec("user", uuid=uid, promptId=str(uuid.uuid4()), turnPosition={"promptIndex": 0, "turnIndex": 1},
                message={"role": "user", "content": msg["message"]["content"]})
        if first and persist and not resume:
            ses.write({"type": "ai-title", "aiTitle": text.split("\n")[0][:40], "sessionId": sid})
        first = False
        # 1) 생각 + 도구
        m1 = "msg_" + uuid.uuid4().hex[:10]
        send({"type": "stream_event", "event": {"type": "message_start", "message": {"id": m1}}})
        send({"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}}})
        send({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                                "delta": {"type": "thinking_delta", "thinking": "**살펴보기** 요청을 확인한다."}}})
        th = {"id": m1, "model": model, "content": [{"type": "thinking", "thinking": "**살펴보기** 요청을 확인한다."}]}
        send({"type": "assistant", "message": th, "session_id": sid, "uuid": str(uuid.uuid4())})
        ses.rec("assistant", message=th, apiBlockIndex=0)
        time.sleep(DELAY)
        steers, interrupted = [], False
        if not read_only:
            tool_id = "toolu_" + uuid.uuid4().hex[:12]
            send({"type": "stream_event", "event": {"type": "content_block_start", "index": 1,
                                                    "content_block": {"type": "tool_use", "id": tool_id, "name": "Bash"}}})
            tu = {"id": m1, "model": model, "content": [{"type": "tool_use", "id": tool_id, "name": "Bash",
                                                         "input": {"command": "ls", "description": "폴더 보기"}}],
                  "stop_reason": "tool_use"}
            send({"type": "assistant", "message": tu, "session_id": sid, "uuid": str(uuid.uuid4())})
            ses.rec("assistant", message=tu, apiBlockIndex=1)
            wait = float(os.environ.get("FAKE_CLAUDE_SLOW_SEC") or 1.5) if "천천히" in text else DELAY
            end = time.time() + wait
            while time.time() < end:
                try:
                    m = inbox.get(timeout=max(0.01, end - time.time()))
                except queue.Empty:
                    break
                if m is None:
                    break
                if m.get("type") == "control_request" and (m.get("request") or {}).get("subtype") == "interrupt":
                    send({"type": "control_response", "response": {"subtype": "success", "request_id": m.get("request_id")}})
                    interrupted = True
                    break
                if m.get("type") == "user":
                    steers.append(m)
            if interrupted:
                ses.rec("user", message={"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user for tool use]"}]})
                send({"type": "result", "subtype": "error_during_execution", "is_error": True, "terminal_reason": "aborted_tools",
                      "session_id": sid, "usage": {"input_tokens": 10, "output_tokens": 5}})
                return 0
            result = {"type": "tool_result", "tool_use_id": tool_id, "content": "README.md\ncalc.py", "is_error": False}
            um = {"role": "user", "content": [result]}
            send({"type": "user", "message": um, "session_id": sid, "uuid": str(uuid.uuid4()),
                  "tool_use_result": {"stdout": "README.md\ncalc.py", "stderr": "", "interrupted": False}})
            ses.rec("user", message=um, toolUseResult={"stdout": "README.md\ncalc.py", "stderr": "", "interrupted": False})
        steer_texts = []
        for st in steers:
            st_text = "\n".join(b.get("text") or "" for b in st["message"]["content"] if b.get("type") == "text")
            steer_texts.append(st_text)
            send(dict(st, isReplay=True, session_id=sid))
            ses.rec("attachment", attachment={"type": "queued_command", "prompt": st["message"]["content"],
                                              "source_uuid": st.get("uuid"), "commandMode": "prompt"})
        # 2) 답
        answer = fake_answer(text, steer_texts)
        m2 = "msg_" + uuid.uuid4().hex[:10]
        send({"type": "stream_event", "event": {"type": "message_start", "message": {"id": m2}}})
        if schema:
            structured_answer(ses, sid, model, m2, protocol_answer(text, answer), resumed_same_model)
            continue
        send({"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}})
        for i in range(0, len(answer), 16):
            if i == 128:
                time.sleep(float(os.environ.get('FAKE_STREAM_PAUSE_SEC') or 0))
            send({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                                    "delta": {"type": "text_delta", "text": answer[i:i + 16]}}})
            time.sleep(float(os.environ.get('FAKE_STREAM_DELAY') or DELAY / 5))
        am = {"id": m2, "model": model, "content": [{"type": "text", "text": answer}], "stop_reason": "end_turn",
              "usage": {"input_tokens": 12, "cache_creation_input_tokens": 200,
                        "cache_read_input_tokens": 1000 if resumed_same_model else 0, "output_tokens": 80}}
        send({"type": "assistant", "message": am, "session_id": sid, "uuid": str(uuid.uuid4())})
        ses.rec("assistant", message=am, apiBlockIndex=0)
        ses.rec("system", subtype="stop_hook_summary")
        send({"type": "result", "subtype": "success", "is_error": False, "result": answer, "session_id": sid,
              "terminal_reason": "completed",
              "modelUsage": {model: {"contextWindow": 200000}},
              "usage": {"input_tokens": 12, "cache_creation_input_tokens": 200,
                        "cache_read_input_tokens": 1000 if resumed_same_model else 0, "output_tokens": 80,
                        "output_tokens_details": {"thinking_tokens": 10}}})


def structured_answer(ses, sid, model, mid, raw, resumed_same_model):
    """--json-schema 로 띄운 턴의 답 — StructuredOutput 도구 입력이 흘러오고, 도구 결과·result.structured_output 이 따른다."""
    tool_id = "toolu_" + uuid.uuid4().hex[:12]
    send({"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
                                            "content_block": {"type": "tool_use", "id": tool_id, "name": "StructuredOutput", "input": {}}}})
    for i in range(0, len(raw), 24):
        send({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                                "delta": {"type": "input_json_delta", "partial_json": raw[i:i + 24]}}})
        time.sleep(float(os.environ.get('FAKE_STREAM_DELAY') or DELAY / 5))
    obj = json.loads(raw)
    usage = {"input_tokens": 12, "cache_creation_input_tokens": 200,
             "cache_read_input_tokens": 1000 if resumed_same_model else 0, "output_tokens": 80}
    am = {"id": mid, "model": model, "content": [{"type": "tool_use", "id": tool_id, "name": "StructuredOutput", "input": obj}],
          "stop_reason": "tool_use", "usage": usage}
    send({"type": "assistant", "message": am, "session_id": sid, "uuid": str(uuid.uuid4())})
    ses.rec("assistant", message=am, apiBlockIndex=0)
    result = {"type": "tool_result", "tool_use_id": tool_id, "content": "Structured output provided successfully"}
    um = {"role": "user", "content": [result]}
    send({"type": "user", "message": um, "session_id": sid, "uuid": str(uuid.uuid4())})
    ses.rec("user", message=um, toolUseResult="Structured output provided successfully")
    ses.rec("system", subtype="stop_hook_summary")
    send({"type": "result", "subtype": "success", "is_error": False, "result": "", "structured_output": obj, "session_id": sid,
          "terminal_reason": "completed", "modelUsage": {model: {"contextWindow": 200000}},
          "usage": dict(usage, output_tokens_details={"thinking_tokens": 10})})


if __name__ == "__main__":
    sys.exit(main() or 0)
