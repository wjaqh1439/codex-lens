"""Claude Code 기록(`<설정 폴더>/projects/<프로젝트>/<세션 ID>.jsonl`)을 Codex app-server 모양으로 바꾼다.

화면·포크·요약 코드는 Codex 의 Thread·Turn·Item 모양만 안다. 여기서 Claude 기록을 그 모양으로 바꿔 두면
turns.normalize_turn 이 두 에이전트를 같은 화면(요청 · 과정 · 답변)으로 만든다.

- 턴: 사람이 보낸 요청(user 기록, 도구 결과·메타가 아닌 것) 하나에서 다음 요청 전까지. 턴 ID = 그 요청 기록의 uuid.
  이 앱이 보낸 요청은 stream-json 입력의 uuid 를 그대로 기록하므로 실행 중 모은 턴과 ID 가 같다.
- 진행 중에 끼워 넣은 말: `queued_command` 첨부(또는 promptIndex > 0 인 요청) → 그 턴의 두 번째 이후 userMessage.
- 백그라운드 작업 알림(`<task-notification>`, origin.kind = task-notification)은 사람이 보낸 말이 아니다 —
  턴 경계는 Claude 와 같게 두되 userMessage 에 `lensNotice` 를 붙여 화면이 "알림"으로 그리게 한다.
- 항목 ID: 글·생각 블록은 `<메시지 ID>:<블록 번호>`, 도구는 tool_use ID — 실행 중 스트림과 같은 규칙이다.
- 가지(되감기)로 버려진 기록은 마지막 기록에서 parentUuid 를 거슬러 닿는 것만 남겨 뺀다.
"""

import difflib
import json
import os
import re
import subprocess
import time
from datetime import datetime

from turns import quiet_until

AGENT_OUTPUT = "agent-output"
INTERRUPT_MARKS = ("[Request interrupted by user", "[Request cancelled by user")
BASH_TIMEOUT_MS = 120000  # Claude Code Bash 도구의 기본 제한 시간(입력에 timeout 이 없을 때)
LIVE_GRACE_SEC = 300      # 다른 곳(CLI)에서 도는 세션: 마지막 기록이 도구 단계이고 이만큼 안에 바뀌었으면 진행 중으로 본다
HEAD_BYTES = 128 * 1024
TAIL_BYTES = 64 * 1024
DIFF_LINES = 400


def ts_of(s):
    """ISO 시각 → 초(float). 실패하면 None."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def project_dir_name(cwd):
    return re.sub(r"[^A-Za-z0-9]", "-", cwd or "")


def _loads(line):
    try:
        o = json.loads(line)
    except (ValueError, TypeError):
        return None
    return o if isinstance(o, dict) else None


def read_records(path):
    out = []
    with open(path, "rb") as f:
        for raw in f:
            o = _loads(raw)
            if o is not None:
                out.append(o)
    return out


# --- 하위 에이전트 기록 ---------------------------------------------------------
# 앞(foreground) 하위 에이전트가 도는 동안 본 기록은 조용하고, 하위 에이전트 기록
# `<세션 ID>/subagents/agent-*.jsonl`(+ 같은 이름의 `.meta.json`, 부른 tool_use ID 가 `toolUseId`)만 자란다.
def _subagent_files(path):
    sub = os.path.join(path[:-len(".jsonl")], "subagents")
    try:
        names = os.listdir(sub)
    except OSError:
        return []
    return [os.path.join(sub, n) for n in names if n.endswith(".jsonl")]


def activity_mtime(path, mtime=None):
    """세션 기록과 그 하위 에이전트 기록 가운데 가장 늦게 바뀐 시각."""
    if mtime is None:
        mtime = os.path.getmtime(path)
    for f in _subagent_files(path):
        try:
            mtime = max(mtime, os.path.getmtime(f))
        except OSError:
            pass
    return mtime


def _tail_records(path, nbytes=TAIL_BYTES):
    with open(path, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - nbytes))
        buf = f.read()
    return list(_lines(buf, drop_first=size > nbytes))


def subagent_progress(path, tool_ids):
    """tool_use ID → 그 하위 에이전트의 진행(도구 수·마지막 단계·마지막 기록 시각). 기록이 없으면 빠진다."""
    out = {}
    if not tool_ids:
        return out
    for f in _subagent_files(path):
        try:
            with open(f[:-len(".jsonl")] + ".meta.json", encoding="utf-8") as mf:
                tid = (json.load(mf) or {}).get("toolUseId")
        except (OSError, ValueError, AttributeError):
            continue
        if tid not in tool_ids:
            continue
        try:
            st = os.stat(f)
            recs = _tail_records(f)
        except OSError:
            continue
        last, steps = None, 0
        for r in recs:
            if r.get("type") != "assistant":
                continue
            for b in _content_blocks(r.get("message")):
                if b.get("type") == "tool_use":
                    steps += 1
                    last = _tool_title(b.get("name") or "도구", b.get("input"))
                elif b.get("type") == "text" and (b.get("text") or "").strip():
                    last = (b.get("text") or "").strip().split("\n")[0][:80]
        out[tid] = {"last": last, "at": int(st.st_mtime), "recent_tools": steps}
    return out


# --- 프로세스 명부 ----------------------------------------------------------------
# Claude Code 는 떠 있는 동안 `<설정 폴더>/sessions/<pid>.json`({pid, sessionId, status, version, procStart …})을
# 두고 끝나면 지운다. 기록만으로는 '조용히 긴 명령을 도는 중'과 '프로세스가 꺼짐'을 가를 수 없어 이걸 같이 본다.
# 형식이 문서화된 것은 아니라, 읽지 못하면 모름(None)으로 두고 시간 유예 판정으로 돌아간다.
_REG_CACHE = {}          # 설정 폴더 -> (읽은 시각, 명부)
_ALIVE_CACHE = {}        # (pid, procStart) -> (확인 시각, 살아 있음)
REG_TTL_SEC = 2.0
NO_PROC_QUIET_SEC = 30   # 명부에 없는 세션: 이만큼 조용해야 꺼진 것으로 본다(띄우는 중·세션 바꾸는 중 경합)


def _start_time(s):
    try:
        return datetime.strptime(" ".join(str(s).split()), "%a %b %d %H:%M:%S %Y")
    except (TypeError, ValueError):
        return None


def _pid_alive(pid, proc_start=None):
    """pid 가 살아 있고, 명부의 procStart(UTC, `ps -o lstart` 모양)와 시작 시각이 같으면 True.
    시작 시각을 견줄 수 없으면(형식이 다름·ps 없음) pid 생존만 본다 — pid 가 재사용된 낡은 명부를 거른다."""
    key = (pid, proc_start)
    hit = _ALIVE_CACHE.get(key)
    if hit and time.time() - hit[0] < 10:
        return hit[1]
    try:
        os.kill(int(pid), 0)
        alive = True
    except PermissionError:
        alive = True
    except (OSError, ValueError, TypeError):
        alive = False
    want = _start_time(proc_start) if alive else None
    if want is not None:
        try:
            out = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True,
                                 timeout=2, env=dict(os.environ, TZ="UTC", LC_ALL="C")).stdout
            got = _start_time(out)
            if got is not None and abs((got - want).total_seconds()) > 2:
                alive = False
        except (OSError, subprocess.SubprocessError):
            pass
    _ALIVE_CACHE[key] = (time.time(), alive)
    if len(_ALIVE_CACHE) > 256:
        _ALIVE_CACHE.pop(next(iter(_ALIVE_CACHE)))
    return alive


def proc_registry(root):
    """{"sessions": {세션 ID: [{pid, status, start}]}, "versions": {명부를 남기는 버전}}. 명부 폴더가 없으면 None.
    생존 확인(_pid_alive)은 물어본 세션의 것만 proc_state 에서 한다."""
    d = os.path.join(root, "sessions")
    hit = _REG_CACHE.get(root)
    if hit and time.time() - hit[0] < REG_TTL_SEC:
        return hit[1]
    try:
        names = [n for n in os.listdir(d) if n.endswith(".json")]
    except OSError:
        reg = None
    else:
        reg = {"sessions": {}, "versions": set()}
        for n in names:
            try:
                with open(os.path.join(d, n), encoding="utf-8") as f:
                    e = json.load(f)
                pid, sid = int(e["pid"]), str(e["sessionId"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if e.get("version"):
                reg["versions"].add(str(e["version"]))
            reg["sessions"].setdefault(sid, []).append({"pid": pid, "status": e.get("status"), "start": e.get("procStart")})
    _REG_CACHE[root] = (time.time(), reg)
    return reg


def proc_state(reg, sid, version=None, mtime=None):
    """이 세션을 돌리는 Claude 프로세스: "alive" | "gone" | None(모름).

    - 명부에 살아 있는 프로세스가 있으면 alive — 기록이 오래 조용해도(긴 명령·하위 에이전트) 돌고 있다.
    - 명부에 있는데 프로세스가 없으면(강제 종료로 남은 명부) gone.
    - 명부에 없으면, 이 기록을 쓴 버전이 명부를 남기는 버전이고 NO_PROC_QUIET_SEC 넘게 조용할 때만 gone.
      명부를 남기지 않는 버전이거나 명부 폴더가 없으면 모름 — 시간 유예로 판정한다."""
    if not reg:
        return None
    procs = reg["sessions"].get(sid) or []
    if any(_pid_alive(p["pid"], p["start"]) for p in procs):
        return "alive"
    if procs:
        return "gone"
    if version and version in reg["versions"] and mtime and time.time() - mtime >= NO_PROC_QUIET_SEC:
        return "gone"
    return None


def subagent_proc(reg, path, meta, version=None, mtime=None):
    """하위 에이전트 기록의 진행: meta 가 끝났다고 하면 gone, 아직이면 부모 세션 프로세스의 상태를 따른다.
    `finished` 를 남기지 않는 버전이면 모름(None) — 부모가 살아 있어도 이 에이전트는 오래전에 끝났을 수 있다."""
    if "finished" not in meta:
        return None
    if meta.get("finished"):
        return "gone"
    parent = os.path.basename(os.path.dirname(os.path.dirname(path)))
    return proc_state(reg, parent, version, mtime)


# --- 요청 글 -------------------------------------------------------------
_CMD = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.S)
_ARGS = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.S)


def _content_blocks(msg):
    c = (msg or {}).get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return [b for b in c or [] if isinstance(b, dict)]


def prompt_of(rec):
    """user 기록이 사람이 보낸 요청이면 (글, 이미지 수) — 도구 결과·메타·로컬 명령 출력이면 None."""
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain") or rec.get("isCompactSummary"):
        return None
    if rec.get("isVisibleInTranscriptOnly"):
        return None
    blocks = _content_blocks(rec.get("message"))
    if any(b.get("type") == "tool_result" for b in blocks):
        return None
    texts = [b.get("text") or "" for b in blocks if b.get("type") == "text"]
    images = image_urls(blocks)
    text = "\n".join(t for t in texts if t)
    if not text.strip() and not images:
        return None
    s = text.lstrip()
    if s.startswith("<local-command-stdout>") or s.startswith("<local-command-stderr>") or s.startswith("Caveat:"):
        return None
    if "<command-name>" in s:
        m = _CMD.search(s)
        a = _ARGS.search(s)
        text = ((m.group(1) if m else "") + (" " + a.group(1) if a and a.group(1) else "")).strip()
        if not text:
            return None
    return text, images


def is_notice(rec):
    """백그라운드 작업(하위 에이전트·백그라운드 명령)이 끝났다고 Claude Code 가 넣은 알림인가."""
    if (rec.get("origin") or {}).get("kind") == "task-notification" or rec.get("turnOrigin") == "task_notification":
        return True
    blocks = _content_blocks(rec.get("message"))
    first = next((b.get("text") or "" for b in blocks if b.get("type") == "text"), "")
    return first.lstrip().startswith("<task-notification>")


def is_interrupt_mark(rec):
    if rec.get("type") != "user":
        return False
    for b in _content_blocks(rec.get("message")):
        if b.get("type") == "text" and (b.get("text") or "").startswith(INTERRUPT_MARKS):
            return True
    return False


def image_urls(blocks):
    """Claude 이미지 블록 → 화면이 보일 주소(base64 는 data URL)."""
    out = []
    for b in blocks or []:
        if not isinstance(b, dict) or b.get("type") != "image":
            continue
        src = b.get("source") or {}
        if src.get("type") == "base64" and src.get("data"):
            out.append("data:%s;base64,%s" % (src.get("media_type") or "image/png", src["data"]))
        elif src.get("url"):
            out.append(src["url"])
        else:
            out.append(None)
    return out


def user_item(iid, text, images=None, client_id=None, notice=False):
    """images: 주소 목록(None 이면 보일 수 없는 이미지) 또는 Codex 입력 항목(image·localImage) 목록."""
    content = [{"type": "text", "text": text}]
    for im in images or []:
        if isinstance(im, dict):
            content.append(im)
        else:
            content.append({"type": "image", "url": im} if im else {"type": "image"})
    it = {"type": "userMessage", "id": iid, "content": content}
    if client_id:
        it["clientId"] = client_id
    if notice:
        it["lensNotice"] = "task-notification"
    return it


# --- 도구 · 블록 → 항목 ---------------------------------------------------------
def _short(s, n=80):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _diff(old, new, path):
    a = (old or "").splitlines()
    b = (new or "").splitlines()
    lines = list(difflib.unified_diff(a, b, "a/" + os.path.basename(path or ""), "b/" + os.path.basename(path or ""),
                                      lineterm="", n=2))
    if len(lines) > DIFF_LINES:
        lines = lines[:DIFF_LINES] + ["… (%d줄 생략)" % (len(lines) - DIFF_LINES)]
    return "\n".join(lines)


def _tool_title(name, inp):
    inp = inp if isinstance(inp, dict) else {}
    for k in ("file_path", "notebook_path", "path", "pattern", "url", "query", "skill", "description", "command"):
        v = inp.get(k)
        if v:
            v = os.path.basename(v) if k in ("file_path", "notebook_path", "path") else v
            return "%s · %s" % (name, _short(v))
    return name


def _int(v):
    try:
        return int(v) if v else None
    except (TypeError, ValueError):
        return None


def tool_item(block, cwd=None):
    """assistant 의 tool_use 블록 → 진행 중 항목(결과는 apply_result 로 채운다)."""
    name = block.get("name") or "도구"
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
    iid = block.get("id")
    base = {"id": iid, "status": "inProgress", "_tool": name}
    if name == "Bash":
        # 뒤에서 돌리는 명령은 바로 돌아오므로 기다릴 시간이 없다
        tmo = None if inp.get("run_in_background") else _int(inp.get("timeout")) or BASH_TIMEOUT_MS
        return dict(base, type="commandExecution", command=inp.get("command") or "", cwd=cwd, exitCode=None,
                    aggregatedOutput="", timeoutMs=tmo)
    if name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        if name == "Write":
            diff = _diff("", inp.get("content") or "", path)
            kind = "add"
        elif name == "MultiEdit":
            diff = "\n".join(_diff(e.get("old_string"), e.get("new_string"), path) for e in inp.get("edits") or []
                             if isinstance(e, dict))
            kind = "update"
        elif name == "NotebookEdit":
            diff = _diff("", inp.get("new_source") or "", path)
            kind = "update"
        else:
            diff = _diff(inp.get("old_string"), inp.get("new_string"), path)
            kind = "update"
        return dict(base, type="fileChange", changes=[{"path": path, "kind": {"type": kind}, "diff": diff}])
    if name == "WebSearch":
        return dict(base, type="webSearch", query=inp.get("query") or "웹 검색")
    if name == "TodoWrite":
        marks = {"completed": "[x]", "in_progress": "[~]"}
        text = "\n".join("- %s %s" % (marks.get(t.get("status"), "[ ]"), t.get("content") or "")
                         for t in inp.get("todos") or [] if isinstance(t, dict))
        return dict(base, type="plan", text=text)
    if name in ("Agent", "Task"):
        return dict(base, type="subAgentActivity", agentPath=inp.get("subagent_type") or inp.get("description") or "에이전트",
                    prompt=((inp.get("description") or "") + "\n\n" + (inp.get("prompt") or "")).strip())
    if name.startswith("mcp__"):
        parts = name.split("__")
        return dict(base, type="mcpToolCall", server=parts[1] if len(parts) > 2 else "mcp",
                    tool=parts[-1], arguments=inp, result=None, error=None)
    return dict(base, type="dynamicToolCall", tool=name, title=_tool_title(name, inp), arguments=inp,
                contentItems="")


def structured_answer(block):
    """StructuredOutput 도구(agent-output 규약의 최종 답) → 답 글. 아니면 None.

    규약 전체를 JSON 그대로 돌려준다 — 본문은 turns.unwrap_output 이, 할 일·결정·메인 반영은 output_report 가 꺼낸다."""
    if block.get("type") != "tool_use" or block.get("name") != "StructuredOutput":
        return None
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
    return json.dumps(inp, ensure_ascii=False, indent=2)


def result_text(block):
    c = block.get("content")
    if isinstance(c, str):
        return c
    out = []
    for b in c or []:
        if isinstance(b, dict):
            if b.get("type") == "text":
                out.append(b.get("text") or "")
            elif b.get("type") == "image":
                out.append("[이미지]")
    return "\n".join(out)


def apply_result(item, block, extra=None):
    """tool_result 블록을 항목에 채워 끝낸다."""
    err = bool(block.get("is_error"))
    text = result_text(block)
    item = dict(item, status="failed" if err else "completed")
    t = item.get("type")
    if t == "commandExecution":
        out = text
        if isinstance(extra, dict) and ("stdout" in extra or "stderr" in extra):
            out = "\n".join(x for x in (extra.get("stdout"), extra.get("stderr")) if x) or text
        m = re.match(r"^Exit code (\d+)", text or "")
        item["exitCode"] = int(m.group(1)) if m else (1 if err else 0)
        item["aggregatedOutput"] = (out or "")[-20000:]
    elif t == "mcpToolCall":
        item["error" if err else "result"] = text
    elif t == "dynamicToolCall":
        item["contentItems"] = text
    elif t == "subAgentActivity":
        if text:
            item["prompt"] = (item.get("prompt") or "") + "\n\n— 결과 —\n" + text[:4000]
        if isinstance(extra, dict) and extra.get("agentId") and not item.get("agentThreadId"):
            item["agentThreadId"] = SUBAGENT_PREFIX + str(extra["agentId"])
    return item


def text_item(iid, text):
    return {"type": "agentMessage", "id": iid, "text": text or ""}


def thinking_item(iid, text):
    return {"type": "reasoning", "id": iid, "summary": [text] if text else [], "content": []}


def strip_private(item):
    return {k: v for k, v in item.items() if not k.startswith("_")}


# --- 기록 → 턴 ---------------------------------------------------------------
def active_chain(records):
    """마지막 기록에서 parentUuid(압축 경계는 logicalParentUuid)를 거슬러 닿는 uuid 집합. 고리를 못 이으면 None."""
    by = {}
    last = None
    for r in records:
        u = r.get("uuid")
        if u and r.get("type") in ("user", "assistant", "system", "attachment") and not r.get("isSidechain"):
            by[u] = r
            last = u
    if not last:
        return None
    seen = set()
    cur = last
    while cur and cur in by and cur not in seen:
        seen.add(cur)
        r = by[cur]
        cur = r.get("parentUuid") or r.get("logicalParentUuid")
    # 고리가 너무 짧게 끊기면(오래된 형식) 전부 쓴다
    return seen if len(seen) * 2 >= len(by) or len(seen) > 50 else None


class TurnBuilder:
    def __init__(self, cwd=None):
        self.cwd = cwd
        self.turns = []
        self.cur = None
        self.tools = {}        # tool_use id -> (turn, item index)
        self.blocks = {}       # message id -> 다음 블록 번호(apiBlockIndex 가 없을 때)
        self.model = None
        self.effort = None
        self.last_ts = None
        self.tail_kind = None  # 마지막 의미 있는 기록: prompt | tool | text | end
        self.grace_until = None

    def _turn(self, tid, ts):
        t = {"id": tid, "status": "completed", "items": [], "startedAt": int(ts) if ts else None,
             "completedAt": int(ts) if ts else None, "error": None, "_ids": {}}
        self.turns.append(t)
        self.cur = t
        return t

    def _put(self, item, ts=None):
        if self.cur is None:
            return
        idx = self.cur["_ids"].get(item["id"])
        if idx is None:
            self.cur["_ids"][item["id"]] = len(self.cur["items"])
            self.cur["items"].append(item)
        else:
            self.cur["items"][idx] = item
        if ts:
            self.cur["completedAt"] = int(ts)

    def prompt(self, iid, text, images, ts, steer=False, notice=False):
        if steer and self.cur is not None:
            self._put(user_item(iid, text, images, notice=notice), ts)
        else:
            t = self._turn(iid, ts)
            t["items"].append(user_item(iid, text, images, notice=notice))
            t["_ids"][iid] = 0
        self.tail_kind = "prompt"

    def block_id(self, msg_id, rec_index):
        if rec_index is None:
            rec_index = self.blocks.get(msg_id, 0)
        self.blocks[msg_id] = rec_index + 1
        return "%s:%d" % (msg_id, rec_index)

    def assistant(self, rec, ts):
        msg = rec.get("message") or {}
        if msg.get("model") == "<synthetic>":
            return
        if msg.get("model"):
            self.model = msg["model"]
        if rec.get("effort"):
            self.effort = rec["effort"]
        if self.cur is None:
            return
        mid = msg.get("id") or rec.get("uuid")
        blocks = _content_blocks(msg)
        api_idx = rec.get("apiBlockIndex")
        for n, b in enumerate(blocks):
            idx = api_idx if (api_idx is not None and len(blocks) == 1) else None
            t = b.get("type")
            if t == "text":
                self._put(text_item(self.block_id(mid, idx), b.get("text")), ts)
                self.tail_kind = "text"
            elif t == "thinking" or t == "redacted_thinking":
                self._put(thinking_item(self.block_id(mid, idx), b.get("thinking") or ""), ts)
            elif t == "tool_use":
                self.block_id(mid, idx)
                ans = structured_answer(b)
                if ans is not None:
                    # 최종 답이다 — 턴이 아직 끝나지 않은 것으로 보여도 과정이 아니라 답변 자리에 둔다
                    self._put(dict(text_item(b.get("id"), ans), phase="final_answer"), ts)
                    self.tools[b.get("id")] = None
                    self.tail_kind = "text"
                    continue
                self._put(dict(tool_item(b, self.cwd), startedAt=ts), ts)
                self.tools[b.get("id")] = self.cur
                self.tail_kind = "tool"
        if rec.get("isApiErrorMessage") or msg.get("error"):
            text = " ".join(b.get("text") or "" for b in blocks if b.get("type") == "text").strip()
            self.cur["error"] = text or str(msg.get("error") or "API 오류")
            self.cur["status"] = "failed"
        if msg.get("stop_reason") == "end_turn":
            self.tail_kind = "end"

    def tool_results(self, rec, ts):
        tail = "tool"
        for b in _content_blocks(rec.get("message")):
            if b.get("type") != "tool_result":
                continue
            tid = b.get("tool_use_id")
            if tid in self.tools and self.tools[tid] is None:
                # StructuredOutput(agent-output 최종 답)의 결과 — 그 턴은 여기서 끝난다. stop 기록을 남기지 않는 실행
                # (agent-hub 의 stream-json 채팅)에서 이걸 도구 단계로 보면 5분 동안 "진행 중"·답 없음으로 보였다.
                tail = "end"
                continue
            turn = self.tools.get(tid)
            if not turn:
                if b.get("tool_use_id") in self.tools:
                    tail = "end"   # StructuredOutput(최종 답)의 결과 — 이 뒤로 이어지는 도구 단계가 없다
                continue
            idx = turn["_ids"].get(b.get("tool_use_id"))
            if idx is None:
                continue
            turn["items"][idx] = apply_result(turn["items"][idx], b, rec.get("toolUseResult"))
            if ts:
                turn["completedAt"] = int(ts)
            tail = "tool"
        self.tail_kind = tail

    def compaction(self, iid, ts):
        self._put({"type": "contextCompaction", "id": iid}, ts)

    def interrupted(self, ts):
        if self.cur is not None:
            self.cur["status"] = "interrupted"
            self.tail_kind = "end"

    def finish(self, mtime=None, live=False, proc=None, sub_activity=None):
        """마지막 턴의 상태를 정한다. live=True 면(이 앱에서 돌고 있음) 진행 중.

        mtime 은 하위 에이전트 기록까지 본 마지막 활동 시각(activity_mtime)이다.
        proc 은 이 세션을 돌리는 프로세스(proc_state): "alive" 면 조용해도 진행 중, "gone" 이면 유예 없이 끊김,
        None(모름)이면 mtime 으로 LIVE_GRACE_SEC 유예를 둔다.
        sub_activity: 진행 중인 Agent 호출 ID 들 → {ID: 하위 에이전트가 지금 하는 일(tool_item) 또는 None}.
        """
        if self.turns:
            last = self.turns[-1]
            agents = [it for it in last["items"] if it.get("type") == "subAgentActivity" and it.get("status") == "inProgress"]
            acts = sub_activity({it["id"] for it in agents}) if sub_activity and agents else {}
            for it in agents:
                if acts.get(it["id"]):
                    it["activity"] = acts[it["id"]]
            if live:
                last["status"] = "inProgress"
            elif last["status"] == "completed" and self.tail_kind in ("prompt", "tool") and (mtime or proc):
                until = 0
                if proc != "alive" and proc != "gone" and mtime:
                    # 긴 명령은 제한 시간까지 기록이 없다 — 그 시각까지는 진행 중으로 본다
                    q = quiet_until([i for i in last["items"] if i.get("status") == "inProgress"])
                    until = max(mtime + LIVE_GRACE_SEC, (q or 0) + LIVE_GRACE_SEC)
                if proc == "alive":
                    last["status"] = "inProgress"   # 명부가 바뀌면 읽기 캐시 열쇠가 달라져 다시 판정한다
                    last["alive"] = True            # 목록·보내기 막기가 10분 무신호로 '끊김'이라 하지 않게
                elif time.time() < until:
                    last["status"] = "inProgress"
                    # 시간이 지나면 같은 기록이라도 '끊김'이 된다 — 읽기 캐시는 이 시각까지만 쓴다
                    self.grace_until = until
                else:
                    # 요청·도구 단계에서 끝 기록 없이 프로세스가 꺼졌거나 조용해졌다(앱 재기동·강제 종료 등).
                    # Codex 의 '끝나지 않음'과 같은 모양(끝 시각 없는 interrupted)으로 내고, 까닭을 cut 에 둔다.
                    last["status"] = "interrupted"
                    last["completedAt"] = None
                    last["cut"] = "process" if proc == "gone" else "quiet"
        out = []
        for t in self.turns:
            items = [strip_private(i) for i in t["items"]]
            for i in items:
                if i.get("status") == "inProgress" and t["status"] != "inProgress":
                    i["status"] = "interrupted" if t["status"] == "interrupted" else "completed"
            d = {k: v for k, v in t.items() if not k.startswith("_")}
            d["items"] = items
            if d["startedAt"] and d["completedAt"]:
                d["durationMs"] = int((d["completedAt"] - d["startedAt"]) * 1000)
            out.append(d)
        return out


def build_turns(records, cwd=None, mtime=None, live=False, proc=None, sub_activity=None, sidechain=False):
    """sidechain=True 면 하위 에이전트 기록(모든 줄이 isSidechain)을 읽는다 — 본 세션에서는 그 줄들을 뺀다."""
    chain = None if sidechain else active_chain(records)
    b = TurnBuilder(cwd)
    for r in records:
        typ = r.get("type")
        if r.get("isSidechain"):
            if not sidechain:
                continue
            r = {k: v for k, v in r.items() if k != "isSidechain"}   # 하위 기록에서는 그 줄들이 본 줄이다
        if chain is not None and r.get("uuid") and typ in ("user", "assistant", "system", "attachment") \
                and r["uuid"] not in chain:
            continue
        ts = ts_of(r.get("timestamp"))
        if ts:
            b.last_ts = ts
        if r.get("cwd") and not b.cwd:
            b.cwd = r["cwd"]
        if typ == "user":
            if r.get("isCompactSummary"):
                b.compaction(r.get("uuid"), ts)
                continue
            if is_interrupt_mark(r):
                b.interrupted(ts)
                continue
            p = prompt_of(r)
            if p:
                pos = r.get("turnPosition") or {}
                b.prompt(r.get("uuid"), p[0], p[1], ts, steer=bool(pos.get("promptIndex")), notice=is_notice(r))
            else:
                b.tool_results(r, ts)
        elif typ == "assistant":
            b.assistant(r, ts)
        elif typ == "attachment":
            att = r.get("attachment") or {}
            mode = att.get("commandMode", "prompt")
            if att.get("type") == "queued_command" and mode in ("prompt", "task-notification"):
                pr = att.get("prompt")
                text = pr if isinstance(pr, str) else "\n".join(x.get("text") or "" for x in pr or []
                                                              if isinstance(x, dict) and x.get("type") == "text")
                ims = image_urls(pr) if isinstance(pr, list) else []
                if text.strip() or ims:
                    b.prompt(att.get("source_uuid") or r.get("uuid"), text, ims, ts, steer=True,
                             notice=mode == "task-notification" or is_notice(att))
            elif att.get("hookEvent") in ("Stop", "SubagentStop") or str(att.get("hookName") or "") in ("Stop", "SubagentStop"):
                b.tail_kind = "end"   # Stop 훅은 턴이 끝났을 때만 돈다
        elif typ == "system":
            if r.get("subtype") == "compact_boundary":
                b.compaction(r.get("uuid"), ts)
            elif r.get("subtype") in ("stop_hook_summary", "turn_duration"):
                b.tail_kind = "end"
    turns = b.finish(mtime, live, proc, sub_activity)
    return turns, {"model": b.model, "effort": b.effort, "cwd": b.cwd, "last_ts": b.last_ts,
                   "grace_until": b.grace_until}


# --- 목록용 메타(앞·뒤 일부만 읽는다) ----------------------------------------------------
def _lines(buf, drop_first=False):
    parts = buf.split(b"\n")
    if drop_first and parts:
        parts = parts[1:]
    for raw in parts:
        if raw.strip():
            o = _loads(raw)
            if o is not None:
                yield o


def scan_meta(path):
    """세션 파일 하나의 목록 메타 — 앞(HEAD_BYTES)에서 작업 폴더·첫 요청을, 뒤(TAIL_BYTES)에서 제목·모델을 읽는다."""
    st = os.stat(path)
    meta = {"id": os.path.basename(path)[:-len(".jsonl")], "path": path, "mtime": st.st_mtime, "size": st.st_size,
            "cwd": None, "preview": "", "name": None, "model": None, "effort": None, "created": None, "branch": None,
            "version": None, "entrypoint": None, "prompts": 0}
    with open(path, "rb") as f:
        head = f.read(HEAD_BYTES)
        if st.st_size > HEAD_BYTES + TAIL_BYTES:
            f.seek(st.st_size - TAIL_BYTES)
            tail = f.read()
            tail_recs = list(_lines(tail, drop_first=True))
        else:
            tail_recs = None
    head_recs = list(_lines(head if st.st_size <= HEAD_BYTES else head.rsplit(b"\n", 1)[0]))
    # 첫 요청이 앞부분 밖에 있으면 조금 더 읽는다(첨부가 많은 세션)
    if not any(prompt_of(r) for r in head_recs) and st.st_size > HEAD_BYTES:
        with open(path, "rb") as f:
            more = f.read(min(st.st_size, 2 * 1024 * 1024))
        head_recs = list(_lines(more.rsplit(b"\n", 1)[0]))
    side = is_subagent_path(path)
    for r in head_recs + (tail_recs or []):
        if side and r.get("isSidechain"):
            r = {k: v for k, v in r.items() if k != "isSidechain"}
        _meta_from(meta, r)
    return meta


def _meta_from(meta, r):
    typ = r.get("type")
    if r.get("version"):
        meta["last_version"] = r["version"]   # 이어 돌린 프로세스의 버전(명부 판정용) — 앞·뒤 기록 가운데 마지막
    if r.get("cwd") and not meta["cwd"]:
        meta["cwd"] = r["cwd"]
        meta["branch"] = r.get("gitBranch")
        meta["version"] = r.get("version")
        meta["entrypoint"] = r.get("entrypoint")
    if r.get("timestamp") and not meta["created"]:
        meta["created"] = ts_of(r["timestamp"])
    if typ == "ai-title" and r.get("aiTitle") and not meta.get("_custom"):
        meta["name"] = r["aiTitle"]
    elif typ == "custom-title" and r.get("customTitle"):
        meta["name"] = r["customTitle"]
        meta["_custom"] = True
    elif typ == "summary" and r.get("summary") and not meta["name"]:
        meta["name"] = r["summary"]
    elif typ == "user" and not meta["preview"] and not is_notice(r):
        p = prompt_of(r)
        if p:
            meta["preview"] = p[0][:2000]
    elif typ == "assistant":
        m = (r.get("message") or {}).get("model")
        if m and m != "<synthetic>":
            meta["model"] = m
        if r.get("effort"):
            meta["effort"] = r["effort"]


def fork_lines(path, last_turn_id, new_id):
    """last_turn_id 턴의 끝까지 자른 기록 줄들(sessionId 를 new_id 로 바꾼다). 턴을 못 찾으면 ValueError."""
    recs = read_records(path)
    start = None
    cut = len(recs)
    for i, r in enumerate(recs):
        if r.get("type") == "user" and r.get("uuid") and not (r.get("turnPosition") or {}).get("promptIndex") \
                and prompt_of(r):
            if start is not None:
                cut = i
                break
            if r["uuid"] == last_turn_id:
                start = i
    if start is None:
        raise ValueError("포크할 턴을 찾지 못했습니다: %s" % last_turn_id)
    out = []
    for r in recs[:cut]:
        if r.get("type") in ("queue-operation", "last-prompt"):
            continue            # 대기열·마지막 요청 표시는 원본 세션의 것이다
        if "sessionId" in r:
            r = dict(r, sessionId=new_id)
        out.append(json.dumps(r, ensure_ascii=False))
    return out


# --- 하위 에이전트(Agent 도구) 기록 ---------------------------------------------------
# Claude Code 는 하위 에이전트의 일을 부모 기록이 아니라 <세션 ID>/subagents/agent-*.jsonl 에 적는다.
# 그동안 부모 기록은 Agent 호출에서 멈춰 있으므로, 이 파일들의 변경 시각·마지막 도구 호출이 그 턴의 신호다.
def subagent_files(session_path):
    """(기록 파일, meta 파일) 목록. 없으면 빈 목록."""
    d = os.path.join(session_path[:-len(".jsonl")], "subagents")
    try:
        names = os.listdir(d)
    except OSError:
        return []
    return [(os.path.join(d, n), os.path.join(d, n[:-len(".jsonl")] + ".meta.json"))
            for n in names if n.endswith(".jsonl")]


# 하위 에이전트 기록을 세션처럼 열 때의 스레드 ID — 기록 파일 이름 그대로(`agent-<agentId>`).
# Codex 스레드 ID 는 UUID 라 겹치지 않는다. 기록은 읽기만 된다(이어 시키기·포크는 부모 세션에서).
SUBAGENT_PREFIX = "agent-"


def is_subagent_id(tid):
    return str(tid or "").startswith(SUBAGENT_PREFIX)


def is_subagent_path(path):
    return os.path.basename(os.path.dirname(path or "")) == "subagents"


def subagent_meta(path):
    """하위 에이전트 기록 옆의 `.meta.json`(agentType·description·toolUseId·finished …). 없으면 {}."""
    try:
        with open(path[:-len(".jsonl")] + ".meta.json", encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        return {}
    return m if isinstance(m, dict) else {}


def subagent_threads(session_path):
    """{부모의 Agent 도구 호출 ID: 하위 에이전트 스레드 ID(`agent-…`)} — meta 의 toolUseId 로 잇는다."""
    out = {}
    for path, meta_path in subagent_files(session_path):
        try:
            with open(meta_path, encoding="utf-8") as f:
                tid = (json.load(f) or {}).get("toolUseId")
        except (OSError, ValueError, AttributeError):
            continue
        if tid:
            out[tid] = os.path.basename(path)[:-len(".jsonl")]
    return out


def with_subagent_threads(turns, session_path):
    """턴들의 하위 에이전트 항목에 그 기록의 스레드 ID 를 붙인다(이미 있거나 기록이 없으면 그대로)."""
    ids = None
    out = []
    for t in turns:
        items = t.get("items") or []
        if any(i.get("type") == "subAgentActivity" and not i.get("agentThreadId") for i in items):
            if ids is None:
                ids = subagent_threads(session_path)
            if ids:
                items = [dict(i, agentThreadId=ids[i["id"]]) if i.get("type") == "subAgentActivity"
                         and not i.get("agentThreadId") and i.get("id") in ids else i for i in items]
                t = dict(t, items=items)
        out.append(t)
    return out


def _open_tool(path, cwd=None):
    """하위 에이전트 기록의 뒤쪽에서 결과가 아직 없는 마지막 도구 호출 → tool_item(startedAt 포함). 없으면 None."""
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
            buf = f.read()
    except OSError:
        return None
    opened = {}
    for r in _lines(buf, drop_first=size > TAIL_BYTES):
        msg = r.get("message") if isinstance(r.get("message"), dict) else {}
        for b in _content_blocks(msg):
            if r.get("type") == "assistant" and b.get("type") == "tool_use" and b.get("id"):
                opened[b["id"]] = dict(tool_item(b, cwd or r.get("cwd")), startedAt=ts_of(r.get("timestamp")))
            elif r.get("type") == "user" and b.get("type") == "tool_result":
                opened.pop(b.get("tool_use_id"), None)
    if not opened:
        return None
    return strip_private(list(opened.values())[-1])


def subagent_activity(session_path, ids):
    """{부모의 Agent 도구 호출 ID(ids 안의 것만): 하위 에이전트가 지금 돌리는 도구(tool_item) 또는 None}."""
    out = {}
    if not ids:
        return out
    for path, meta_path in subagent_files(session_path):
        try:
            with open(meta_path, encoding="utf-8") as f:
                tid = (json.load(f) or {}).get("toolUseId")
        except (OSError, ValueError):
            continue
        if tid in ids:
            out[tid] = _open_tool(path)
    return out
