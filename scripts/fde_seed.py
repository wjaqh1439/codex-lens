#!/usr/bin/env python3
"""FDE 케이스 데모 · 회귀 시드 — 이해 · 계획 · 검증까지 간 케이스(가짜 에이전트 답을 그대로 반영).

python3 scripts/fde_seed.py <폴더>
남겨 두는 것: 이해 카드의 답하지 않은 선택지, 계획의 해결 방법 선택지, 실패한 검증 하나.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import fde  # noqa: E402
from fake_app_server import fake_fde  # noqa: E402

BRIEF = """# 설비 점검 경로

현장 점검원이 하루에 설비를 돌며 점검한다. 점검원마다 근무 시간 안에 맡은 설비를 모두 점검해야 한다.
점검원 한 명이 하루에 점검할 수 있는 설비는 최대 12대다.
설비 사이 이동 시간은 travel_times.csv 만 쓴다(방향별로 다를 수 있다).
"""


def answer(cwd, case, step):
    fde.apply_result(cwd, case, {"step": step}, {"answer": fake_fde(fde.build_prompt(cwd, case, step)), "model": "gpt-demo", "effort": "low"})


def build(cwd):
    os.makedirs(cwd, exist_ok=True)
    with open(os.path.join(cwd, "brief.md"), "w", encoding="utf-8") as f:
        f.write(BRIEF)
    with open(os.path.join(cwd, "travel_times.csv"), "w", encoding="utf-8") as f:
        f.write("from,to,minutes\nE1,E2,7\nE2,E1,9\n")
    case = fde.empty_case()
    case["problem"] = "점검원별 하루 점검 경로를 만들어야 한다. 모든 설비를 근무 시간 안에 점검하고, 이동 시간은 표만 쓴다."
    fde.add_context(cwd, case, {"path": "brief.md"})
    fde.add_context(cwd, case, {"path": "travel_times.csv"})
    fde.add_context(cwd, case, {"kind": "text", "text": "담당자: 점검원 한 명의 이동 시간은 하루 90분을 넘기지 않으면 좋겠다."})
    answer(cwd, case, "understand")
    answer(cwd, case, "plan")
    case["checks"].append({"id": fde._next(case, "V"), "name": "근무 시간 초과 검사", "command": "echo '점검원 P3 18분 초과' >&2; exit 1", "by": "user"})
    for c in case["checks"]:
        r = fde.run_check(cwd, c["command"])
        r.update(check=c["id"], name=c["name"], at=fde._now(), plan_v=1)
        case["runs"].append(r)
    fde.save(cwd, case)
    return case


if __name__ == "__main__":
    c = build(os.path.abspath(sys.argv[1]))
    print(json.dumps({"plan": len(c["plan"]), "runs": len(c["runs"])}))
