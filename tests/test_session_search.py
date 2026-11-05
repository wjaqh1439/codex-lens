"""session_search 의 색인·갱신·일치 규칙. 실행: python3 -m unittest discover -s tests -v"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import session_search as ss  # noqa: E402


class FakeList:
    """updated_at 내림차순 쪽 나누기만 흉내 낸 thread/list."""

    def __init__(self, items):
        self.items = list(items)
        self.calls = 0

    def __call__(self, p):
        self.calls += 1
        items = sorted(self.items, key=lambda t: t["updatedAt"], reverse=True)
        off = int(p.get("cursor") or 0)
        lim = p["limit"]
        more = len(items) > off + lim
        return {"data": items[off:off + lim], "nextCursor": str(off + lim) if more else None}


def th(i, title, up, cwd="/w/proj"):
    return {"id": "t%d" % i, "name": None, "preview": title + "\n\n[컨텍스트] 본문 낱말", "cwd": cwd, "updatedAt": up,
            "turns": []}


class MatchTest(unittest.TestCase):
    def test_words_case_and_folder(self):
        t = th(1, "멘션 이후 추천 탭이 잘려서 안보임", 1, cwd="/x/261031 codex-lens/")
        self.assertTrue(ss.matches(t, ss.terms("추천 멘션")))
        self.assertTrue(ss.matches(t, ss.terms("CODEX-LENS 멘션")))
        self.assertFalse(ss.matches(t, ss.terms("본문")))       # 첫 줄 아래 붙인 글은 제목이 아니다
        self.assertTrue(ss.matches(dict(t, name="Claude 붙이기"), ss.terms("claude")))
        self.assertEqual(ss.terms("  "), [])


class IndexTest(unittest.TestCase):
    def setUp(self):
        self._old = ss.PAGE, ss.SYNC_SEC
        ss.PAGE, ss.SYNC_SEC = 3, 0

    def tearDown(self):
        ss.PAGE, ss.SYNC_SEC = self._old

    def test_build_then_sync_only_new_pages(self):
        src = FakeList([th(i, "세션 %d" % i, 100 + i) for i in range(10)])
        idx = ss.CodexIndex(src, ["cli"])
        got = idx.snapshot()
        self.assertEqual(len(got), 10)
        self.assertNotIn("turns", got[0])
        # 새 세션과 갱신된 옛 세션 — 앞쪽만 다시 읽어 반영한다
        src.items.append(th(99, "새로 만든 세션", 500))
        src.items[0] = th(0, "이름 바뀐 세션", 501)
        before = src.calls
        got = {t["id"]: t for t in idx.snapshot()}
        self.assertEqual(len(got), 11)
        self.assertEqual(got["t0"]["preview"].split("\n")[0], "이름 바뀐 세션")
        self.assertLessEqual(src.calls - before, 2)

    def test_list_rows_are_searchable_during_sync_interval(self):
        ss.SYNC_SEC = 300
        src = FakeList([th(1, "옛 제목", 100, cwd="/w/retired")])
        idx = ss.CodexIndex(src, ["cli"])
        idx.snapshot()
        before = src.calls
        idx.remember([th(2, "새 세션", 102), th(1, "바뀐 제목", 103, cwd="/w/new-folder")])
        got = {t["id"]: t for t in idx.snapshot()}
        self.assertEqual(src.calls, before)
        self.assertTrue(ss.matches(got["t2"], ss.terms("새 세션")))
        self.assertTrue(ss.matches(got["t1"], ss.terms("바뀐 new-folder")))
        self.assertFalse(ss.matches(got["t1"], ss.terms("retired")))
        idx.remember([th(1, "오래된 제목", 99)])
        self.assertEqual(idx.snapshot()[0]["preview"].split("\n")[0], "바뀐 제목")

    def test_partial_list_does_not_advance_sync_boundary(self):
        src = FakeList([th(i, "기존 %d" % i, 100 + i) for i in range(4)])
        idx = ss.CodexIndex(src, ["cli"])
        idx.snapshot()
        boundary = idx.top
        src.items.extend(th(i, "새 세션 %d" % i, 100 + i) for i in range(4, 14))
        idx.remember([src.items[-1]])
        self.assertEqual(idx.top, boundary)
        self.assertEqual(len(idx.snapshot()), 14)

    def test_rebuild_keeps_new_rows_seen_while_reading(self):
        ss.SYNC_SEC = 300
        src = FakeList([th(1, "원본", 100)])
        idx = ss.CodexIndex(src, ["cli"])
        idx.snapshot()
        idx.remember([th(9, "지워진 옛 세션", 99)])
        def req(p):
            idx.remember([th(2, "읽는 중 새 세션", 102)])
            return src(p)
        idx.request = req
        idx._build()
        got = {t["id"]: t for t in idx.snapshot()}
        self.assertIn("t2", got)
        self.assertNotIn("t9", got)

    def test_failed_build_retries(self):
        state = {"fail": True}

        def req(p):
            if state["fail"]:
                raise RuntimeError("app-server down")
            return {"data": [th(1, "a", 1)], "nextCursor": None}
        idx = ss.CodexIndex(req, ["cli"])
        with self.assertRaises(RuntimeError):
            idx.snapshot()
        state["fail"] = False
        self.assertEqual(len(idx.snapshot()), 1)


if __name__ == "__main__":
    unittest.main()
