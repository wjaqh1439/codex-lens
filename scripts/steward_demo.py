#!/usr/bin/env python3
"""steward 연동 데모 — 가짜 steward 가 시드의 Claude 세션을 돌리는 중이라고 알려 주는 가짜 app-server 데모를 띄운다.

python3 scripts/steward_demo.py --home <폴더> --port <포트>
그 세션(#/s/c1a0de00-0000-4000-8000-000000000003)에서 멈춤·바로 넣기·턴 끝나고가 steward 로 가고, 화면 알림으로 확인한다.
"""
import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSION = "c1a0de00-0000-4000-8000-000000000003"   # tests/seed.py 의 Claude Code 기록


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    st_home = os.path.join(os.path.abspath(a.home), "steward")
    st = subprocess.Popen([sys.executable, os.path.join(ROOT, "tests", "fake_steward.py"), "--home", st_home, "--session", SESSION],
                          stdout=subprocess.PIPE, text=True)
    print("가짜 steward:", json.loads(st.stdout.readline())["url"], flush=True)
    env = dict(os.environ, CODEX_LENS_STEWARD_HOME=st_home)
    try:
        return subprocess.call([sys.executable, os.path.join(ROOT, "scripts", "run_fake_demo.py"), "--home", a.home,
                                "--port", str(a.port), "--host", a.host], env=env)
    finally:
        st.terminate()


if __name__ == "__main__":
    sys.exit(main())
