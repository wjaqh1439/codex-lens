"""세션 부모·자식 묶기(session_tree.py). 실행: python3 -m unittest discover -s tests -v"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from session_tree import Launches, group, thread_parent  # noqa: E402


def m(i, parent=None):
    out = {"id": i, "title": i}
    if parent:
        out["parent"] = parent
    return out


def ids(items):
    return [(x["id"], ids(x.get("children") or [])) if x.get("children") else x["id"] for x in items]


class GroupTest(unittest.TestCase):
    def test_child_on_same_page_goes_under_parent_and_lifts_it(self):
        seen = set()
        # 갱신 순: 자식 c 가 가장 최근, 부모 p 는 오래됨 — 묶음은 c 자리(맨 위)에 선다
        out = group([m("c", "p"), m("x"), m("p"), m("y")], seen, lambda i: None)
        self.assertEqual(ids(out), [("p", ["c"]), "x", "y"])
        self.assertEqual(seen, {"c", "x", "p", "y"})

    def test_missing_parent_is_pulled_up_once(self):
        seen, asked = set(), []

        def meta(i):
            asked.append(i)
            return {"id": i, "title": "부모"}
        out = group([m("a"), m("c1", "p"), m("c2", "p")], seen, meta)
        self.assertEqual(ids(out), ["a", ("p", ["c1", "c2"])])
        self.assertEqual(asked, ["p"])
        # 다음 쪽: 부모 자신과 더 오래된 자식 — 부모는 다시 내지 않고 자식만 낸다(화면이 이미 있는 부모 아래에 붙인다)
        out = group([m("p"), m("c3", "p"), m("b")], seen, meta)
        self.assertEqual(ids(out), ["c3", "b"])
        self.assertEqual(out[0]["parent"], "p")

    def test_unreadable_parent_keeps_child_top_level(self):
        out = group([m("c", "gone")], set(), lambda i: None)
        self.assertEqual(ids(out), ["c"])

    def test_grandchildren_and_cycles(self):
        out = group([m("g", "c"), m("c", "p"), m("p")], set(), lambda i: None)
        self.assertEqual(ids(out), [("p", [("c", ["g"])])])
        out = group([m("a", "b"), m("b", "a")], set(), lambda i: None)
        self.assertEqual(sorted(x["id"] for x in out), ["a", "b"])

    def test_does_not_mutate_input(self):
        src = [m("c", "p"), m("p")]
        group(src, set(), lambda i: None)
        self.assertNotIn("children", src[1])


class ParentSourceTest(unittest.TestCase):
    def test_codex_subagent(self):
        th = {"id": "c", "source": {"subAgent": {"thread_spawn": {"parent_thread_id": "p", "agent_nickname": "Pascal"}}}}
        self.assertEqual(thread_parent(th), ("p", "Pascal"))
        self.assertEqual(thread_parent({"id": "c", "parentThreadId": "p", "agentRole": "worker"}), ("p", "worker"))
        self.assertEqual(thread_parent({"id": "c", "source": "cli"}), (None, None))


class LaunchesTest(unittest.TestCase):
    def test_session_created_while_command_ran(self):
        lw = Launches(os.path.join(tempfile.mkdtemp(prefix="launch-"), "launches.json"))
        kid = "0A1B2C3D-0000-4000-8000-000000000001"
        out = "── 새 세션 · 시작\n{\"session_id\": \"%s\"}\nparent 01a1aaaa-0000-0000-0000-00000000000f" % kid
        self.assertEqual(lw.observe("01a1aaaa-0000-0000-0000-00000000000f", out, 100.0, 130.0), 1)   # 자기 ID 는 뺀다
        self.assertEqual(lw.observe("01a1aaaa-0000-0000-0000-00000000000f", out, 100.0, 131.0), 0)   # 같은 명령 — 다시 쓰지 않는다
        self.assertEqual(lw.parent_of(kid.lower(), 112), "01a1aaaa-0000-0000-0000-00000000000f")
        # 명령 전에 이미 있던 세션(목록·검색 출력에 나온 옛 세션)은 자식이 아니다
        self.assertIsNone(lw.parent_of(kid.lower(), 50))
        self.assertIsNone(lw.parent_of("ffffffff-0000-4000-8000-000000000000", 112))
        # 다시 읽어도 남는다
        self.assertEqual(Launches(lw.store.path).parent_of(kid.lower(), 129), "01a1aaaa-0000-0000-0000-00000000000f")


if __name__ == "__main__":
    unittest.main()
