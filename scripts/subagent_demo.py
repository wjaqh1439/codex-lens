"""Claude 하위 에이전트 진행 표시 데모 — 가짜 데모에 Claude 기록 두 개를 시드해 띄운다.

- "하위 에이전트 진행 중": 본 기록은 Agent 호출에서 멈춰 있고(6분 전), 하위 에이전트 기록만 20초마다 자란다.
- "끊긴 턴": 같은 모양인데 하위 에이전트도 조용하다(앱 재기동으로 프로세스가 꺼진 경우) → 끝나지 않음.
- "프로세스 살아 있음": 기록이 10분 조용하지만 프로세스 명부(`claude-home/sessions/<pid>.json`)에 살아 있는
  프로세스(이 스크립트)가 있다 → 진행 중.
- "프로세스 꺼짐": 40초 전에 쓴 기록(5분 유예 안)이지만 명부의 프로세스가 없다 → 바로 끝나지 않음(프로세스가 꺼졌습니다).

    python3 scripts/subagent_demo.py --home /tmp/cl-subagent-demo --port 19xxx
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def session(cwd, sid, title, t0):
    recs = [
        {"type": "user", "uuid": sid + "-u1", "timestamp": iso(t0), "cwd": cwd, "sessionId": sid,
         "message": {"role": "user", "content": title}},
        {"type": "assistant", "uuid": sid + "-a1", "timestamp": iso(t0 + 3), "sessionId": sid,
         "message": {"id": sid + "-m1", "model": "claude-opus-5-5", "content": [
             {"type": "text", "text": "메인 반영은 서브에이전트에게 맡깁니다."}]}},
        {"type": "assistant", "uuid": sid + "-a2", "timestamp": iso(t0 + 20), "sessionId": sid,
         "message": {"id": sid + "-m2", "model": "claude-opus-5-5", "stop_reason": "tool_use", "content": [
             {"type": "tool_use", "id": "toolu_" + sid[:8], "name": "Agent",
              "input": {"subagent_type": "general-purpose", "description": "메인 반영",
                        "prompt": "작업 브랜치를 main 에 병합하고 태그를 단다."}}]}},
        {"type": "custom-title", "customTitle": title, "sessionId": sid},
    ]
    for prev, r in zip(recs, recs[1:]):
        if "uuid" in r:
            r["parentUuid"] = prev["uuid"]
    return recs


SUB_STEPS = ["git fetch 없이 main 상태 확인", "git merge --no-ff task/demo", "python3 -m unittest discover -s tests",
             "git tag v0.18.5", "python3 scripts/browser_check.py"]


def write_sub(sub_dir, tool_id, n):
    os.makedirs(sub_dir, exist_ok=True)
    with open(os.path.join(sub_dir, "agent-demo.meta.json"), "w") as f:
        json.dump({"agentType": "general-purpose", "toolUseId": tool_id, "requestShape": "foreground"}, f)
    with open(os.path.join(sub_dir, "agent-demo.jsonl"), "a") as f:
        f.write(json.dumps({"type": "assistant", "isSidechain": True, "timestamp": iso(time.time()), "message": {
            "content": [{"type": "tool_use", "id": "b%d" % n, "name": "Bash",
                         "input": {"command": SUB_STEPS[n % len(SUB_STEPS)]}}]}}, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--port", type=int, required=True)
    a = ap.parse_args()
    home = os.path.abspath(a.home)
    claude_home = os.path.join(home, "claude-home")
    cwd = os.path.join(home, "work")
    os.makedirs(cwd, exist_ok=True)
    proj = os.path.join(claude_home, "projects", cwd.replace("/", "-"))
    os.makedirs(proj, exist_ok=True)
    now = time.time()
    old = now - 360
    paths = {}
    for sid, title in (("5b0e1d2a-0000-4000-8000-00000000a001", "하위 에이전트 진행 중"),
                       ("5b0e1d2a-0000-4000-8000-00000000a002", "끊긴 턴(하위 에이전트도 조용함)")):
        p = os.path.join(proj, sid + ".jsonl")
        with open(p, "w") as f:
            f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in session(cwd, sid, title, old - 30)) + "\n")
        os.utime(p, (old, old))
        paths[sid] = p
    run_sid, cut_sid = list(paths)
    # 프로세스 명부로 가르는 두 세션
    alive_sid, gone_sid = "5b0e1d2a-0000-4000-8000-00000000a003", "5b0e1d2a-0000-4000-8000-00000000a004"
    for sid, title, t_rec in ((alive_sid, "프로세스 살아 있음(기록은 10분 조용)", now - 600),
                              (gone_sid, "프로세스 꺼짐(기록은 40초 전)", now - 40)):
        p = os.path.join(proj, sid + ".jsonl")
        with open(p, "w") as f:
            f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in session(cwd, sid, title, t_rec - 30)) + "\n")
        os.utime(p, (t_rec, t_rec))
    reg = os.path.join(claude_home, "sessions")
    os.makedirs(reg, exist_ok=True)
    started = subprocess.run(["ps", "-o", "lstart=", "-p", str(os.getpid())], capture_output=True, text=True,
                             env=dict(os.environ, TZ="UTC", LC_ALL="C")).stdout.strip()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    for pid, sid, extra in ((os.getpid(), alive_sid, {"procStart": started}), (dead.pid, gone_sid, {})):
        with open(os.path.join(reg, "%d.json" % pid), "w") as f:
            json.dump(dict({"pid": pid, "sessionId": sid, "status": "busy", "version": "demo"}, **extra), f)
    cut_sub = os.path.join(paths[cut_sid][:-len(".jsonl")], "subagents")
    write_sub(cut_sub, "toolu_" + cut_sid[:8], 0)
    for f in os.listdir(cut_sub):
        os.utime(os.path.join(cut_sub, f), (old, old))
    run_sub = os.path.join(paths[run_sid][:-len(".jsonl")], "subagents")

    def grow():
        n = 1
        while True:
            write_sub(run_sub, "toolu_" + run_sid[:8], n)
            n += 1
            time.sleep(20)

    threading.Thread(target=grow, daemon=True).start()
    env = dict(os.environ, CODEX_LENS_DEMO_CLAUDE_HOME=claude_home)
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "scripts", "run_fake_demo.py"), "--home",
                             os.path.join(home, "fake"), "--port", str(a.port)], env=env)
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()


if __name__ == "__main__":
    main()
