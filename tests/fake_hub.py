#!/usr/bin/env python3
"""가짜 agent-hub — 상태판(/api/chats/marks)과 send·kill 만 흉내 낸다. 단위 시험과 브라우저 회귀(--hub-only)가 쓴다.

python3 tests/fake_hub.py --port 0 --session <세션 ID>   (첫 줄에 {"url": …} 를 찍는다)
시험용 조작: GET /calls(받은 send·kill), POST /set {status?, pending?}(채팅 상태 바꾸기).
진짜 agent-hub 처럼 expected_session_id 를 채팅의 session_id 와 글자 그대로 맞춰 보고, 다르면 409 를 준다.
"""
import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeHub:
    def __init__(self, chats, port=0):
        self.chats, self.calls = chats, []
        hub = self

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

            def do_GET(self):
                if self.path == "/api/chats/marks":
                    return self._json(200, {"chats": hub.chats})
                if self.path == "/calls":
                    return self._json(200, {"calls": [{"path": p, "body": b} for p, b in hub.calls]})
                self._json(404, {"error": "없는 API"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/set":
                    for c in hub.chats:
                        c.update({k: body[k] for k in ("status", "pending") if k in body})
                    return self._json(200, {"chats": hub.chats})
                hub.calls.append((self.path, body))
                chat = next((c for c in hub.chats if self.path.startswith("/api/chats/%s/" % c["id"])), None)
                if not chat or body.get("expected_session_id") != chat["session_id"]:
                    return self._json(409, {"error": "실행부의 대화 ID가 바뀌었다"})
                if self.path.endswith("/send"):
                    steer = body.get("mode") == "steer"
                    if not steer:
                        chat["pending"] = int(chat.get("pending") or 0) + 1
                    return self._json(200, {"event": {"kind": "user", "steered": steer, "queued": not steer}, "chat": chat})
                if self.path.endswith("/kill"):
                    chat.update(status="idle", pending=0)
                    return self._json(200, {"killed": True})
                self._json(404, {"error": "없는 API"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--session", required=True, help="agent-hub 가 돌린다고 할 세션 ID")
    a = ap.parse_args()
    hub = FakeHub([{"id": "chat_fake0001", "status": "running", "session_id": a.session, "pending": 0,
                    "profile": "claude", "updated_at": 1}], a.port)
    print(json.dumps({"url": hub.url}), flush=True)
    try:
        hub.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
