"""steward 가 돌리는 세션 알아보기 — 그 세션의 응답 중에는 steward 로 끼워 넣고(바로 넣기)·대기열에 넣고·멈춘다.

steward(steward, agent-hub 의 다음 세대)도 에이전트 프로세스를 직접 쥐므로 이 앱은 그 턴에
끼워 넣거나 멈출 수 없다. steward 세션의 `agent_session_id`(Claude 세션 ID · Codex 스레드 ID)가 이 앱의 세션 ID 와 같으면
steward 관제 API 로 대신 보낸다 — `POST /v1/sessions/<id>/turns {request_id, input, mode}`·`POST /v1/sessions/<id>/cancel`.
도는 세션은 `GET /v1/sessions?state=running,needs_input` 하나를 짧게 캐시해 본다.

관제 주소와 토큰은 steward 데이터 폴더에서 읽는다 — `<폴더>/control/endpoint.json` 의 url(관제가 떠 있는 동안만 있다)과
같은 기기 CLI 가 쓰는 `<폴더>/tokens/cli.token`. 폴더: `CODEX_LENS_STEWARD_HOME`(빈 값이면 끈다), 없으면 운영만
steward 기본 위치(macOS `~/Library/Application Support/steward`, 그 밖 `$XDG_DATA_HOME/steward`·`~/.local/share/steward`).
돌려주는 표시는 `hub_link.HubLink.chat_for` 와 같은 모양이고 `via` 가 "steward" 다 — 화면·app.py 는 둘을 같게 다룬다.
"""

import base64
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import applog
from hub_link import HubError

MARKS_TTL = 2.0
TIMEOUT = 4.0
RUNNING = ("running", "needs_input")
_DATA_URL = re.compile(r"^data:(image/[a-z0-9.+-]+);base64,(.*)$", re.I | re.S)


def default_home(demo=False):
    v = os.environ.get("CODEX_LENS_STEWARD_HOME")
    if v is None:
        if demo:
            return None
        if sys.platform == "darwin":
            v = "~/Library/Application Support/steward"
        else:
            v = os.path.join(os.environ.get("XDG_DATA_HOME") or "~/.local/share", "steward")
    return os.path.abspath(os.path.expanduser(v)) if v else None


class StewardLink:
    def __init__(self, home):
        self.home = home or None
        self.lock = threading.Lock()
        self.cache = (0.0, {})        # (읽은 시각, agent_session_id -> steward 세션)
        self.failed_at = 0.0
        self.sent = {}                # client_id -> 결과 — 다시 보내기가 두 번 들어가지 않게

    @property
    def enabled(self):
        return bool(self.home) and os.path.isdir(self.home)

    def _endpoint(self):
        try:
            with open(os.path.join(self.home, "control", "endpoint.json"), encoding="utf-8") as f:
                url = (json.load(f).get("url") or "").rstrip("/")
            with open(os.path.join(self.home, "tokens", "cli.token"), encoding="utf-8") as f:
                token = f.read().strip()
        except (OSError, ValueError) as e:
            raise HubError("steward 관제가 실행 중이 아닙니다: %s" % e)
        if not url or not token:
            raise HubError("steward 관제 주소·토큰이 비어 있습니다")
        return url, token

    def _call(self, method, path, body=None, timeout=TIMEOUT, raw=None, ctype=None):
        url, token = self._endpoint()
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers = {"Authorization": "Bearer " + token}
        if data is not None:
            headers["Content-Type"] = ctype or "application/json"
        req = urllib.request.Request(url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read() or b"{}").get("error") or {}
                msg = err.get("message") if isinstance(err, dict) else str(err)
            except ValueError:
                msg = None
            raise HubError("steward: %s" % (msg or e), 409 if e.code == 409 else 502)
        except (OSError, ValueError) as e:
            raise HubError("steward 에 닿지 못했습니다: %s" % e)

    def _marks(self):
        now = time.time()
        with self.lock:
            ts, data = self.cache
            if now - ts < MARKS_TTL:
                return data
        data = {}
        try:
            res = self._call("GET", "/v1/sessions?state=%s&limit=200" % ",".join(RUNNING), timeout=2.0)
            for s in res.get("sessions") or []:
                sid = (s.get("agent_session_id") or "").lower()
                if sid and s.get("phase") in RUNNING:
                    cur = data.get(sid)
                    if not cur or (s.get("updated_at") or "") > (cur.get("updated_at") or ""):
                        data[sid] = s
        except HubError as e:
            if now - self.failed_at > 300:
                applog.log("steward_marks_failed", level="warn", error=str(e)[:300])
                self.failed_at = now
        with self.lock:
            self.cache = (now, data)
        return data

    def chat_for(self, session_id):
        """그 세션을 돌리는 steward 세션 — hub_link 와 같은 모양 {chat_id, running, pending, …, via}. 아니면 None."""
        if not self.enabled or not session_id:
            return None
        s = self._marks().get(session_id.lower())
        if not s:
            return None
        turn = s.get("turn") or {}
        return {"chat_id": s["id"], "status": s.get("phase"), "running": True, "pending": 0,
                "profile": s.get("profile"), "session_id": s.get("agent_session_id"),
                "turn_id": turn.get("id") if turn.get("state") == "running" else None, "via": "steward"}

    def forget(self):
        with self.lock:
            self.cache = (0.0, {})

    def _attachments(self, images):
        out = []
        for i, im in enumerate(images or [], 1):
            m = _DATA_URL.match((im or {}).get("url") or "")
            if not m:
                raise ValueError("steward 로는 붙여 넣은 이미지만 보낼 수 있습니다")
            mime = m.group(1).lower()
            name = "image-%d.%s" % (i, mime.split("/")[1].replace("jpeg", "jpg"))
            b = self._call("POST", "/v1/blobs?name=" + urllib.parse.quote(name), raw=base64.b64decode(m.group(2)),
                           ctype=mime, timeout=30)
            out.append({k: b[k] for k in ("sha256", "name", "mime", "size") if b.get(k) is not None})
        return out

    def send(self, session_id, chat, text, images=None, mode="steer", client_id=None):
        """진행 중인 steward 턴에 바로 넣거나(steer) 그 턴이 끝나면 보내게(queue) 한다.
        steward 가 끼워 넣지 못하면 스스로 대기열로 돌린다 — 결과의 steered·queued 로 안다."""
        if client_id and client_id in self.sent:
            return self.sent[client_id]
        mode = mode if mode in ("steer", "queue") else "queue"
        t0 = time.time()
        atts = self._attachments(images)
        body = {"request_id": "lens-%s" % (client_id or uuid.uuid4().hex), "mode": mode,
                "input": {"text": text or "", "attachments": atts} if atts else {"text": text or ""}}
        res = self._call("POST", "/v1/sessions/%s/turns" % chat["chat_id"], body, timeout=30)
        self.forget()
        state = res.get("state")
        out = {"id": session_id, "hub": chat["chat_id"], "via": "steward", "steered": state == "steered",
               "queued": state == "queued"}
        if client_id:
            self.sent[client_id] = out
        applog.log("steward_send", thread=session_id, session=chat["chat_id"], mode=mode, state=state,
                   chars=len(text or ""), image_count=len(atts), ms=int((time.time() - t0) * 1000))
        return out

    def interrupt(self, session_id, chat):
        body = {"turn_id": chat["turn_id"]} if chat.get("turn_id") else {}
        self._call("POST", "/v1/sessions/%s/cancel" % chat["chat_id"], body, timeout=15)
        self.forget()
        applog.log("steward_interrupt", thread=session_id, session=chat["chat_id"])
        return {"ok": True, "hub": chat["chat_id"], "via": "steward"}
