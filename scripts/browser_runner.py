"""브라우저 회귀 실행기 — e2e_*.mjs 를 ego-browser 의 새 Space 에서 돌리고, 성공·실패와 관계없이 그 Space 를 닫는다.

회귀 스크립트는 `globalThis.browserTask`(taskSpace 결과)와 그 `page("p1")` 을 쓴다. ego-browser 가 PATH 에 있어야 한다.
"""
import json
import subprocess


def run(script_path, name):
    """script_path 의 JS 를 새 Space 에서 돌리고 종료 코드를 돌려준다."""
    with open(script_path, encoding="utf-8") as f:
        body = f.read()
    code = ("const browserTask = await taskSpace(%s);\nglobalThis.browserTask = browserTask;\ntry {\n%s\n} finally {\n"
            "  try { await browserTask.finish({ keep: [] }); } catch (e) { /* 스크립트가 이미 닫았다 */ }\n}\n"
            % (json.dumps(name, ensure_ascii=False), body))
    return subprocess.run(["ego-browser", "nodejs"], input=code, text=True).returncode


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeDemo:
    """가짜 app-server 데모(run_fake_demo.py)를 빈 포트에 띄우고, with 블록이 끝나면 내린다."""

    def __init__(self, home, args=(), env=None, port=None, host="127.0.0.1"):
        import os
        import sys
        self.port = port or free_port()
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.cmd = [sys.executable, os.path.join(root, "scripts", "run_fake_demo.py"), "--home", str(home),
                    "--port", str(self.port), "--host", host] + list(args)
        self.env = dict(os.environ, **(env or {}))
        self.url = "http://127.0.0.1:%d/" % self.port
        self.proc = None

    def __enter__(self):
        import time
        import urllib.request
        self.proc = subprocess.Popen(self.cmd, env=self.env)
        for _ in range(200):
            try:
                urllib.request.urlopen(self.url + "api/info", timeout=1).read()
                return self
            except OSError:
                time.sleep(0.1)
        self.__exit__()
        raise RuntimeError("가짜 데모가 뜨지 않았다: " + " ".join(self.cmd))

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
