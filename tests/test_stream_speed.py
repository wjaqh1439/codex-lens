"""텍스트 수신 속도: 도구·완성본·이전 턴·대기를 섞지 않는다."""
import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from stream_speed import StreamSpeed
from lens import Lens, LiveTurn


class SpeedTest(unittest.TestCase):
    def test_rate_and_expiry(self):
        s = StreamSpeed()
        for t in (10, 10.25, 10.5):
            s.add('text', 'a' * 40, t)
        self.assertEqual(s.snapshot(10.5)['tps'], 40)
        self.assertTrue(s.snapshot(11)['estimated'])
        self.assertIsNone(s.snapshot(12.5))
        s.add('text', 'a' * 40, 13)
        self.assertIsNone(s.snapshot(13))

    def test_unicode_bytes_and_single_burst(self):
        s = StreamSpeed()
        for _ in range(100):
            s.add('text', 'old replay' * 100, 10)
        self.assertIsNone(s.snapshot(10))
        for t in (10.25, 10.5):
            s.add('text', '한글', t)  # 6바이트씩, 0.5초 동안 12바이트
        self.assertEqual(s.snapshot(10.5)['tps'], 6)

    def test_window_and_item_isolation(self):
        s = StreamSpeed()
        for i in range(1000):
            s.add('reasoning', 'abcd', 10 + i / 4)
        self.assertLessEqual(len(s.samples), 14)
        self.assertEqual(s.snapshot(10 + 999 / 4)['tps'], 4)
        s.add('answer', 'a' * 4000, 260)
        self.assertIsNone(s.snapshot(260))
        s.clear()
        self.assertIsNone(s.snapshot(260))

    def test_events_and_terminal_states(self):
        lens = Lens.__new__(Lens)
        lens.bus = Mock()
        lens.turn_listeners = []
        lens.queue_of = lambda tid: []
        turn = LiveTurn('thread')
        def emit(method, t, **params):
            with patch('lens._now', return_value=t):
                lens._apply(turn, method, params)
        emit('turn/started', 10, turn={'id': 'turn'})
        for t in (10, 10.25, 10.5):
            emit('item/reasoning/summaryTextDelta', t, itemId='r', delta='abcd')
        with patch('lens._now', return_value=10.5):
            self.assertGreater(turn.snapshot()['live']['speed']['tps'], 0)
        emit('item/started', 10.6, item={'id': 'cmd', 'type': 'commandExecution'})
        emit('item/commandExecution/outputDelta', 10.7, itemId='cmd', delta='x' * 10000)
        self.assertIsNone(turn.speed.snapshot(10.7))
        for t in (11, 11.25, 11.5):
            emit('item/agentMessage/delta', t, itemId='a', delta='abcd')
        emit('item/completed', 11.6, item={'id': 'a', 'type': 'agentMessage', 'text': 'complete'})
        self.assertIsNone(turn.speed.snapshot(11.6))
        for status in ('completed', 'interrupted', 'failed'):
            for t in (12, 12.25, 12.5):
                emit('item/agentMessage/delta', t, itemId='a', delta='abcd')
            emit('turn/completed', 12.6, turn={'status': status})
            self.assertNotIn('speed', turn.snapshot()['live'])
        emit('turn/started', 15, turn={'id': 'next'})
        self.assertIsNone(turn.speed.snapshot(15))


if __name__ == '__main__':
    unittest.main()
