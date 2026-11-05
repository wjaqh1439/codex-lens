import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from turns import normalize_turn, subagent_title  # noqa: E402

CHILD = "01a1aa76-179b-7892-b616-2ddd8fc5016d"


def turn(items, status="completed"):
    return normalize_turn({"id": "t1", "status": status, "items": items})


class SubAgentSteps(unittest.TestCase):
    def test_started_names_agent_and_model(self):
        st = turn([{"type": "subAgentActivity", "id": "a", "kind": "started", "agentThreadId": CHILD,
                    "agentPath": "/root/integrate_archive", "model": "gpt-6.1-sol", "reasoningEffort": "high"}])["process"][0]
        self.assertEqual(st["kind"], "agent")
        self.assertEqual(st["title"], "시작 · integrate_archive")
        self.assertEqual(st["agents"], [{"thread": CHILD, "path": "/root/integrate_archive", "name": "integrate_archive"}])
        self.assertEqual((st["model"], st["effort"]), ("gpt-6.1-sol", "high"))

    def test_wait_without_targets_lists_turn_agents(self):
        # Codex 의 wait 항목은 대상·상태를 비워 둔다(0.165 실측) — 이 턴의 하위 에이전트로 채운다
        p = turn([
            {"type": "subAgentActivity", "id": "a", "kind": "started", "agentThreadId": CHILD, "agentPath": "/root/integrate_archive"},
            {"type": "collabAgentToolCall", "id": "w", "tool": "wait", "status": "inProgress", "senderThreadId": "P",
             "receiverThreadIds": [], "prompt": None, "model": None, "reasoningEffort": None, "agentsStates": {}},
        ], status="inProgress")["process"]
        w = p[1]
        self.assertEqual(w["title"], "기다림 · integrate_archive")
        self.assertEqual(w["agents"][0]["thread"], CHILD)

    def test_wait_reports_states(self):
        w = turn([{"type": "collabAgentToolCall", "id": "w", "tool": "wait", "status": "completed",
                   "receiverThreadIds": [CHILD], "agentsStates": {CHILD: {"status": "completed", "message": "다 했습니다"}}}])["process"][0]
        self.assertEqual(w["title"], "기다림 · 01a1aa76")
        self.assertEqual((w["agents"][0]["status"], w["agents"][0]["message"]), ("completed", "다 했습니다"))

    def test_wait_with_no_known_agent(self):
        w = turn([{"type": "collabAgentToolCall", "id": "w", "tool": "wait", "status": "completed",
                   "receiverThreadIds": [], "agentsStates": {}}])["process"][0]
        self.assertEqual((w["title"], w["agents"]), ("기다림 · 전체", []))

    def test_spawn_prompt_is_detail(self):
        s = turn([{"type": "collabAgentToolCall", "id": "s", "tool": "spawnAgent", "status": "completed",
                   "receiverThreadIds": [CHILD], "prompt": "테스트 돌려", "agentsStates": {}}])["process"][0]
        self.assertEqual((s["title"], s["detail"]), ("만들기 · 01a1aa76", "테스트 돌려"))

    def test_subagent_title(self):
        th = {"source": {"subAgent": {"thread_spawn": {"parent_thread_id": "P", "agent_path": "/root/integrate_archive",
                                                        "agent_nickname": "Lorentz"}}}, "agentNickname": "Lorentz"}
        self.assertEqual(subagent_title(th), "하위 에이전트 · integrate_archive (Lorentz)")
        self.assertEqual(subagent_title({"source": "vscode"}), "")
        self.assertEqual(subagent_title({"source": {"subAgent": "review"}}), "")


if __name__ == "__main__":
    unittest.main()
