#!/usr/bin/env python3
"""FDE 케이스 데모 — 시드 케이스(scripts/fde_seed.py)와 가짜 에이전트로 앱을 띄우고 HTTPS(tailscale serve) 주소를 붙인다.

Air 에서: air run -C . --no-wait --lane gui -- python3 scripts/fde_demo.py   (주소는 air logs <ID> 의 "데모 주소" 줄)
끄기: air cancel <ID> — 신호를 받으면 이 스크립트가 붙인 HTTPS 매핑만 내리고 앱을 끈다. 따로 끄지 않으면 --hours 뒤에 스스로 내린다.
CODEX_LENS_FEEDBACK_ORIGIN 을 주면 그 의견 위젯을 붙인다(데모에서 보냄 표시는 위젯이 environment=demo 로 붙인다).
"""
import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import fde_seed  # noqa: E402

FEEDBACK = os.environ.get("CODEX_LENS_FEEDBACK_ORIGIN", "")  # 의견 위젯 출처 — 비우면 위젯을 넣지 않는다


def tailscale():
    return shutil.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


def used_https_ports(ts):
    st = json.loads(subprocess.check_output([ts, "serve", "status", "--json"], text=True) or "{}")
    used = {int(p) for p in (st.get("TCP") or {})}
    used.update(int(k.rsplit(":", 1)[1]) for k in (st.get("Web") or {}))
    return used


def free_local(port):
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=8)
    a = ap.parse_args()
    home = tempfile.mkdtemp(prefix="codex-lens-fde-demo-")
    cwd = os.path.join(home, "plant-inspection")
    fde_seed.build(cwd)
    ts = tailscale()
    used = used_https_ports(ts)
    ports = [p for p in range(19300, 19900) if p not in used and free_local(p)]
    local, public = ports[0], ports[1]
    env = dict(os.environ, CODEX_LENS_FEEDBACK_ORIGIN=FEEDBACK, FAKE_CODEX_DELAY="0.6")
    log = open(os.path.join(home, "server.log"), "w")
    app = subprocess.Popen([sys.executable, os.path.join(ROOT, "scripts", "run_fake_demo.py"), "--home", home, "--port", str(local)],
                           stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    for _ in range(200):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/api/info" % local, timeout=1).read()
            break
        except OSError:
            time.sleep(0.1)
    subprocess.run([ts, "serve", "--bg", "--https=%d" % public, "http://127.0.0.1:%d" % local], check=True)
    dns = json.loads(subprocess.check_output([ts, "status", "--json"], text=True))["Self"]["DNSName"].rstrip(".")
    url = "https://%s:%d/#/case?cwd=%s" % (dns, public, urllib.request.quote(cwd))
    try:
        urllib.request.urlopen("https://%s:%d/api/info" % (dns, public), timeout=20).read()
        reach = "확인"
    except OSError as e:
        reach = "확인 실패: %s" % e
    print(json.dumps({"url": url, "local": local, "public": public, "cwd": cwd, "home": home, "https": reach}, ensure_ascii=False), flush=True)
    print("데모 주소 %s" % url, flush=True)

    def stop(*_):
        subprocess.run([ts, "serve", "--https=%d" % public, "off"])
        try:
            os.killpg(app.pid, signal.SIGTERM)
        except OSError:
            pass
        print("데모를 내렸습니다", flush=True)
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    deadline = time.time() + a.hours * 3600
    while time.time() < deadline and app.poll() is None:
        time.sleep(5)
    stop()


if __name__ == "__main__":
    main()
