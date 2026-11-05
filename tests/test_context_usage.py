"""호출 단위 컨텍스트·세션 격리·재기동 복원. 실제 모델을 실행하지 않는다."""
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from context_usage import context_usage
from lens import Lens
from store import JsonStore


class ContextTest(unittest.TestCase):
    def test_last_call_not_cumulative_or_cached_again(self):
        self.assertEqual(context_usage({"last": {"inputTokens": 100000, "cachedInputTokens": 80000,
                                                  "outputTokens": 8528, "reasoningOutputTokens": 1000},
                                        "total": {"totalTokens": 999999}, "modelContextWindow": 258400}),
                         {"tokens": 108528, "window": 258400, "percent": 42.0})

    def test_zero_unknown_capacity_and_invalid_samples(self):
        self.assertEqual(context_usage({"last": {"totalTokens": 0}, "modelContextWindow": 200000})["percent"], 0)
        self.assertIsNone(context_usage({"last": {"totalTokens": 100}})["percent"])
        for val in (-1, True, float('inf'), float('nan'), '100'):
            self.assertIsNone(context_usage({"last": {"totalTokens": val}}))
        self.assertIsNone(context_usage({"total": {"totalTokens": 100}}))

    def test_context_without_live_turn_is_persisted_and_forks_are_excluded(self):
        with tempfile.TemporaryDirectory() as home:
            lens = Lens.__new__(Lens)
            lens.lock = threading.RLock()
            lens.jobs = Mock()
            lens.jobs.owns.side_effect = lambda tid: tid == 'fork'
            lens.client = Mock()
            lens.client.agent_of.return_value = 'codex'
            lens.contexts = JsonStore(os.path.join(home, 'context.json'), {"threads": {}})
            lens.meta_cache = {'one': {}}
            lens.live = {}
            lens.bus = Mock()
            p = {"threadId": 'one', "tokenUsage": {"last": {"totalTokens": 84000}, "modelContextWindow": 200000}}
            lens._on_note('thread/tokenUsage/updated', p)
            self.assertEqual(lens.meta_cache['one']['context']['percent'], 42)
            lens.bus.publish.assert_called_once_with('one', {"type": 'context', "context": lens.meta_cache['one']['context']})
            lens._on_note('thread/tokenUsage/updated', dict(p, threadId='fork'))
            lens._on_note('thread/tokenUsage/updated', p)  # 같은 값은 다시 보내지 않는다.
            self.assertEqual(lens.bus.publish.call_count, 1)
            restored = JsonStore(lens.contexts.path, {"threads": {}}).read()['threads']
            self.assertEqual(set(restored), {'one'})
            self.assertEqual(restored['one']['tokens'], 84000)
            lens.client.agent_of.return_value = 'claude'
            lens._on_note('thread/tokenUsage/updated', dict(p, threadId='claude'))
            self.assertNotIn('claude', lens.contexts.read()['threads'])
            lens._on_note('thread/context/updated', dict(p, threadId='claude'))
            self.assertEqual(lens.contexts.read()['threads']['claude']['percent'], 42)
