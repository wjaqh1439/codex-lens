"""기기별 설정 파일(local_env.py). 실행: python3 -m unittest discover -s tests -v"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import local_env  # noqa: E402


class LocalEnvTest(unittest.TestCase):
    def test_fills_missing_keys_only(self):
        home = tempfile.mkdtemp(prefix="cl-env-")
        with open(os.path.join(home, "env"), "w") as f:
            f.write("# 의견 위젯\nCODEX_LENS_FEEDBACK_ORIGIN='https://fb.example:8456'\n\nexport CODEX_LENS_PORT=9000\n"
                    "CODEX_LENS_HOST=0.0.0.0\n깨진 줄\n")
        env = {"CODEX_LENS_HOST": "127.0.0.1"}
        added = local_env.load(home, env)
        self.assertEqual(env, {"CODEX_LENS_HOST": "127.0.0.1", "CODEX_LENS_FEEDBACK_ORIGIN": "https://fb.example:8456",
                               "CODEX_LENS_PORT": "9000"})   # 이미 있는 값이 이긴다
        self.assertEqual(sorted(added), ["CODEX_LENS_FEEDBACK_ORIGIN", "CODEX_LENS_PORT"])

    def test_missing_file(self):
        env = {}
        self.assertEqual(local_env.load(tempfile.mkdtemp(prefix="cl-env-"), env), [])
        self.assertEqual(env, {})


if __name__ == "__main__":
    unittest.main()
