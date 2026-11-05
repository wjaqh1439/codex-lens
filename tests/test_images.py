"""이미지 입력은 실제 JSON-RPC 경계와 대기열 저장을 통해 검증한다."""
import tempfile
import unittest
from lens import Lens
from unittest.mock import Mock

PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='

class ImagesTest(unittest.TestCase):
    def test_image_only_turn_steer_and_queue(self):
        with tempfile.TemporaryDirectory() as home:
            lens = Lens(home, 'test', codex_argv=['unused'])
            lens.client = Mock()
            lens.client.request.return_value = {'turn': {'id': 'turn'}}
            try:
                images = [{'url': PNG}]
                lens.send_turn('sid', '', images=images)
                method, params, _ = lens.client.request.call_args.args
                self.assertEqual(method, 'turn/start')
                self.assertEqual(params['input'][-1], {'type': 'image', 'url': PNG})
                lens.send_turn('sid', '추가', mode='steer', images=images)
                self.assertEqual(lens.client.request.call_args.args[0], 'turn/steer')
                self.assertEqual(lens.client.request.call_args.args[1]['input'][-1]['url'], PNG)
                lens.send_turn('sid', '', mode='queue', images=images)
                self.assertEqual(lens.queue_of('sid')[0]['images'], images)
                lens.live.clear()
                lens._drain('sid')
                self.assertEqual(lens.client.request.call_args.args[1]['input'][-1]['url'], PNG)
            finally:
                lens.stop.set()

    def test_invalid_images(self):
        for value in ([{'url': 'https://example.org/image.png'}], [{'url': 'data:image/png;base64,!!!'}],
                      [{'url': PNG}] * 9, 'wrong'):
            with self.subTest(value=str(value)[:60]), self.assertRaises(ValueError):
                Lens.validate_images(value)
