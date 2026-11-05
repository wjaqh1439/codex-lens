#!/usr/bin/env python3
"""가짜 steward 관제 — 세션 목록(GET /v1/sessions)·첨부(POST /v1/blobs)·턴(POST …/turns)·취소(POST …/cancel)만 흉내 낸다.

데이터 폴더에 진짜처럼 `control/endpoint.json`(url)과 `tokens/cli.token` 을 써 두고, 토큰이 다르면 401 을 준다.
목록의 `state` 질의는 쉼표로 나눈 phase 로 거른다. 턴은 도는 세션에 steer 면 steered, queue 면 queued 로 답한다.
"""
import hashlib
import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "tok-1"


class FakeSteward:
    def __init__(self, home, sessions, port=0):
        self.home, self.sessions, self.calls, self.blobs, self.auth = home, sessions, [], [], []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                data = json.dumps(obj, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authed(self):
                fake.auth.append(self.headers.get("Authorization"))
                if self.headers.get("Authorization") != "Bearer " + TOKEN:
                    self._json(401, {"error": {"code": "unauthorized", "message": "인증이 필요합니다"}})
                    return False
                return True

            def do_GET(self):
                if not self._authed():
                    return
                u = urllib.parse.urlparse(self.path)
                if u.path == "/v1/sessions":
                    q = urllib.parse.parse_qs(u.query)
                    phases = q["state"][0].split(",") if "state" in q else None
                    return self._json(200, {"sessions": [s for s in fake.sessions if not phases or s["phase"] in phases],
                                            "next_cursor": None})
                self._json(404, {"error": {"code": "not_found", "message": "없는 API"}})

            def do_POST(self):
                if not self._authed():
                    return
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                u = urllib.parse.urlparse(self.path)
                if u.path == "/v1/blobs":
                    fake.blobs.append(raw)
                    name = (urllib.parse.parse_qs(u.query).get("name") or [None])[0]
                    return self._json(201, {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw), "name": name,
                                            "mime": self.headers.get("Content-Type")})
                body = json.loads(raw or b"{}")
                fake.calls.append((u.path, body))
                s = next((s for s in fake.sessions if u.path.startswith("/v1/sessions/%s/" % s["id"])), None)
                if not s:
                    return self._json(404, {"error": {"code": "not_found", "message": "세션이 없습니다"}})
                if u.path.endswith("/turns"):
                    running = s["phase"] == "running"
                    state = ("steered" if body.get("mode") == "steer" else "queued") if running else "accepted"
                    return self._json(202, {"request_id": body.get("request_id"), "state": state})
                if u.path.endswith("/cancel"):
                    s["phase"] = "interrupted"
                    return self._json(202, {"ok": True})
                self._json(404, {"error": {"code": "not_found", "message": "없는 API"}})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        os.makedirs(os.path.join(home, "control"), exist_ok=True)
        os.makedirs(os.path.join(home, "tokens"), exist_ok=True)
        with open(os.path.join(home, "control", "endpoint.json"), "w") as f:
            json.dump({"url": self.url, "pid": os.getpid()}, f)
        with open(os.path.join(home, "tokens", "cli.token"), "w") as f:
            f.write(TOKEN + "\n")

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self


def main():
    """python3 tests/fake_steward.py --home <폴더> --session <세션 ID> — 데모용. 첫 줄에 {"url": …} 를 찍는다."""
    import argparse
    import sys
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True, help="CODEX_LENS_STEWARD_HOME 으로 줄 폴더")
    ap.add_argument("--session", required=True, help="steward 가 돌린다고 할 세션 ID")
    ap.add_argument("--port", type=int, default=0)
    a = ap.parse_args()
    st = FakeSteward(a.home, [{"id": "ses_fake0001", "phase": "running", "agent_session_id": a.session, "profile": "claude",
                               "updated_at": "2026-11-05T00:00:00Z", "turn": {"id": "trn_fake0001", "state": "running"}}], a.port)
    print(json.dumps({"url": st.url}), flush=True)
    try:
        st.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
