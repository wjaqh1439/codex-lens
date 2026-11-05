#!/usr/bin/env python3
"""데모 시드 — 떠 있는 **데모** 서버 API 로 진짜 Codex(또는 Claude Code) 세션·과정 요약·인라인 질문·코멘트·문서 코멘트를 만든다.

  python3 scripts/seed_demo.py --url http://127.0.0.1:<데모 포트> [--agent claude] [--model <모델>] [--effort low]

견본 프로젝트는 데모 홈(기본 ~/.codex-lens-demo)의 sample-project 에 만든다. 운영 서버에는 쓰지 않는다
(/api/info 의 demo 가 거짓이면 멈춘다). 표준 라이브러리만 쓴다.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests"))
import seed  # noqa: E402  견본 파일 내용(README·calc.py)을 같이 쓴다

DESIGN = """# 설계 메모

계산은 `calc()` 한 함수가 맡습니다.

## 원칙

- 입력 검사는 호출하는 쪽이 합니다.
- 오류는 문자열로 돌려줍니다.
- 지원 연산은 `+ - * /` 네 가지입니다.

## 나중에

1. 곱셈 기호 `x` 받기
2. 결과 소수점 자리 정하기
"""


def call(base, method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def say(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def wait_turn(base, sid, limit=600):
    t0 = time.time()
    while time.time() - t0 < limit:
        s = call(base, "GET", "/api/sessions/" + sid)
        turns = s.get("turns") or []
        if turns and turns[-1].get("status") != "inProgress" and (s.get("session") or {}).get("status") != "running":
            return s
        time.sleep(2)
    raise SystemExit("턴이 %d초 안에 끝나지 않았습니다" % limit)


def wait_job(base, jid, limit=600):
    t0 = time.time()
    while time.time() - t0 < limit:
        j = call(base, "GET", "/api/jobs/" + jid)["job"]
        if j.get("status") != "running":
            return j
        time.sleep(2)
    raise SystemExit("작업 %s 이 %d초 안에 끝나지 않았습니다" % (jid, limit))


def plain(md):
    """화면에 보이는 글에 가깝게 — 인라인 질문·코멘트의 인용으로 쓸 조각을 고른다."""
    for line in md.splitlines():
        t = re.sub(r"^\s*(#+|[-*]|\d+\.)\s+", "", line)
        t = re.sub(r"[*`_]|\[([^\]]*)\]\([^)]*\)", lambda m: m.group(1) or "", t).strip()
        if len(t) >= 12 and "|" not in t:
            return t[:40].strip()
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--home", default=os.environ.get("CODEX_LENS_HOME") or os.path.expanduser("~/.codex-lens-demo"))
    ap.add_argument("--agent", default="codex", choices=["codex", "claude"])
    ap.add_argument("--model", default=None, help="비우면 그 에이전트 목록의 기본 모델")
    ap.add_argument("--effort", default="low")
    a = ap.parse_args()
    base = a.url.rstrip("/")
    info = call(base, "GET", "/api/info")
    if not info.get("demo"):
        raise SystemExit("데모 서버가 아닙니다 — 운영 데이터에는 시드를 넣지 않습니다")

    proj = os.path.join(os.path.expanduser(a.home), "sample-project")
    os.makedirs(os.path.join(proj, "docs"), exist_ok=True)
    for name, body in (("README.md", seed.README), ("calc.py", seed.CALC), ("docs/design.md", DESIGN)):
        with open(os.path.join(proj, name), "w") as f:
            f.write(body)
    say("견본 프로젝트 " + proj)

    model = a.model
    if not model:
        ms = [m for m in call(base, "GET", "/api/models").get("models") or [] if (m.get("agent") or "codex") == a.agent]
        model = next((m["id"] for m in ms if m.get("default")), ms[0]["id"] if ms else None)
    say("%s · 모델 %s · effort %s" % (a.agent, model, a.effort))

    out = call(base, "POST", "/api/sessions", {
        "cwd": proj, "model": model, "agent": a.agent, "effort": a.effort, "client_id": str(uuid.uuid4()),
        "text": "calc.py 를 읽고 0으로 나눌 때 어떻게 동작하는지 설명해 줘. 파일은 고치지 말고, 개선 아이디어를 두세 가지 짧게 제안해 줘."})
    sid = out["id"]
    say("세션 " + sid)
    s = wait_turn(base, sid)
    say("턴 1 끝")

    call(base, "POST", "/api/sessions/%s/turns" % sid, {
        "model": model, "effort": a.effort, "client_id": str(uuid.uuid4()),
        "text": "@docs/design.md 의 원칙과 지금 코드가 맞는지 한 문단으로 정리해 줘.",
        "mentions": [{"kind": "file", "path": os.path.join(proj, "docs", "design.md")}]})
    s = wait_turn(base, sid)
    say("턴 2 끝")
    t1, t2 = s["turns"][0], s["turns"][-1]

    job = call(base, "POST", "/api/sessions/%s/summaries" % sid, {
        "turn_id": t1["id"], "request": t1["request"]["text"], "client_id": str(uuid.uuid4())})["job"]
    job = wait_job(base, job["id"])
    say("과정 요약 %s · 캐시 %s" % (job.get("status"), json.dumps(job.get("usage"))))

    quote = plain(t1["answer"]["text"])
    ans = t1["answer"]["text"]
    if quote:
        q = call(base, "POST", "/api/sessions/%s/questions" % sid, {
            "turn_id": t1["id"], "part": "answer", "quote": quote, "start": max(0, ans.find(quote[:8])),
            "question": "이 부분을 초보자도 알게 쉽게 풀어 줘.", "client_id": str(uuid.uuid4())})["job"]
        q = wait_job(base, q["id"])
        say("인라인 질문 %s · 포크 %s" % (q.get("status"), q.get("fork_mode")))

    quote2 = plain(t2["answer"]["text"])
    if quote2:
        call(base, "POST", "/api/sessions/%s/comments" % sid, {
            "id": str(uuid.uuid4()), "turn_id": t2["id"], "part": "answer", "quote": quote2,
            "start": max(0, t2["answer"]["text"].find(quote2[:8])), "text": "이 판단의 근거가 된 코드 줄도 짚어 주면 좋겠다.",
            "state": "none", "response": t2["answer"]["text"].split("\n")[0][:200]})
        say("코멘트 1개")

    line = "오류는 문자열로 돌려줍니다."
    have = call(base, "GET", "/api/md-comments?path=" + urllib.parse.quote(os.path.join(proj, "docs", "design.md"))).get("comments") or []
    if any(c.get("quote") == line for c in have):
        say("끝(문서 코멘트는 이미 있음). 세션 " + sid)
        return
    call(base, "POST", "/api/md-comments", {
        "id": str(uuid.uuid4()), "path": os.path.join(proj, "docs", "design.md"), "quote": line,
        "start": DESIGN.find(line), "line": DESIGN.splitlines().index("- " + line), "text": "예외를 쓰는 편이 테스트하기 쉽지 않을까?",
        "state": "none"})
    say("문서 코멘트 1개 — 끝. 세션 " + sid)


if __name__ == "__main__":
    main()
