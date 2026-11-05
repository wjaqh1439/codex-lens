"""Codex 응답별 실측 생성 속도: 기록(rollout)의 출력 토큰 ÷ 응답 시간. 도구 실행 시간은 빼고 턴별로 나눈다."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from codex_speed import ResponseClock, RolloutRates, summarize


def line(sec, kind, payload):
    m, s = divmod(sec, 60)
    return json.dumps({"timestamp": "2026-11-05T07:%02d:%06.3fZ" % (m, s), "ordinal": 1, "type": kind,
                       "payload": payload}) + "\n"


def item(sec, ptype, **kw):
    return line(sec, "response_item", dict({"type": ptype}, **kw))


def usage(sec, out):
    return line(sec, "event_msg", {"type": "token_count", "info": {"last_token_usage": {
        "input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": out, "reasoning_output_tokens": 5, "total_tokens": 1000 + out}}})


TURN_A = [
    line(0, "event_msg", {"type": "task_started", "turn_id": "A"}),
    item(0.1, "message", id="m1", role="developer", content=[]),
    item(0.2, "message", id="m2", role="user", content=[]),
    item(1.0, "reasoning", summary=[]),
    item(1.6, "custom_tool_call", name="exec", input="ls"),     # 응답 1: 0.2 → 1.6 (1.4초)
    item(4.6, "custom_tool_call_output", output="x" * 50000),  # 도구 3초 — 속도에 넣지 않는다
    usage(4.6, 140),
    item(6.1, "message", id="m3", role="assistant", content=[]),  # 응답 2: 4.6 → 6.1 (1.5초)
    usage(6.1, 300),
    line(6.2, "event_msg", {"type": "task_complete", "turn_id": "A"}),
]


class ClockTest(unittest.TestCase):
    def test_usage_without_output_is_ignored(self):
        c = ResponseClock()
        c.begin(0)
        self.assertIsNone(c.usage(100))          # 모델 항목 없이 온 사용량(속도 계산 불가)
        c.output(2)
        self.assertEqual(c.usage(100), {"out": 100, "reasoning": 0, "sec": 2})
        self.assertIsNone(c.usage(100))          # 같은 응답을 두 번 세지 않는다

    def test_summary(self):
        self.assertIsNone(summarize([]))
        s = summarize([{"out": 140, "sec": 1.4}, {"out": 300, "sec": 1.5}])
        self.assertEqual((s["tps"], s["avg_tps"], s["responses"], s["total_out"]), (200.0, 151.7, 2, 440))


class RolloutTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "rollout.jsonl")

    def write(self, text, mode="a"):
        with open(self.path, mode) as f:
            f.write(text)

    def test_turn_responses_exclude_tool_time(self):
        self.write("".join(TURN_A), "w")
        rows = RolloutRates().turn(self.path, "A")
        self.assertEqual([(r["out"], r["sec"]) for r in rows], [(140, 1.4), (300, 1.5)])

    def test_incremental_partial_line_new_turn_and_reset(self):
        rr = RolloutRates()
        head, tail = TURN_A[:8], TURN_A[8:]
        self.write("".join(head) + tail[0][:25], "w")         # 마지막 줄이 반만 쓰였다
        self.assertEqual(len(rr.turn(self.path, "A")), 1)
        self.write(tail[0][25:] + "".join(tail[1:]))
        self.assertEqual(len(rr.turn(self.path, "A")), 2)
        # 다음 턴은 따로 센다 — 이전 턴의 마지막 시각을 끌어오지 않는다
        self.write(line(60, "event_msg", {"type": "task_started", "turn_id": "B"})
                   + item(60.5, "message", id="u", role="user", content=[])
                   + item(61.5, "message", id="a", role="assistant", content=[]) + usage(61.5, 50))
        self.assertEqual([(r["out"], r["sec"]) for r in rr.turn(self.path, "B")], [(50, 1.0)])
        self.assertEqual(len(rr.turn(self.path, "A")), 2)
        # 파일이 바뀌면(짧아짐) 처음부터 다시 읽는다
        self.write("".join(TURN_A[:7]), "w")
        self.assertEqual(len(rr.turn(self.path, "A")), 1)
        self.assertEqual(rr.turn(self.path, "B"), [])

    def test_missing_or_broken(self):
        rr = RolloutRates()
        self.assertEqual(rr.turn(None, "A"), [])
        self.assertEqual(rr.turn(os.path.join(self.dir, "none.jsonl"), "A"), [])
        self.write("not json\n{\"timestamp\":\"bad\",\"type\":\"event_msg\",\"payload\":{\"type\":\"token_count\"}}\n" + "".join(TURN_A), "w")
        self.assertEqual(len(rr.turn(self.path, "A")), 2)


if __name__ == "__main__":
    unittest.main()
