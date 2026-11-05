"""Codex 스레드의 턴을 화면 모양(요청 · 과정 · 답변)으로 바꾼다.

입력은 app-server 의 Turn(`thread/read includeTurns` 또는 실행 중 알림으로 모은 항목)이다.
- 요청: 첫 userMessage. 진행 중에 끼워 넣은(turn/steer) 뒤의 userMessage 들은 `request.steers` 로 따로 둔다
- 답변: 마지막 agentMessage(phase=final_answer 가 있으면 그것). 멈춘·실패한 턴은 final_answer 만 답으로 본다
- 과정: 그 밖의 모든 항목(중간 메시지·생각·명령·파일 변경·도구 호출 …)
"""

import json
import os
import re
import shlex

OUTPUT_LIMIT = 4000      # 명령 출력·도구 결과를 화면에 실을 최대 글자
DIFF_LIMIT = 6000


def quiet_until(items):
    """진행 중인 명령이 끝날 때까지 기록이 없어도 되는 시각(epoch 초). 모르면 None.

    Claude Code 는 명령이 끝나야 결과를 적고 그동안 아무 신호도 내지 않는다 — 명령의 제한 시간(timeoutMs)까지는
    조용한 것이 정상이다. 하위 에이전트 안에서 도는 명령은 그 에이전트 항목의 activity 로 본다."""
    out = None
    for it in items or []:
        if it.get("status") != "inProgress":
            continue
        for x in (it, it.get("activity") or {}):
            if x.get("timeoutMs") and x.get("startedAt"):
                end = x["startedAt"] + x["timeoutMs"] / 1000.0
                out = end if out is None else max(out, end)
    return out


def _clip(s, n):
    s = s or ""
    if len(s) <= n:
        return s
    return s[: n // 2] + "\n… (%d자 생략) …\n" % (len(s) - n) + s[-n // 2:]


def _json_field(v):
    """agent-output 의 todos·decisions·merge 는 JSON 글로 감싸 오기도 한다("[…]"·"null") — 풀어 준다."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def parse_output(text):
    """agent-output 규약({"protocol":"agent-output/1","body":…})의 답 → {body, todos, decisions, merge}. 아니면 None.

    코드 블록(```json)으로 감싼 답도 읽는다. 형식이 어긋난 항목은 버리고 본문은 살린다.
    """
    t = (text or "").strip()
    m = re.match(r"^```(?:json)?\s*\n([\s\S]*?)\n```$", t, re.I)
    if m:
        t = m.group(1).strip()
    if not (t.startswith("{") and '"protocol"' in t):
        return None
    try:
        obj = json.loads(t)
    except ValueError:
        return None
    if not (isinstance(obj, dict) and str(obj.get("protocol", "")).startswith("agent-output") and "body" in obj):
        return None
    raw_todos = _json_field(obj.get("todos"))
    todos = [{"id": str(x.get("id") or i), "title": str(x["title"]), "detail": x.get("detail") or "",
              "owner": x.get("owner") or "agent", "project": x.get("project") or None}
             for i, x in enumerate(raw_todos if isinstance(raw_todos, list) else [])
             if isinstance(x, dict) and x.get("title")]
    decisions = []
    raw_dec = _json_field(obj.get("decisions"))
    for i, d in enumerate(raw_dec if isinstance(raw_dec, list) else []):
        if not isinstance(d, dict) or not d.get("question"):
            continue
        opts = [o for o in (d.get("options") or []) if isinstance(o, dict) and str(o.get("label") or "").strip()]
        decisions.append({"id": str(d.get("id") or i), "header": d.get("header") or "", "question": d["question"],
                          "options": [{"label": str(o["label"]), "description": o.get("description") or "",
                                       "recommended": bool(o.get("recommended"))} for o in opts],
                          "multiSelect": bool(d.get("multiSelect")), "allowOther": d.get("allowOther") is not False})
    merge = _json_field(obj.get("merge"))
    if not isinstance(merge, dict):
        merge = None
    else:
        merge = {"summary": str(merge.get("summary") or ""), "bump": merge.get("bump") or None,
                 "version": merge.get("version") or None,
                 "checks": [str(c) for c in (merge.get("checks") or []) if isinstance(c, str)]}
    return {"body": obj.get("body") or "", "todos": todos, "decisions": decisions, "merge": merge}


def unwrap_output(text):
    """최종 출력 규약({"protocol":"agent-output/1","body":…})으로 쓴 답은 본문만 꺼낸다."""
    out = parse_output(text)
    return text if out is None else out["body"]


def output_report(text):
    """답에 실린 할 일·결정·메인 반영 요청 — 화면이 타입별 카드로 그린다. 없으면 None."""
    out = parse_output(text)
    if not out or not (out["todos"] or out["decisions"] or out["merge"]):
        return None
    return {k: out[k] for k in ("todos", "decisions", "merge")}


def user_text(item):
    parts, images, mentions = [], [], []
    for c in item.get("content") or []:
        t = c.get("type")
        if t == "text":
            parts.append(c.get("text") or "")
        elif t == "image":
            images.append({"url": c.get("url")} if c.get("url") else {"file": c.get("fileId")})
        elif t == "localImage":
            images.append({"path": c.get("path")})
        elif t in ("mention", "skill"):
            mentions.append({"kind": t, "name": c.get("name"), "path": c.get("path")})
    return "\n".join(p for p in parts if p), images, mentions


def short_cmd(cmd):
    """셸 래퍼(["/bin/zsh", "-lc", "…"] 또는 "/bin/zsh -lc '…'")를 벗긴다."""
    if isinstance(cmd, str) and re.match(r"^\S*/?(ba|z)?sh\s+-l?c\s", cmd):
        try:
            cmd = shlex.split(cmd)
        except ValueError:
            pass
    if isinstance(cmd, list):
        if len(cmd) >= 3 and cmd[1] in ("-lc", "-c"):
            cmd = cmd[2]
        else:
            cmd = " ".join(cmd)
    return (cmd or "").strip()


# 하위 에이전트 — Codex 의 subAgentActivity.kind 와 collabAgentToolCall.tool 을 화면 말로(앞 칩이 '하위 에이전트')
AGENT_ACTIVITY = {"started": "시작", "interacted": "메시지 보냄", "interrupted": "멈춤", "completed": "끝남"}
AGENT_TOOL = {"spawnAgent": "만들기", "sendInput": "입력 보냄", "sendMessage": "메시지 보냄", "followupTask": "후속 작업 맡김",
              "resumeAgent": "다시 열기", "wait": "기다림", "closeAgent": "닫기", "interruptAgent": "멈추기",
              "listAgents": "목록 보기"}


def agent_name(path, thread_id=None):
    """`/root/integrate_archive` → `integrate_archive`. 경로를 모르면 스레드 ID 앞 8자."""
    name = (path or "").rstrip("/").rsplit("/", 1)[-1]
    return name or (str(thread_id or "")[:8] or "하위 에이전트")


def turn_agents(items):
    """이 턴에 나온 하위 에이전트 {스레드 ID: 경로} — 순서 유지."""
    out = {}
    for it in items:
        if it.get("type") == "subAgentActivity" and it.get("agentThreadId"):
            out.setdefault(it["agentThreadId"], it.get("agentPath") or "")
    return out


def subagent_title(th):
    """하위 에이전트 세션의 제목 — 첫 요청이 암호화돼 미리보기가 비므로 `하위 에이전트 · 이름 (별명)`."""
    sub = (th.get("source") or {}).get("subAgent") if isinstance(th.get("source"), dict) else None
    spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
    if not isinstance(spawn, dict):
        return ""
    nick = th.get("agentNickname") or spawn.get("agent_nickname")
    name = agent_name(spawn.get("agent_path"), None) if spawn.get("agent_path") else ""
    return "하위 에이전트 · " + (name or nick or "") + (" (%s)" % nick if name and nick else "")


def step_of(item, agents=None):
    """과정 항목 하나 → {id, kind, title, detail, status, ms}. 화면에 보일 것이 없으면 None.
    agents 는 이 턴의 하위 에이전트 {스레드 ID: 경로}(`turn_agents`) — 기다림 항목이 대상을 비워 두면 이걸로 채운다."""
    t = item.get("type")
    sid = item.get("id")
    base = {"id": sid, "kind": t, "status": item.get("status"), "ms": item.get("durationMs")}
    if t == "agentMessage":
        return dict(base, kind="message", title="중간 메시지", detail=unwrap_output(item.get("text") or ""))
    if t == "reasoning":
        text = "\n\n".join(x for x in (item.get("summary") or []) if x)
        if not text:
            text = "\n\n".join(x for x in (item.get("content") or []) if x)
        return dict(base, kind="reasoning", title="생각", detail=text, private=not text)
    if t == "commandExecution":
        cmd = short_cmd(item.get("command"))
        code = item.get("exitCode")
        return dict(base, kind="command", title=cmd.split("\n")[0][:200] or "명령", command=cmd,
                    cwd=item.get("cwd"), exit_code=code, detail=_clip(item.get("aggregatedOutput") or "", OUTPUT_LIMIT),
                    timeout_ms=item.get("timeoutMs"))
    if t == "fileChange":
        changes = item.get("changes") or []
        files = []
        for ch in changes:
            kind = (ch.get("kind") or {}).get("type") if isinstance(ch.get("kind"), dict) else ch.get("kind")
            files.append({"path": ch.get("path"), "kind": kind, "diff": _clip(ch.get("diff") or "", DIFF_LIMIT)})
        names = [os.path.basename(f["path"] or "") for f in files]
        title = names[0] if names else "파일"
        if len(names) > 1:
            title += " 외 %d개" % (len(names) - 1)
        return dict(base, kind="file_change", title=title, files=files)
    if t == "mcpToolCall":
        res = item.get("result")
        detail = item.get("error") or res
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False)
        return dict(base, kind="tool", title="%s · %s" % (item.get("server"), item.get("tool")),
                    arguments=_clip(json.dumps(item.get("arguments"), ensure_ascii=False), OUTPUT_LIMIT),
                    detail=_clip(detail or "", OUTPUT_LIMIT))
    if t == "dynamicToolCall":
        # Claude 기록(claude_history)은 결과를 글 그대로, 제목을 "도구 · 대상"으로 준다
        ci = item.get("contentItems")
        return dict(base, kind="tool", title=item.get("title") or item.get("tool") or "도구",
                    arguments=_clip(json.dumps(item.get("arguments"), ensure_ascii=False), OUTPUT_LIMIT),
                    detail=_clip(ci if isinstance(ci, str) else json.dumps(ci, ensure_ascii=False), OUTPUT_LIMIT))
    if t == "webSearch":
        return dict(base, kind="web_search", title=item.get("query") or "웹 검색")
    if t == "plan":
        return dict(base, kind="plan", title="계획", detail=item.get("text") or "")
    if t == "imageView":
        return dict(base, kind="image", title=os.path.basename(str(item.get("path") or "")) or "이미지",
                    path=item.get("path"))
    if t == "contextCompaction":
        return dict(base, kind="compaction", title="대화 압축")
    if t == "subAgentActivity":
        path = item.get("agentPath") or ""
        verb = AGENT_ACTIVITY.get(item.get("kind"), item.get("kind") or "활동")
        agent = {"thread": item.get("agentThreadId"), "path": path, "name": agent_name(path, item.get("agentThreadId"))}
        step = dict(base, kind="agent", title="%s · %s" % (verb, agent["name"]), agents=[agent],
                    model=item.get("model"), effort=item.get("reasoningEffort"), detail="")
        if item.get("progress"):
            step["progress"] = item["progress"]
        act = item.get("activity")
        if act and item.get("status") == "inProgress":
            step["activity"] = step_of(act)    # 하위 에이전트가 지금 돌리는 도구(긴 명령의 제한 시간까지 포함)
        return step
    if t == "collabAgentToolCall":
        known = agents or {}
        ids = list(item.get("receiverThreadIds") or [])
        states = item.get("agentsStates") or {}
        for k in states:
            if k not in ids:
                ids.append(k)
        everyone = not ids and item.get("tool") in ("wait", "listAgents")
        if everyone:
            ids = list(known)           # 대상 없이 기다림 = 이 턴의 하위 에이전트 전부
        out = []
        for tid in ids:
            stt = states.get(tid) or {}
            out.append({"thread": tid, "path": known.get(tid) or "", "name": agent_name(known.get(tid), tid),
                        "status": stt.get("status"), "message": stt.get("message")})
        verb = AGENT_TOOL.get(item.get("tool"), item.get("tool") or "호출")
        who = ", ".join(a["name"] for a in out[:3]) + (" 외 %d" % (len(out) - 3) if len(out) > 3 else "")
        if everyone:
            who = who or "전체"
        return dict(base, kind="agent", title="%s · %s" % (verb, who) if who else verb, agents=out,
                    model=item.get("model"), effort=item.get("reasoningEffort"), detail=item.get("prompt") or "")
    if t == "functionCallOutput":
        return None
    if t in ("enteredReviewMode", "exitedReviewMode"):
        return dict(base, kind="review", title="리뷰 모드", detail=item.get("review") or "")
    if t == "hookPrompt":
        return None
    return dict(base, kind="other", title=t or "항목")


_NOTICE = re.compile(r"<task-notification>(.*?)(?:</task-notification>|$)", re.S)


def _tag(block, name):
    m = re.search(r"<%s>(.*?)</%s>" % (name, name), block, re.S)
    return m.group(1).strip() if m else ""


def parse_notices(text):
    """Claude Code 백그라운드 작업 알림 글 → [{status, summary, result}] (알림 블록마다 하나)."""
    out = []
    for m in _NOTICE.finditer(text or ""):
        b = m.group(1)
        out.append({"status": _tag(b, "status"), "summary": _tag(b, "summary"), "result": _tag(b, "result")})
    return out


def _notice_of(item, text):
    if not item.get("lensNotice"):
        return None
    return parse_notices(text) or [{"status": "", "summary": "", "result": text}]


def normalize_turn(turn, index=None):
    items = turn.get("items") or []
    req_parts, images, mentions, req_ids, steers = [], [], [], [], []
    notice = None
    agent_msgs = []
    for it in items:
        if it.get("type") == "userMessage":
            text, imgs, ments = user_text(it)
            if req_ids:
                # 첫 요청 뒤에 온 사용자 말 = 진행 중인 턴에 끼워 넣은 말. id 가 '_' 로 시작하면 아직 Codex 가 받지 않은 자리표시다.
                st = {"id": it.get("id"), "text": text, "images": imgs,
                      "pending": str(it.get("id") or "").startswith("_steer:")}
                n = _notice_of(it, text)
                if n:
                    st["notice"] = n
                steers.append(st)
                continue
            notice = _notice_of(it, text)
            if text:
                req_parts.append(text)
            images += imgs
            mentions += ments
            req_ids.append(it.get("id"))
        elif it.get("type") == "agentMessage":
            agent_msgs.append(it)
    answer_item = None
    finals = [m for m in agent_msgs if m.get("phase") == "final_answer"]
    if finals:
        answer_item = finals[-1]
    elif agent_msgs and turn.get("status") not in ("inProgress", "interrupted", "failed"):
        # 멈춘·실패한 턴의 마지막 말은 답이 아니라 하던 중의 중간 말이다("~하겠습니다") — 과정에 둔다
        answer_item = agent_msgs[-1]
    process = []
    agents = turn_agents(items)
    for it in items:
        if it.get("type") == "userMessage" or it is answer_item:
            continue
        st = step_of(it, agents)
        if st:
            process.append(st)
    answer = None
    if answer_item is not None:
        raw = answer_item.get("text") or ""
        answer = {"id": answer_item.get("id"), "text": unwrap_output(raw)}
        report = output_report(raw)
        if report:
            answer["report"] = report
    err = turn.get("error")
    return {
        "id": turn.get("id"),
        "index": index,
        "status": turn.get("status"),
        "started_at": turn.get("startedAt"),
        "completed_at": turn.get("completedAt"),
        "duration_ms": turn.get("durationMs"),
        "request": {"text": "\n\n".join(req_parts), "images": images, "mentions": mentions, "ids": req_ids,
                    "steers": steers, "notice": notice},
        "process": process,
        "counts": count_steps(process),
        "quiet_until": quiet_until(items) if turn.get("status") == "inProgress" else None,
        "answer": answer,
        "error": (err.get("message") if isinstance(err, dict) else err) or None,
    }


def count_steps(process):
    c = {}
    for s in process:
        c[s["kind"]] = c.get(s["kind"], 0) + 1
    return c

