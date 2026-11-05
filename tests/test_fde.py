"""FDE 케이스(fde.py) — 단위(컨텍스트 · 파싱 · 검증 실행 · 보고서)와 가짜 에이전트로 HTTP 사이클 한 바퀴.
실행: python3 -m unittest tests.test_fde -v"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import fde  # noqa: E402
from fake_app_server import fake_fde  # noqa: E402


class UnitTest(unittest.TestCase):
    def setUp(self):
        self.cwd = tempfile.mkdtemp(prefix="fde-unit-")
        with open(os.path.join(self.cwd, "brief.md"), "w") as f:
            f.write("하루 최대 12대를 점검한다.\n")
        self.case = fde.empty_case()
        self.case["problem"] = "설비 점검 경로를 만든다"

    def tearDown(self):
        shutil.rmtree(self.cwd, ignore_errors=True)

    def test_context_and_prompt(self):
        self.assertEqual(fde.add_context(self.cwd, self.case, {"path": "brief.md"})["id"], "C1")
        self.assertEqual(fde.add_context(self.cwd, self.case, {"kind": "text", "text": "담당자: 하루 이동 90분 이내"})["id"], "C2")
        with self.assertRaises(ValueError):
            fde.add_context(self.cwd, self.case, {"path": "없는.md"})
        self.case["feedback"].append({"id": "F1", "target": "understanding", "text": "용어를 더 쉽게", "status": "open"})
        p = fde.build_prompt(self.cwd, self.case, "understand")
        self.assertTrue(p.startswith("[FDE 케이스 — codex-lens · step=understand]"))
        for s in ("설비 점검 경로를 만든다", "하루 최대 12대를 점검한다.", "하루 이동 90분 이내", "용어를 더 쉽게", "도구 없이"):
            self.assertIn(s, p)

    def test_parse(self):
        u = fde.parse("understand", fake_fde("[FDE 케이스 — codex-lens · step=understand]"))
        self.assertEqual(len(u["choices"]), 1)
        self.assertEqual(sum(o["recommended"] for o in u["choices"][0]["options"]), 1)
        p = fde.parse("plan", fake_fde("[FDE 케이스 — codex-lens · step=plan]"))
        self.assertEqual([m["main"] for m in p["modules"]], [False, True, False])
        self.assertEqual(p["options"][0]["eval"]["구현 난이도"], "쉬움")
        with self.assertRaises(ValueError):
            fde.parse("understand", "형식을 지키지 않은 답")
        with self.assertRaises(ValueError):
            fde.parse("plan", json.dumps({"headline": "x", "modules": []}))

    def test_run_check_and_metrics(self):
        self.assertEqual(fde.parse_metrics("x\nMETRIC violations=0\nMETRIC t=1.5\nMETRIC bad=abc"), {"violations": 0, "t": 1.5})
        ok = fde.run_check(self.cwd, "echo METRIC travel=412")
        self.assertEqual((ok["ok"], ok["metrics"]), (True, {"travel": 412}))
        bad = fde.run_check(self.cwd, "echo 실패 >&2; exit 3")
        self.assertEqual((bad["ok"], bad["exit"]), (False, 3))
        self.assertIn("실패", bad["tail"])
        self.assertFalse(fde.run_check(self.cwd, "sleep 5", timeout=0.3)["ok"])
        self.case["runs"] = [dict(ok, check="V1", plan_v=1), dict(ok, check="V1", plan_v=2, metrics={"travel": 380})]
        self.assertEqual(fde.metric_table(self.case), {"1": {"travel": 412}, "2": {"travel": 380}})

    def test_decide_and_report(self):
        fde.apply_result(self.cwd, self.case, {"step": "understand"}, {"answer": fake_fde("[FDE 케이스 — codex-lens · step=understand]")})
        cid = self.case["understanding"][0]["data"]["choices"][0]["id"]
        d = fde.decide(self.case, {"id": cid, "chosen": "업무량 균형 먼저"})
        self.assertEqual((d["id"], d["recommended"]), ("D1", "이동 시간 먼저"))
        with self.assertRaises(ValueError):
            fde.decide(self.case, {"id": cid, "chosen": "다시"})
        fde.save(self.cwd, self.case)
        rep = open(os.path.join(self.cwd, "fde", "CASE.md"), encoding="utf-8").read()
        self.assertIn("→ **업무량 균형 먼저**", rep)
        self.assertEqual(fde.load(self.cwd)["decisions"][0]["chosen"], "업무량 균형 먼저")


class FlowTest(unittest.TestCase):
    """가짜 에이전트로 이해 → 계획 → 검증 → 개선 → 사용 한 바퀴."""

    @classmethod
    def setUpClass(cls):
        import test_app
        cls.T = test_app
        test_app.setUpModule()
        cls.S = test_app.SRV
        cls.cwd = tempfile.mkdtemp(prefix="fde-flow-")
        with open(os.path.join(cls.cwd, "brief.md"), "w") as f:
            f.write("하루 최대 12대를 점검한다.\n")

    @classmethod
    def tearDownClass(cls):
        cls.T.tearDownModule()
        shutil.rmtree(cls.cwd, ignore_errors=True)

    def call(self, method, path, body=None, expect=200):
        st, r = self.S.req(method, path, body)
        self.assertEqual(st, expect, r)
        return r

    def get(self, cwd=None):
        return self.call("GET", "/api/fde?cwd=" + urllib.parse.quote(cwd or self.cwd))

    def step(self, step, cwd=None, **kw):
        r = self.call("POST", "/api/fde/step", dict(cwd=cwd or self.cwd, step=step, **kw))
        self.assertNotIn("prompt", r["job"])
        return self.T.wait(lambda: (lambda v: v if not v["case"]["pending"] else None)(self.get(cwd)), 15)

    def act(self, action, expect=200, **kw):
        return self.call("POST", "/api/fde/act/" + action, dict(cwd=self.cwd, **kw), expect)

    def test_cycle(self):
        self.assertIsNone(self.get()["case"])
        self.call("POST", "/api/fde", {"cwd": self.cwd, "problem": "설비 점검 경로를 만든다"})
        self.act("context", path="brief.md")
        self.call("POST", "/api/fde/step", {"cwd": self.cwd, "step": "plan"}, 400)   # 이해부터

        v = self.step("understand")
        und = v["case"]["understanding"][-1]
        d = self.act("decide", id=und["data"]["choices"][0]["id"], chosen="이동 시간 먼저")["result"]
        self.assertEqual(d["id"], "D1")

        v = self.step("ask", target="understanding", question="하루 한도는 바꿀 수 있나요?")
        self.assertIn("하루 한도는 바꿀 수 있나요", v["case"]["qa"][-1]["data"]["headline"])
        self.act("feedback", target="understanding", text="야간 점검은 범위 밖이라고 적어 줘")
        v = self.step("understand")
        self.assertTrue(v["case"]["understanding"][-1]["data"]["headline"].startswith("피드백 반영"))
        self.assertEqual(v["case"]["feedback"][0]["applied_in"], 2)

        v = self.step("plan")
        self.assertEqual([c["name"] for c in v["case"]["checks"]], ["규칙 위반 세기", "단위 시험"])
        self.act("check_add", name="실패하는 검사", command="exit 1")
        r = self.call("POST", "/api/fde/run", {"cwd": self.cwd})
        self.assertEqual([x["ok"] for x in r["runs"]], [True, True, False])
        self.assertEqual(r["metrics"], {"1": {"violations": 0, "travel": 412}})

        # 계획을 다시 만들면 메인 알고리즘 다음 버전 — 바뀐 검증 명령이 더해지고 지표가 v2 에 붙는다
        v = self.step("plan")
        self.assertEqual(v["case"]["plan"][-1]["data"]["change"], "경계 재배정을 더했다 — travel 을 줄이는 것이 목표")
        new = [c for c in v["case"]["checks"] if "travel=380" in c["command"]][0]
        r = self.call("POST", "/api/fde/run", {"cwd": self.cwd, "ids": [new["id"]]})
        self.assertEqual(r["metrics"]["2"], {"violations": 0, "travel": 380})

        self.act("usage", text="월요일엔 경로 하나가 근무 시간을 넘겼다")
        self.act("usage", expect=400, text="")
        self.assertIn("근무 시간을 넘겼다", fde.build_prompt(self.cwd, self.get()["case"], "understand"))
        rep = open(os.path.join(self.cwd, "fde", "CASE.md"), encoding="utf-8").read()
        for s in ("## 이해 (v2)", "## 계획 (v2)", "## 메인 알고리즘 버전 × 지표", "## 결정 기록", "## 사용 기록"):
            self.assertIn(s, rep)

    def test_broken_answer_is_error(self):
        cwd = tempfile.mkdtemp(prefix="fde-broken-")
        try:
            self.call("POST", "/api/fde", {"cwd": cwd, "problem": "망가진 답 주기"})
            v = self.step("understand", cwd=cwd)
            self.assertEqual(v["case"]["understanding"], [])
            self.assertIn("결과를 읽지 못했습니다", v["case"]["errors"][-1]["error"])
        finally:
            shutil.rmtree(cwd, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
