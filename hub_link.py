"""agent-hub 가 돌리는 세션 알아보기 — 그 세션의 응답 중에는 agent-hub 로 끼워 넣고(바로 넣기)·대기열에 넣고·멈춘다.

이 앱 밖에서 도는 턴은 이 앱이 프로세스를 쥐고 있지 않아 직접 끼워 넣을 수 없다. agent-hub 채팅이면 채팅 메타의
`session_id`(Claude 세션 ID · Codex 스레드 ID)가 이 앱의 세션 ID 와 같으니, agent-hub 의
`POST /api/chats/<id>/send {text, images, mode, expected_session_id}` 로 대신 보낸다(agent-hub 의 `hub send --mode` 와 같은 길).
상태는 `GET /api/chats/<id>` 대신 가벼운 상태판 `GET /api/chats/marks` 하나를 짧게 캐시해 본다.

주소: `CODEX_LENS_HUB_URL`(운영 기본 http://127.0.0.1:8715, 데모 기본 없음 — 빈 값이면 끈다).
"""

import json
import re
import threading
import time
import urllib.error
import urllib.request

import applog

MARKS_TTL = 2.0
TIMEOUT = 4.0
_DATA_URL = re.compile(r"^data:(image/[a-z0-9.+-]+);base64,(.*)$", re.I | re.S)


class HubError(Exception):
    """agent-hub 에 닿지 못했거나 거절당했다. status 는 화면에 돌려줄 HTTP 상태."""

    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


class HubLink:
    def __init__(self, url):
        self.url = (url or "").rstrip("/")
        self.lock = threading.Lock()
        self.cache = (0.0, {})        # (읽은 시각, session_id -> 채팅 표시)
        self.failed_at = 0.0          # 마지막 실패 기록 시각 — 꺼진 허브로 로그를 채우지 않는다
        self.sent = {}                # client_id -> 결과 — 다시 보내기가 두 번 들어가지 않게

    @property
    def enabled(self):
        return bool(self.url)

    def _call(self, method, path, body=None, timeout=TIMEOUT):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read() or b"{}").get("error") or str(e)
            except ValueError:
                msg = str(e)
            raise HubError("agent-hub: %s" % msg, 409 if e.code == 409 else 502)
        except (OSError, ValueError) as e:
            raise HubError("agent-hub 에 닿지 못했습니다: %s" % e)

    def _marks(self):
        now = time.time()
        with self.lock:
            ts, data = self.cache
            if now - ts < MARKS_TTL:
                return data
        data = {}
        try:
            res = self._call("GET", "/api/chats/marks", timeout=2.0)
            for c in res.get("chats") or []:
                sid = (c.get("session_id") or "").lower()
                if not sid:
                    continue
                cur = data.get(sid)
                # 같은 대화를 이어 연 채팅이 여럿이면 지금 도는 것, 없으면 마지막에 바뀐 것
                key = (c.get("status") == "running", c.get("updated_at") or 0)
                if not cur or key > (cur.get("status") == "running", cur.get("updated_at") or 0):
                    data[sid] = c
        except HubError as e:
            if now - self.failed_at > 300:
                applog.log("hub_marks_failed", level="warn", error=str(e)[:300])
                self.failed_at = now
        with self.lock:
            self.cache = (now, data)
        return data

    def chat_for(self, session_id):
        """그 세션을 돌리는 agent-hub 채팅 — {chat_id, status, running, pending, profile}. 아니면 None."""
        if not self.enabled or not session_id:
            return None
        c = self._marks().get(session_id.lower())
        if not c:
            return None
        return {"chat_id": c["id"], "status": c.get("status"), "running": c.get("status") == "running",
                "pending": int(c.get("pending") or 0), "profile": c.get("profile"), "url": self.url,
                # agent-hub 는 expected_session_id 를 글자 그대로 맞춰 본다 — 그쪽 표기를 그대로 돌려준다
                "session_id": c.get("session_id"), "via": "agent-hub"}

    def forget(self):
        with self.lock:
            self.cache = (0.0, {})

    @staticmethod
    def _images(images):
        out = []
        for i, im in enumerate(images or [], 1):
            m = _DATA_URL.match((im or {}).get("url") or "")
            if not m:
                raise ValueError("agent-hub 로는 붙여 넣은 이미지만 보낼 수 있습니다")
            ext = m.group(1).split("/")[1].replace("jpeg", "jpg")
            out.append({"name": "image-%d.%s" % (i, ext), "content_type": m.group(1).lower(), "data_base64": m.group(2)})
        return out

    def send(self, session_id, chat, text, images=None, mode="steer", client_id=None):
        """진행 중인 agent-hub 턴에 바로 넣거나(steer) 그 턴이 끝나면 보내게(queue) 한다.
        agent-hub 가 끼워 넣지 못하면(턴 시작 직후 등) 스스로 대기열로 돌린다 — 결과의 steered·queued 로 안다."""
        if client_id and client_id in self.sent:
            return self.sent[client_id]
        body = {"text": text or "", "mode": mode if mode in ("steer", "queue") else "queue",
                "images": self._images(images), "expected_session_id": chat["session_id"]}
        t0 = time.time()
        res = self._call("POST", "/api/chats/%s/send" % chat["chat_id"], body, timeout=30)
        ev = res.get("event") or {}
        self.forget()
        out = {"id": session_id, "hub": chat["chat_id"], "via": "agent-hub", "steered": bool(ev.get("steered")),
               "queued": bool(ev.get("queued")) and not ev.get("steered")}
        if client_id:
            self.sent[client_id] = out
        applog.log("hub_send", thread=session_id, chat=chat["chat_id"], mode=body["mode"], steered=out["steered"],
                   queued=out["queued"], chars=len(text or ""), image_count=len(body["images"]),
                   ms=int((time.time() - t0) * 1000))
        return out

    def interrupt(self, session_id, chat):
        self._call("POST", "/api/chats/%s/kill" % chat["chat_id"], {"expected_session_id": chat["session_id"]}, timeout=15)
        self.forget()
        applog.log("hub_interrupt", thread=session_id, chat=chat["chat_id"])
        return {"ok": True, "hub": chat["chat_id"], "via": "agent-hub"}
