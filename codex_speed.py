"""Codex 모델 응답 하나하나의 실제 출력 토큰 생성 속도.

Codex 는 모델 응답(Responses API 호출)이 끝날 때마다 실제 사용량(`last_token_usage` /
`thread/tokenUsage/updated` 의 `last`)을 남긴다. 응답 시간은 그 응답을 보낸 시각(사용자 요청·도구 결과가
기록된 시각)부터 모델이 낸 마지막 항목(추론·말·도구 호출)이 기록된 시각까지다 — 도구를 실행한 시간은 빠지고
첫 토큰을 기다린 시간(입력 처리·대기열)은 들어간다. 그래서 짧은 응답일수록 순수 생성 속도보다 낮게 나온다.

같은 계산을 두 곳에 쓴다.
- 기록(rollout JSONL): 이 앱 밖에서 도는 세션과 지난 턴. 파일은 덧붙이기만 하므로 읽은 위치를 기억해 이어 읽는다.
- 이 앱이 받는 app-server 알림: 이 앱이 돌리는 턴(`ResponseClock` 을 `LiveTurn` 이 쥔다).
"""
import json
import os
import re
import threading
from datetime import datetime

# 모델이 내는 항목(응답 기록) — 이것들이 기록된 마지막 시각이 응답의 끝이다.
MODEL_ITEMS = {"reasoning", "function_call", "custom_tool_call", "local_shell_call", "web_search_call",
               "tool_search_call", "image_generation_call"}
# 모델에 들어가는 항목 — 응답 사이에 이것이 기록되면 다음 응답을 보낸 시각으로 본다.
INPUT_ITEMS = {"function_call_output", "custom_tool_call_output", "local_shell_call_output",
               "tool_search_output", "mcp_tool_call_output"}
MAX_RESPONSES = 400          # 턴마다 남기는 응답 수(긴 턴의 메모리 상한)
# 줄 앞머리만 보고 종류를 가른다 — 도구 출력처럼 큰 줄을 매번 JSON 으로 풀지 않는다.
_HEAD = re.compile(r'^\{"timestamp":\s*"([^"]+)".*?"type":\s*"([a-z_]+)",\s*"payload":\s*\{"type":\s*"([a-z_]+)"')
_ROLE = re.compile(r'"role":\s*"([a-z]+)"')


def _ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


class ResponseClock:
    """응답 경계를 따라가며 사용량이 오면 그 응답의 (출력 토큰, 걸린 초)를 낸다."""

    def __init__(self):
        self.req_at = None       # 다음 응답을 보낸 것으로 볼 시각(마지막 입력)
        self.start = None        # 지금 응답의 시작
        self.end = None          # 지금 응답에서 모델이 낸 마지막 항목의 시각

    def begin(self, ts):
        """턴 시작 — 이전 턴의 남은 상태를 버린다."""
        self.req_at, self.start, self.end = ts, None, None

    def input(self, ts):
        self.req_at = ts

    def output(self, ts):
        if self.start is None:
            self.start = self.req_at if self.req_at is not None else ts
        self.end = ts

    def usage(self, out_tokens, reasoning_tokens=0):
        if self.start is None or self.end is None or out_tokens is None:
            return None
        sec = self.end - self.start
        self.start = self.end = None
        if sec <= 0 or out_tokens <= 0:
            return None
        return {"out": int(out_tokens), "reasoning": int(reasoning_tokens or 0), "sec": round(sec, 3)}


def summarize(responses):
    """턴의 응답 목록 → 화면용 요약(마지막 응답·턴 평균). 응답이 없으면 None."""
    if not responses:
        return None
    last = responses[-1]
    out = sum(r["out"] for r in responses)
    sec = sum(r["sec"] for r in responses)
    return {"tps": round(last["out"] / last["sec"], 1), "out": last["out"], "sec": last["sec"],
            "avg_tps": round(out / sec, 1) if sec > 0 else None, "total_out": out, "total_sec": round(sec, 3),
            "responses": len(responses), "measured": True}


class _FileState:
    def __init__(self, ino):
        self.ino = ino
        self.offset = 0
        self.clock = ResponseClock()
        self.turn = None
        self.turns = {}          # 턴 ID → [응답]


def _feed(st, line):
    m = _HEAD.match(line[:400])
    if not m:
        return
    ts, kind, ptype = _ts(m.group(1)), m.group(2), m.group(3)
    if ts is None:
        return
    if kind == "event_msg":
        if ptype == "task_started":
            try:
                st.turn = json.loads(line)["payload"].get("turn_id")
            except (ValueError, KeyError, AttributeError):
                st.turn = None
            st.clock.begin(ts)
        elif ptype == "token_count":
            try:
                info = json.loads(line)["payload"].get("info") or {}
            except (ValueError, KeyError, AttributeError):
                return
            last = info.get("last_token_usage") or {}
            r = st.clock.usage(last.get("output_tokens"), last.get("reasoning_output_tokens"))
            if r and st.turn:
                lst = st.turns.setdefault(st.turn, [])
                lst.append(dict(r, at=ts))
                del lst[:-MAX_RESPONSES]
        return
    if kind != "response_item":
        return
    if ptype in MODEL_ITEMS:
        st.clock.output(ts)
    elif ptype in INPUT_ITEMS:
        st.clock.input(ts)
    elif ptype == "message":
        role = _ROLE.search(line[:600])
        if role and role.group(1) == "assistant":
            st.clock.output(ts)
        elif role:
            st.clock.input(ts)


class RolloutRates:
    """rollout 경로별로 읽은 위치·상태를 기억해 이어 읽는다(스레드 안전)."""

    def __init__(self, max_files=64):
        self.lock = threading.Lock()
        self.files = {}
        self.max_files = max_files

    def turns(self, path):
        """→ {턴 ID: [응답]}. 읽지 못하면 빈 dict."""
        with self.lock:
            st = self._sync(path)
            return {k: list(v) for k, v in st.turns.items()} if st else {}

    def turn(self, path, turn_id):
        """→ 그 턴의 [응답](없으면 빈 목록)."""
        with self.lock:
            st = self._sync(path)
            return list(st.turns.get(turn_id) or ()) if st and turn_id else []

    def _sync(self, path):
        if not path:
            return None
        try:
            stat = os.stat(path)
        except OSError:
            return None
        st = self.files.pop(path, None)
        if st is None or st.ino != stat.st_ino or stat.st_size < st.offset:
            st = _FileState(stat.st_ino)
        self.files[path] = st          # 최근에 쓴 파일을 뒤로 — 넘치면 가장 오래 안 쓴 것부터 버린다
        while len(self.files) > self.max_files:
            self.files.pop(next(iter(self.files)))
        if stat.st_size > st.offset:
            try:
                with open(path, "rb") as f:
                    f.seek(st.offset)
                    data = f.read(stat.st_size - st.offset)
            except OSError:
                return st
            end = data.rfind(b"\n")       # 아직 다 쓰이지 않은 마지막 줄은 다음에 읽는다
            if end >= 0:
                for raw in data[:end].split(b"\n"):
                    if raw:
                        _feed(st, raw.decode("utf-8", "replace"))
                st.offset += end + 1
        return st
