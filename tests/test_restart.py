"""scripts/restart.py — 앱만 다시 띄우고 진행 중인 세션은 이어지는지(가짜 app-server)."""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import seed  # noqa: E402

SCRIPT = os.path.join(ROOT, "scripts", "restart.py")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait(fn, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    raise AssertionError("시간 안에 조건이 맞지 않았다")


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(st) and not st.startswith("Z")


class RestartScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cl-restart-")
        seed_path, self.proj = seed.build(self.tmp)
        self.home = os.path.join(self.tmp, "home")
        self.port = free_port()
        env = {k: v for k, v in os.environ.items() if not k.startswith("CODEX_LENS")}
        env.update(CODEX_LENS_HOME=self.home, CODEX_LENS_PORT=str(self.port), CODEX_LENS_FEEDBACK_ORIGIN="",
                   CODEX_HOME=os.path.join(self.tmp, "codex-home"), FAKE_CODEX_SEED=seed_path,
                   FAKE_CODEX_DELAY="0.02", FAKE_CODEX_SLOW_SEC="3",
                   CODEX_LENS_CODEX_ARGV=json.dumps([sys.executable, os.path.join(ROOT, "tests", "fake_app_server.py")]))
        self.env = env

    def tearDown(self):
        self.run_script("stop")
        for pid in self.broker_pids():
            if alive(pid):
                os.kill(pid, signal.SIGTERM)

    def run_script(self, *args):
        return subprocess.run([sys.executable, SCRIPT] + list(args), env=self.env, capture_output=True, text=True, timeout=60)

    def api(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method,
                                   headers={"Content-Type": "application/json"} if data else {})
        with urllib.request.urlopen(r, timeout=20) as resp:
            return json.loads(resp.read() or b"{}")

    def app_pid(self):
        with open(os.path.join(self.home, "server.json")) as f:
            return json.load(f)["pid"]

    def broker_pids(self):
        try:
            with open(os.path.join(self.home, "logs", "broker.jsonl")) as f:
                return sorted({json.loads(l)["pid"] for l in f if '"broker_start"' in l})
        except OSError:
            return []

    def test_restart_keeps_running_turn(self):
        r = self.run_script("restart")          # 안 떠 있으면 띄우기만
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        first = self.app_pid()
        out = self.api("POST", "/api/sessions", {"cwd": self.proj, "text": "천천히 살펴봐 줘"})
        sid = out["id"]
        wait(lambda: self.api("GET", "/api/sessions/" + sid)["turns"][-1]["status"] == "inProgress")
        r = self.run_script("restart")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("진행 중이던 세션은 이어집니다", r.stdout)
        self.assertFalse(alive(first))
        self.assertNotEqual(self.app_pid(), first)
        turn = wait(lambda: (lambda t: t if t["status"] == "completed" else None)(
            self.api("GET", "/api/sessions/" + sid)["turns"][-1]))
        self.assertEqual(turn["request"]["text"], "천천히 살펴봐 줘")
        self.assertEqual(len(self.broker_pids()), 1)          # 같은 중계기에 다시 붙었다
        r = self.run_script("status")
        self.assertEqual(r.returncode, 0)
        self.assertIn("소켓 있음", r.stdout)
        # 끄기는 앱만 — 중계기는 남는다
        app = self.app_pid()
        self.assertEqual(self.run_script("stop").returncode, 0)
        self.assertFalse(alive(app))
        self.assertTrue(alive(self.broker_pids()[0]))
        self.assertEqual(self.run_script("status").returncode, 3)

    def test_finds_old_app_without_server_json(self):
        # 옛 버전처럼 `cd 체크아웃 && python3 app.py` 로 띄웠고 server.json 이 없다
        p = subprocess.Popen([sys.executable, "app.py"], cwd=ROOT, env=self.env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            wait(lambda: os.path.exists(os.path.join(self.home, "server.json")))
            os.unlink(os.path.join(self.home, "server.json"))
            # 다른 데이터 홈의 앱은 건드리지 않는다
            other = subprocess.run([sys.executable, SCRIPT, "stop"], capture_output=True, text=True, timeout=30,
                                   env=dict(self.env, CODEX_LENS_HOME=os.path.join(self.tmp, "other")))
            self.assertIn("떠 있는 앱이 없습니다", other.stdout)
            self.assertIsNone(p.poll())
            r = self.run_script("restart")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            p.wait(5)
            self.assertNotEqual(self.app_pid(), p.pid)
        finally:
            if p.poll() is None:
                p.kill()


if __name__ == "__main__":
    unittest.main()
