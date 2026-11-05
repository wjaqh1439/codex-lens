#!/usr/bin/env python3
"""재전송 데모 — 가짜 app-server 데모 앞에 502 프록시를 세운다. 보낸 글에 "재시작"이 있으면 그 순간 앱이 10초 내려간 것처럼
502 를 돌려주고, 화면은 기다렸다가 같은 글을 한 번만 다시 보낸다.
    python3 scripts/resend_demo.py --port <포트>
"""
import argparse
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from browser_check import DownProxy, free_port  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, required=True, help="브라우저가 여는 프록시 포트")
    ap.add_argument("--home", default=None)
    a = ap.parse_args()
    home = a.home or tempfile.mkdtemp(prefix="codex-lens-resend-demo-")
    app_port = free_port()
    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "scripts", "run_fake_demo.py"), "--home", home, "--port", str(app_port)])
    target = "http://127.0.0.1:%d/" % app_port
    for _ in range(100):
        try:
            urllib.request.urlopen(target + "api/info", timeout=1).read()
            break
        except OSError:
            time.sleep(0.1)
    proxy = DownProxy(target, trigger="재시작", port=a.port)
    print("재전송 데모 %s (앱 %s)" % (proxy.url, target), flush=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        srv.wait()
    finally:
        proxy.close()
        srv.terminate()


if __name__ == "__main__":
    main()
