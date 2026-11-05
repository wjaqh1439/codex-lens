"""가짜 app-server 로 HTTP API 전체 흐름을 검증한다. 실행: python3 -m unittest discover -s tests -v"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from unittest.mock import Mock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="codex-lens-test-")
for k in list(os.environ):
    if k.startswith("CODEX_LENS"):
        os.environ.pop(k)
os.environ["CODEX_LENS_HOME"] = os.path.join(TMP, "home")
os.environ["CODEX_LENS_FEEDBACK_ORIGIN"] = ""
os.environ["CODEX_LENS_HUB_URL"] = ""   # 진짜 agent-hub 에 닿지 않는다(허브 연동은 가짜 허브로 따로 본다)
os.environ["CODEX_LENS_PORT_RANGE"] = "29400-29499"   # 시험용 대역 — 운영 대역과 겹치지 않게
os.environ["CODEX_LENS_PORT_EXPOSE"] = ""   # 진짜 tailscale serve 매핑을 만들지 않는다(기본 내보내기 끔)
os.environ["CODEX_LENS_EXTERNAL_WATCH_SEC"] = "0.15"   # 다른 곳의 턴이 끝났는지 서버가 확인하는 간격(운영 기본 3초)
os.environ["CODEX_HOME"] = os.path.join(TMP, "codex-home")   # 진짜 ~/.codex 를 건드리지 않는다
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(TMP, "claude-home")   # 진짜 ~/.claude 도 건드리지 않는다
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import app as appmod  # noqa: E402
import seed  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402
from lens import Lens, MENTION_MARK  # noqa: E402
from codex_rpc import RpcError  # noqa: E402
from turns import normalize_turn, unwrap_output  # noqa: E402
from store import JsonStore  # noqa: E402

SEED, PROJ = seed.build(TMP, external=True)
EXT_END = os.path.join(TMP, "external-end")   # 가짜 app-server 가 밖에서 돌던 턴을 끝난 것으로 보이게 하는 파일(줄마다 `ID[:상태]`)
with open(SEED) as _f:
    _seed = json.load(_f)
_seed["threads"] += seed.external_queue_threads(PROJ)
with open(SEED, "w") as _f:
    json.dump(_seed, _f, ensure_ascii=False)
LOG = os.path.join(TMP, "fake-calls.jsonl")
CLOG = os.path.join(TMP, "fake-claude-calls.jsonl")
S1 = "01a1aaaa000000000000000000000001"
S3 = seed.CLAUDE_S3          # 가짜 Claude Code 기록


def calls(method=None):
    out = []
    if os.path.exists(LOG):
        with open(LOG) as f:
            for line in f:
                o = json.loads(line)
                if method is None or o["method"] == method:
                    out.append(o)
    return out


class Server:
    def __init__(self):
        env = dict(os.environ, FAKE_CODEX_SEED=SEED, FAKE_CODEX_LOG=LOG, FAKE_CODEX_DELAY="0.02",
                   FAKE_CODEX_PERSIST_DELAY="0.8", FAKE_CODEX_EXTERNAL_END=EXT_END,
                   FAKE_CODEX_ROLLOUT_DIR=os.path.join(TMP, "rollouts"))
        cenv = dict(os.environ, FAKE_CLAUDE_LOG=CLOG, FAKE_CLAUDE_DELAY="0.02")
        self.lens = Lens(os.environ["CODEX_LENS_HOME"], "test", codex_env=env,
                         codex_argv=[sys.executable, os.path.join(ROOT, "tests", "fake_app_server.py")],
                         claude_env=cenv, claude_argv=[sys.executable, os.path.join(ROOT, "tests", "fake_claude.py")])
        self.app = appmod.App(self.lens)
        appmod.Handler.app = self.app
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), appmod.Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method,
                                   headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def text(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path), timeout=10) as resp:
            return resp.read().decode()


SRV = None


def setUpModule():
    global SRV
    SRV = Server()


def tearDownModule():
    SRV.httpd.shutdown()
    SRV.lens.client.shutdown_backend()


def rd(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def wait(fn, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("시간 안에 조건이 맞지 않았다")


class HistoryReadTest(unittest.TestCase):
    def setUp(self):
        self.lens = Lens.__new__(Lens)
        self.lens.lock = threading.RLock()
        self.lens.starting_threads = {}
        self.lens.live = {}
        self.lens.client = Mock()
        self.empty = RpcError("failed to read session metadata: rollout at /fake/rollout.jsonl is empty")

    @patch("lens.time.sleep")
    def test_external_new_thread_retries_only_read(self, sleep):
        self.lens.client.request.side_effect = [self.empty, self.empty, {"thread": {"id": "new", "turns": []}}]
        th, turns, pending = self.lens._raw_turns("new")
        self.assertEqual(th["id"], "new")
        self.assertEqual(turns, [])
        self.assertFalse(pending)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual([c.args[0] for c in self.lens.client.request.call_args_list], ["thread/read"] * 3)

    @patch("lens.time.sleep")
    def test_persistent_empty_and_other_failures_are_not_hidden(self, sleep):
        self.lens.client.request.side_effect = self.empty
        with self.assertRaisesRegex(RpcError, "is empty"):
            self.lens._raw_turns("old")
        self.assertEqual(self.lens.client.request.call_count, 4)
        self.lens.client.request.reset_mock()
        self.lens.client.request.side_effect = RpcError("permission denied")
        with self.assertRaisesRegex(RpcError, "permission denied"):
            self.lens._raw_turns("old")
        self.assertEqual(self.lens.client.request.call_count, 2)

    def test_paginated_history_fallback_still_loads_all_pages(self):
        self.lens.client.request.side_effect = [RpcError("use pagination"), {"thread": {"id": "old"}},
                                                {"data": [{"id": "one"}], "nextCursor": "next"},
                                                {"data": [{"id": "two"}], "nextCursor": None}]
        th, turns, pending = self.lens._raw_turns("old")
        self.assertEqual([t["id"] for t in turns], ["one", "two"])
        self.assertFalse(pending)
        self.assertEqual(self.lens.client.request.call_args_list[-1].args[1]["cursor"], "next")

    @patch("lens.time.sleep")
    def test_live_fallback_does_not_drop_an_earlier_turn(self, sleep):
        self.lens.starting_threads["new"] = {"thread": {"id": "new"}, "turn_id": "first", "pending": False}
        self.lens.live["new"] = Mock(turn_id="second")
        self.lens.client.request.side_effect = self.empty
        with self.assertRaises(RpcError):
            self.lens._raw_turns("new")


class TurnsTest(unittest.TestCase):
    def test_image_attachments_survive_request_and_steer(self):
        with open(SEED) as f:
            raw = json.load(f)["threads"][0]["turns"]
        first = normalize_turn(raw[0])["request"]
        self.assertTrue(first["images"][0]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(first["images"][1]["path"], os.path.join(PROJ, "첨부 화면 #1.png"))
        steer = normalize_turn(raw[1])["request"]["steers"][0]
        self.assertEqual(steer["text"], "")
        self.assertEqual(len(steer["images"]), 3)
        self.assertEqual(steer["images"][0], first["images"][0])
        self.assertEqual(steer["images"][2], {"file": "unavailable-image"})

    def test_split_request_process_answer(self):
        with open(SEED) as f:
            th = json.load(f)["threads"][0]
        t = normalize_turn(th["turns"][0], 1)
        self.assertIn("0으로 나누기", t["request"]["text"])
        self.assertEqual(t["answer"]["id"], "m2")
        kinds = [s["kind"] for s in t["process"]]
        self.assertEqual(kinds, ["message", "reasoning", "command", "file_change", "command"])
        self.assertEqual(t["process"][2]["title"], "cat calc.py README.md")   # 셸 래퍼를 벗긴다
        self.assertEqual(t["process"][3]["title"], "calc.py 외 1개")
        self.assertEqual(t["counts"]["command"], 2)

    def test_private_reasoning_and_last_message_fallback(self):
        t = normalize_turn({"id": "x", "status": "completed", "items": [
            {"type": "userMessage", "id": "u", "content": [{"type": "text", "text": "hi"}]},
            {"type": "reasoning", "id": "r", "summary": [], "content": []},
            {"type": "agentMessage", "id": "a", "text": "끝", "phase": None}]})
        self.assertEqual(t["answer"]["text"], "끝")
        self.assertTrue(t["process"][0]["private"])

    def test_stopped_turn_keeps_commentary_in_process(self):
        # 멈춘·실패한 턴의 마지막 중간 말("~하겠습니다")은 답변이 아니다 — 과정에 남는다
        items = [{"type": "userMessage", "id": "u", "content": [{"type": "text", "text": "데모 다시 띄워줘"}]},
                 {"type": "agentMessage", "id": "c", "text": "다시 띄우겠습니다.", "phase": "commentary"}]
        for status in ("interrupted", "failed"):
            t = normalize_turn({"id": "x", "status": status, "items": items})
            self.assertIsNone(t["answer"], status)
            self.assertEqual([s["kind"] for s in t["process"]], ["message"])
        items2 = items + [{"type": "agentMessage", "id": "f", "text": "끝", "phase": "final_answer"}]
        self.assertEqual(normalize_turn({"id": "x", "status": "interrupted", "items": items2})["answer"]["text"], "끝")
        self.assertEqual(normalize_turn({"id": "x", "status": "completed", "items": items})["answer"]["text"], "다시 띄우겠습니다.")

    def test_readonly_command(self):
        from forks import readonly_command
        self.assertTrue(readonly_command("/bin/zsh -lc 'cat a.md | head -5'"))
        self.assertTrue(readonly_command(["/bin/bash", "-lc", "git log --oneline -3"]))
        self.assertFalse(readonly_command("/bin/zsh -lc 'rm -rf build'"))
        self.assertFalse(readonly_command("echo hi > a.txt"))
        self.assertFalse(readonly_command("git commit -m x"))

    def test_claude_task_notification_is_notice_not_request(self):
        import claude_history as ch
        note = ("<task-notification>\n<task-id>a1</task-id>\n<status>completed</status>\n"
                "<summary>Agent \"메인 반영\" finished</summary>\n<result>병합하지 않았습니다.</result>\n</task-notification>")
        bg = ("<task-notification>\n<task-id>b2</task-id>\n<status>failed</status>\n"
              "<summary>Background command \"대기\" failed</summary>\n</task-notification>")
        def u(i, parent, content, **kw):
            return dict({"type": "user", "uuid": i, "parentUuid": parent, "timestamp": "2026-11-05T04:00:0%dZ" % len(i),
                         "message": {"role": "user", "content": content}}, **kw)
        def a(i, parent, text):
            return {"type": "assistant", "uuid": i, "parentUuid": parent, "timestamp": "2026-11-05T04:00:05Z",
                    "message": {"id": "m" + i, "role": "assistant", "model": "claude-x",
                                "content": [{"type": "text", "text": text}]}}
        recs = [u("p1", None, "진행해줘", origin={"kind": "human"}), a("a1", "p1", "맡겼습니다."),
                u("n1", "a1", note, origin={"kind": "task-notification", "producer": "session-task"},
                  promptSource="system", turnOrigin="task_notification"),
                a("a2", "n1", "다시 합칩니다."),
                {"type": "attachment", "uuid": "q1", "parentUuid": "a2", "timestamp": "2026-11-05T04:00:06Z",
                 "attachment": {"type": "queued_command", "prompt": bg, "source_uuid": "s1",
                                "commandMode": "task-notification", "origin": {"kind": "task-notification"}}},
                a("a3", "q1", "끝났습니다.")]
        turns, _ = ch.build_turns(recs)
        self.assertEqual(len(turns), 2)          # 턴 경계는 Claude 와 같다
        t0, t1 = normalize_turn(turns[0], 1), normalize_turn(turns[1], 2)
        self.assertIsNone(t0["request"]["notice"])
        self.assertEqual(t0["request"]["text"], "진행해줘")
        n = t1["request"]["notice"]
        self.assertEqual(n, [{"status": "completed", "summary": 'Agent "메인 반영" finished', "result": "병합하지 않았습니다."}])
        st = t1["request"]["steers"][0]["notice"][0]
        self.assertEqual((st["status"], st["result"]), ("failed", ""))
        self.assertEqual(t1["answer"]["text"], "끝났습니다.")
        # 목록 미리보기에는 알림 글을 쓰지 않는다
        meta = {"preview": "", "cwd": None, "created": None, "name": None}
        ch._meta_from(meta, recs[2])
        self.assertEqual(meta["preview"], "")

    def test_claude_structured_answer_ends_turn(self):
        """StructuredOutput 로 답하고 Stop 훅이 돈 기록은 방금 쓰였어도 끝난 턴이고, 답은 답변 자리에 간다."""
        import claude_history as ch
        recs = [
            {"type": "user", "uuid": "u1", "timestamp": "2026-11-05T06:00:00Z",
             "message": {"role": "user", "content": "고쳐 줘"}},
            {"type": "assistant", "uuid": "a1", "timestamp": "2026-11-05T06:00:05Z",
             "message": {"id": "m1", "model": "claude-opus-5-5", "stop_reason": "tool_use", "content": [
                 {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}},
            {"type": "user", "uuid": "r1", "timestamp": "2026-11-05T06:00:06Z",
             "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a"}]}},
            {"type": "assistant", "uuid": "a2", "timestamp": "2026-11-05T06:00:09Z",
             "message": {"id": "m2", "model": "claude-opus-5-5", "stop_reason": "tool_use", "content": [
                 {"type": "tool_use", "id": "t2", "name": "StructuredOutput",
                  "input": {"protocol": "agent-output/1", "body": "다 고쳤습니다", "todos": []}}]}},
            {"type": "user", "uuid": "r2", "timestamp": "2026-11-05T06:00:10Z",
             "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "ok"}]}},
        ]
        for prev, r in zip(recs, recs[1:]):
            r["parentUuid"] = prev["uuid"]
        stop = {"type": "attachment", "uuid": "h1", "parentUuid": "r2", "timestamp": "2026-11-05T06:00:10Z",
                "attachment": {"type": "hook_success", "hookName": "Stop", "hookEvent": "Stop"}}
        for tail in ([], [stop]):
            turns, _ = ch.build_turns(recs + tail, mtime=time.time())
            self.assertEqual(turns[-1]["status"], "completed")
            t = normalize_turn(turns[-1])
            self.assertEqual(t["answer"]["text"], "다 고쳤습니다")
            self.assertNotIn("message", [s["kind"] for s in t["process"]])
        # 이 앱에서 아직 도는 턴이라도 최종 답은 답변 자리에 둔다
        turns, _ = ch.build_turns(recs, mtime=time.time(), live=True)
        self.assertEqual(turns[-1]["status"], "inProgress")
        self.assertEqual(normalize_turn(turns[-1])["answer"]["text"], "다 고쳤습니다")
        # 도구 결과를 기다리는 방금 쓴 기록은 진행 중 — 캐시는 유예가 끝날 때까지만 쓴다
        turns, info = ch.build_turns(recs[:2], mtime=time.time())
        self.assertEqual(turns[-1]["status"], "inProgress")
        self.assertTrue(info["grace_until"])
        # 유예가 지난 도구 단계는 완료가 아니라 끝 기록 없이 끊긴 턴이다(프로세스가 꺼짐 — 끝 시각 없는 interrupted)
        turns, info = ch.build_turns(recs[:3], mtime=time.time() - ch.LIVE_GRACE_SEC - 1)
        self.assertEqual(turns[-1]["status"], "interrupted")
        self.assertIsNone(turns[-1]["completedAt"])
        self.assertIsNone(info["grace_until"])

    def test_claude_subagent_long_command_is_quiet_not_stalled(self):
        """하위 에이전트가 긴 명령을 돌리는 동안 부모 기록은 멈춰 있다 — 하위 기록의 변경 시각이 신호이고,
        명령의 제한 시간까지는 조용해도 '지연'이 아니며(quiet_until), 유예가 지나도 진행 중으로 본다."""
        import claude_history as ch
        import claude_rpc
        import lens as lensmod
        from datetime import datetime, timezone
        now = time.time()

        def iso(t):
            return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")
        d = tempfile.mkdtemp()
        path = os.path.join(d, "sess.jsonl")
        recs = [{"type": "user", "uuid": "u1", "timestamp": iso(now - 400), "message": {"role": "user", "content": "반영해 줘"}},
                {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "timestamp": iso(now - 399),
                 "message": {"id": "m1", "model": "claude-opus-5-5", "stop_reason": "tool_use", "content": [
                     {"type": "tool_use", "id": "ag1", "name": "Agent",
                      "input": {"subagent_type": "general-purpose", "description": "반영", "prompt": "..."}}]}}]
        with open(path, "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in recs))
        os.utime(path, (now - 399, now - 399))
        sub = os.path.join(d, "sess", "subagents")
        os.makedirs(sub)
        with open(os.path.join(sub, "agent-x.meta.json"), "w") as f:
            json.dump({"toolUseId": "ag1", "agentType": "general-purpose"}, f)
        srecs = [{"type": "assistant", "timestamp": iso(now - 100), "message": {"content": [
                     {"type": "tool_use", "id": "s1", "name": "Bash", "input": {"command": "ls"}}]}},
                 {"type": "user", "timestamp": iso(now - 99), "message": {"content": [
                     {"type": "tool_result", "tool_use_id": "s1", "content": "a"}]}},
                 {"type": "assistant", "timestamp": iso(now - 98), "message": {"content": [
                     {"type": "tool_use", "id": "s2", "name": "Bash",
                      "input": {"command": "nice -n 5 python3 -m unittest\nmore", "timeout": 600000}}]}}]
        spath = os.path.join(sub, "agent-x.jsonl")
        with open(spath, "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in srecs))
        os.utime(spath, (now - 98, now - 98))
        self.assertAlmostEqual(ch.activity_mtime(path), now - 98, delta=1)
        # 부모 기록은 유예(300초)를 넘겼지만 하위 에이전트의 명령이 제한 시간 안이라 진행 중이다
        turns, info = ch.build_turns(ch.read_records(path), mtime=now - 399,
                                     sub_activity=lambda ids: ch.subagent_activity(path, ids))
        self.assertEqual(turns[-1]["status"], "inProgress")
        self.assertGreater(info["grace_until"], now + 400)
        t = normalize_turn(turns[-1])
        step = t["process"][-1]
        self.assertEqual(step["kind"], "agent")
        self.assertEqual(step["activity"]["kind"], "command")
        self.assertEqual(step["activity"]["title"], "nice -n 5 python3 -m unittest")
        self.assertAlmostEqual(t["quiet_until"], now - 98 + 600, delta=1)
        # 목록 상태: 마지막 갱신이 10분을 넘겨도 명령 제한 시간 안이면 '실행 중'이고 quiet_until 을 싣는다
        st = lensmod.turn_state({"status": "inProgress", "quietUntil": now + 100}, now - 700, now)
        self.assertEqual((st["kind"], st["quiet_until"]), ("running", now + 100))
        self.assertEqual(lensmod.turn_state({"status": "inProgress"}, now - 700, now)["kind"], "unfinished")
        # 뒤에서 돌리는 명령·끝난 턴은 기다릴 시간이 없다
        bg = ch.tool_item({"id": "b", "name": "Bash", "input": {"command": "x", "run_in_background": True}})
        self.assertIsNone(bg["timeoutMs"])
        self.assertEqual(ch.tool_item({"id": "c", "name": "Bash", "input": {"command": "x"}})["timeoutMs"],
                         ch.BASH_TIMEOUT_MS)
        self.assertIsNone(normalize_turn(dict(turns[-1], status="completed"))["quiet_until"])

        # 이 앱에서 도는 턴: 하위 에이전트 소식이 부모 Agent 항목의 activity 로 알려진다(= 신호)
        turn = object.__new__(claude_rpc.Run)
        sent = []
        turn.tools = {"ag1": {"type": "subAgentActivity", "id": "ag1", "status": "inProgress", "_tool": "Agent"}}
        turn.th, turn.sub_signal, turn.tid = {"cwd": d}, 0, "sess"
        turn.client = type("C", (), {"find": staticmethod(lambda tid: path)})()
        turn.emit = lambda m, p: sent.append((m, p))
        turn._on_sub({"type": "assistant", "parent_tool_use_id": "ag1", "message": {"content": [
            {"type": "tool_use", "id": "s9", "name": "Bash", "input": {"command": "make test", "timeout": 300000}}]}})
        self.assertEqual(sent[-1][0], "item/updated")
        act = sent[-1][1]["item"]["activity"]
        self.assertEqual((act["command"], act["timeoutMs"]), ("make test", 300000))
        self.assertNotIn("_tool", sent[-1][1]["item"])
        self.assertEqual(sent[-1][1]["item"]["agentThreadId"], "agent-x")   # 하위 기록(meta toolUseId)을 세션으로 연다
        turn._on_sub({"type": "user", "parent_tool_use_id": "ag1", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "s9", "content": "ok"}]}})
        self.assertIsNone(sent[-1][1]["item"]["activity"])
        n = len(sent)
        turn._on_sub({"type": "stream_event", "parent_tool_use_id": "ag1", "event": {}})   # 5초 안의 조각은 알리지 않는다
        self.assertEqual(len(sent), n)

    def test_claude_subagent_keeps_turn_running_and_cut_turn_is_unfinished(self):
        """앞 하위 에이전트가 도는 동안 본 기록이 조용해도 하위 에이전트 기록이 자라면 진행 중이고,
        둘 다 조용해지면 완료가 아니라 '끝 기록 없이 끊김'(끝 시각 없는 interrupted)이다."""
        import claude_history as ch
        from lens import turn_state
        recs = [
            {"type": "user", "uuid": "u1", "timestamp": "2026-11-05T06:17:05Z",
             "message": {"role": "user", "content": "메인 반영"}},
            {"type": "assistant", "uuid": "a1", "timestamp": "2026-11-05T06:17:08Z",
             "message": {"id": "m1", "model": "claude-opus-5-5", "content": [{"type": "text", "text": "서브에이전트에게 맡깁니다."}]}},
            {"type": "assistant", "uuid": "a2", "timestamp": "2026-11-05T06:17:31Z",
             "message": {"id": "m2", "model": "claude-opus-5-5", "stop_reason": "tool_use", "content": [
                 {"type": "tool_use", "id": "toolu_agent", "name": "Agent",
                  "input": {"subagent_type": "general-purpose", "description": "메인 반영", "prompt": "병합해"}}]}},
        ]
        for prev, r in zip(recs, recs[1:]):
            r["parentUuid"] = prev["uuid"]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s1.jsonl")
            with open(path, "w") as f:
                f.write("\n".join(json.dumps(r) for r in recs) + "\n")
            old = time.time() - ch.LIVE_GRACE_SEC - 60
            os.utime(path, (old, old))
            # 하위 에이전트 기록이 없으면 유예가 지난 도구 단계는 끊긴 턴이다
            turns, info = ch.build_turns(ch.read_records(path), mtime=ch.activity_mtime(path))
            self.assertEqual(turns[-1]["status"], "interrupted")
            self.assertIsNone(turns[-1]["completedAt"])
            self.assertEqual(turn_state({"status": "interrupted", "completedAt": None, "cut": True}, time.time(), time.time()),
                             {"kind": "unfinished"})
            # 목록: 본 기록은 오래됐어도 하위 에이전트 활동(signal)이 최근이면 실행 중
            now = time.time()
            self.assertEqual(turn_state({"status": "inProgress", "signal": int(now)}, now - 3600, now)["kind"], "running")
            t = normalize_turn(turns[-1])
            self.assertIsNone(t["answer"])   # 하던 중의 말은 답이 아니다
            # 하위 에이전트 기록이 방금 자랐으면 진행 중이고, 그 에이전트의 마지막 단계를 읽는다
            sub = os.path.join(d, "s1", "subagents")
            os.makedirs(sub)
            with open(os.path.join(sub, "agent-x.meta.json"), "w") as f:
                json.dump({"agentType": "general-purpose", "toolUseId": "toolu_agent"}, f)
            with open(os.path.join(sub, "agent-x.jsonl"), "w") as f:
                f.write(json.dumps({"type": "assistant", "isSidechain": True, "message": {"content": [
                    {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "git merge --no-ff x"}}]}}) + "\n")
            act = ch.activity_mtime(path)
            self.assertGreater(act, old + 30)
            turns, info = ch.build_turns(ch.read_records(path), mtime=act)
            self.assertEqual(turns[-1]["status"], "inProgress")
            self.assertTrue(info["grace_until"])
            prog = ch.subagent_progress(path, {"toolu_agent"})
            self.assertEqual(prog["toolu_agent"]["last"], "Bash · git merge --no-ff x")
            self.assertEqual(ch.subagent_progress(path, {"other"}), {})
            item = dict(turns[-1]["items"][-1], progress=prog["toolu_agent"])
            step = normalize_turn(dict(turns[-1], items=turns[-1]["items"][:-1] + [item]))["process"][-1]
            self.assertEqual((step["kind"], step["status"]), ("agent", "inProgress"))
            self.assertEqual(step["progress"]["last"], "Bash · git merge --no-ff x")

    def test_claude_process_registry_decides_running_or_cut(self):
        """Claude 프로세스 명부(`<설정>/sessions/<pid>.json`): 살아 있으면 오래 조용해도 진행 중,
        꺼졌으면 5분 유예 없이 '끝나지 않음'(cut=process), 명부를 모르면 예전처럼 시간 유예."""
        import claude_history as ch
        from claude_rpc import ClaudeClient
        from lens import turn_state
        recs = [
            {"type": "user", "uuid": "u1", "timestamp": "2026-11-05T06:17:05Z", "version": "9.9.9",
             "cwd": TMP, "message": {"role": "user", "content": "메인 반영"}},
            {"type": "assistant", "uuid": "a1", "timestamp": "2026-11-05T06:17:31Z", "version": "9.9.9",
             "message": {"id": "m2", "model": "claude-opus-5-5", "stop_reason": "tool_use", "content": [
                 {"type": "tool_use", "id": "toolu_agent", "name": "Agent",
                  "input": {"subagent_type": "general-purpose", "description": "메인 반영", "prompt": "병합해"}}]}},
        ]
        recs[1]["parentUuid"] = "u1"
        SID = "5e1f0c9a-0000-4000-8000-00000000d0de"
        me = os.getpid()
        started = subprocess.run(["ps", "-o", "lstart=", "-p", str(me)], capture_output=True, text=True,
                                 env=dict(os.environ, TZ="UTC", LC_ALL="C")).stdout.strip()
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "projects", "p")
            os.makedirs(proj)
            path = os.path.join(proj, SID + ".jsonl")
            with open(path, "w") as f:
                f.write("\n".join(json.dumps(r) for r in recs) + "\n")
            old = time.time() - 3600
            os.utime(path, (old, old))
            sessions = os.path.join(root, "sessions")

            def registry(*entries):
                ch._REG_CACHE.clear()
                ch._ALIVE_CACHE.clear()
                if os.path.isdir(sessions):
                    shutil.rmtree(sessions)
                os.makedirs(sessions)
                for n, e in enumerate(entries):
                    with open(os.path.join(sessions, "%d.json" % n), "w") as f:
                        json.dump(dict({"version": "9.9.9", "status": "busy"}, **e), f)
                return ch.proc_registry(root)

            # 명부 폴더가 없으면 모름 — 시간 유예(한 시간 전 기록은 '끊김', 까닭 quiet)
            ch._REG_CACHE.clear()
            self.assertIsNone(ch.proc_state(ch.proc_registry(root), SID, "9.9.9", old))
            turns, _ = ch.build_turns(ch.read_records(path), mtime=old)
            self.assertEqual((turns[-1]["status"], turns[-1]["cut"]), ("interrupted", "quiet"))
            # 살아 있는 프로세스가 이 세션을 쥐고 있으면 한 시간 조용해도 진행 중(긴 명령·하위 에이전트)
            reg = registry({"pid": me, "sessionId": SID, "procStart": started})
            self.assertEqual(ch.proc_state(reg, SID, "9.9.9", old), "alive")
            turns, info = ch.build_turns(ch.read_records(path), mtime=old, proc="alive")
            self.assertEqual(turns[-1]["status"], "inProgress")
            self.assertTrue(turns[-1]["alive"])
            self.assertIsNone(info["grace_until"])
            self.assertEqual(turn_state({"status": "inProgress", "alive": True}, old, time.time())["kind"], "running")
            # 같은 pid 라도 시작 시각이 다르면 재사용된 pid — 낡은 명부다
            reg = registry({"pid": me, "sessionId": SID, "procStart": "Mon Jan  1 00:00:00 2001"})
            self.assertEqual(ch.proc_state(reg, SID, "9.9.9", old), "gone")
            # 명부에 남았는데 프로세스가 없음 → 방금 쓴 기록이라도 유예 없이 끊김(cut=process)
            reg = registry({"pid": dead.pid, "sessionId": SID})
            self.assertEqual(ch.proc_state(reg, SID, "9.9.9", time.time()), "gone")
            turns, info = ch.build_turns(ch.read_records(path), mtime=time.time(), proc="gone")
            self.assertEqual((turns[-1]["status"], turns[-1]["cut"]), ("interrupted", "process"))
            self.assertIsNone(turns[-1]["completedAt"])
            self.assertEqual(turns[-1]["items"][-1]["status"], "interrupted")   # 하위 에이전트 단계도 멈춤
            self.assertIsNone(info["grace_until"])
            # 명부에 없음: 이 버전이 명부를 남기고 30초 넘게 조용하면 꺼진 것, 방금 썼거나 다른 버전이면 모름
            reg = registry({"pid": me, "sessionId": "other", "procStart": started})
            self.assertEqual(ch.proc_state(reg, SID, "9.9.9", time.time() - ch.NO_PROC_QUIET_SEC - 1), "gone")
            self.assertIsNone(ch.proc_state(reg, SID, "9.9.9", time.time()))
            self.assertIsNone(ch.proc_state(reg, SID, "1.0.0", old))
            # ClaudeClient 읽기: 명부가 바뀌면 캐시를 쓰지 않고 다시 판정한다
            cl = ClaudeClient(root=root)
            registry({"pid": me, "sessionId": SID, "procStart": started})
            t = cl._m_thread_read({"threadId": SID, "includeTurns": True})["thread"]["turns"][-1]
            self.assertEqual(t["status"], "inProgress")
            registry({"pid": me, "sessionId": "other", "procStart": started})
            t = cl._m_thread_read({"threadId": SID, "includeTurns": True})["thread"]["turns"][-1]
            self.assertEqual((t["status"], t["cut"]), ("interrupted", "process"))

    def test_claude_subagent_opens_as_read_only_session(self):
        """Claude 하위 에이전트 기록(`<세션>/subagents/agent-<ID>.jsonl`)은 `agent-<ID>` 세션으로 열린다 —
        부모 턴의 하위 에이전트 단계가 그 ID 를 싣고, 그 세션은 부모를 가리키며 읽기만 된다."""
        import claude_history as ch
        from claude_rpc import ClaudeClient
        from codex_rpc import RpcError
        SID = "5e1f0c9a-0000-4000-8000-00000000a9e7"
        recs = [{"type": "user", "uuid": "u1", "timestamp": "2026-11-05T06:17:05Z", "cwd": TMP,
                 "message": {"role": "user", "content": "메인 반영"}},
                {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "timestamp": "2026-11-05T06:17:31Z",
                 "message": {"id": "m1", "model": "claude-opus-5-5", "stop_reason": "tool_use", "content": [
                     {"type": "tool_use", "id": "toolu_ag", "name": "Agent",
                      "input": {"subagent_type": "general-purpose", "description": "메인 반영", "prompt": "병합해"}}]}},
                {"type": "user", "uuid": "u2", "parentUuid": "a1", "timestamp": "2026-11-05T06:18:31Z",
                 "toolUseResult": {"status": "completed", "agentId": "abc123def456"},
                 "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_ag", "content": "병합했습니다"}]}},
                {"type": "assistant", "uuid": "a2", "parentUuid": "u2", "timestamp": "2026-11-05T06:18:40Z",
                 "message": {"id": "m2", "model": "claude-opus-5-5", "content": [{"type": "text", "text": "끝났습니다."}]}}]
        side = [{"type": "user", "uuid": "s1", "isSidechain": True, "agentId": "abc123def456", "cwd": TMP,
                 "timestamp": "2026-11-05T06:17:32Z", "message": {"role": "user", "content": "병합해"}},
                {"type": "assistant", "uuid": "s2", "parentUuid": "s1", "isSidechain": True, "timestamp": "2026-11-05T06:17:40Z",
                 "message": {"id": "sm1", "model": "claude-sonnet-5-5", "content": [
                     {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "git merge x"}}]}},
                {"type": "user", "uuid": "s3", "parentUuid": "s2", "isSidechain": True, "timestamp": "2026-11-05T06:17:50Z",
                 "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "b1", "content": "ok"}]}},
                {"type": "assistant", "uuid": "s4", "parentUuid": "s3", "isSidechain": True, "timestamp": "2026-11-05T06:18:30Z",
                 "message": {"id": "sm2", "model": "claude-sonnet-5-5", "content": [{"type": "text", "text": "병합했습니다"}]}}]
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "projects", "p")
            sub = os.path.join(proj, SID, "subagents")
            os.makedirs(sub)
            for path, rs in ((os.path.join(proj, SID + ".jsonl"), recs), (os.path.join(sub, "agent-abc123def456.jsonl"), side)):
                with open(path, "w") as f:
                    f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rs) + "\n")
            with open(os.path.join(sub, "agent-abc123def456.meta.json"), "w") as f:
                json.dump({"agentType": "general-purpose", "description": "메인 반영", "toolUseId": "toolu_ag",
                           "finished": True}, f, ensure_ascii=False)
            cl = ClaudeClient(root=root)
            th = cl._m_thread_read({"threadId": SID, "includeTurns": True})["thread"]
            item = [i for i in th["turns"][-1]["items"] if i.get("type") == "subAgentActivity"][0]
            self.assertEqual(item["agentThreadId"], "agent-abc123def456")
            step = [s for s in normalize_turn(th["turns"][-1])["process"] if s["kind"] == "agent"][0]
            self.assertEqual(step["agents"][0]["thread"], "agent-abc123def456")
            # 결과가 아직 없는(진행 중) 항목도 meta 의 toolUseId 로 잇는다
            turns = ch.with_subagent_threads([{"items": [{"type": "subAgentActivity", "id": "toolu_ag"}]}], os.path.join(proj, SID + ".jsonl"))
            self.assertEqual(turns[0]["items"][0]["agentThreadId"], "agent-abc123def456")
            # 하위 에이전트 세션: 본 세션 목록에는 없고, 열면 sidechain 줄이 턴이 된다
            self.assertNotIn("agent-abc123def456", [t["id"] for t in cl.iter_threads()])
            self.assertTrue(cl.owns("agent-abc123def456"))
            sth = cl._m_thread_read({"threadId": "agent-abc123def456", "includeTurns": True})["thread"]
            self.assertEqual(sth["parentThreadId"], SID)
            self.assertTrue(sth["readOnly"])
            self.assertEqual(sth["name"], "하위 에이전트 · general-purpose · 메인 반영")
            self.assertEqual(len(sth["turns"]), 1)
            st = normalize_turn(sth["turns"][0])
            self.assertEqual(st["status"], "completed")
            self.assertEqual(st["request"]["text"], "병합해")
            self.assertEqual(st["answer"]["text"], "병합했습니다")
            self.assertEqual([s["kind"] for s in st["process"]], ["command"])
            for m, p in (("turn/start", {"threadId": "agent-abc123def456", "input": []}),
                         ("thread/fork", {"threadId": "agent-abc123def456", "lastTurnId": "s1"})):
                with self.assertRaises(RpcError):
                    cl.request(m, p)

    def test_unwrap_agent_output_protocol(self):
        self.assertEqual(unwrap_output('{"protocol":"agent-output/1","body":"본문","report":{}}'), "본문")
        self.assertEqual(unwrap_output("평범한 글"), "평범한 글")

    def test_agent_output_report_cards(self):
        # 진짜 기록처럼 todos·decisions 가 JSON 글로, merge 가 "null" 로 와도 읽는다
        dec = [{"id": "list-states", "header": "목록 상태", "question": "목록에 어떤 상태를 보일까요?",
                "options": [{"label": "모두", "description": "전부 보인다", "recommended": True},
                            {"label": "지금처럼", "description": "", "recommended": False}, {"description": "이름 없음"}],
                "multiSelect": False, "allowOther": True}, {"header": "질문 없음"}]
        text = json.dumps({"protocol": "agent-output/1", "body": "본문", "todos": json.dumps([{"id": "t", "title": "데모 내리기", "owner": "agent"}]),
                           "decisions": json.dumps(dec), "merge": "null"}, ensure_ascii=False)
        t = normalize_turn({"id": "t1", "status": "completed", "items": [
            {"type": "userMessage", "id": "u", "content": [{"type": "text", "text": "물음"}]},
            {"type": "agentMessage", "id": "a", "text": "```json\n" + text + "\n```"}]}, 1)
        self.assertEqual(t["answer"]["text"], "본문")
        r = t["answer"]["report"]
        self.assertEqual(r["todos"][0]["title"], "데모 내리기")
        self.assertEqual(len(r["decisions"]), 1)
        self.assertEqual([o["label"] for o in r["decisions"][0]["options"]], ["모두", "지금처럼"])
        self.assertTrue(r["decisions"][0]["options"][0]["recommended"])
        self.assertIsNone(r["merge"])
        # 메인 반영 요청만 있는 답 · 카드 거리가 없는 답
        t2 = normalize_turn({"id": "t2", "status": "completed", "items": [{"type": "agentMessage", "id": "a", "text": json.dumps(
            {"protocol": "agent-output/1", "body": "끝", "todos": [], "decisions": [],
             "merge": {"summary": "v1 — 기능", "bump": "minor", "version": "1.0.0", "checks": ["시험 통과", 3]}})}]}, 1)
        self.assertEqual(t2["answer"]["report"]["merge"], {"summary": "v1 — 기능", "bump": "minor", "version": "1.0.0", "checks": ["시험 통과"]})
        t3 = normalize_turn({"id": "t3", "status": "completed", "items": [{"type": "agentMessage", "id": "a", "text": json.dumps(
            {"protocol": "agent-output/1", "body": "끝", "todos": [], "decisions": [], "merge": None})}]}, 1)
        self.assertNotIn("report", t3["answer"])

    def test_claude_structured_output_keeps_report(self):
        import claude_history
        recs = [{"type": "user", "uuid": "u1", "timestamp": "2026-11-05T00:00:00Z",
                 "message": {"role": "user", "content": "정할 것 있어?"}},
                {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "timestamp": "2026-11-05T00:00:01Z",
                 "message": {"id": "m1", "role": "assistant", "stop_reason": "end_turn", "content": [
                     {"type": "tool_use", "id": "toolu_1", "name": "StructuredOutput", "input": {
                         "protocol": "agent-output/1", "body": "고르세요", "todos": [], "merge": None,
                         "decisions": [{"id": "d", "question": "어느 쪽?", "options": [{"label": "가"}, {"label": "나"}],
                                        "multiSelect": True, "allowOther": True}]}}]}}]
        turns, _ = claude_history.build_turns(recs)
        t = normalize_turn(turns[-1], 1)
        self.assertEqual(t["answer"]["text"], "고르세요")
        self.assertEqual(t["answer"]["report"]["decisions"][0]["question"], "어느 쪽?")


class ImageTest(unittest.TestCase):
    def test_local_image_bytes_and_head(self):
        path = "/api/image?path=" + urllib.request.quote(os.path.join(PROJ, "첨부 화면 #1.png"))
        for method in ("GET", "HEAD"):
            req = urllib.request.Request("http://127.0.0.1:%d%s" % (SRV.port, path), method=method)
            with urllib.request.urlopen(req) as r:
                self.assertEqual(r.headers["Content-Type"], "image/png")
                self.assertEqual(r.headers["Cache-Control"], "no-store")
                self.assertEqual(int(r.headers["Content-Length"]), len(seed.image_fixture()))
                self.assertEqual(r.read(), seed.image_fixture() if method == "GET" else b"")

    def test_missing_or_non_image_is_an_error(self):
        for path, status in (("relative.png", 400), (os.path.join(PROJ, "missing.png"), 404),
                             (os.path.join(PROJ, "README.md"), 415)):
            with self.subTest(path=path):
                self.assertEqual(SRV.req("GET", "/api/image?path=" + urllib.request.quote(path))[0], status)


LENS_PORT = os.path.join(ROOT, "bin", "lens-port")


def lens_port(*args, env=None, cwd=None):
    import subprocess
    e = dict(os.environ, CODEX_LENS_URL="http://127.0.0.1:%d" % SRV.port)
    e.update(env or {})
    return subprocess.run([sys.executable, LENS_PORT] + list(args), capture_output=True, text=True, env=e,
                          cwd=cwd or TMP, timeout=60)


def http_ok(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=3) as r:
            return r.status == 200
    except OSError:
        return False


class PortsTest(unittest.TestCase):
    def test_free_port_skips_taken(self):
        import socket
        from ports import Ports, port_free
        home = tempfile.mkdtemp(prefix="ports-", dir=TMP)
        lo = 29380
        socks = []
        for p in (lo, lo + 1):          # 둘은 남이 쓰고 있다
            s = socket.socket()
            s.bind(("127.0.0.1", p))
            s.listen()
            socks.append(s)
        try:
            pm = Ports(home, port_range=(lo, lo + 2))
            self.assertFalse(port_free(lo))
            self.assertEqual(pm.free_port(), lo + 2)
            with self.assertRaises(ValueError):     # 남은 하나는 2분 예약 — 더 줄 게 없다
                pm.free_port()
            pm.close()
        finally:
            for s in socks:
                s.close()

    def test_cli_run_list_stop(self):
        tid = "port-thread-1"
        r = lens_port("run", "--name", "web", "--", sys.executable, "-m", "http.server", "{port}", "--bind", "{host}",
                      env={"CODEX_THREAD_ID": tid})
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("열림 — http://127.0.0.1:", r.stdout)
        st, d = SRV.req("GET", "/api/ports?thread=" + tid)
        e = d["items"][0]
        self.assertEqual((e["state"], e["name"], e["thread_id"]), ("listening", "web", tid))
        self.assertTrue(29400 <= e["port"] <= 29499)
        self.assertIn(str(e["port"]), e["cmd"])            # {port} 를 바꿔 넣었다
        self.assertTrue(http_ok(e["port"]))
        self.assertNotIn("env", e)                           # 환경변수는 저장·노출하지 않는다
        # 세션 읽기에 그 세션의 포트가 실린다
        self.assertEqual(SRV.app.ports.list(tid)[0]["id"], e["id"])
        # 같은 이름으로 다시 띄우면 이전 것을 끄고 같은 포트를 다시 쓴다
        r2 = lens_port("run", "--name", "web", "--sh", "exec %s -m http.server $PORT --bind $CODEX_LENS_BIND" % sys.executable,
                       env={"CODEX_THREAD_ID": tid})
        self.assertEqual(r2.returncode, 0, r2.stderr + r2.stdout)
        st, d = SRV.req("GET", "/api/ports?thread=" + tid)
        states = {x["id"]: x["state"] for x in d["items"]}
        self.assertEqual(states[e["id"]], "stopped")
        new = [x for x in d["items"] if x["state"] == "listening"]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["port"], e["port"])
        self.assertTrue(http_ok(e["port"]))
        self.assertIn(str(e["port"]), lens_port("list").stdout)
        # 포트 번호로 끈다 — 프로세스가 사라지고 포트가 빈다
        r3 = lens_port("stop", str(e["port"]))
        self.assertEqual(r3.returncode, 0, r3.stderr)
        self.assertFalse(http_ok(e["port"]))
        st, x = SRV.req("GET", "/api/ports/" + new[0]["id"])
        self.assertEqual(x["state"], "stopped")
        logs = lens_port("logs", new[0]["id"]).stdout
        self.assertIn("# $ /bin/sh -c", logs)          # 띄운 명령과
        self.assertIn('"GET / HTTP', logs)              # 서버 출력이 로그에 남는다
        # 끝난 것 지우기
        st, c = SRV.req("POST", "/api/ports/clear", {})
        self.assertGreaterEqual(c["removed"], 2)
        self.assertFalse(os.path.exists(x["log"]))

    def test_expose_hook_adds_public_url_and_removes_it(self):
        from ports import Ports
        home = tempfile.mkdtemp(prefix="ports-x-", dir=TMP)
        rec = os.path.join(home, "hook.log")
        hook = os.path.join(home, "hook.py")
        with open(hook, "w") as f:
            f.write("import sys\nopen(%r, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n" % rec)
        pm = Ports(home, port_range=(29300, 29349), public_url="https://lens.example:{public}/")
        pm.expose_cmd = "%s %s expose {port} {public}" % (sys.executable, hook)
        pm.unexpose_cmd = "%s %s off {public}" % (sys.executable, hook)
        try:
            e = pm.run(argv=[sys.executable, "-m", "http.server", "{port}", "--bind", "127.0.0.1"], cwd=TMP, name="x")
            self.assertIsNone(pm.url_of(e))            # 내보내기 전에는 바깥 주소가 없다
            x = wait(lambda: (lambda c: c if c.get("expose") == "ok" else None)(pm.get(e["id"])), 15)
            self.assertNotEqual(x["public"], x["port"])
            self.assertEqual(pm.url_of(x), "https://lens.example:%d/" % x["public"])
            pm.stop(e["id"])
            with open(rec) as f:
                self.assertEqual(f.read().split("\n")[:2], ["expose %d %d" % (x["port"], x["public"]), "off %d" % x["public"]])
            self.assertEqual(pm.get(e["id"])["expose"], "removed")
        finally:
            pm.close()

    def test_tailscale_default_expose(self):
        """CODEX_LENS_PORT_EXPOSE 가 없고 tailscale 이 로그인돼 있으면 serve 로 내보내는 것이 기본이다 — 빈 값이면 끈다."""
        import ports
        import weblink
        home = tempfile.mkdtemp(prefix="ports-ts-", dir=TMP)
        serve = {"TCP": {"29361": {"HTTPS": True}}, "Web": {"me.ts.net:29361": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:1"}}}}}
        info = (serve, {"dns": "me.ts.net", "ips": ["100.1.2.3"]})
        with patch.object(weblink, "tailscale_bin", return_value="/opt/tail scale"), \
                patch.object(weblink, "tailscale_info", return_value=info), patch.object(weblink, "forget_cache"):
            with patch.dict(os.environ):
                os.environ.pop("CODEX_LENS_PORT_EXPOSE")
                pm = ports.Ports(home, port_range=(29360, 29362))
            try:
                self.assertTrue(pm.ts_auto)
                self.assertEqual(pm.expose_cmd, "'/opt/tail scale' serve --bg --https={public} http://127.0.0.1:{port}")
                self.assertEqual(pm.unexpose_cmd, "'/opt/tail scale' serve --https={public} off")
                self.assertEqual(pm.url_of({"port": 29360, "public": 29362}), "https://me.ts.net:29362/")
                self.assertEqual(ports.serve_ports(), {29361})
                # 다른 도구가 쥔 serve 포트(29361)는 바깥 포트로 고르지 않는다
                self.assertEqual(pm.free_port(avoid={29360, 29361}), 29362)
            finally:
                pm.close()
            pm = ports.Ports(home)          # 빈 값(시험 환경) — 끔
            try:
                self.assertFalse(pm.ts_auto)
                self.assertEqual(pm.expose_cmd, "")
            finally:
                pm.close()
        with patch.object(weblink, "tailscale_bin", return_value=None), patch.dict(os.environ):
            os.environ.pop("CODEX_LENS_PORT_EXPOSE")
            pm = ports.Ports(home)          # tailscale 없음 — 끔
            try:
                self.assertFalse(pm.ts_auto)
            finally:
                pm.close()

    def test_cli_reports_early_exit(self):
        r = lens_port("run", "--name", "bad", "--sh", "echo 망가짐; exit 3")
        self.assertEqual(r.returncode, 1)
        self.assertIn("망가짐", r.stderr)
        st, d = SRV.req("GET", "/api/ports")
        bad = [x for x in d["items"] if x["name"] == "bad"][0]
        self.assertEqual((bad["state"], bad["exit_code"]), ("exited", 3))

    def test_thread_from_cwd_and_restart_adopts(self):
        from ports import Ports
        wt = os.path.join(TMP, "port-wt")
        os.makedirs(os.path.join(wt, "sub"), exist_ok=True)
        SRV.lens.registry.update(lambda d: d["mine"].__setitem__("port-thread-cwd", {"cwd": wt}))
        r = lens_port("run", "--name", "cwd", "--", sys.executable, "-m", "http.server", "{port}", "--bind", "127.0.0.1",
                      cwd=os.path.join(wt, "sub"), env={"CODEX_THREAD_ID": ""})
        self.assertEqual(r.returncode, 0, r.stderr)
        e = SRV.app.ports.list("port-thread-cwd")[0]
        st, sess = SRV.req("GET", "/api/ports/%d" % e["port"])
        self.assertEqual(sess["id"], e["id"])
        # 앱을 다시 띄운 것처럼 — 새 관리자가 같은 기록을 읽어 살아 있는 프로세스를 이어 보고 끌 수 있다
        pm = Ports(os.environ["CODEX_LENS_HOME"])
        try:
            self.assertEqual(pm.get(e["id"])["state"], "listening")
            pm.stop(e["id"])
            self.assertFalse(http_ok(e["port"]))
        finally:
            pm.close()


class ApiTest(unittest.TestCase):
    def test_feedback_widget_marks_html(self):
        old = appmod.FEEDBACK_ORIGIN
        try:
            appmod.FEEDBACK_ORIGIN = "https://fb.example"
            html = SRV.app.index_html()
            self.assertIn('class="demo has-fb"' if appmod.DEMO else 'class="has-fb"', html)
            self.assertIn("https://fb.example/feedback-widget.js", html)
            appmod.FEEDBACK_ORIGIN = ""
            self.assertNotIn("has-fb", SRV.app.index_html())
        finally:
            appmod.FEEDBACK_ORIGIN = old

    def test_01_index_and_static(self):
        html = SRV.text("/")
        self.assertIn("<title>Codex Lens</title>", html)
        self.assertNotIn("{{", html)
        self.assertIn("app.js", html)
        self.assertIn("CodexMd", SRV.text("/static/md.js"))
        st, info = SRV.req("GET", "/api/info")
        self.assertEqual(st, 200)
        self.assertFalse(info["demo"])
        st, cl = SRV.req("GET", "/api/changelog")
        self.assertIn("0.1.0", cl["text"])

    def test_02_list_and_read(self):
        st, r = SRV.req("GET", "/api/sessions")
        self.assertEqual(st, 200)
        ids = [s["id"] for s in r["sessions"]]
        self.assertIn(S1, ids)
        s2 = [s for s in r["sessions"] if s["id"].endswith("2")][0]
        self.assertEqual(s2["title"], "할 일 정리")    # 이름이 있으면 이름
        st, d = SRV.req("GET", "/api/sessions/" + S1)
        self.assertEqual(st, 200)
        self.assertEqual(len(d["turns"]), 2)
        self.assertEqual(d["turns"][0]["index"], 1)
        self.assertEqual(d["session"]["branch"], "main")
        # 화면의 읽기 과정이 쓰는 서버 단계 시간 — 에이전트 기록 읽기와 정리
        self.assertIsInstance(d["timing"]["read_ms"], int)
        self.assertGreaterEqual(d["timing"]["build_ms"], 0)

    def test_02b_search_titles_and_folders(self):
        def ids(q, extra=""):
            st, r = SRV.req("GET", "/api/sessions?q=" + urllib.parse.quote(q) + extra)
            self.assertEqual(st, 200, r)
            return [s["id"] for s in r["sessions"]], r
        S2 = "01a1aaaa000000000000000000000002"
        # 대소문자 무시, 낱말 순서 무관, 낱말마다 따로 찾는다
        self.assertEqual(ids("readme 사용법")[0], [S1])
        self.assertEqual(ids("사용법   README")[0], [S1])
        # 제목만 본다 — S2 는 이름("할 일 정리")이 제목이라 첫 요청의 README 로는 찾지 않는다
        got = ids("README")[0]
        self.assertIn(S1, got)
        self.assertNotIn(S2, got)
        self.assertIn(S2, ids("할 일")[0])
        # 폴더 이름으로도 찾고 Codex·Claude 를 갱신 시각 순으로 합친다
        got, r = ids("SAMPLE-project")
        self.assertTrue({S1, S2, S3} <= set(got), got)
        ups = [s["updated_at"] for s in r["sessions"]]
        self.assertEqual(ups, sorted(ups, reverse=True))
        self.assertEqual(ids("없는낱말")[0], [])
        # 다음 쪽
        first, r1 = ids("sample-project", "&limit=1")
        self.assertEqual(len(first), 1)
        self.assertTrue(r1["next_cursor"])
        second, _ = ids("sample-project", "&limit=1&cursor=" + r1["next_cursor"])
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first, second)
        # app-server 의 searchTerm(대소문자 구분·느림)은 쓰지 않는다
        self.assertFalse([c for c in calls("thread/list") if (c.get("params") or {}).get("searchTerm")])

    def test_03_new_session_runs_and_streams(self):
        st, r = SRV.req("POST", "/api/sessions", {"cwd": PROJ, "text": "README 를 읽어 줘", "client_id": "c-new-1",
                                                   "model": "gpt-fake-sol", "effort": "high",
                                                   "mentions": [{"kind": "file", "path": os.path.join(PROJ, "README.md"), "rel": "README.md"},
                                                                {"kind": "session", "id": S1, "title": "계산기"}]})
        self.assertEqual(st, 200, r)
        sid = r["id"]
        # 첫 flush 전 빈 파일이어도 보낸 요청과 고른 모델을 돌려준다.
        st, early = SRV.req("GET", "/api/sessions/" + sid)
        self.assertEqual(st, 200, early)
        self.assertTrue(early["history_pending"])
        self.assertEqual(early["session"]["model"], "gpt-fake-sol")
        self.assertEqual(early["session"]["effort"], "high")
        self.assertEqual(early["session"]["title"], "README 를 읽어 줘")
        self.assertEqual(early["turns"][0]["id"], r["turn_id"])
        self.assertIn("README 를 읽어 줘", early["turns"][0]["request"]["text"])
        # 같은 client_id 재전송은 같은 결과(중복 전송 방지)
        st2, r2 = SRV.req("POST", "/api/sessions", {"cwd": PROJ, "text": "README 를 읽어 줘", "client_id": "c-new-1"})
        self.assertEqual(r2["id"], sid)
        self.assertEqual(len([c for c in calls("thread/start")]), 1)
        # 새 세션에는 포트 관리 지침(lens-port)을 개발자 지침으로 준다
        self.assertIn("lens-port run", calls("thread/start")[-1]["params"]["developerInstructions"])
        start = calls("turn/start")[-1]["params"]
        self.assertEqual(start["effort"], "high")
        ctx = start["input"][1]["text"]
        self.assertIn(MENTION_MARK, ctx)
        self.assertIn(os.path.join(PROJ, "README.md"), ctx)
        self.assertIn("[멘션한 세션]", ctx)
        self.assertIn("두 가지를 고쳤습니다", ctx)      # 멘션한 세션의 답이 맥락으로 들어간다

        def done():
            st, d = SRV.req("GET", "/api/sessions/" + sid)
            t = d["turns"][-1] if d["turns"] else None
            return d if t and t["status"] == "completed" and t["answer"] else None
        d = wait(done)
        t = d["turns"][-1]
        self.assertTrue(d["session"]["mine"])
        self.assertIn("작업을 마쳤습니다", t["answer"]["text"])
        self.assertEqual([s["kind"] for s in t["process"]], ["reasoning", "command"])
        # 완료 알림 뒤에도 아직 비어 있으면 답을 지키고, 저장이 끝나면 대기를 해제한다.
        self.assertTrue(d["history_pending"])
        def stored():
            st, d = SRV.req("GET", "/api/sessions/" + sid)
            self.assertEqual(st, 200, d)
            return d if not d["history_pending"] else None
        persisted = wait(stored)
        self.assertEqual(persisted["session"]["context"], {"tokens": 1280, "window": 258400, "percent": 0.5})
        self.assertEqual(len(persisted["turns"]), 1)
        self.assertNotIn(sid, SRV.lens.starting_threads)
        # 다음 턴 — 응답 중이면 409
        st, r = SRV.req("POST", "/api/sessions/%s/turns" % sid, {"text": "고마워", "client_id": "c-t2"})
        self.assertEqual(st, 200, r)
        st, r = SRV.req("POST", "/api/sessions/%s/turns" % sid, {"text": "또", "client_id": "c-t3"})
        self.assertEqual(st, 409)
        wait(lambda: SRV.req("GET", "/api/sessions/" + sid)[1]["turns"][-1]["status"] == "completed")

    def test_04_sse_live_events(self):
        st, r = SRV.req("POST", "/api/sessions", {"cwd": PROJ, "text": "스트림 시험", "client_id": "c-sse"})
        sid = r["id"]
        resp = urllib.request.urlopen("http://127.0.0.1:%d/api/sessions/%s/events" % (SRV.port, sid), timeout=10)
        kinds = []
        t0 = time.time()
        while time.time() - t0 < 10:
            line = resp.readline().decode()
            if line.startswith("data: "):
                ev = json.loads(line[6:])
                kinds.append(ev["type"])
                if ev["type"] == "turn_done":
                    break
        resp.close()
        self.assertIn("live", kinds)
        self.assertEqual(kinds[-1], "turn_done")

    def test_04b_codex_turn_has_measured_rate(self):
        """Codex 턴은 기록(rollout)의 실제 출력 토큰 ÷ 응답 시간을 싣는다 — 지난 턴도, 진행 중 스냅숏도."""
        st, r = SRV.req("POST", "/api/sessions", {"cwd": PROJ, "text": "속도 시험", "client_id": "c-rate"})
        self.assertEqual(st, 200, r)
        sid = r["id"]
        turn = wait(lambda: (lambda t: t if t["status"] == "completed" and t.get("rate") else None)(
            SRV.req("GET", "/api/sessions/" + sid)[1]["turns"][-1]))
        rate = turn["rate"]
        self.assertTrue(rate["measured"])
        self.assertEqual(rate["responses"], 2)                  # 명령 앞 응답 + 답 응답
        self.assertGreater(rate["tps"], 0)
        self.assertEqual(rate["total_out"], 120 + rate["out"])
        self.assertNotIn("speed", turn.get("live") or {})       # Codex 는 수신량 추정을 쓰지 않는다
        lt = SRV.lens.live.get(sid)
        if lt:
            self.assertEqual(lt.snapshot()["rate"]["responses"], 2)

    def test_05_summary_uses_session_model_fork(self):
        st, r = SRV.req("POST", "/api/sessions/%s/summaries" % S1, {"turn_id": "turn-a1", "request": "calc.py 고쳐 줘",
                                                                    "effort": "medium", "client_id": "sum1"})
        self.assertEqual(st, 200, r)
        self.assertEqual(r["job"]["id"], "sum1")
        job = wait(lambda: (lambda j: j if j["status"] != "running" else None)(SRV.req("GET", "/api/jobs/sum1")[1]["job"]))
        self.assertEqual(job["status"], "done", job)
        self.assertIn("한 줄 요약", job["answer"])
        self.assertEqual(job["model"], "gpt-fake-sol")
        self.assertEqual(job["usage"]["cached"], 1000)          # 같은 모델 → 캐시 읽음
        fork = calls("thread/fork")[-1]["params"]
        self.assertEqual(fork["model"], "gpt-fake-sol")
        self.assertEqual(fork["lastTurnId"], "turn-a1")
        self.assertTrue(fork["ephemeral"])
        self.assertIn("[과정 요약 요청", calls("turn/start")[-1]["params"]["input"][0]["text"])
        self.assertEqual(calls("turn/start")[-1]["params"]["effort"], "medium")   # 고른 effort 로 돈다
        self.assertEqual(job["effort"], "medium")
        # 세션 읽기에 작업이 실린다
        d = SRV.req("GET", "/api/sessions/" + S1)[1]
        self.assertIn("sum1", [j["id"] for j in d["jobs"]])

    def test_06_inline_question_and_followup_reuse_fork(self):
        st, r = SRV.req("POST", "/api/sessions/%s/questions" % S1, {"turn_id": "turn-a1", "quote": "0 나누기 분기", "start": 10,
                                                                    "part": "answer", "question": "왜 문자열로 돌려줘?", "client_id": "q1"})
        self.assertEqual(st, 200, r)
        j1 = wait(lambda: (lambda j: j if j["status"] != "running" else None)(SRV.req("GET", "/api/jobs/q1")[1]["job"]))
        self.assertEqual(j1["status"], "done", j1)
        self.assertEqual(j1["fork_mode"], "new")
        self.assertIn("왜 문자열로 돌려줘?", j1["answer"])
        fork = calls("thread/fork")[-1]["params"]
        self.assertFalse(fork["ephemeral"])
        self.assertEqual(fork["sandbox"], "read-only")
        prompt = calls("turn/start")[-1]["params"]["input"][0]["text"]
        self.assertIn("> 0 나누기 분기", prompt)
        # 포크는 세션 목록에서 숨는다
        ids = [s["id"] for s in SRV.req("GET", "/api/sessions")[1]["sessions"]]
        self.assertNotIn(j1["fork_thread"], ids)
        # 이어 묻기는 같은 포크
        st, r = SRV.req("POST", "/api/sessions/%s/questions" % S1, {"turn_id": "turn-a1", "quote": "계산기 예제", "part": "q:q1",
                                                                    "question": "더 쉽게", "parent_id": "q1", "client_id": "q2"})
        j2 = wait(lambda: (lambda j: j if j["status"] != "running" else None)(SRV.req("GET", "/api/jobs/q2")[1]["job"]))
        self.assertEqual(j2["status"], "done", j2)
        self.assertEqual(j2["fork_mode"], "reused")
        self.assertEqual(j2["fork_thread"], j1["fork_thread"])
        self.assertEqual(j2["depth"], 1)
        self.assertIn("앞 답에서 고른 부분", calls("turn/start")[-1]["params"]["input"][0]["text"])
        # 지우기
        st, r = SRV.req("DELETE", "/api/jobs/q2")
        self.assertTrue(r["deleted"])
        self.assertEqual(SRV.req("GET", "/api/jobs/q2")[0], 404)

    def test_07_question_needs_quote(self):
        st, r = SRV.req("POST", "/api/sessions/%s/questions" % S1, {"turn_id": "turn-a1", "quote": "  "})
        self.assertEqual(st, 400)

    def test_08_comments_lifecycle(self):
        body = {"id": "cm1", "turn_id": "turn-a1", "part": "answer", "quote": "사용법", "text": "예시를 더", "state": "input"}
        st, r = SRV.req("POST", "/api/sessions/%s/comments" % S1, body)
        self.assertEqual(st, 200)
        st, r2 = SRV.req("POST", "/api/sessions/%s/comments" % S1, dict(body, text="다른 글"))   # 같은 id 는 한 번만
        self.assertEqual(r2["comment"]["text"], "예시를 더")
        self.assertEqual(len(SRV.req("GET", "/api/sessions/%s/comments" % S1)[1]["comments"]), 1)
        SRV.lens.mark_sent(S1, {"cm1"})
        c = SRV.req("GET", "/api/sessions/%s/comments" % S1)[1]["comments"][0]
        self.assertEqual(c["state"], "sent")
        st, r = SRV.req("PATCH", "/api/comments/cm1", {"state": "memo"})
        self.assertEqual(r["comment"]["state"], "memo")
        st, r = SRV.req("DELETE", "/api/comments/cm1")
        self.assertTrue(r["deleted"])

    def test_09_files_and_md_comments(self):
        st, r = SRV.req("GET", "/api/files?cwd=%s&q=dsg" % urllib.request.quote(PROJ))
        self.assertEqual(r["files"][0]["rel"], "docs/design.md")      # 띄엄띄엄 맞춰도 찾는다
        path = os.path.join(PROJ, "docs", "design.md")
        st, f = SRV.req("GET", "/api/file?path=" + urllib.request.quote(path))
        self.assertIn("설계 메모", f["text"])
        st, r = SRV.req("POST", "/api/md-comments", {"id": "md1", "path": path, "quote": "오류는 문자열로", "line": 5, "text": "예외가 낫지 않나?"})
        self.assertEqual(st, 200)
        cs = SRV.req("GET", "/api/md-comments?path=" + urllib.request.quote(path))[1]["comments"]
        self.assertEqual([c["id"] for c in cs], ["md1"])
        SRV.req("PATCH", "/api/md-comments/md1", {"state": "input"})
        SRV.lens.mark_sent(S1, {"md1"})
        self.assertEqual(SRV.req("GET", "/api/md-comments?path=" + urllib.request.quote(path))[1]["comments"][0]["state"], "sent")
        self.assertEqual(SRV.req("GET", "/api/file?path=/nope.md")[0], 404)

    def test_09b_tree(self):
        q = urllib.request.quote
        st, r = SRV.req("GET", "/api/tree?root=" + q(PROJ))
        self.assertEqual(st, 200, r)
        names = [(e["kind"], e["name"]) for e in r["entries"]]
        self.assertEqual(names[0], ("dir", "docs"))                    # 폴더가 먼저
        self.assertIn(("file", "README.md"), names)
        st, r = SRV.req("GET", "/api/tree?root=%s&dir=docs" % q(PROJ))
        self.assertEqual([e["rel"] for e in r["entries"]], ["docs/design.md"])
        self.assertEqual(SRV.req("GET", "/api/tree?root=%s&dir=../.." % q(PROJ))[0], 400)   # 프로젝트 밖
        self.assertEqual(SRV.req("GET", "/api/tree?root=/nope-dir")[0], 400)

    def test_09c_refine_doc_by_comments(self):
        q = urllib.request.quote
        path = os.path.join(PROJ, "README.md")
        before = rd(path)
        SRV.req("POST", "/api/md-comments", {"id": "rf1", "path": path, "quote": "작은 파이썬 계산기입니다.", "line": 2,
                                             "start": 8, "text": "무엇을 하는지 한 문장 더"})
        SRV.req("POST", "/api/md-comments", {"id": "rf2", "path": path, "quote": "곱셈 기호 `x` 도 받기", "line": 11, "start": 90,
                                             "text": ""})
        SRV.req("POST", "/api/md-comments", {"id": "rf3", "path": path, "quote": "사용법", "text": "메모", "state": "none"})
        SRV.req("PATCH", "/api/md-comments/rf3", {"state": "memo"})
        # 탐색기에 열린 코멘트 수가 보인다(메모는 뺀다)
        tr = SRV.req("GET", "/api/tree?root=" + q(PROJ))[1]
        self.assertEqual({e["name"]: e["comments"] for e in tr["entries"]}["README.md"], 2)
        self.assertEqual(SRV.req("POST", "/api/md-refines", {"path": path, "comment_ids": []})[0], 400)
        body = {"path": path, "root": PROJ, "client_id": "rf-req-1", "model": "gpt-fake-sol", "effort": "low",
                "instruction": "문장은 짧게"}
        st, r = SRV.req("POST", "/api/md-refines", body)
        self.assertEqual(st, 200, r)
        ref = r["refine"]
        self.assertEqual(ref["comment_ids"], ["rf1", "rf2"])            # 열린 코멘트만, 문서 순서대로
        self.assertNotIn("before", ref)
        self.assertEqual(SRV.req("POST", "/api/md-refines", body)[1]["refine"]["thread_id"], ref["thread_id"])  # 중복 전송
        start = [c for c in calls("turn/start") if c["params"]["threadId"] == ref["thread_id"]]
        self.assertEqual(len(start), 1)
        prompt = start[0]["params"]["input"][0]["text"]
        self.assertIn("[codex-lens · 문서 다듬기]", prompt)
        self.assertIn("- 문서: " + path, prompt)
        self.assertIn("> 작은 파이썬 계산기입니다.\n의견: 무엇을 하는지 한 문장 더", prompt)
        self.assertIn("의견: (없음", prompt)
        self.assertIn("추가 요청: 문장은 짧게", prompt)
        self.assertEqual(start[0]["params"]["effort"], "low")
        self.assertEqual(calls("thread/start")[-1]["params"]["cwd"], PROJ)   # 프로젝트 폴더에서 새 세션
        cs = {c["id"]: c for c in SRV.req("GET", "/api/md-comments?path=" + q(path))[1]["comments"]}
        self.assertEqual((cs["rf1"]["state"], cs["rf1"]["refine_id"]), ("sent", "rf-req-1"))
        self.assertEqual(cs["rf3"]["state"], "memo")

        def done():
            rs = SRV.req("GET", "/api/md-refines?path=" + q(path))[1]["refines"]
            return rs[0] if rs and rs[0]["status"] != "running" and rs[0].get("answer") else None
        ref = wait(done)
        self.assertEqual(ref["status"], "completed", ref)
        self.assertIn("코멘트 2개를 반영했습니다", ref["answer"])
        after = rd(path)
        self.assertIn("작은 파이썬 계산기입니다. (다듬음)", after)
        self.assertEqual(ref["diff"]["added"], 2)
        self.assertEqual(ref["diff"]["lines"], [2, 11])
        self.assertTrue(ref["can_revert"] and ref["current"])
        # 되돌리기 — 고치기 전 글로, 코멘트는 다시 열린다
        st, r = SRV.req("POST", "/api/md-refines/rf-req-1/revert", {})
        self.assertEqual(st, 200, r)
        self.assertEqual(rd(path), before)
        self.assertFalse(r["refine"]["can_revert"])
        cs = {c["id"]: c for c in SRV.req("GET", "/api/md-comments?path=" + q(path))[1]["comments"]}
        self.assertEqual(cs["rf1"]["state"], "none")
        self.assertEqual(SRV.req("POST", "/api/md-refines/rf-req-1/revert", {})[0], 400)
        for cid in ("rf1", "rf2", "rf3"):
            SRV.req("DELETE", "/api/md-comments/" + cid)

    def test_09d_unread_body_keeps_connection_clean(self):
        # 본문을 쓰지 않는 API 에 본문을 보내도 같은 연결의 다음 요청이 깨지지 않는다(keep-alive)
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", SRV.port, timeout=10)
        c.request("POST", "/api/md-refines/nope/revert", body=b"{}", headers={"Content-Type": "application/json"})
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 404)
        c.request("GET", "/api/info")
        r = c.getresponse()
        self.assertEqual(r.status, 200, r.read()[:200])
        r.read()
        c.close()

    def test_09b_refine(self):
        done = lambda jid: wait(lambda: (lambda j: j if j["status"] != "running" else None)(SRV.req("GET", "/api/jobs/" + jid)[1]["job"]))
        self.assertEqual(SRV.req("POST", "/api/refine", {"text": "  "})[0], 400)
        # 세션 화면 — 마지막 끝난 턴까지 같은 모델로 일회용 포크
        st, r = SRV.req("POST", "/api/refine", {"text": "README 고쳐 줘 @calc.py 좀", "thread_id": S1, "turn_id": "turn-a1",
                                                "draft_key": S1, "client_id": "rf1"})
        self.assertEqual(st, 200, r)
        j = done("rf1")
        self.assertEqual(j["status"], "done", j)
        self.assertTrue(j["result"]["request"].startswith("목표: README 고쳐 줘"))
        self.assertIn("@calc.py", j["result"]["request"])
        self.assertEqual(j["result"]["lost_mentions"], [])
        self.assertEqual(j["result"]["missing_context"][0]["options"], ["calc.py", "README.md"])
        self.assertEqual(j["result"]["opinion"], "범위가 분명해 바로 진행해도 됩니다.")
        self.assertEqual(j["effort"], "low")
        fork = calls("thread/fork")[-1]["params"]
        self.assertEqual((fork["threadId"], fork["lastTurnId"], fork["model"]), (S1, "turn-a1", "gpt-fake-sol"))
        self.assertTrue(fork["ephemeral"])
        self.assertEqual(fork["sandbox"], "read-only")
        prompt = calls("turn/start")[-1]["params"]["input"][0]["text"]
        self.assertIn("[요청 다듬기", prompt)
        self.assertIn("[원문]\nREADME 고쳐 줘 @calc.py 좀", prompt)
        self.assertIn("원문에 적힌 일을 실행하지 마세요", prompt)
        # 세션 읽기의 작업 목록에는 싣지 않는다(입력창 일이다)
        self.assertNotIn("rf1", [x["id"] for x in SRV.req("GET", "/api/sessions/" + S1)[1]["jobs"]])
        # 추가 질문 답변을 반영해 다시 다듬기 — 같은 형식으로 답변이 함께 간다
        st, r = SRV.req("POST", "/api/refine", {"text": j["result"]["request"], "thread_id": S1, "turn_id": "turn-a1", "effort": "medium",
                                                "answers": [{"question": "어느 파일을 고칠까요?", "answer": "calc.py"}], "client_id": "rf2"})
        j2 = done("rf2")
        self.assertIn("(답변 반영)", j2["result"]["request"])
        self.assertEqual(j2["result"]["missing_context"], [])
        self.assertEqual(calls("turn/start")[-1]["params"]["effort"], "medium")
        self.assertIn('"answer": "calc.py"', calls("turn/start")[-1]["params"]["input"][0]["text"])
        # 새 세션 화면 — 이을 턴이 없으니 고른 모델·작업 폴더로 일회용 빈 세션
        st, r = SRV.req("POST", "/api/refine", {"text": "새 기능 만들어 줘", "cwd": PROJ, "model": "gpt-fake-luna", "client_id": "rf3"})
        j3 = done("rf3")
        self.assertEqual(j3["status"], "done", j3)
        start = calls("thread/start")[-1]["params"]
        self.assertTrue(start["ephemeral"])
        self.assertEqual((start["cwd"], start["model"], start["sandbox"]), (PROJ, "gpt-fake-luna", "read-only"))
        self.assertIsNone(j3["thread_id"])
        self.assertNotIn(j3["fork_thread"], [x["id"] for x in SRV.req("GET", "/api/sessions")[1]["sessions"]])
        self.assertEqual(SRV.req("POST", "/api/refine", {"text": "x", "cwd": "/없는/폴더"})[0], 400)
        # 형식이 어긋난 답은 실패로 남고 글은 그대로다
        SRV.req("POST", "/api/refine", {"text": "망가진 답 주기", "cwd": PROJ, "client_id": "rf4"})
        j4 = done("rf4")
        self.assertEqual(j4["status"], "failed")
        self.assertIn("형식", j4["error"])
        self.assertIsNone(j4["result"])
        # 다시 시도는 같은 입력으로 새 작업
        st, r = SRV.req("POST", "/api/jobs/rf3/retry", {})
        self.assertEqual(r["replaced"], "rf3")
        self.assertEqual(done(r["job"]["id"])["text"], "새 기능 만들어 줘")

    def test_09d_refine_modes_prompts_preview(self):
        done = lambda jid: wait(lambda: (lambda j: j if j["status"] != "running" else None)(SRV.req("GET", "/api/jobs/" + jid)[1]["job"]))
        import refine
        # 목적별 다듬기 지시 — 기본값, 고치기, 기본으로 되돌리기
        st, r = SRV.req("GET", "/api/refine/prompts")
        self.assertEqual(st, 200, r)
        self.assertEqual(r["prompts"]["request"]["text"], refine.REFINE_PROMPT)
        self.assertEqual(r["prompts"]["writing"]["text"], refine.WRITING_REFINE_PROMPT)
        self.assertFalse(r["prompts"]["writing"]["custom"])
        self.assertEqual(SRV.req("POST", "/api/refine/prompts", {"mode": "x", "text": "a"})[0], 400)
        st, r = SRV.req("POST", "/api/refine/prompts", {"mode": "writing", "text": "고친 지시로 다듬어 주세요."})
        self.assertTrue(r["prompt"]["custom"])
        # 함께 보내는 내용 — 실제 작업과 같은 조립. 세션 화면이면 포크 머리말, 새 세션이면 작업 폴더
        st, r = SRV.req("POST", "/api/refine/preview", {"mode": "writing", "thread_id": S1, "turn_id": "turn-a1"})
        self.assertTrue(r["forked"])
        self.assertEqual([p["key"] for p in r["parts"]], ["head", "prompt", "fixed", "source", "guard"])
        self.assertEqual(r["parts"][1]["text"], "고친 지시로 다듬어 주세요.")
        st, r = SRV.req("POST", "/api/refine/preview", {"mode": "request", "cwd": PROJ, "answers": [{"question": "q", "answer": "a"}]})
        self.assertFalse(r["forked"])
        self.assertEqual([p["key"] for p in r["parts"]], ["head", "prompt", "opinion", "answer", "fixed", "source", "guard", "answers"])
        self.assertIn(PROJ, r["parts"][0]["text"])
        # 좋은 글 목적 — 저장된 고친 지시가 작업에 고정되고, 본문은 request 로, 편집 메모는 따로 온다
        st, r = SRV.req("POST", "/api/refine", {"text": "생각 메모 @calc.py", "thread_id": S1, "turn_id": "turn-a1", "mode": "writing",
                                                "client_id": "rfw1"})
        self.assertEqual(st, 200, r)
        j = done("rfw1")
        self.assertEqual(j["status"], "done", j)
        self.assertEqual((j["mode"], j["prompt"]), ("writing", "고친 지시로 다듬어 주세요."))
        self.assertEqual(j["result"]["request"], "다듬은 글: 생각 메모 @calc.py (고친 지시)")
        self.assertEqual(j["result"]["editing_notes"][0]["note"], "이 말이 무엇을 가리키는지 확인하세요.")
        self.assertEqual(j["result"]["missing_context"], [])
        prompt = calls("turn/start")[-1]["params"]["input"][0]["text"]
        self.assertIn("고친 지시로 다듬어 주세요.", prompt)
        self.assertNotIn(refine.OPINION_PROMPT, prompt)
        # 기본 프롬프트로 — 다음 작업부터 기본 지시
        st, r = SRV.req("POST", "/api/refine/prompts", {"mode": "writing", "text": None})
        self.assertFalse(r["prompt"]["custom"])
        # 다듬기 모델 — 세션과 다른 모델로 포크하고, Codex 속도(serviceTier)를 넘긴다. 다른 에이전트의 모델은 거절한다
        st, r = SRV.req("POST", "/api/refine", {"text": "README 고쳐", "thread_id": S1, "turn_id": "turn-a1", "refine_model": "gpt-fake-luna",
                                                "tier": "priority", "effort": "high", "client_id": "rfm1"})
        self.assertEqual(st, 200, r)
        j = done("rfm1")
        self.assertEqual((j["status"], j["model"]), ("done", "gpt-fake-luna"), j)
        self.assertEqual(calls("thread/fork")[-1]["params"]["model"], "gpt-fake-luna")
        ts = calls("turn/start")[-1]["params"]
        self.assertEqual((ts["model"], ts["effort"], ts["serviceTier"]), ("gpt-fake-luna", "high", "priority"))
        self.assertEqual(SRV.req("POST", "/api/refine", {"text": "x", "thread_id": S1, "turn_id": "turn-a1", "refine_model": "없는-모델"})[0], 400)
        # 다시 시도는 목적 · 지시 · 다듬기 모델을 그대로 잇는다
        st, r = SRV.req("POST", "/api/jobs/rfw1/retry", {})
        j2 = done(r["job"]["id"])
        self.assertEqual((j2["mode"], j2["prompt"]), ("writing", "고친 지시로 다듬어 주세요."))
        st, r = SRV.req("POST", "/api/jobs/rfm1/retry", {})
        self.assertEqual(done(r["job"]["id"])["model"], "gpt-fake-luna")

    def test_refine_parse(self):
        import refine
        wrapped = json.dumps({"protocol": "agent-output/1", "body": "다듬었습니다.\n```json\n" + json.dumps(
            {"request": "@a.py 고치기", "missing_context": ["범위는?", {"question": ""}], "opinion": 3}) + "\n```"})
        out = refine.parse(wrapped, "@a.py @세션:회의 고쳐")
        self.assertEqual(out["request"], "@a.py 고치기")
        self.assertEqual(out["missing_context"], [{"question": "범위는?", "purpose": ""}])
        self.assertEqual(out["opinion"], "")
        self.assertEqual(out["lost_mentions"], ["@세션:회의"])
        self.assertNotIn("@b.com", refine.parse('{"request":"메일"}', "a@b.com 로")["lost_mentions"])
        with self.assertRaises(ValueError):
            refine.parse('{"request":"  "}', "x")

    def test_10_models(self):
        st, r = SRV.req("GET", "/api/models")
        self.assertEqual(r["models"][0]["efforts"], ["low", "medium", "high"])
        codex = [m for m in r["models"] if m["agent"] == "codex"]
        self.assertEqual([m["id"] for m in codex], ["gpt-fake-sol", "gpt-fake-luna"])   # Codex 가 숨긴 모델은 빠진다
        self.assertEqual(codex[0]["tiers"], [{"id": "priority", "name": "Fast", "description": "2x speed, increased usage"}])
        self.assertEqual(codex[1]["tiers"], [])
        self.assertTrue(all(m["tiers"] == [] for m in r["models"] if m["agent"] == "claude"))   # Claude 에는 속도 등급이 없다
        self.assertEqual(r["standard_tier"], "default")
        empty = {"model": "", "effort": "", "tier": ""}
        low = {"summary": "low", "question": "low"}
        self.assertEqual(r["settings"], {"hidden": [], "defaults": {"codex": empty, "claude": empty},
                                         "fork_efforts": {"codex": low, "claude": low}})

        # 아무것도 고르지 않았을 때 실제로 쓰일 값 — 새 세션은 Codex 설정, 세션은 그 세션의 값
        self.assertEqual(r["current"], {"model": "gpt-fake-sol", "effort": "high"})
        self.assertEqual(r["currents"]["codex"], r["current"])
        claude_ids = [m["id"] for m in r["models"] if m.get("agent") == "claude"]
        self.assertIn(r["currents"]["claude"]["model"], claude_ids + [None])   # Claude 는 설정의 기본 모델(effort 는 비움)
        self.assertIsNone(r["currents"]["claude"]["effort"])
        st, r = SRV.req("GET", "/api/models?thread=" + S1)
        self.assertEqual(r["current"], {"model": "gpt-fake-sol", "effort": "medium"})
        self.assertEqual(r["agent"], "codex")
    def _done(self, sid):
        """세션의 마지막 턴이 끝날 때까지 — 막 만든 세션은 기록이 쓰이기 전이라 턴 목록이 잠시 빌 수 있다."""
        def f():
            t = SRV.req("GET", "/api/sessions/" + sid)[1].get("turns") or []
            return bool(t) and t[-1]["status"] == "completed"
        return wait(f)

    def test_10b_model_settings_defaults_and_fast(self):
        """모델 관리: 에이전트별 기본 모델·effort·속도는 새 세션의 "기본"(값 없음)에 쓰이고, 기본 모델은 숨길 수 없다."""
        try:
            st, r = SRV.req("POST", "/api/models/settings", {"model": "gpt-fake-nope"})
            self.assertEqual(st, 400, r)
            st, r = SRV.req("POST", "/api/models/settings", {"model": "gpt-fake-luna", "effort": "high"})
            self.assertEqual(st, 400, r)    # luna 는 high 를 받지 않는다
            st, r = SRV.req("POST", "/api/models/settings", {"model": "gpt-fake-luna", "tier": "priority"})
            self.assertEqual(st, 400, r)    # luna 는 Fast 가 없다
            st, r = SRV.req("POST", "/api/models/settings", {"model": "gpt-fake-sol", "effort": "high", "tier": "priority",
                                                               "hidden": ["gpt-fake-luna", "gpt-fake-sol"]})
            self.assertEqual(st, 200, r)
            self.assertEqual(r["settings"]["hidden"], ["gpt-fake-luna"])   # 기본 모델은 숨김에서 빠진다
            st, r = SRV.req("GET", "/api/models")
            self.assertEqual(r["settings"]["defaults"]["codex"]["tier"], "priority")
            self.assertEqual(r["currents"]["codex"], {"model": "gpt-fake-sol", "effort": "high"})   # 칩이 보일 실제 값
            self.assertEqual(r["settings"]["defaults"]["claude"]["model"], "")      # 다른 에이전트 기본값은 그대로
            self.assertEqual(len([m for m in r["models"] if m["agent"] == "codex"]), 2)   # 숨김은 고르기 목록에서만 뺀다
            # 아무것도 고르지 않은 새 세션 → 기본값이 thread/start · turn/start 에 실린다
            st, r = SRV.req("POST", "/api/sessions", {"cwd": PROJ, "text": "기본값으로", "client_id": "c-def-1"})
            self.assertEqual(st, 200, r)
            ts = calls("thread/start")[-1]["params"]
            self.assertEqual((ts.get("model"), ts.get("serviceTier")), ("gpt-fake-sol", "priority"))
            t1 = calls("turn/start")[-1]["params"]
            self.assertEqual((t1.get("model"), t1.get("effort"), t1.get("serviceTier")), ("gpt-fake-sol", "high", "priority"))
            self._done(r["id"])
            # 기존 세션의 다음 턴은 "기본"이면 아무것도 넘기지 않는다(세션 값 유지), 고르면 그대로
            sid = r["id"]
            SRV.req("POST", "/api/sessions/%s/turns" % sid, {"text": "그대로", "client_id": "c-def-2"})
            t2 = calls("turn/start")[-1]["params"]
            self.assertNotIn("serviceTier", t2)
            self.assertNotIn("model", t2)
            self._done(sid)
            SRV.req("POST", "/api/sessions/%s/turns" % sid, {"text": "표준으로", "client_id": "c-def-3", "tier": "default"})
            self.assertEqual(calls("turn/start")[-1]["params"]["serviceTier"], "default")
            self._done(sid)
            # 직접 고른 모델이 기본 effort·속도를 받지 않으면 그 칸은 넘기지 않는다
            st, r = SRV.req("POST", "/api/sessions", {"cwd": PROJ, "text": "가벼운 모델로", "client_id": "c-def-4",
                                                       "model": "gpt-fake-luna"})
            self.assertEqual(st, 200, r)
            ts = calls("thread/start")[-1]["params"]
            t4 = calls("turn/start")[-1]["params"]
            self.assertEqual(ts.get("model"), "gpt-fake-luna")
            self.assertNotIn("serviceTier", ts)
            self.assertNotIn("effort", t4)
            self._done(r["id"])
            # Claude Code: 다른 에이전트 모델·속도는 거절, 기본 모델은 Claude 새 세션에만 쓰인다
            st, r = SRV.req("POST", "/api/models/settings", {"agent": "claude", "model": "gpt-fake-sol"})
            self.assertEqual(st, 400, r)
            st, r = SRV.req("POST", "/api/models/settings", {"agent": "claude", "tier": "priority"})
            self.assertEqual(st, 400, r)
            st, r = SRV.req("POST", "/api/models/settings", {"agent": "claude", "model": "claude-sonnet-5-5", "hidden": ["claude-sonnet-5-5"]})
            self.assertEqual(st, 200, r)
            self.assertEqual(r["settings"]["defaults"]["claude"]["model"], "claude-sonnet-5-5")
            st, r2 = SRV.req("GET", "/api/models")
            self.assertEqual(r2["currents"]["claude"]["model"], "claude-sonnet-5-5")
            self.assertEqual(r["settings"]["defaults"]["codex"]["model"], "gpt-fake-sol")
            self.assertEqual(r["settings"]["hidden"], [])   # 어느 에이전트든 기본 모델은 숨길 수 없다
            st, r = SRV.req("POST", "/api/sessions", {"cwd": PROJ, "text": "클로드 기본값으로", "client_id": "c-def-5", "agent": "claude"})
            self.assertEqual(st, 200, r)
            d = wait(lambda: (lambda d: d if d.get("turns") and d["turns"][-1]["status"] == "completed" else None)(
                SRV.req("GET", "/api/sessions/" + r["id"])[1]), 20)
            self.assertEqual((d["session"]["agent"], d["session"]["model"]), ("claude", "claude-sonnet-5-5"))
        finally:
            SRV.req("POST", "/api/models/settings", {"agent": "codex", "model": "", "effort": "", "tier": "", "hidden": []})
            SRV.req("POST", "/api/models/settings", {"agent": "claude", "model": "", "effort": "", "tier": ""})

    def test_10d_recent_picks(self):
        """최근 칩: 보내기가 성공하면 그때 쓴 {model, effort, tier} 가 에이전트별로 맨 앞에 남고, 같은 조합은 하나만."""
        st, r = SRV.req("POST", "/api/sessions", {"cwd": PROJ, "text": "최근 칩 1", "client_id": "c-rp-1",
                                                   "recent": {"agent": "codex", "model": "gpt-fake-luna", "effort": "low", "tier": ""}})
        self.assertEqual(st, 200, r)
        sid = r["id"]
        self._done(sid)
        SRV.req("POST", "/api/sessions/%s/turns" % sid, {"text": "최근 칩 2", "client_id": "c-rp-2", "model": "gpt-fake-sol",
                                                         "recent": {"agent": "codex", "model": "gpt-fake-sol", "effort": "high", "tier": "priority"}})
        self._done(sid)
        SRV.req("POST", "/api/sessions/%s/turns" % sid, {"text": "최근 칩 3", "client_id": "c-rp-3",
                                                         "recent": {"agent": "codex", "model": "gpt-fake-luna", "effort": "low", "tier": ""}})
        self._done(sid)
        SRV.req("POST", "/api/sessions/%s/turns" % sid, {"text": "모델 없는 기록은 무시", "client_id": "c-rp-4", "recent": {"agent": "codex"}})
        self._done(sid)
        st, r = SRV.req("GET", "/api/models")
        got = [(e["model"], e["effort"], e["tier"]) for e in r["recent"]["codex"]]
        self.assertEqual(got[:2], [("gpt-fake-luna", "low", ""), ("gpt-fake-sol", "high", "priority")])
        self.assertEqual(len(got), len(set(got)))
        self.assertTrue(all(e["model"] != "gpt-fake-luna" for e in r["recent"]["claude"]))   # 에이전트별로 따로
        # recent 가 이상해도 보내기는 실패하지 않는다
        st, r = SRV.req("POST", "/api/sessions/%s/turns" % sid, {"text": "이상한 기록", "client_id": "c-rp-5", "recent": "x"})
        self.assertEqual(st, 200, r)
        self._done(sid)

    def test_10c_fork_effort_defaults(self):
        """모델 관리: 과정 요약 · 인라인 질문의 기본 effort 를 에이전트마다 정하고, 고르지 않은 포크에 쓴다."""
        def done(jid):
            return wait(lambda: (lambda j: j if j["status"] != "running" else None)(SRV.req("GET", "/api/jobs/" + jid)[1]["job"]))
        try:
            st, r = SRV.req("POST", "/api/models/settings", {"fork_efforts": {"question": "max"}})
            self.assertEqual(st, 400, r)    # Codex 모델은 아무도 max 를 받지 않는다
            st, r = SRV.req("POST", "/api/models/settings", {"fork_efforts": {"summary": "medium", "question": "high"}})
            self.assertEqual(st, 200, r)
            self.assertEqual(r["settings"]["fork_efforts"]["codex"], {"summary": "medium", "question": "high"})
            self.assertEqual(r["settings"]["fork_efforts"]["claude"], {"summary": "low", "question": "low"})   # 다른 에이전트는 그대로
            self.assertEqual(r["settings"]["defaults"]["codex"], {"model": "", "effort": "", "tier": ""})   # 새 세션 기본값도 그대로
            # 고르지 않은 요약 · 질문은 기본값으로 돈다
            SRV.req("POST", "/api/sessions/%s/summaries" % S1, {"turn_id": "turn-a1", "request": "요약", "client_id": "fe-sum"})
            j = done("fe-sum")
            self.assertEqual((j["effort"], calls("turn/start")[-1]["params"]["effort"]), ("medium", "medium"))
            SRV.req("POST", "/api/sessions/%s/questions" % S1, {"turn_id": "turn-a1", "quote": "계산기", "client_id": "fe-q"})
            j = done("fe-q")
            self.assertEqual((j["effort"], calls("turn/start")[-1]["params"]["effort"]), ("high", "high"))
            # 고르면 그 값
            SRV.req("POST", "/api/sessions/%s/summaries" % S1, {"turn_id": "turn-a1", "request": "요약", "effort": "low", "client_id": "fe-sum2"})
            self.assertEqual(done("fe-sum2")["effort"], "low")
            # 비우면 환경변수 기본(low)으로
            st, r = SRV.req("POST", "/api/models/settings", {"fork_efforts": {"summary": ""}})
            self.assertEqual(r["settings"]["fork_efforts"]["codex"], {"summary": "low", "question": "high"})
            # Claude Code 는 Claude 모델이 받는 값(max 등)을 받는다
            st, r = SRV.req("POST", "/api/models/settings", {"agent": "claude", "fork_efforts": {"summary": "max"}})
            self.assertEqual(st, 200, r)
            self.assertEqual(SRV.req("GET", "/api/models")[1]["settings"]["fork_efforts"]["claude"]["summary"], "max")
        finally:
            SRV.req("POST", "/api/models/settings", {"agent": "codex", "fork_efforts": {"summary": "", "question": ""}})
            SRV.req("POST", "/api/models/settings", {"agent": "claude", "fork_efforts": {"summary": "", "question": ""}})

    def test_10d_fit_effort(self):
        """세션 모델이 받지 않는 기본 effort 는 가장 가까운 값(같은 거리면 약한 쪽)으로 맞춘다."""
        from lens import fit_effort
        self.assertEqual(fit_effort("max", ["low", "medium", "high"]), "high")
        self.assertEqual(fit_effort("high", ["low"]), "low")
        self.assertEqual(fit_effort("medium", ["low", "high"]), "low")
        self.assertEqual(fit_effort("medium", []), "medium")          # 모르는 모델은 그대로
        self.assertEqual(fit_effort("high", ["low", "high"]), "high")

    def _new_slow(self, cid):
        st, r = SRV.req("POST", "/api/sessions", {"cwd": PROJ, "text": "천천히 살펴봐 줘", "client_id": cid})
        self.assertEqual(st, 200, r)
        sid = r["id"]
        wait(lambda: SRV.lens.live.get(sid) and SRV.lens.live[sid].turn_id)
        return sid

    def _last_done(self, sid, n):
        def f():
            d = SRV.req("GET", "/api/sessions/" + sid)[1]
            return d if len(d["turns"]) == n and d["turns"][-1]["status"] == "completed" and d["turns"][-1]["answer"] else None
        return wait(f)

    def test_12_steer_goes_into_running_turn(self):
        """응답 중 '바로 넣기'는 turn/steer 로 진행 중인 턴에 들어가고, 요청과 따로 '끼워 넣은 말'로 보인다."""
        sid = self._new_slow("c-steer-1")
        st, r = SRV.req("POST", "/api/sessions/%s/turns" % sid, {"text": "README 도 같이 봐", "client_id": "c-steer-2", "mode": "steer"})
        self.assertEqual(st, 200, r)
        self.assertTrue(r["steered"])
        steer = calls("turn/steer")[-1]["params"]
        self.assertEqual(steer["expectedTurnId"], r["turn_id"])
        self.assertEqual(steer["clientUserMessageId"], "c-steer-2")
        # 같은 client_id 재전송은 다시 넣지 않는다
        n = len(calls("turn/steer"))
        SRV.req("POST", "/api/sessions/%s/turns" % sid, {"text": "README 도 같이 봐", "client_id": "c-steer-2", "mode": "steer"})
        self.assertEqual(len(calls("turn/steer")), n)
        d = self._last_done(sid, 1)
        t = d["turns"][0]
        self.assertEqual(t["request"]["text"], "천천히 살펴봐 줘")
        self.assertEqual([x["text"] for x in t["request"]["steers"]], ["README 도 같이 봐"])
        self.assertFalse(t["request"]["steers"][0]["pending"])
        self.assertIn("끼워 넣은 말도 읽었습니다", t["answer"]["text"])

    def test_13_queue_sends_after_turn(self):
        """'턴 끝나고'는 대기열에 두었다가 턴이 끝나면 차례로 새 턴으로 보낸다."""
        sid = self._new_slow("c-q-1")
        st, r = SRV.req("POST", "/api/sessions/%s/turns" % sid, {"text": "다음 일 하나", "client_id": "c-q-2", "mode": "queue"})
        self.assertEqual(st, 200, r)
        self.assertTrue(r["queued"])
        st, r2 = SRV.req("POST", "/api/sessions/%s/turns" % sid, {"text": "다음 일 둘", "client_id": "c-q-3", "mode": "queue"})
        q = SRV.req("GET", "/api/sessions/" + sid)[1]["queue"]
        self.assertEqual([x["text"] for x in q], ["다음 일 하나", "다음 일 둘"])
        # 같은 client_id 재전송은 한 번만 쌓인다
        SRV.req("POST", "/api/sessions/%s/turns" % sid, {"text": "다음 일 둘", "client_id": "c-q-3", "mode": "queue"})
        self.assertEqual(len(SRV.req("GET", "/api/sessions/" + sid)[1]["queue"]), 2)
        d = self._last_done(sid, 3)
        self.assertEqual([t["request"]["text"] for t in d["turns"]], ["천천히 살펴봐 줘", "다음 일 하나", "다음 일 둘"])
        self.assertEqual(d["queue"], [])
        # mode 없이 응답 중에 보내면 예전처럼 409
        sid2 = self._new_slow("c-q-4")
        st, r = SRV.req("POST", "/api/sessions/%s/turns" % sid2, {"text": "끼어들기", "client_id": "c-q-5"})
        self.assertEqual(st, 409)
        self._last_done(sid2, 1)

    def test_14_queue_item_steer_remove_and_stop(self):
        """기다리는 메시지를 골라 바로 넣거나 뺄 수 있다. 멈춘 턴 뒤에는 대기열을 자동으로 보내지 않는다."""
        sid = self._new_slow("c-qi-1")
        for i, cid in enumerate(["c-qi-2", "c-qi-3", "c-qi-4"]):
            SRV.req("POST", "/api/sessions/%s/turns" % sid, {"text": "대기 %d" % i, "client_id": cid, "mode": "queue"})
        q = SRV.req("GET", "/api/sessions/" + sid)[1]["queue"]
        st, r = SRV.req("POST", "/api/sessions/%s/queue/%s/send" % (sid, q[1]["id"]))
        self.assertEqual(st, 200, r)
        self.assertTrue(r.get("steered"), r)
        st, r = SRV.req("DELETE", "/api/sessions/%s/queue/%s" % (sid, q[2]["id"]))
        self.assertEqual([x["text"] for x in r["queue"]], ["대기 0"])
        self.assertEqual(SRV.req("DELETE", "/api/sessions/%s/queue/%s" % (sid, q[2]["id"]))[0], 404)
        SRV.req("POST", "/api/sessions/%s/interrupt" % sid, {})
        wait(lambda: SRV.req("GET", "/api/sessions/" + sid)[1]["turns"][-1]["status"] == "interrupted")
        time.sleep(0.5)
        d = SRV.req("GET", "/api/sessions/" + sid)[1]
        self.assertEqual(len(d["turns"]), 1)
        self.assertEqual([x["text"] for x in d["queue"]], ["대기 0"])      # 멈춤 뒤엔 남아 있다
        self.assertEqual([x["text"] for x in d["turns"][0]["request"]["steers"]], ["대기 1"], json.dumps(d["turns"][0], ensure_ascii=False)[:1500])
        # 쉬는 중에 '지금 보내기'는 새 턴으로 나간다
        st, r = SRV.req("POST", "/api/sessions/%s/queue/%s/send" % (sid, q[0]["id"]))
        self.assertEqual(st, 200, r)
        self.assertTrue(r.get("turn_id"))
        d = self._last_done(sid, 2)
        self.assertEqual(d["turns"][-1]["request"]["text"], "대기 0")
        self.assertEqual(d["queue"], [])

    def test_15_rejected_steer_falls_back_to_queue(self):
        """Codex 가 끼워 넣기를 거절하면(턴 끝 무렵) 메시지를 잃지 않고 다음 턴으로 보낸다."""
        sid = self._new_slow("c-rj-1")
        lt = SRV.lens.live[sid]
        real = lt.turn_id
        lt.turn_id = "turn-stale"          # 기대 턴이 어긋나 거절된다
        st, r = SRV.req("POST", "/api/sessions/%s/turns" % sid, {"text": "거절될 말", "client_id": "c-rj-2", "mode": "steer"})
        lt.turn_id = real
        self.assertEqual(st, 200, r)
        self.assertTrue(r.get("queued"), r)
        d = self._last_done(sid, 2)
        self.assertEqual(d["turns"][-1]["request"]["text"], "거절될 말")
        self.assertEqual(d["turns"][0]["request"]["steers"], [])

    def test_16_list_states_and_external_session(self):
        # 다른 app-server 가 돌리는 턴은 'interrupted' + completedAt 없음으로 보인다 — 기록이 최근이면 진행 중
        S = lambda n: "01a1aaaa00000000000000000000000%d" % n
        st, r = SRV.req("GET", "/api/sessions?limit=100")
        by = {s["id"]: s for s in r["sessions"]}
        kind = lambda n: (by[S(n)].get("state") or {}).get("kind")
        self.assertEqual(by[S(3)]["status"], "running")
        self.assertTrue(by[S(3)]["state"]["external"])
        self.assertTrue(by[S(3)]["state"]["last_signal"])
        self.assertEqual(kind(4), "interrupted")       # 마지막 턴을 사용자가 멈춤(completedAt 있음)
        self.assertEqual(kind(5), "unfinished")        # 끝 기록 없이 10분 넘게 소식 없음
        self.assertEqual(kind(6), "failed")
        self.assertEqual(kind(7), "error")             # Codex 의 systemError
        self.assertIsNone(kind(1))                     # 완료는 표시 없음
        asked = [c["params"]["threadId"] for c in calls("thread/turns/list")]
        self.assertNotIn(S(2), asked)                  # 하루 넘게 바뀌지 않은 세션은 묻지 않는다
        self.assertNotIn(S(7), asked)
        n = len(asked)
        SRV.req("GET", "/api/sessions?limit=100")
        again = {c["params"]["threadId"] for c in calls("thread/turns/list")[n:]}
        self.assertLessEqual(again, {S(3)})            # 기록이 그대로인 세션은 다시 묻지 않는다(S3 은 계속 바뀐다)
        st, d = SRV.req("GET", "/api/sessions/" + S(3))
        last = d["turns"][-1]
        self.assertEqual(last["status"], "inProgress")
        self.assertTrue(last["external"]["last_signal"])
        self.assertIsNone(last["answer"])
        # 다른 곳에서 진행 중이면 이 앱에서 새 턴을 열지 않는다(보내는 방식을 안 줬을 때 — queue·steer 는 ExternalQueueTest)
        st, r = SRV.req("POST", "/api/sessions/%s/turns" % S(3), {"text": "같이 돌리면 안 된다", "client_id": "ext-busy-1"})
        self.assertEqual(st, 409, r)
        self.assertIn("다른 곳", r["error"])
        st, d = SRV.req("GET", "/api/sessions/" + S(4))
        self.assertEqual([t["status"] for t in d["turns"]], ["unfinished", "interrupted"])
        self.assertFalse(any(t.get("external") for t in d["turns"]))

    def test_99_logs_written(self):
        import applog
        applog.flush()
        p = os.path.join(os.environ["CODEX_LENS_HOME"], "logs", "app.jsonl")
        with open(p) as f:
            lines = [json.loads(x) for x in f]
        events = {x["event"] for x in lines}
        self.assertIn("api", events)
        self.assertIn("job_end", events)
        self.assertIn("md_refine_start", events)
        self.assertIn("md_refine_end", events)
        api_line = [x for x in lines if x["event"] == "api"][0]
        self.assertIn("ms", api_line)
        self.assertEqual(api_line["env"], "production")

    def test_12_new_session_in_worktree(self):
        import subprocess
        repo = os.path.join(TMP, "wt-repo")
        os.makedirs(os.path.join(repo, "src"))
        for name, text in (("README.md", "# 시험\n"), ("src/a.py", "x = 1\n")):
            with open(os.path.join(repo, name), "w") as f:
                f.write(text)
        g = ["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
        subprocess.run(g + ["add", "."], check=True)
        subprocess.run(g + ["commit", "-qm", "첫 커밋"], check=True)
        with open(os.path.join(repo, "README.md"), "a") as f:
            f.write("커밋 안 한 변경\n")
        st, r = SRV.req("GET", "/api/git?cwd=" + urllib.request.quote(os.path.join(repo, "src")))
        self.assertEqual((r["git"], r["branch"], r["dirty"], r["has_head"]), (True, "main", 1, True))
        plain = os.path.join(TMP, "plain")
        os.makedirs(plain, exist_ok=True)
        self.assertFalse(SRV.req("GET", "/api/git?cwd=" + urllib.request.quote(plain))[1]["git"])

        st, r = SRV.req("POST", "/api/sessions", {"cwd": os.path.join(repo, "src"), "text": "a.py 고쳐 줘", "worktree": True,
                                                   "client_id": "c-wt", "mentions": [
                                                       {"kind": "file", "path": os.path.join(repo, "src", "a.py"), "rel": "src/a.py"}]})
        self.assertEqual(st, 200, r)
        cwd = calls("thread/start")[-1]["params"]["cwd"]
        wt_root = os.path.dirname(cwd)
        self.assertTrue(cwd.startswith(os.path.join(os.environ["CODEX_LENS_HOME"], "worktrees") + os.sep))
        self.assertEqual((os.path.basename(wt_root), os.path.basename(cwd)), ("wt-repo", "src"))   # 원래 하위 폴더에 해당
        self.assertTrue(os.path.isfile(os.path.join(cwd, "a.py")))
        branch = subprocess.run(["git", "-C", cwd, "symbolic-ref", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.assertTrue(branch.startswith("codex-lens/"), branch)
        with open(os.path.join(wt_root, "README.md")) as f:
            self.assertEqual(f.read(), "# 시험\n")                   # 커밋된 상태에서 딴다
        ctx = calls("turn/start")[-1]["params"]["input"][1]["text"]
        self.assertIn(os.path.join(cwd, "a.py"), ctx)                # 멘션도 워크트리 안 파일로
        self.assertNotIn(os.path.join(repo, "src", "a.py"), ctx)
        meta = SRV.req("GET", "/api/sessions/" + r["id"])[1]["session"]
        self.assertEqual(meta["worktree"]["branch"], branch)
        self.assertEqual(meta["worktree"]["origin_cwd"], os.path.join(repo, "src"))
        self.assertEqual(meta["location"]["kind"], "worktree")
        self.assertEqual(os.path.realpath(meta["location"]["root"]), os.path.realpath(wt_root))
        self.assertEqual(os.path.realpath(meta["location"]["repo"]), os.path.realpath(repo))
        self.assertIn(os.path.join(repo, "src"), SRV.req("GET", "/api/cwds")[1]["cwds"])   # 최근 폴더는 원래 고른 곳
        wait(lambda: SRV.req("GET", "/api/sessions/" + r["id"])[1]["turns"][-1]["status"] == "completed")

        # 끄면 원래 폴더 그대로, git 이 아닌 폴더는 켜도 그대로
        for cwd_in, flag, cid in ((repo, False, "c-wt-off"), (plain, True, "c-wt-nogit")):
            st, r = SRV.req("POST", "/api/sessions", {"cwd": cwd_in, "text": "읽기만", "worktree": flag, "client_id": cid})
            self.assertEqual(st, 200, r)
            self.assertEqual(calls("thread/start")[-1]["params"]["cwd"], cwd_in)
            self.assertNotIn("worktree", SRV.req("GET", "/api/sessions/" + r["id"])[1]["session"])
            wait(lambda: SRV.req("GET", "/api/sessions/" + r["id"])[1]["turns"][-1]["status"] == "completed")


class ArchiveTest(unittest.TestCase):
    def test_archive_restore_persistence_and_search(self):
        path = "/api/sessions/" + S1 + "/archive"
        before = SRV.req("GET", "/api/sessions/" + S1)[1]
        registry = SRV.lens.registry
        try:
            for _ in range(2):
                st, result = SRV.req("POST", path, {"archived": True})
                self.assertEqual(st, 200, result)
                self.assertTrue(result["session"]["archived"])
            self.assertNotIn(S1, [s["id"] for s in SRV.req("GET", "/api/sessions?q=calc.py")[1]["sessions"]])
            SRV.lens.registry = JsonStore(registry.path, {"mine": {}, "forks": {}})
            archived = SRV.req("GET", "/api/sessions?archived=1&q=calc.py")[1]["sessions"]
            self.assertEqual([s["id"] for s in archived], [S1])
            after = SRV.req("GET", "/api/sessions/" + S1)[1]
            self.assertTrue(after["session"]["archived"])
            self.assertEqual(before["turns"], after["turns"])
            self.assertEqual(before["comments"], after["comments"])
            for _ in range(2):
                st, result = SRV.req("POST", path, {"archived": False})
                self.assertEqual(st, 200, result)
                self.assertFalse(result["session"]["archived"])
            self.assertIn(S1, [s["id"] for s in SRV.req("GET", "/api/sessions")[1]["sessions"]])
            self.assertNotIn(S1, [s["id"] for s in SRV.req("GET", "/api/sessions?archived=1")[1]["sessions"]])
        finally:
            SRV.lens.registry.update(lambda d: d.get("archived", {}).pop(S1, None))
            registry._data = None
            SRV.lens.registry = registry

    def test_claude_archive_indexed_search_and_restore(self):
        path = "/api/sessions/" + S3 + "/archive"
        before = SRV.req("GET", "/api/sessions/" + S3)[1]
        query = urllib.parse.quote("목록 체크")
        try:
            st, result = SRV.req("POST", path, {"archived": True})
            self.assertEqual(st, 200, result)
            self.assertEqual(result["session"]["agent"], "claude")
            self.assertTrue(result["session"]["archived"])
            for url in ("/api/sessions?limit=100", "/api/sessions?q=" + query):
                self.assertNotIn(S3, [s["id"] for s in SRV.req("GET", url)[1]["sessions"]])
            archived = SRV.req("GET", "/api/sessions?archived=1&q=" + query)[1]["sessions"]
            self.assertEqual([s["id"] for s in archived], [S3])
            self.assertTrue(archived[0]["archived"])
            self.assertEqual(before["turns"], SRV.req("GET", "/api/sessions/" + S3)[1]["turns"])
            # A filtered fork must stay hidden even when it has an archive entry.
            SRV.lens.registry.update(lambda d: d["forks"].__setitem__(S3, {}))
            self.assertNotIn(S3, [s["id"] for s in SRV.req("GET", "/api/sessions?archived=1&q=" + query)[1]["sessions"]])
        finally:
            SRV.lens.registry.update(lambda d: d["forks"].pop(S3, None))
            SRV.req("POST", path, {"archived": False})
        self.assertIn(S3, [s["id"] for s in SRV.req("GET", "/api/sessions?q=" + query)[1]["sessions"]])

    def test_reject_invalid_and_missing_sessions(self):
        for value in [None, "false", 0, [], {}]:
            self.assertEqual(SRV.req("POST", "/api/sessions/" + S1 + "/archive", {"archived": value})[0], 400)
        self.assertNotEqual(SRV.req("POST", "/api/sessions/missing/archive", {"archived": True})[0], 200)
        self.assertNotIn("missing", SRV.lens.registry.read().get("archived", {}))

    def test_running_turn_survives_archive(self):
        st, created = SRV.req("POST", "/api/sessions", {"cwd": PROJ, "text": "천천히 보관 검증", "client_id": "archive-running"})
        self.assertEqual(st, 200, created)
        sid = created["id"]
        try:
            self.assertEqual(SRV.req("POST", "/api/sessions/" + sid + "/archive", {"archived": True})[0], 200)
            self.assertNotIn(sid, [s["id"] for s in SRV.req("GET", "/api/sessions")[1]["sessions"]])
            wait(lambda: SRV.req("GET", "/api/sessions/" + sid)[1]["turns"][-1]["status"] == "completed")
            self.assertTrue(SRV.req("GET", "/api/sessions/" + sid)[1]["session"]["archived"])
        finally:
            SRV.req("POST", "/api/sessions/" + sid + "/archive", {"archived": False})

    def test_failed_save_keeps_previous_archive_state(self):
        with patch.object(SRV.lens.registry, "save", side_effect=OSError("archive save failed")):
            st, _ = SRV.req("POST", "/api/sessions/" + S1 + "/archive", {"archived": True})
        self.assertEqual(st, 500)
        self.assertFalse(SRV.req("GET", "/api/sessions/" + S1)[1]["session"]["archived"])
        self.assertIn(S1, [s["id"] for s in SRV.req("GET", "/api/sessions")[1]["sessions"]])

    def test_pagination_past_over_one_hundred_hidden_sessions(self):
        # 제외 상한 100개를 넘어 빈 페이지가 이어져도 다음 표시 대상까지 읽는다.
        with tempfile.TemporaryDirectory() as home:
            lens = Lens.__new__(Lens)
            lens.lock = threading.RLock()
            lens.live, lens.meta_cache = {}, {}
            lens.contexts = JsonStore(os.path.join(home, "context.json"), {"threads": {}})
            from collections import OrderedDict
            from session_tree import Launches
            lens.launches = Launches(os.path.join(home, "launches.json"))
            lens.list_pages, lens.parent_misses = OrderedDict(), {}
            lens._warm_search = Mock()
            lens.codex_index = Mock()
            lens.registry = JsonStore(os.path.join(home, "registry.json"), {"mine": {}, "forks": {},
                                       "archived": {"a%03d" % i: 1 for i in range(105)}})
            threads = [{"id": "a%03d" % i} for i in range(105)] + [{"id": "visible1"}, {"id": "visible2"}, {"id": "visible3"}]
            def request(method, p, timeout):
                data = [t for t in threads if t["id"] not in p.get("excludedThreadIds", [])]
                offset = int(p.get("cursor") or 0)
                end = offset + p["limit"]
                return {"data": data[offset:end], "nextCursor": str(end) if end < len(data) else None}
            lens.client = Mock(request=Mock(side_effect=request))
            first = lens.list_sessions(limit=2)
            self.assertEqual([s["id"] for s in first["sessions"]], ["visible1", "visible2"])
            second = lens.list_sessions(cursor=first["next_cursor"], limit=2)
            self.assertEqual([s["id"] for s in second["sessions"]], ["visible3"])
            self.assertIsNone(second["next_cursor"])
            lens.registry.update(lambda d: d.__setitem__("archived", {"visible3": 1}))
            self.assertEqual([s["id"] for s in lens.list_sessions(archived=True, limit=1)["sessions"]], ["visible3"])


class ReattachTest(unittest.TestCase):
    """앱을 다시 띄워도(Lens 를 새로 만들어도) 중계기가 쥔 app-server 와 돌던 턴이 이어진다."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="re-", dir=TMP)
        self.env = dict(os.environ, FAKE_CODEX_SEED=SEED, FAKE_CODEX_LOG=LOG, FAKE_CODEX_DELAY="0.02",
                        FAKE_CODEX_SLOW_SEC="2")
        self.argv = [sys.executable, os.path.join(ROOT, "tests", "fake_app_server.py")]
        self.lenses = []

    def tearDown(self):
        for ln in reversed(self.lenses):
            ln.stop.set()
        if self.lenses:
            self.lenses[-1].client.shutdown_backend()
        for ln in self.lenses:
            ln.client.close()

    def lens(self, argv=None):
        ln = Lens(self.home, "test", codex_env=self.env, codex_argv=argv or self.argv)
        self.lenses.append(ln)
        return ln

    def test_turn_survives_app_restart(self):
        a = self.lens()
        out = a.create_session(PROJ, "천천히 살펴봐 줘")
        tid = out["id"]
        a.client.ensure()
        broker_pid, server_pid = a.client.proc.info["pid"], a.client.proc.pid
        wait(lambda: a._running(tid) and a.live[tid].turn_id)
        # 앱이 내려간다 — 연결만 끊기고 app-server 와 턴은 남는다
        a.stop.set()
        a.client.close()
        time.sleep(0.3)
        b = self.lens()
        wait(lambda: b.client.proc and b.client.proc.alive(), 5)   # 소켓이 있으면 바로 다시 붙는다
        self.assertTrue(b.client.reattached)
        self.assertEqual((b.client.proc.info["pid"], b.client.proc.pid), (broker_pid, server_pid))
        self.assertIn(tid, b.client.loaded)
        # 끊긴 동안의 알림까지 다시 받아 진행 중인 턴을 그대로 보이고, 끝나면 끝난 것으로 받는다
        done = []
        b.turn_listeners.append(lambda *a_: done.append(a_))
        wait(lambda: done, 10)
        self.assertEqual(done[0][:3], (tid, out["turn_id"], "completed"))
        d = b.read_session(tid)
        last = d["turns"][-1]
        self.assertEqual(last["status"], "completed")
        self.assertEqual(last["request"]["text"], "천천히 살펴봐 줘")
        self.assertTrue(last["answer"])
        # 다시 붙은 쪽에서 다음 턴도 보낼 수 있다(thread/resume 없이)
        resumes = len(calls("thread/resume"))
        b.send_turn(tid, "다음 일")
        wait(lambda: b.live[tid].status == "completed" and b.live[tid].turn_id != out["turn_id"])
        self.assertEqual(len(calls("thread/resume")), resumes)

    def test_finished_while_down_is_delivered(self):
        a = self.lens()
        out = a.create_session(PROJ, "천천히 해 줘")
        wait(lambda: a._running(out["id"]) and a.live[out["id"]].turn_id)
        a.stop.set()
        a.client.close()
        time.sleep(2.5)   # 앱이 내려가 있는 동안 턴이 끝난다
        b = self.lens()
        done = []
        b.turn_listeners.append(lambda *a_: done.append(a_))
        wait(lambda: done, 10)
        self.assertEqual(done[0][:3], (out["id"], out["turn_id"], "completed"))

    def test_stale_broker_retires_only_when_idle(self):
        a = self.lens()
        a.client.ensure()
        old = a.client.proc.info["pid"]
        out = a.create_session(PROJ, "천천히 해 줘")
        wait(lambda: a._running(out["id"]) and a.live[out["id"]].turn_id)
        a.stop.set()
        a.client.close()
        # 실행 파일이 바뀌었어도(지문이 다름) 턴이 돌고 있으면 그대로 다시 붙는다
        other = [sys.executable, "-I", os.path.join(ROOT, "tests", "fake_app_server.py")]
        b = self.lens(other)
        b.client.ensure()
        self.assertEqual(b.client.proc.info["pid"], old)
        self.assertTrue(b.client.proc.info["stale"])
        wait(lambda: b.live.get(out["id"]) and b.live[out["id"]].status == "completed", 10)
        b.stop.set()
        b.client.close()
        # 일이 없으면 물러나고 새 실행 파일로 새 중계기가 뜬다
        c = self.lens(other)
        c.client.ensure()
        self.assertNotEqual(c.client.proc.info["pid"], old)
        self.assertFalse(c.client.reattached)

    def test_direct_mode_without_broker(self):
        os.environ["CODEX_LENS_BROKER"] = "0"
        try:
            ln = self.lens()
        finally:
            os.environ.pop("CODEX_LENS_BROKER")
        self.assertIsNone(ln.client.socket_path)
        self.assertTrue(ln.models())
        self.assertFalse(os.path.exists(os.path.join(self.home, "codex.sock")))


if __name__ == "__main__":
    unittest.main()


def claude_calls():
    out = []
    if os.path.exists(CLOG):
        with open(CLOG) as f:
            for line in f:
                out.append(json.loads(line))
    return out


def job_done(jid):
    return wait(lambda: (lambda j: j if j["status"] != "running" else None)(SRV.req("GET", "/api/jobs/" + jid)[1]["job"]))


class ClaudeTest(unittest.TestCase):
    """Claude Code 세션 — 가짜 claude CLI(tests/fake_claude.py)와 가짜 기록(seed.build_claude)."""

    def _done(self, sid, n):
        def f():
            d = SRV.req("GET", "/api/sessions/" + sid)[1]
            t = d.get("turns") or []
            return d if len(t) == n and t[-1]["status"] != "inProgress" and not t[-1].get("live", {}).get("status") in ("starting", "inProgress") else None
        return wait(f)

    def test_c1_list_merges_agents_by_update_time(self):
        st, r = SRV.req("GET", "/api/sessions?limit=40")
        self.assertEqual(st, 200)
        seeded = [s for s in r["sessions"] if s["id"] in (S1, S3, "01a1aaaa000000000000000000000002")]
        self.assertEqual([s["id"] for s in seeded], [S1, S3, "01a1aaaa000000000000000000000002"])
        s3 = seeded[1]
        self.assertEqual(s3["agent"], "claude")
        self.assertEqual(s3["title"], "README 할 일 체크 목록")      # ai-title
        self.assertEqual(s3["model"], "claude-fake-opus")
        self.assertEqual(seeded[0]["agent"], "codex")
        # 한 개씩 넘겨도 같은 순서로 빠짐·겹침 없이 이어진다
        ids, cur = [], None
        for _ in range(60):
            st, p = SRV.req("GET", "/api/sessions?limit=1" + ("&cursor=" + cur if cur else ""))
            ids += [s["id"] for s in p["sessions"]]
            cur = p["next_cursor"]
            if not cur:
                break
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([i for i in ids if i in (S1, S3, "01a1aaaa000000000000000000000002")], [S1, S3, "01a1aaaa000000000000000000000002"])
        # 검색도 Claude 기록을 찾는다
        st, q = SRV.req("GET", "/api/sessions?q=" + urllib.parse.quote("체크 목록"))
        self.assertEqual([s["id"] for s in q["sessions"]], [S3])

    def test_c1b_child_sessions_nest_under_parent(self):
        s2, s4 = "01a1aaaa000000000000000000000002", "01a1aaaa00000000000000000000000d"
        st, r = SRV.req("GET", "/api/sessions?limit=40")
        top = {s["id"]: s for s in r["sessions"]}
        self.assertNotIn(s4, top)                         # Codex 하위 에이전트는 맨 위에 따로 서지 않는다
        kids = top[S1].get("children") or []
        self.assertEqual([k["id"] for k in kids], [s4])
        self.assertEqual((kids[0]["parent"], kids[0]["agent_name"]), (S1, "Pascal"))
        st, d = SRV.req("GET", "/api/sessions/" + s4)
        self.assertEqual((d["session"]["parent"], d["session"]["parent_title"]), (S1, top[S1]["title"]))
        # 쪽을 나눠 읽어도 겹치지 않고, 부모를 앞 쪽에서 냈으면 자식만 parent 를 달고 나온다
        seen, cur = [], None
        for _ in range(60):
            st, p = SRV.req("GET", "/api/sessions?limit=1" + ("&cursor=" + cur if cur else ""))
            for s in p["sessions"]:
                seen.append(s["id"])
                seen += [k["id"] for k in s.get("children") or []]
                if s["id"] == s4:
                    self.assertEqual(s["parent"], S1)
            cur = p["next_cursor"]
            if not cur:
                break
        self.assertEqual(len(seen), len(set(seen)))
        self.assertIn(s4, seen)
        # S2(Codex) 의 명령이 도는 동안 생긴 S3(Claude) — 오래된 S2 가 S3 자리로 올라온다
        created = SRV.req("GET", "/api/sessions/" + S3)[1]["session"]["created_at"]
        SRV.lens.launches.store.update(lambda d: d["items"].update({S3.lower(): [[s2, created - 1, created + 1]]}))
        try:
            st, r = SRV.req("GET", "/api/sessions?limit=40")
            order = [s["id"] for s in r["sessions"] if s["id"] in (S1, S3, s2)]
            self.assertEqual(order, [S1, s2])
            self.assertEqual([k["id"] for k in r["sessions"][[s["id"] for s in r["sessions"]].index(s2)]["children"]], [S3])
            # 검색에 자식만 걸려도 부모를 끌어올려 그 아래에 보인다
            st, q = SRV.req("GET", "/api/sessions?q=" + urllib.parse.quote("체크 목록"))
            self.assertEqual([(s["id"], [k["id"] for k in s.get("children") or []]) for s in q["sessions"]], [(s2, [S3])])
        finally:
            SRV.lens.launches.store.update(lambda d: d["items"].clear())

    def test_c1d_archived_parent_is_not_pulled_up(self):
        s4 = "01a1aaaa00000000000000000000000d"
        self.assertEqual(SRV.req("POST", "/api/sessions/" + S1 + "/archive", {"archived": True})[0], 200)
        try:
            st, r = SRV.req("GET", "/api/sessions?limit=40")
            ids = [s["id"] for s in r["sessions"]]
            self.assertNotIn(S1, ids)                       # 보관한 부모는 하위 세션 자리로 끌어올리지 않는다
            self.assertIn(s4, ids)
            self.assertEqual(r["sessions"][ids.index(s4)]["parent"], S1)
            st, r = SRV.req("GET", "/api/sessions?limit=40&archived=1")
            self.assertEqual([(s["id"], s.get("children")) for s in r["sessions"]], [(S1, None)])   # 보관 안 한 자식은 보관 목록에 없다
            # 하위 세션 머리의 부모 링크는 보관한 부모도 가리킨다
            st, d = SRV.req("GET", "/api/sessions/" + s4)
            self.assertEqual(d["session"]["parent"], S1)
            self.assertTrue(d["session"].get("parent_title"))
        finally:
            SRV.req("POST", "/api/sessions/" + S1 + "/archive", {"archived": False})
        # 끌어올린 부모에도 목록 행 상태(state)가 붙는다
        st, r = SRV.req("GET", "/api/sessions?limit=1")
        self.assertIn("state", r["sessions"][0])

    def test_c1c_session_launched_by_a_command_nests_under_it(self):
        from lens import LiveTurn
        lt = LiveTurn(S1)
        cmd = {"type": "commandExecution", "id": "cmd-launch", "command": "codex exec '시험'", "aggregatedOutput": ""}
        SRV.lens._apply(lt, "item/started", {"item": cmd})
        kid = SRV.lens.client.request("thread/start", {"cwd": PROJ})["thread"]["id"]
        old = SRV.req("GET", "/api/sessions?limit=40")[1]
        try:
            SRV.lens._apply(lt, "item/completed", {"item": dict(cmd, aggregatedOutput='{"session_id": "%s"}' % kid)})
            st, r = SRV.req("GET", "/api/sessions?limit=40")
            top = {s["id"]: s for s in r["sessions"]}
            self.assertNotIn(kid, top)
            self.assertIn(kid, [k["id"] for k in top[S1].get("children") or []])
            self.assertIn(kid, [s["id"] for s in old["sessions"]])   # 명령이 끝나기 전에는 따로 섰다
        finally:
            SRV.lens.launches.store.update(lambda d: d["items"].clear())

    def test_c2_read_history_as_turns(self):
        st, d = SRV.req("GET", "/api/sessions/" + S3)
        self.assertEqual(st, 200, d)
        self.assertEqual(d["session"]["agent"], "claude")
        self.assertEqual(d["session"]["effort"], "high")
        t = d["turns"][0]
        self.assertEqual(t["id"], "c3-turn-1")
        self.assertEqual(t["status"], "completed")
        self.assertEqual(t["request"]["text"], "README 의 할 일을 체크 목록으로 바꿔 줘")
        self.assertEqual([s["kind"] for s in t["process"]], ["reasoning", "message", "tool", "file_change"])
        self.assertEqual(t["process"][2]["title"], "Read · README.md")
        self.assertIn("+- [ ] 곱셈", t["process"][3]["files"][0]["diff"])
        self.assertIn("체크 목록", t["answer"]["text"])

    def test_c3_models_listed_per_agent(self):
        st, r = SRV.req("GET", "/api/models")
        agents = {m["agent"] for m in r["models"]}
        self.assertEqual(agents, {"codex", "claude"})
        cl = [m for m in r["models"] if m["agent"] == "claude"]
        self.assertIn("claude-opus-5-5", [m["id"] for m in cl])
        self.assertEqual(cl[0]["efforts"], ["low", "medium", "high", "xhigh", "max"])

    def test_c0_image_inputs_become_claude_image_blocks(self):
        import claude_rpc
        b = claude_rpc._blocks([{"type": "text", "text": "이 그림"}, {"type": "image", "url": "data:image/png;base64,iVBORw0KGgo="},
                                {"type": "image", "url": "https://example.com/a.png"}])
        self.assertEqual(b[1], {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}})
        self.assertEqual(b[2]["source"], {"type": "url", "url": "https://example.com/a.png"})

    def test_c4_new_claude_session_then_resume(self):
        st, r = SRV.req("POST", "/api/sessions", {"cwd": PROJ, "text": "README 를 읽어 줘", "client_id": "cl-new-1",
                                                   "agent": "claude", "model": "claude-sonnet-5-5", "effort": "low"})
        self.assertEqual(st, 200, r)
        sid = r["id"]
        d = self._done(sid, 1)
        t = d["turns"][0]
        self.assertEqual(d["session"]["agent"], "claude")
        self.assertEqual(d["session"]["context"]["window"], 200000)
        self.assertEqual(d["session"]["context"]["tokens"], 292)
        self.assertTrue(d["session"]["mine"])
        self.assertEqual(t["status"], "completed")
        self.assertEqual(t["id"], r["turn_id"])                     # 실행 중 턴 ID = 기록의 요청 uuid
        self.assertIn("작업을 마쳤습니다", t["answer"]["text"])
        self.assertEqual([s["kind"] for s in t["process"]], ["reasoning", "command"])
        a = claude_calls()[-1]["argv"]
        self.assertIn("--session-id", a)
        self.assertEqual(a[a.index("--model") + 1], "claude-sonnet-5-5")
        self.assertEqual(a[a.index("--effort") + 1], "low")
        self.assertEqual(a[a.index("--permission-mode") + 1], "bypassPermissions")
        # 포트 관리: 이 앱이 만든 Claude 세션에는 지침을 시스템 지침으로 덧붙이고, 명령이 세션을 알게 CODEX_THREAD_ID 를 준다
        hint = a[a.index("--append-system-prompt") + 1]
        self.assertIn("lens-port run", hint)
        self.assertEqual(claude_calls()[-1]["thread_env"], sid)
        st, r2 = SRV.req("POST", "/api/sessions/%s/turns" % sid, {"text": "고마워", "client_id": "cl-t2"})
        self.assertEqual(st, 200, r2)
        d = self._done(sid, 2)
        self.assertEqual(d["turns"][1]["request"]["text"], "고마워")
        self.assertEqual(d["turns"][1]["status"], "completed")
        a = claude_calls()[-1]["argv"]
        self.assertEqual(a[a.index("--resume") + 1], sid)            # 다음 턴은 이어받는다
        self.assertEqual(a[a.index("--model") + 1], "claude-sonnet-5-5")   # 고르지 않아도 세션 모델을 쓴다
        self.assertEqual(a[a.index("--append-system-prompt") + 1], hint)     # 턴마다 같은 지침
        # 포크(과정 요약)도 원본과 같은 지침으로 돌아야 캐시를 읽는다
        st, r3 = SRV.req("POST", "/api/sessions/%s/summaries" % sid, {"turn_id": d["turns"][1]["id"], "request": "요약", "client_id": "cl-sum-mine"})
        self.assertEqual(st, 200, r3)
        self.assertEqual(job_done("cl-sum-mine")["status"], "done")
        a = claude_calls()[-1]["argv"]
        self.assertNotEqual(a[a.index("--resume") + 1], sid)
        self.assertEqual(a[a.index("--append-system-prompt") + 1], hint)

    def test_c4b_image_turn_is_sent_and_read_back(self):
        png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
        st, r = SRV.req("POST", "/api/sessions", {"cwd": PROJ, "text": "이 그림 봐 줘", "client_id": "cl-img-1", "agent": "claude",
                                                   "images": [{"url": png}]})
        self.assertEqual(st, 200, r)
        d = self._done(r["id"], 1)
        req = d["turns"][0]["request"]
        self.assertEqual(req["text"], "이 그림 봐 줘")
        self.assertEqual(req["images"], [{"url": png}])          # 기록의 base64 이미지 블록을 data URL 로 되돌려 보인다

    def test_c5_steer_and_interrupt(self):
        st, r = SRV.req("POST", "/api/sessions", {"cwd": PROJ, "text": "천천히 살펴봐 줘", "client_id": "cl-slow-1", "agent": "claude"})
        sid = r["id"]
        wait(lambda: SRV.lens.live.get(sid) and SRV.lens.live[sid].status == "inProgress" and SRV.lens.live[sid].items.get(r["turn_id"]))
        time.sleep(0.2)
        st, s2 = SRV.req("POST", "/api/sessions/%s/turns" % sid, {"text": "README 도 같이 봐", "client_id": "cl-steer-2", "mode": "steer"})
        self.assertEqual(st, 200, s2)
        self.assertTrue(s2.get("steered"), s2)
        d = self._done(sid, 1)
        t = d["turns"][0]
        self.assertEqual(t["request"]["text"], "천천히 살펴봐 줘")
        self.assertEqual([x["text"] for x in t["request"]["steers"]], ["README 도 같이 봐"])
        self.assertIn("끼워 넣은 말도 읽었습니다", t["answer"]["text"])
        # 멈춤
        st, r3 = SRV.req("POST", "/api/sessions/%s/turns" % sid, {"text": "천천히 다시", "client_id": "cl-slow-2"})
        wait(lambda: SRV.lens.live.get(sid) and SRV.lens.live[sid].turn_id == r3["turn_id"] and len(SRV.lens.live[sid].items) > 2)
        st, x = SRV.req("POST", "/api/sessions/%s/interrupt" % sid, {})
        self.assertEqual(st, 200, x)
        d = self._done(sid, 2)
        self.assertEqual(d["turns"][1]["status"], "interrupted")

    def test_c6_summary_forks_with_cache_and_cleans_up(self):
        st, r = SRV.req("POST", "/api/sessions/%s/summaries" % S3, {"turn_id": "c3-turn-1", "request": "체크 목록", "client_id": "cl-sum1"})
        self.assertEqual(st, 200, r)
        job = job_done("cl-sum1")
        self.assertEqual(job["status"], "done", job)
        self.assertIn("한 줄 요약", job["answer"])
        self.assertEqual(job["model"], "claude-fake-opus")
        self.assertEqual(job["usage"]["cached"], 1000)                # 같은 모델·같은 앞부분 → 캐시 읽음
        a = claude_calls()[-1]["argv"]
        fork = a[a.index("--resume") + 1]
        self.assertNotEqual(fork, S3)
        self.assertEqual(a[a.index("--permission-mode") + 1], "dontAsk")   # 읽기 전용
        self.assertNotIn("--append-system-prompt", a)                      # 밖에서 만든 세션에는 지침을 넣지 않는다
        self.assertEqual(job["fork_thread"], fork)
        import glob
        self.assertEqual(glob.glob(os.path.join(TMP, "claude-home", "projects", "*", fork + ".jsonl")), [])   # 일회용은 지운다
        # 원본 기록은 그대로
        self.assertEqual(len(SRV.req("GET", "/api/sessions/" + S3)[1]["turns"]), 1)

    def test_c7_question_keeps_fork_hidden(self):
        st, r = SRV.req("POST", "/api/sessions/%s/questions" % S3, {"turn_id": "c3-turn-1", "quote": "체크 목록", "start": 5,
                                                                    "part": "answer", "question": "왜?", "client_id": "cl-q1"})
        self.assertEqual(st, 200, r)
        j = job_done("cl-q1")
        self.assertEqual(j["status"], "done", j)
        self.assertIn("질문: 왜?", j["answer"])
        fork = j["fork_thread"]
        self.assertIn(fork, SRV.lens.registry.read()["forks"])
        ids = [s["id"] for s in SRV.req("GET", "/api/sessions?limit=100")[1]["sessions"]]
        self.assertNotIn(fork, ids)
        # 이어 묻기는 같은 포크를 다시 쓴다
        st, r = SRV.req("POST", "/api/sessions/%s/questions" % S3, {"turn_id": "c3-turn-1", "quote": "체크", "parent_id": "cl-q1",
                                                                    "part": "q:cl-q1", "question": "더 쉽게", "client_id": "cl-q2"})
        j2 = job_done("cl-q2")
        self.assertEqual(j2["status"], "done", j2)
        self.assertEqual(j2["fork_thread"], fork)

    def test_c8_refine_new_claude_session_is_ephemeral(self):
        st, r = SRV.req("POST", "/api/refine", {"text": "README 고쳐", "cwd": PROJ, "agent": "claude", "client_id": "cl-rf1"})
        self.assertEqual(st, 200, r)
        j = job_done("cl-rf1")
        self.assertEqual(j["status"], "done", j)
        self.assertTrue(j["result"]["request"].startswith("목표: README 고쳐"))
        a = claude_calls()[-1]["argv"]
        self.assertIn("--no-session-persistence", a)
        self.assertEqual(a[a.index("--permission-mode") + 1], "dontAsk")



class HubSteerTest(unittest.TestCase):
    """agent-hub 가 돌리는 세션: 응답 중 바로 넣기·턴 끝나고·멈춤을 agent-hub 로 넘기고, 이 앱이 따로 턴을 열지 않는다."""
    SID = "c1a0de00-0000-4000-8000-000000000003"   # 시드의 Claude Code 기록

    def setUp(self):
        from hub_link import HubLink
        from fake_hub import FakeHub
        self.hub = FakeHub([{"id": "chat_old", "status": "idle", "session_id": self.SID, "pending": 0, "updated_at": 1},
                            {"id": "chat_run", "status": "running", "session_id": self.SID.upper(), "pending": 2, "updated_at": 2,
                             "profile": "claude"}]).start()
        self.saved = SRV.app.hub
        SRV.app.hub = HubLink(self.hub.url)

    def tearDown(self):
        SRV.app.hub = self.saved
        self.hub.httpd.shutdown()

    def test_read_marks_hub_running(self):
        st, d = SRV.req("GET", "/api/sessions/%s" % self.SID)
        self.assertEqual(st, 200)
        self.assertEqual((d["hub"]["chat_id"], d["hub"]["running"], d["hub"]["pending"]), ("chat_run", True, 2))

    def test_send_goes_to_hub_not_local(self):
        before = len(claude_calls())
        body = {"text": "README 도 봐", "client_id": "hub-c1", "mode": "steer",
                "mentions": [{"kind": "file", "path": "/tmp/x/README.md", "label": "@README.md"}],
                "images": [{"url": "data:image/png;base64," + __import__("base64").b64encode(seed.image_fixture()).decode()}]}
        st, r = SRV.req("POST", "/api/sessions/%s/turns" % self.SID, body)
        self.assertEqual(st, 200, r)
        self.assertEqual((r["hub"], r["steered"], r["queued"]), ("chat_run", True, False))
        path, sent = self.hub.calls[-1]
        self.assertEqual(path, "/api/chats/chat_run/send")
        self.assertEqual((sent["mode"], sent["expected_session_id"]), ("steer", self.SID.upper()))
        self.assertTrue(sent["text"].startswith("README 도 봐"))
        self.assertIn("/tmp/x/README.md", sent["text"])          # 멘션 안내도 이 앱이 턴을 열 때처럼 붙는다
        self.assertEqual(sent["images"][0]["content_type"], "image/png")
        self.assertFalse(SRV.lens._running(self.SID))
        self.assertEqual(len(claude_calls()), before)              # 이 앱은 claude 를 띄우지 않았다
        # 같은 client_id 로 다시 보내도 한 번만 간다
        n = len(self.hub.calls)
        st, r2 = SRV.req("POST", "/api/sessions/%s/turns" % self.SID, body)
        self.assertEqual((st, r2, len(self.hub.calls)), (200, r, n))

    def test_queue_mode_and_no_mode_default_to_queue(self):
        for i, mode in enumerate(("queue", None)):
            b = {"text": "끝나면 테스트도", "client_id": "hub-q%d" % i}
            if mode:
                b["mode"] = mode
            st, r = SRV.req("POST", "/api/sessions/%s/turns" % self.SID, b)
            self.assertEqual((st, r["queued"], self.hub.calls[-1][1]["mode"]), (200, True, "queue"))

    def test_interrupt_goes_to_hub(self):
        st, r = SRV.req("POST", "/api/sessions/%s/interrupt" % self.SID, {})
        self.assertEqual((st, r.get("hub")), (200, "chat_run"))
        self.assertEqual(self.hub.calls[-1], ("/api/chats/chat_run/kill", {"expected_session_id": self.SID.upper()}))

    def test_idle_hub_chat_or_hub_down_leaves_session_alone(self):
        from hub_link import HubLink
        self.hub.chats = [dict(self.hub.chats[1], status="idle")]
        st, d = SRV.req("GET", "/api/sessions/%s" % self.SID)
        self.assertEqual((st, d["hub"]["running"]), (200, False))
        SRV.app.hub = HubLink("http://127.0.0.1:9")            # 꺼진 허브 — 읽기는 그대로 된다
        st, d = SRV.req("GET", "/api/sessions/%s" % self.SID)
        self.assertEqual((st, d["hub"]), (200, None))
        st, r = SRV.req("POST", "/api/sessions/%s/interrupt" % self.SID, {})
        self.assertEqual(st, 400)                                  # 이 앱이 돌리는 턴도 없다


class ExternalQueueTest(unittest.TestCase):
    """다른 곳(이 앱 밖)에서 도는 턴: 응답 중과 같은 두 선택지(바로 넣기·턴 끝나고)를 받고 둘 다 대기열에 둔다.
    그 턴이 끝나면 서버가 화면 없이도 대기열을 한 번씩만 보낸다. 멈춤·실패·끊김 뒤에는 보내지 않는다."""

    @staticmethod
    def X(n):
        return "01a1aaaa00000000000000000000xq%s" % "abc"[n - 1]

    def tearDown(self):
        if os.path.exists(EXT_END):
            os.unlink(EXT_END)
        for n in (1, 2, 3):                       # 시험끼리 대기열·감시를 물려주지 않는다
            SRV.lens.queues.update(lambda d, n=n: d["threads"].pop(self.X(n), None))
            SRV.lens.ext_wait.pop(self.X(n), None)

    def starts(self, sid):
        return [c["params"] for c in calls("turn/start") if c["params"]["threadId"] == sid]

    def send(self, sid, text, mode):
        return SRV.req("POST", "/api/sessions/%s/turns" % sid, {"text": text, "mode": mode, "client_id": "xq-" + text})

    def external_running(self, sid):
        st, d = SRV.req("GET", "/api/sessions/" + sid)
        self.assertEqual(st, 200)
        self.assertTrue(d["turns"][-1].get("external"), d["turns"][-1]["status"])

    def test_queue_and_steer_both_wait_and_send_once_after_the_external_turn_ends(self):
        sid = self.X(1)
        self.external_running(sid)
        n0 = len(calls("turn/steer"))
        st, r = self.send(sid, "먼저 보낼 말", "queue")
        self.assertEqual((st, r.get("queued")), (200, True), r)
        st, r = self.send(sid, "끼워 넣고 싶은 말", "steer")      # 다른 프로세스의 턴에는 끼워 넣을 수 없다 — 대기열로 간다
        self.assertEqual((st, r.get("queued")), (200, True), r)
        self.assertEqual(len(calls("turn/steer")), n0)
        q = SRV.req("GET", "/api/sessions/" + sid)[1]["queue"]
        self.assertEqual([e["text"] for e in q], ["먼저 보낼 말", "끼워 넣고 싶은 말"])
        # 같은 요청 ID 로 다시 와도 한 번만 쌓인다
        st, r2 = self.send(sid, "먼저 보낼 말", "queue")
        self.assertEqual(len(SRV.req("GET", "/api/sessions/" + sid)[1]["queue"]), 2, r2)
        # 화면의 "넣기" 도 다른 곳의 턴에는 넣지 못하니 대기열에 그대로 둔다
        st, r = SRV.req("POST", "/api/sessions/%s/queue/%s/send" % (sid, q[1]["id"]))
        self.assertEqual((st, r.get("queued")), (200, True), r)
        # 그 턴이 도는 동안에는 보내지 않는다(서버가 여러 번 확인하는 시간)
        time.sleep(1.0)
        self.assertEqual(self.starts(sid), [])
        self.assertEqual(len(SRV.req("GET", "/api/sessions/" + sid)[1]["queue"]), 2)
        # 끝나면 화면을 열지 않아도 차례로 한 번씩 간다
        with open(EXT_END, "w") as f:
            f.write(sid)
        wait(lambda: len(self.starts(sid)) == 2, 15)
        texts = [p["input"][0]["text"] for p in self.starts(sid)]
        self.assertEqual(texts, ["먼저 보낼 말", "끼워 넣고 싶은 말"])
        wait(lambda: SRV.req("GET", "/api/sessions/" + sid)[1]["queue"] == [])
        time.sleep(1.0)
        self.assertEqual(len(self.starts(sid)), 2)      # 더 보내지 않는다
        self.assertFalse(SRV.lens.ext_wait.get(sid))
        import applog
        applog.flush()
        with open(os.path.join(os.environ["CODEX_LENS_HOME"], "logs", "app.jsonl")) as f:
            events = [json.loads(x) for x in f]
        mine = [x["event"] for x in events if x.get("thread") == sid]
        for ev in ("turn_queued", "external_queue_watch", "external_queue_send", "turn_dequeued"):
            self.assertIn(ev, mine)

    def test_stopped_or_failed_external_turn_keeps_the_queue(self):
        for n, state in ((2, "interrupted"), (3, "failed")):
            sid = self.X(n)
            st, r = self.send(sid, "멈춘 뒤에는 보내지 않는다 %d" % n, "queue")
            self.assertEqual((st, r.get("queued")), (200, True), r)
            with open(EXT_END, "w") as f:
                f.write("%s:%s" % (sid, state))
            wait(lambda: sid not in SRV.lens.ext_wait, 15)       # 감시를 마치고 대기열에 둔다
            time.sleep(0.6)
            self.assertEqual(self.starts(sid), [], state)
            q = SRV.req("GET", "/api/sessions/" + sid)[1]["queue"]
            self.assertEqual(len(q), 1, state)
            # 사용자가 "지금 보내기" 를 누르면 그때 한 번 간다
            st, r = SRV.req("POST", "/api/sessions/%s/queue/%s/send" % (sid, q[0]["id"]))
            self.assertEqual(st, 200, r)
            self.assertTrue(r.get("turn_id"), r)
            self.assertEqual(len(self.starts(sid)), 1, state)
            self.assertEqual(SRV.req("GET", "/api/sessions/" + sid)[1]["queue"], [])
            os.unlink(EXT_END)

    def test_new_external_turn_before_send_keeps_the_message_once(self):
        """끝난 것으로 보이다가 다시 도는 턴 — 도는 동안에는 보내지 않고, 끝난 뒤 한 번만 보낸다."""
        sid = self.X(3)
        n0 = len(self.starts(sid))
        st, r = self.send(sid, "다시 도는 동안 기다릴 말", "queue")
        self.assertEqual(st, 200, r)
        # 끝남 → 다시 도는 중(파일을 지움)을 번갈아 보이면 연속 확인이 끊겨 보내지 않는다
        for _ in range(4):                          # 끝남으로 보이는 시간(0.08초)이 확인 간격(0.15초)보다 짧아 한 번만 걸린다
            with open(EXT_END, "w") as f:
                f.write(sid)
            time.sleep(0.08)
            os.unlink(EXT_END)
            time.sleep(0.4)
        self.assertEqual(len(self.starts(sid)), n0)
        self.assertEqual(len(SRV.req("GET", "/api/sessions/" + sid)[1]["queue"]), 1)

    def test_concurrent_drain_runs_once(self):
        lens = SRV.lens
        lens.draining.add("x-guard")
        try:
            lens.queues.update(lambda d: d["threads"].setdefault("x-guard", []).append({"id": "q1", "text": "t", "client_id": "c"}))
            lens._drain("x-guard")                  # 이미 잇는 중이면 건드리지 않는다
            self.assertEqual(len(lens.queue_of("x-guard")), 1)
        finally:
            lens.draining.discard("x-guard")
            lens.queues.update(lambda d: d["threads"].pop("x-guard", None))


class ClaudeHistoryEndTest(unittest.TestCase):
    def test_structured_output_result_ends_turn_without_stop_record(self):
        """agent-hub 의 stream-json 채팅은 stop 기록을 남기지 않는다 — StructuredOutput 결과에서 턴이 끝난 것으로 본다."""
        import claude_history
        ts = "2026-11-05T05:00:00.000Z"
        recs = [
            {"type": "user", "uuid": "u1", "timestamp": ts, "message": {"role": "user", "content": "확인해 줘"}},
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "timestamp": ts, "message": {"id": "m1", "role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}},
            {"type": "user", "uuid": "r1", "parentUuid": "a1", "timestamp": ts, "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "README.md"}]}},
            {"type": "assistant", "uuid": "a2", "parentUuid": "r1", "timestamp": ts, "message": {"id": "m2", "role": "assistant", "content": [
                {"type": "tool_use", "id": "t2", "name": "StructuredOutput", "input": {"protocol": "agent-output/1", "body": "다 봤습니다"}}]}},
            {"type": "user", "uuid": "r2", "parentUuid": "a2", "timestamp": ts, "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t2", "content": "Structured output provided successfully"}]}},
        ]
        turns, _ = claude_history.build_turns(recs, mtime=time.time())
        self.assertEqual(turns[-1]["status"], "completed")
        # 일반 도구 결과로 끝난 기록은 여전히 다른 곳에서 진행 중으로 본다
        turns, _ = claude_history.build_turns(recs[:3], mtime=time.time())
        self.assertEqual(turns[-1]["status"], "inProgress")
