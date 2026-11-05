"""가짜 app-server 시드 — 견본 프로젝트 폴더와 그 폴더에서 오간 CLI 세션 두 개(턴·과정 항목 포함)."""

import base64
import json
import os
import struct
import subprocess
import time
import zlib

README = """# 계산기 예제

작은 파이썬 계산기입니다.

## 사용법

- `python3 calc.py 1 + 2` 처럼 실행합니다.
- 0으로 나누면 오류 대신 안내 문구를 보여 줍니다.

## 할 일

1. 곱셈 기호 `x` 도 받기
2. 소수점 반올림 자리 정하기
"""

CALC = """import sys

def calc(a, op, b):
    if op == '/':
        if b == 0:
            return '0으로 나눌 수 없습니다'
        return a / b
    return {'+': a + b, '-': a - b, '*': a * b}[op]

if __name__ == '__main__':
    a, op, b = sys.argv[1:4]
    print(calc(float(a), op, float(b)))
"""


def image_fixture():
    """브라우저가 실제로 디코딩할 수 있는 640×360 PNG 견본."""
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    rows = bytearray()
    for y in range(360):
        rows.append(0)
        for x in range(640):
            color = (244, 240, 251)
            if 24 <= x < 616 and 24 <= y < 336:
                color = (255, 255, 255)
            if 40 <= x < 600 and 40 <= y < 80:
                color = (109, 40, 217)
            if 40 <= x < 210 and 100 <= y < 316:
                color = (225, 214, 247)
            if 230 <= x < 600 and (100 <= y < 184 or 204 <= y < 316):
                color = (214, 238, 234)
            rows.extend(color)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB', 640, 360, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(bytes(rows))) + chunk(b'IEND', b''))


def external_threads(proj, t0):
    """목록 상태를 보일 세션들 — 이 앱 밖에서 도는 세션, 끊긴 턴·멈춘 턴, 실패, 시스템 오류.

    다른 app-server 가 돌리는 턴은 status 'interrupted' 에 completedAt 이 비어 보인다(사용자가 멈춘 턴은 completedAt 이 있다).
    _live_external 이면 가짜 app-server 가 읽을 때마다 updatedAt 을 방금으로 올린다 — 기록이 계속 쓰이는 중이라는 뜻.
    """
    def th(n, preview, upd, turns, **kw):
        d = {"id": "01a1aaaa00000000000000000000000%d" % n, "preview": preview, "cwd": proj, "model": "gpt-fake-sol",
             "reasoningEffort": "low", "createdAt": upd - 60, "updatedAt": upd, "source": "cli", "path": None,
             "cliVersion": "0.165.0", "name": None, "gitInfo": None, "turns": turns}
        d.update(kw)
        return d

    def turn(tid, text, status, start, done, items=()):
        return {"id": tid, "status": status, "startedAt": start, "completedAt": done, "durationMs": (done - start) * 1000 if done else None,
                "error": {"message": "모델 호출이 실패했습니다"} if status == "failed" else None,
                "items": [{"type": "userMessage", "id": "u-" + tid, "content": [{"type": "text", "text": text}]}] + list(items)}
    running = th(3, "다른 곳에서 돌고 있는 세션 — 테스트를 고쳐 줘", t0 + 7190, [
        turn("turn-c1", "다른 곳에서 돌고 있는 세션 — 테스트를 고쳐 줘", "interrupted", t0 + 7000, None, [
            {"type": "commandExecution", "id": "c6", "command": "python3 -m unittest", "status": "inProgress",
             "aggregatedOutput": "", "exitCode": None, "durationMs": None}])],
        source="vscode", reasoningEffort="high", _live_external=True)
    stopped = th(4, "끊긴 세션", t0 - 3500, [
        turn("turn-d1", "끊긴 세션", "interrupted", t0 - 3600, None),
        turn("turn-d2", "멈춘 턴", "interrupted", t0 - 3550, t0 - 3540)])
    broken = th(5, "프로세스가 꺼진 세션", t0 - 1800, [turn("turn-e1", "프로세스가 꺼진 세션", "interrupted", t0 - 1900, None)])
    failed = th(6, "실패한 세션", t0 - 600, [turn("turn-f1", "실패한 세션", "failed", t0 - 700, t0 - 600)])
    broken_sys = th(7, "시스템 오류 세션", t0 - 300, [turn("turn-g1", "시스템 오류 세션", "completed", t0 - 400, t0 - 300)],
                    status={"type": "systemError"})
    # 하위 에이전트 — Codex 0.165 의 wait 항목은 대상·상태를 비워 둔다(실측). 하위 세션은 첫 요청이 암호화돼 미리보기가 비어 있다.
    child_id = "01a1aaaa000000000000000000000009"
    parent = th(8, "하위 에이전트에게 통합을 맡긴 세션", t0 - 250, [
        turn("turn-h1", "하위 에이전트에게 통합을 맡긴 세션", "completed", t0 - 400, t0 - 250, [
            {"type": "subAgentActivity", "id": "sa1", "kind": "started", "agentThreadId": child_id,
             "agentPath": "/root/integrate_archive", "model": "gpt-fake-sol", "reasoningEffort": "high"},
            {"type": "collabAgentToolCall", "id": "w1", "tool": "wait", "status": "completed", "senderThreadId": "x",
             "receiverThreadIds": [], "prompt": None, "model": None, "reasoningEffort": None, "agentsStates": {}},
            {"type": "agentMessage", "id": "m-h1", "phase": "final_answer", "text": "하위 에이전트가 통합을 마쳤습니다."}])])
    child = th(9, "", t0 - 260, [
        turn("turn-i1", "", "completed", t0 - 390, t0 - 260, [
            {"type": "agentMessage", "id": "m-i1", "phase": "commentary", "text": "보관 기능을 main 에 통합하고 테스트를 돌리겠습니다."},
            {"type": "commandExecution", "id": "c-i1", "command": "python3 -m unittest", "status": "completed",
             "aggregatedOutput": "OK", "exitCode": 0, "durationMs": 900},
            {"type": "agentMessage", "id": "m-i2", "phase": "final_answer", "text": "통합 완료 — 테스트 92개 통과."}])],
        source={"subAgent": {"thread_spawn": {"parent_thread_id": parent["id"], "depth": 1, "agent_path": "/root/integrate_archive",
                                              "agent_nickname": "Lorentz", "agent_role": "worker"}}}, agentNickname="Lorentz")
    return [running, stopped, broken, failed, broken_sys, parent, child]


def external_queue_threads(proj, t0=None):
    """밖에서 도는 턴을 끝내며 대기열 전송을 시험할 세션들(단위 시험 전용 — build() 에는 넣지 않는다).
    가짜 app-server 의 FAKE_CODEX_EXTERNAL_END 파일에 ID 를 적으면 그 턴이 끝난 것으로 보인다."""
    t0 = t0 or int(time.time())
    out = []
    for n in (1, 2, 3):
        tid = "01a1aaaa00000000000000000000xq%s" % "abc"[n - 1]   # 다른 시험이 끝 숫자로 시드를 고르니 숫자로 끝나지 않게 한다
        out.append({"id": tid, "preview": "밖에서 도는 세션 %d" % n, "cwd": proj, "model": "gpt-fake-sol", "reasoningEffort": "low",
                    "createdAt": t0 - 60, "updatedAt": t0, "source": "cli", "path": None, "cliVersion": "0.165.0", "name": None,
                    "gitInfo": None, "_live_external": True,
                    "turns": [{"id": "turn-x%d" % n, "status": "interrupted", "startedAt": t0 - 30, "completedAt": None, "durationMs": None,
                               "error": None, "items": [
                                   {"type": "userMessage", "id": "u-x%d" % n, "content": [{"type": "text", "text": "밖에서 시킨 일 %d" % n}]},
                                   {"type": "commandExecution", "id": "cx%d" % n, "command": "sleep 100", "status": "inProgress",
                                    "aggregatedOutput": "", "exitCode": None, "durationMs": None}]}]})
    return out


def build(root, external=False):
    """root 아래 sample-project 와 seed.json 을 만들고 seed.json 경로를 돌려준다."""
    proj = os.path.join(root, "sample-project")
    os.makedirs(os.path.join(proj, "docs"), exist_ok=True)
    image_path = os.path.join(proj, "첨부 화면 #1.png")
    image = image_fixture()
    with open(image_path, "wb") as f:
        f.write(image)
    image_url = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
    with open(os.path.join(proj, "README.md"), "w") as f:
        f.write(README)
    with open(os.path.join(proj, "calc.py"), "w") as f:
        f.write(CALC)
    with open(os.path.join(proj, "docs", "design.md"), "w") as f:
        f.write("# 설계 메모\n\n계산은 `calc()` 한 함수가 맡습니다.\n\n- 입력 검사는 호출하는 쪽이 합니다.\n- 오류는 문자열로 돌려줍니다.\n")
    # 커밋 하나 있는 git 저장소로 둔다 — 새 세션 화면의 "워크트리에서 작업"이 보인다
    if not os.path.isdir(os.path.join(proj, ".git")):
        g = ["git", "-C", proj, "-c", "user.name=codex-lens", "-c", "user.email=codex-lens@localhost", "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q", proj], check=True)
        subprocess.run(g + ["add", "."], check=True)
        subprocess.run(g + ["commit", "-qm", "견본 프로젝트"], check=True)
    t0 = int(time.time()) - 7200
    s1 = {
        "id": "01a1aaaa000000000000000000000001", "preview": "calc.py 에 0으로 나누기 처리를 넣고 README 에 사용법을 적어 줘",
        "cwd": proj, "model": "gpt-fake-sol", "reasoningEffort": "medium", "createdAt": t0, "updatedAt": t0 + 300,
        "source": "cli", "path": os.path.join(root, "rollout-1.jsonl"), "cliVersion": "0.165.0", "name": None,
        "gitInfo": {"branch": "main"},
        "turns": [
            {"id": "turn-a1", "status": "completed", "startedAt": t0, "completedAt": t0 + 95, "durationMs": 95000, "error": None,
             "items": [
                 {"type": "userMessage", "id": "u1", "content": [{"type": "text", "text": "calc.py 에 0으로 나누기 처리를 넣고 README 에 사용법을 적어 줘"},
                     {"type": "image", "url": image_url}, {"type": "localImage", "path": image_path}]},
                 {"type": "agentMessage", "id": "m1", "text": "calc.py 와 README 를 먼저 읽겠습니다.", "phase": "commentary"},
                 {"type": "reasoning", "id": "r1", "summary": ["**구조 파악**\n\n나눗셈 분기가 없어 0 이면 예외가 난다."], "content": []},
                 {"type": "commandExecution", "id": "c1", "command": "/bin/zsh -lc 'cat calc.py README.md'", "cwd": proj, "status": "completed",
                  "aggregatedOutput": CALC[:120], "exitCode": 0, "durationMs": 40, "commandActions": [], "source": "agent", "processId": None},
                 {"type": "fileChange", "id": "f1", "status": "completed", "changes": [
                     {"path": os.path.join(proj, "calc.py"), "kind": {"type": "update", "move_path": None}, "diff": "@@\n+    if op == '/':\n+        if b == 0:\n+            return '0으로 나눌 수 없습니다'"},
                     {"path": os.path.join(proj, "README.md"), "kind": {"type": "update", "move_path": None}, "diff": "@@\n+## 사용법"}]},
                 {"type": "commandExecution", "id": "c2", "command": "/bin/zsh -lc 'python3 calc.py 4 / 0'", "cwd": proj, "status": "completed",
                  "aggregatedOutput": "0으로 나눌 수 없습니다\n", "exitCode": 0, "durationMs": 55, "commandActions": [], "source": "agent", "processId": None},
                 {"type": "agentMessage", "id": "m2", "phase": "final_answer", "text":
                  "두 가지를 고쳤습니다.\n\n1. **calc.py** — `b == 0` 이면 예외 대신 `0으로 나눌 수 없습니다` 를 돌려줍니다.\n"
                  "2. **README.md** — `## 사용법` 절을 새로 썼습니다.\n\n확인: `python3 calc.py 4 / 0` 이 안내 문구를 출력했습니다.\n\n"
                  "| 파일 | 바뀐 것 |\n|---|---|\n| calc.py | 0 나누기 분기 |\n| README.md | 사용법 절 |\n\n설계는 [설계 메모](" + os.path.join(proj, "docs", "design.md") + ") 에 있습니다."},
             ]},
            {"id": "turn-a2", "status": "completed", "startedAt": t0 + 200, "completedAt": t0 + 230, "durationMs": 30000, "error": None,
             "items": [
                 {"type": "userMessage", "id": "u2", "content": [{"type": "text", "text": "곱셈 기호 x 도 받을 수 있을까?"}]},
                 {"type": "userMessage", "id": "u2-steer", "content": [
                     {"type": "image", "url": image_url},
                     {"type": "localImage", "path": os.path.join(proj, "missing.png")},
                     {"type": "image", "fileId": "unavailable-image"}]},
                 {"type": "reasoning", "id": "r2", "summary": [], "content": []},
                 {"type": "agentMessage", "id": "m3", "phase": "final_answer", "text":
                  "네, 가능합니다. `op` 가 `x` 이면 `*` 로 바꿔 계산하면 됩니다.\n\n```python\nif op == 'x':\n    op = '*'\n```\n\n원하시면 바로 고치겠습니다. 데모: http://127.0.0.1:9/demo/"},
             ]},
        ],
    }
    s2 = {
        "id": "01a1aaaa000000000000000000000002", "preview": "README 할 일 목록을 정리해 줘", "cwd": proj, "model": "gpt-fake-luna",
        "reasoningEffort": "low", "createdAt": t0 - 86400, "updatedAt": t0 - 86000, "source": "exec", "path": None,
        "cliVersion": "0.165.0", "name": "할 일 정리", "gitInfo": None,
        "turns": [
            {"id": "turn-b1", "status": "completed", "startedAt": t0 - 86400, "completedAt": t0 - 86390, "durationMs": 10000, "error": None,
             "items": [
                 {"type": "userMessage", "id": "u3", "content": [{"type": "text", "text": "README 할 일 목록을 정리해 줘"}]},
                 {"type": "agentMessage", "id": "m4", "phase": "final_answer", "text":
                  "할 일 두 개를 번호 목록으로 정리했습니다.\n\n<details><summary>데모 정리 명령</summary>\n\n해당 폴더에서 실행:\n\n```\nkill %1\n```\n\n</details>\n\n| 할 일 | 메모 |\n|---|---|\n| 설치 | 첫 줄<br>둘째 줄 |"},
             ]},
        ],
    }
    # s1 이 띄운 Codex 하위 에이전트 — 목록에서 s1 아래에 묶인다
    sub_id = "01a1aaaa00000000000000000000000d"   # external_threads 가 3~9 를 쓴다
    s4 = {
        "id": sub_id, "preview": "calc.py 의 나눗셈 분기를 시험으로 확인해 줘", "cwd": proj, "model": "gpt-fake-luna",
        "reasoningEffort": "low", "createdAt": t0 + 60, "updatedAt": t0 + 120, "parentThreadId": s1["id"],
        "source": {"subAgent": {"thread_spawn": {"parent_thread_id": s1["id"], "depth": 1, "agent_path": None,
                                                 "agent_nickname": "Pascal", "agent_role": "worker"}}},
        "path": None, "cliVersion": "0.165.0", "name": None, "gitInfo": None,
        "turns": [
            {"id": "turn-d1", "status": "completed", "startedAt": t0 + 60, "completedAt": t0 + 110, "durationMs": 50000, "error": None,
             "items": [
                 {"type": "userMessage", "id": "u-d1", "content": [{"type": "text", "text": "calc.py 의 나눗셈 분기를 시험으로 확인해 줘"}]},
                 {"type": "agentMessage", "id": "m-d1", "phase": "final_answer", "text": "`python3 calc.py 4 / 0` 이 안내 문구를 돌려줍니다."},
             ]},
        ],
    }
    path = os.path.join(root, "seed.json")
    with open(path, "w") as f:
        json.dump({"threads": [s1, s2, s4] + (external_threads(proj, t0) if external else [])}, f, ensure_ascii=False, indent=1)
    build_claude(os.path.join(root, "claude-home"), proj, t0)
    if external:
        build_claude_subagent(os.path.join(root, "claude-home"), proj, int(time.time()))   # 지금 도는 중
    return path, proj


CLAUDE_S3 = "c1a0de00-0000-4000-8000-000000000003"


def build_claude(home, proj, t0):
    """가짜 Claude Code 기록 하나(CLAUDE_CONFIG_DIR=home) — 진짜 기록과 같은 모양(요청·생각·도구·도구 결과·답·제목)."""
    from datetime import datetime, timezone
    cwd = os.path.realpath(proj)
    d = os.path.join(home, "projects", "".join(c if c.isalnum() else "-" for c in cwd))
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, CLAUDE_S3 + ".jsonl")
    sid, n, last = CLAUDE_S3, [0], [None]

    def rec(typ, **kw):
        n[0] += 1
        r = {"parentUuid": last[0], "isSidechain": False, "type": typ, "uuid": kw.pop("uuid", None) or "c3-%04d" % n[0],
             "timestamp": datetime.fromtimestamp(t0 - 60 + n[0], timezone.utc).isoformat().replace("+00:00", "Z"),
             "userType": "external", "entrypoint": "cli", "cwd": cwd, "sessionId": sid, "version": "2.1.318", "gitBranch": "main"}
        r.update(kw)
        last[0] = r["uuid"]
        return r
    m = "claude-fake-opus"
    old_todo, new_todo = "1. 곱셈 기호 `x` 도 받기\n2. 소수점 반올림 자리 정하기", "- [ ] 곱셈 기호 `x` 도 받기\n- [ ] 소수점 반올림 자리 정하기"
    recs = [
        rec("user", uuid="c3-turn-1", promptId="p1", turnPosition={"promptIndex": 0, "turnIndex": 1},
            message={"role": "user", "content": "README 의 할 일을 체크 목록으로 바꿔 줘"}),
        rec("assistant", message={"id": "msg_c3a", "model": m, "content": [{"type": "thinking", "thinking": "**README 읽기** 할 일 절을 찾는다."}]}, apiBlockIndex=0),
        rec("assistant", message={"id": "msg_c3a", "model": m, "content": [{"type": "text", "text": "README 를 먼저 읽겠습니다."}]}, apiBlockIndex=1),
        rec("assistant", message={"id": "msg_c3a", "model": m, "stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "toolu_c3r", "name": "Read", "input": {"file_path": os.path.join(cwd, "README.md")}}]}, apiBlockIndex=2),
        rec("user", message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_c3r", "content": "1\t# 계산기 예제"}]}),
        rec("assistant", message={"id": "msg_c3b", "model": m, "stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "toolu_c3e", "name": "Edit", "input": {"file_path": os.path.join(cwd, "README.md"),
                                                                          "old_string": old_todo, "new_string": new_todo}}]}, apiBlockIndex=0),
        rec("user", message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_c3e", "content": "The file has been updated."}]}),
        rec("assistant", message={"id": "msg_c3c", "model": m, "stop_reason": "end_turn", "content": [{"type": "text", "text":
            "할 일 두 개를 **체크 목록**으로 바꿨습니다.\n\n- `README.md` 의 `## 할 일` 절만 고쳤습니다.\n- 다른 절은 그대로입니다."}]},
            apiBlockIndex=0, effort="high"),
        rec("system", subtype="stop_hook_summary"),
        {"type": "ai-title", "aiTitle": "README 할 일 체크 목록", "sessionId": sid},
    ]
    with open(path, "w") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.utime(path, (t0, t0))
    return path


CLAUDE_S9 = "c1a0de00-0000-4000-8000-000000000009"


def build_claude_subagent(home, proj, t0):
    """CLI 에서 도는 Claude 세션 — 하위 에이전트가 1분 전부터 긴 명령(제한 시간 10분)을 돌리는 중이다.

    부모 기록은 Agent 호출에서, 하위 기록(<세션>/subagents/)은 명령 호출에서 멈춰 있다. 30초 넘게 조용하지만
    명령의 제한 시간 안이라 '지연'이 아니라 '실행 중'이어야 한다."""
    from datetime import datetime, timezone
    cwd = os.path.realpath(proj)
    d = os.path.join(home, "projects", "".join(c if c.isalnum() else "-" for c in cwd))
    os.makedirs(d, exist_ok=True)
    sid = CLAUDE_S9

    def iso(t):
        return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")
    base = {"isSidechain": False, "userType": "external", "entrypoint": "cli", "cwd": cwd, "sessionId": sid,
            "version": "2.1.318", "gitBranch": "main"}
    recs = [
        dict(base, type="user", uuid="c9-1", parentUuid=None, timestamp=iso(t0 - 95),
             message={"role": "user", "content": "시험 돌리고 메인 반영해 줘"}),
        dict(base, type="assistant", uuid="c9-2", parentUuid="c9-1", timestamp=iso(t0 - 90),
             message={"id": "msg_c9a", "model": "claude-fake-opus", "stop_reason": "tool_use", "content": [
                 {"type": "tool_use", "id": "toolu_c9agent", "name": "Agent", "input": {
                     "subagent_type": "general-purpose", "description": "메인 반영", "prompt": "시험을 돌리고 병합한다."}}]}),
    ]
    path = os.path.join(d, sid + ".jsonl")
    with open(path, "w") as f:
        f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs))
    os.utime(path, (t0 - 90, t0 - 90))
    sub = os.path.join(d, sid, "subagents")
    os.makedirs(sub, exist_ok=True)
    with open(os.path.join(sub, "agent-c9.meta.json"), "w") as f:
        json.dump({"agentType": "general-purpose", "description": "메인 반영", "toolUseId": "toolu_c9agent"}, f)
    srecs = [
        dict(base, type="user", isSidechain=True, uuid="c9s-1", timestamp=iso(t0 - 89),
             message={"role": "user", "content": "시험을 돌리고 병합한다."}),
        dict(base, type="assistant", isSidechain=True, uuid="c9s-2", timestamp=iso(t0 - 60),
             message={"id": "msg_c9s", "model": "claude-fake-sonnet", "stop_reason": "tool_use", "content": [
                 {"type": "tool_use", "id": "toolu_c9bash", "name": "Bash", "input": {
                     "command": "nice -n 5 python3 -m unittest discover -s tests", "timeout": 600000}}]}),
    ]
    spath = os.path.join(sub, "agent-c9.jsonl")
    with open(spath, "w") as f:
        f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in srecs))
    os.utime(spath, (t0 - 60, t0 - 60))
    return path


OUTPUT_S1 = "01a1aaaa0000000000000000000000d1"   # 할 일 · 정할 것 · 메인 반영이 모두 있는 답
OUTPUT_S2 = "01a1aaaa0000000000000000000000d2"   # 메인 반영 요청만 있는 답


def output_threads(proj):
    """agent-output/1 최종 답을 가진 Codex 스레드 둘 — 타입별 카드 데모·회귀(browser_check --outputs-only)용.

    진짜 기록처럼 todos·decisions 는 JSON 글로, merge 는 "null" 글로 감싼 것도 섞는다."""
    t0 = int(time.time()) - 600
    report = {
        "protocol": "agent-output/1",
        "body": "세션 목록에 진행 상태를 더했습니다. 작업 브랜치 `fix/status` 에 커밋했고 `main` 에는 아직 넣지 않았습니다.\n\n"
                "## 바뀐 것\n- 목록 행에 **실행 중** · **지연** 표시\n- 세션 화면 진행 줄에 단계 · 경과 · 마지막 신호",
        "todos": json.dumps([
            {"id": "demo", "title": "확인이 끝나면 데모를 내린다", "detail": "데모 서버를 끄고 이 작업에서 붙인 HTTPS 매핑만 내린다.",
             "owner": "agent", "project": None},
            {"id": "device", "title": "실제 휴대폰에서 안전 영역 확인", "detail": "하단 내비게이션 바와 입력창이 겹치지 않는지 본다.",
             "owner": "user", "project": None}], ensure_ascii=False),
        "decisions": json.dumps([
            {"id": "list-states", "header": "목록 상태", "question": "세션 목록 행에 어떤 상태를 보일까요?",
             "options": [{"label": "마지막 턴 상태를 모두 보인다", "description": "실행 중·지연·멈춤·실패·끝나지 않음을 보이고 완료는 숨긴다.", "recommended": True},
                         {"label": "실행 중과 지연만 더한다", "description": "멈춤·실패는 세션 화면에서만 본다.", "recommended": False},
                         {"label": "지금처럼 둔다", "description": "", "recommended": False}],
             "multiSelect": False, "allowOther": True},
            {"id": "notify", "header": "알림", "question": "어떤 때 알림을 보낼까요? 여러 개 고를 수 있습니다.",
             "options": [{"label": "턴이 끝났을 때", "description": "", "recommended": True},
                         {"label": "30초 넘게 신호가 없을 때", "description": "지연으로 바뀐 순간 한 번", "recommended": True},
                         {"label": "실패했을 때", "description": "", "recommended": False}],
             "multiSelect": True, "allowOther": False}], ensure_ascii=False),
        "merge": {"summary": "v0.16.0 — 세션 목록·화면에 진행 상태(실행 중·지연·멈춤·실패)를 보인다.", "bump": "minor", "version": "0.16.0",
                  "checks": ["python3 -m unittest discover -s tests — 77개 통과", "browser_check.py 통과", "데모 HTTPS 접속 확인"]},
    }
    merge_only = {"protocol": "agent-output/1", "body": "고쳤습니다. 메인 반영만 남았습니다.", "todos": [], "decisions": [],
                  "merge": {"summary": "v0.15.2 — 답변 속 표가 좁은 화면에서 가로로 넘치지 않게 한다.", "bump": "patch", "version": "0.15.2",
                            "checks": ["412×915 회귀 통과"]}}

    def thread(tid, title, ask, out, dt):
        return {"id": tid, "preview": ask, "cwd": proj, "model": "gpt-fake-sol", "reasoningEffort": "medium",
                "createdAt": t0 + dt, "updatedAt": t0 + dt + 60, "source": "cli", "path": None, "cliVersion": "0.165.0",
                "name": title, "gitInfo": {"branch": "fix/status"},
                "turns": [{"id": tid + "-t1", "status": "completed", "startedAt": t0 + dt, "completedAt": t0 + dt + 60,
                           "durationMs": 60000, "error": None, "items": [
                               {"type": "userMessage", "id": tid + "-u1", "content": [{"type": "text", "text": ask}]},
                               {"type": "agentMessage", "id": tid + "-m1", "phase": "final_answer",
                                "text": json.dumps(out, ensure_ascii=False)}]}]}
    return [thread(OUTPUT_S1, "[카드] 진행 상태 표시", "세션 진행 상태가 보이게 해 줘", report, 0),
            thread(OUTPUT_S2, "[카드] 표 넘침 고치기", "답변 표가 모바일에서 넘쳐", merge_only, 30)]
