#!/usr/bin/env python3
"""Codex Lens — Codex·Claude Code 세션을 빠르게 만들고, 턴(요청·과정·답변)으로 읽고, 고른 부분에 묻고 코멘트하는 웹 앱.

표준 라이브러리만 쓴다(macOS·Linux 공통). 실행: python3 app.py  (환경변수는 AGENTS.md)
"""

import json
import mimetypes
import os
import queue
import re
import shutil
import sys
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import applog  # noqa: E402
import local_env  # noqa: E402
import refine  # noqa: E402
import weblink  # noqa: E402
from codex_rpc import RpcError  # noqa: E402
from docs import Docs  # noqa: E402
from fde import Fde  # noqa: E402
from hub_link import HubError, HubLink  # noqa: E402
from lens import STANDARD_TIER, Busy, Lens  # noqa: E402
from ports import Ports  # noqa: E402
from steward_link import StewardLink, default_home as steward_home  # noqa: E402

VERSION = open(os.path.join(ROOT, "VERSION")).read().strip()
DEMO = os.environ.get("CODEX_LENS_DEMO") == "1"
HOME = os.path.abspath(os.path.expanduser(os.environ.get("CODEX_LENS_HOME")
                                          or ("~/.codex-lens-demo" if DEMO else "~/.codex-lens")))
LOCAL_ENV = local_env.load(HOME)   # <데이터 홈>/env — 이 기기만의 설정(환경변수가 이긴다)
HOST = os.environ.get("CODEX_LENS_HOST", "127.0.0.1")
PORT = int(os.environ.get("CODEX_LENS_PORT") or os.environ.get("PORT") or (18761 if DEMO else 8761))
FEEDBACK_ORIGIN = os.environ.get("CODEX_LENS_FEEDBACK_ORIGIN", "")
# agent-hub 가 돌리는 세션은 응답 중 보내기·멈춤을 agent-hub 로 넘긴다(hub_link.py). 데모는 운영 허브에 닿지 않게 기본으로 끈다.
HUB_URL = os.environ.get("CODEX_LENS_HUB_URL", "" if DEMO else "http://127.0.0.1:8715")
APP_NAME = "Codex Lens"
SSE_HEARTBEAT = 15
PORT_CLI = os.path.join(ROOT, "bin", "lens-port")


def api_base(host=HOST, port=PORT):
    """이 기기 안에서 앱 API 에 닿는 주소 — 모든 주소에 묶였으면 루프백으로 부른다."""
    h = host.strip("[]")
    if h in ("", "0.0.0.0"):
        h = "127.0.0.1"
    elif h == "::":
        h = "::1"
    return "http://%s:%d" % ("[%s]" % h if ":" in h else h, port)


def port_instructions():
    """Codex 세션에 주는 개발자 지침 — 포트를 여는 서버는 codex-lens 포트 관리자로 띄운다."""
    return "\n".join([
        "[codex-lens 포트 관리]",
        "이 세션은 codex-lens 에서 돈다. 데모·개발 서버처럼 포트를 여는 프로세스는 포트 번호를 직접 정하거나 백그라운드(&, nohup)로 "
        "띄우지 말고 lens-port 로 띄운다. 빈 포트를 골라 주고, 사용자가 codex-lens 의 \"포트\" 화면에서 보고 끌 수 있다.",
        "- lens-port 는 PATH 에 있다. 이름만으로 안 되면 전체 경로 \"%s\" 로 부른다." % PORT_CLI,
        "- 띄우기: lens-port run --name <짧은 이름> -- <명령…>   명령 안의 {port}·{host} 는 고른 포트·바인드 주소로 바뀌고 "
        "환경변수 PORT·CODEX_LENS_BIND 에도 같은 값이 들어간다. 서버가 반드시 그 포트·주소({host})에 묶이게 하고 "
        "바인드 주소를 따로 정하지 않는다.",
        "  예) lens-port run --name web -- python3 -m http.server {port} --bind {host}",
        "  셸 문법이 필요하면) lens-port run --name vite --sh 'npm run dev -- --port $PORT --host $CODEX_LENS_BIND'",
        "- 포트가 열릴 때까지 기다렸다가 주소를 출력한다. 실패하면 로그 끝을 보여 주고 0 이 아닌 코드로 끝난다.",
        "- 같은 이름으로 다시 run 하면 이전 것을 끄고 같은 포트로 다시 띄운다. 목록 lens-port list · 끄기 lens-port stop <ID|포트> · "
        "로그 lens-port logs <ID|포트> · 빈 포트 번호만 필요할 때 lens-port free",
        "- 답변에는 lens-port 가 출력한 주소를 그대로 적는다 — \"다른 기기에서 열 주소\" 줄이 있으면 그 주소를, 없으면 "
        "127.0.0.1 주소를 적고 다른 기기에서는 codex-lens 포트 화면의 \"열기\"로 연다고 덧붙인다. IP 주소를 따로 찾지 않는다.",
        "- 다른 포트 관리 도구(proc 등)를 쓰라는 지침이 있어도 이 세션에서는 lens-port 를 쓴다.",
    ])


def codex_env():
    """운영은 Codex 기본 기록(CODEX_HOME 또는 ~/.codex)을 그대로 읽는다.
    데모는 데모 홈 안의 codex-home 을 쓴다 — 로그인 파일(auth.json)만 원래 것을 가리키는 심볼릭 링크로 둔다."""
    env = dict(os.environ)
    if DEMO and not os.environ.get("CODEX_LENS_DEMO_CODEX_HOME_KEEP"):
        ch = os.environ.get("CODEX_LENS_DEMO_CODEX_HOME") or os.path.join(HOME, "codex-home")
        os.makedirs(ch, exist_ok=True)
        src = os.path.join(os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex"), "auth.json")
        dst = os.path.join(ch, "auth.json")
        if os.path.exists(src) and not os.path.lexists(dst):
            os.symlink(src, dst)
        cfg = os.path.join(ch, "config.toml")
        if not os.path.exists(cfg):
            with open(cfg, "w") as f:
                f.write("# codex-lens 데모 전용 설정\n")
        env["CODEX_HOME"] = ch
    return port_env(env)


def port_env(env):
    """에이전트가 돌리는 명령(bin/lens-port)이 이 앱을 찾는 주소."""
    env["CODEX_LENS_URL"] = api_base()
    env["PATH"] = os.path.dirname(PORT_CLI) + os.pathsep + env.get("PATH", os.defpath)
    env["CODEX_LENS_HOME"] = HOME
    return env


def claude_env():
    """(env, 데모가 운영 설정 폴더를 같이 쓰는지).

    운영은 Claude Code 기본 설정 폴더(CLAUDE_CONFIG_DIR 또는 ~/.claude)를 그대로 쓴다.
    데모는 CODEX_LENS_DEMO_CLAUDE_HOME 을 주면 그 폴더를 CLAUDE_CONFIG_DIR 로 쓰고 로그인 파일(.credentials.json)만
    원래 것을 가리키는 심볼릭 링크로 둔다(Linux). macOS 는 로그인이 키체인에 설정 폴더별로 묶여 따로 둔 폴더에서
    로그인되지 않으므로, 주지 않으면 운영 설정 폴더를 같이 쓰고 목록에는 데모가 만든 세션만 보인다."""
    env = port_env(dict(os.environ))
    if not DEMO:
        return env, False
    ch = os.environ.get("CODEX_LENS_DEMO_CLAUDE_HOME")
    if not ch:
        return env, not os.environ.get("CODEX_LENS_DEMO_CLAUDE_HOME_KEEP")
    ch = os.path.abspath(os.path.expanduser(ch))
    os.makedirs(ch, exist_ok=True)
    src = os.path.join(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"), ".credentials.json")
    dst = os.path.join(ch, ".credentials.json")
    if os.path.exists(src) and not os.path.lexists(dst) and os.path.abspath(src) != os.path.abspath(dst):
        os.symlink(src, dst)
    env["CLAUDE_CONFIG_DIR"] = ch
    return env, False


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class App:
    def __init__(self, lens=None, hub_url=None, steward_dir=None):
        applog.setup(os.path.join(HOME, "logs", "app.jsonl"), version=VERSION, env="demo" if DEMO else "production")
        if lens is None:
            cenv, mine_only = claude_env()
            lens = Lens(HOME, VERSION, demo=DEMO, codex_env=codex_env(), claude_env=cenv, claude_mine_only=mine_only)
        self.lens = lens
        self.docs = Docs(self.lens)
        self.fde = Fde(self.lens)
        self.hub = HubLink(HUB_URL if hub_url is None else hub_url)
        # steward 가 돌리는 세션도 같은 길로 넘긴다(steward_link.py) — 데모는 운영 steward 에 닿지 않게 기본으로 끈다
        self.steward = StewardLink(steward_home(DEMO) if steward_dir is None else steward_dir)
        self.ports = Ports(HOME, HOST, notify=self._ports_changed)
        if not os.environ.get("CODEX_LENS_NO_PORT_HINT"):
            self.lens.dev_instructions = port_instructions()

    def _ports_changed(self, thread_id=None):
        self.lens.bus.publish("*", {"type": "ports"})
        if thread_id:
            self.lens.bus.publish(thread_id, {"type": "ports"})

    def index_html(self):
        html = read_text(os.path.join(ROOT, "static", "index.html"))
        prefix = "demo-" if DEMO else ""
        title = ("[데모] " if DEMO else "") + APP_NAME
        widget = ""
        if FEEDBACK_ORIGIN:
            widget = ('<script src="%s/feedback-widget.js?v=%s" data-project-id="codex-lens" data-project-name="%s" '
                      'data-app-version="%s" data-environment="%s" data-endpoint="%s/api/feedback" defer></script>'
                      % (FEEDBACK_ORIGIN, VERSION, APP_NAME, VERSION, "demo" if DEMO else "production", FEEDBACK_ORIGIN))
        boot = json.dumps({"version": VERSION, "demo": DEMO, "name": APP_NAME, "home": os.path.expanduser("~"), "port": PORT})
        return (html.replace("{{TITLE}}", title).replace("{{ICON}}", prefix).replace("{{VERSION}}", VERSION)
                .replace("{{BOOT}}", boot).replace("{{DEMO_CLASS}}", " ".join(c for c in ("demo" if DEMO else "", "has-fb" if widget else "") if c))
                .replace("{{FEEDBACK}}", widget))

    def manifest(self):
        prefix = "demo-" if DEMO else ""
        name = ("[데모] " if DEMO else "") + APP_NAME
        return {"name": name, "short_name": name, "start_url": "/", "display": "standalone",
                "background_color": "#101418", "theme_color": "#0f766e",
                "icons": [{"src": "/icons/%sicon-192.png" % prefix, "sizes": "192x192", "type": "image/png"},
                          {"src": "/icons/%sicon-512.png" % prefix, "sizes": "512x512", "type": "image/png"},
                          {"src": "/icons/%sicon-maskable-512.png" % prefix, "sizes": "512x512", "type": "image/png",
                           "purpose": "maskable"}]}


ROUTES = []


def route(method, pattern):
    rx = re.compile("^" + pattern + "$")

    def deco(fn):
        ROUTES.append((method, rx, fn, pattern))
        return fn
    return deco


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _int(v, d):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


# --- API ----------------------------------------------------------------
@route("GET", "/api/info")
def api_info(app, h, q):
    return {"version": VERSION, "demo": DEMO, "home": HOME, "codex_home": app.lens.client.init_info.get("codexHome"),
            "claude_home": app.lens.client.claude.root,
            "sandbox": os.environ.get("CODEX_LENS_SANDBOX", "danger-full-access"), "platform": sys.platform}


@route("GET", "/api/web/resolve")
def api_web_resolve(app, h, q):
    """웹 보기 — 127.0.0.1 같은 이 기기 주소를 다른 기기에서도 열리는 HTTPS 주소로 바꾼다(weblink.py)."""
    def public_for(port):
        try:
            e = app.ports.get(port)
        except KeyError:
            return None
        return app.ports.url_of(e) if e.get("state") in ("starting", "listening") else None
    via = q.get("via") or ((h.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip() + "://"
                           + (h.headers.get("X-Forwarded-Host") or h.headers.get("Host") or "").split(",")[0].strip())
    return weblink.resolve(q.get("url") or "", public_for=public_for, via=via, app_port=PORT)


@route("GET", "/api/changelog")
def api_changelog(app, h, q):
    return {"version": VERSION, "text": read_text(os.path.join(ROOT, "CHANGELOG.md"))}


@route("GET", "/api/image")
def api_image(app, h, q):
    """localImage 첨부를 같은 출처에서 읽는다. 이미지 바이트만 내보낸다."""
    path = os.path.expanduser(q.get("path") or "")
    if not os.path.isabs(path):
        raise HttpError(400, "이미지의 전체 경로가 필요합니다")
    if os.path.exists(path) and not os.path.isfile(path):
        raise HttpError(400, "이미지 파일이 아닙니다")
    try:
        with open(path, "rb") as f:
            data = f.read(32 * 1024 * 1024 + 1)
    except OSError:
        raise HttpError(404, "이미지 파일을 찾거나 읽을 수 없습니다")
    if len(data) > 32 * 1024 * 1024:
        raise HttpError(413, "이미지는 32MB까지 볼 수 있습니다")
    ctype = None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        ctype = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        ctype = "image/jpeg"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        ctype = "image/gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ctype = "image/webp"
    if not ctype:
        raise HttpError(415, "미리보기를 지원하지 않는 이미지입니다")
    h.send_bytes(200, data, ctype, cache="no-store")


@route("GET", "/api/sessions")
def api_sessions(app, h, q):
    return app.lens.list_sessions(cursor=q.get("cursor"), limit=_int(q.get("limit"), 40), search=q.get("q") or None,
                                  cwd=q.get("cwd") or None, archived=q.get("archived") == "1")


@route("GET", "/api/cwds")
def api_cwds(app, h, q):
    return {"cwds": app.lens.recent_cwds(), "home": os.path.expanduser("~")}


@route("GET", "/api/git")
def api_git(app, h, q):
    return app.lens.git_info(q.get("cwd"))


@route("GET", "/api/models")
def api_models(app, h, q):
    # current: 아무것도 고르지 않으면 실제로 쓰일 모델·effort — 세션이면 그 세션의 값(agent 도), 아니면 currents[codex].
    # currents: 새 세션에서 에이전트별로 같은 값(모델 관리 기본값 → 에이전트 설정 → 목록의 기본)
    out = {"models": app.lens.models(fresh=q.get("fresh") == "1"), "settings": app.lens.model_settings(),
           "standard_tier": STANDARD_TIER, "recent": app.lens.recent_picks()}
    if q.get("thread"):
        m = app.lens.session_meta(q["thread"])
        out["current"] = {"model": m.get("model"), "effort": m.get("effort")}
        out["agent"] = m.get("agent") or "codex"
    else:
        out["currents"] = {a: app.lens.default_pick(a) for a in ("codex", "claude")}
        out["current"] = out["currents"]["codex"]
    return out


@route("POST", "/api/models/settings")
def api_model_settings(app, h, q):
    return {"settings": app.lens.set_model_settings(h.body())}


def _note_recent(app, b):
    # 보내기는 이미 성공했다 — 최근 칩 기록이 실패해도 보내기를 실패로 돌리지 않는다
    try:
        app.lens.note_recent(b.get("recent"))
    except Exception as e:
        applog.log("recent_pick", level="warn", error=str(e)[:200])


def _tier(b):
    t = b.get("tier")
    return t if isinstance(t, str) and t else None


@route("POST", "/api/sessions")
def api_create(app, h, q):
    b = h.body()
    out = app.lens.create_session(b.get("cwd"), b.get("text"), b.get("mentions"), b.get("model") or None,
                                  b.get("effort") or None, b.get("client_id"), use_worktree=bool(b.get("worktree")), images=b.get("images"),
                                  agent=b.get("agent"), tier=_tier(b))
    app.lens.mark_sent(out["id"], set(b.get("comment_ids") or []))
    _note_recent(app, b)
    return out


@route("GET", "/api/sessions/(?P<sid>[^/]+)")
def api_session(app, h, q, sid):
    out = app.lens.read_session(sid)
    hub, st = app.hub.chat_for(sid), app.steward.chat_for(sid)   # steward 쪽은 도는 세션만 돌려준다
    out["hub"] = st if st and not (hub and hub["running"]) else hub
    out["ports"] = [_port_brief(e) for e in app.ports.list(sid) if e["state"] in ("starting", "listening")]
    return out


@route("POST", "/api/sessions/(?P<sid>[^/]+)/archive")
def api_archive(app, h, q, sid):
    return app.lens.set_archived(sid, h.body().get("archived"))


@route("POST", "/api/sessions/(?P<sid>[^/]+)/turns")
def api_send(app, h, q, sid):
    b = h.body()
    mode = b.get("mode") if b.get("mode") in ("steer", "queue") else None
    link, hub = _hub_running(app, sid)
    if hub:
        # agent-hub·steward 가 돌리는 턴 — 이 앱이 따로 턴을 열면 두 곳에서 동시에 돈다. 바로 넣기·대기열을 그쪽에 맡긴다.
        if not (b.get("text") or "").strip() and not b.get("images"):
            raise ValueError("보낼 내용이 없습니다")
        out = link.send(sid, hub, _hub_text(app, sid, b.get("text"), b.get("mentions")), app.lens.validate_images(b.get("images")),
                           mode or "queue", b.get("client_id"))
        app.lens.mark_sent(sid, set(b.get("comment_ids") or []))
        app.lens.bus.publish(sid, {"type": "comments"})
        return out
    out = app.lens.send_turn(sid, b.get("text"), b.get("mentions"), b.get("model") or None, b.get("effort") or None,
                             b.get("client_id"), mode=mode, images=b.get("images"), tier=_tier(b))
    app.lens.mark_sent(sid, set(b.get("comment_ids") or []))
    app.lens.bus.publish(sid, {"type": "comments"})
    _note_recent(app, b)
    return out


@route("POST", "/api/sessions/(?P<sid>[^/]+)/queue/(?P<qid>[^/]+)/send")
def api_queue_send(app, h, q, sid, qid):
    link, hub = _hub_running(app, sid)
    if not hub:
        return app.lens.queue_send(sid, qid)
    # 이 앱의 대기열에 있던 말을 agent-hub 가 돌리는 턴에 넣는다 — 못 넣으면 제자리(맨 앞)로 돌려놓는다
    e = app.lens._queue_pop(sid, qid)
    if not e:
        raise KeyError(qid)
    try:
        return link.send(sid, hub, _hub_text(app, sid, e["text"], e.get("mentions")), e.get("images"), "steer", e.get("client_id"))
    except Exception:
        app.lens.queues.update(lambda d: d["threads"].setdefault(sid, []).insert(0, e))
        app.lens._queue_changed(sid)
        raise


@route("DELETE", "/api/sessions/(?P<sid>[^/]+)/queue/(?P<qid>[^/]+)")
def api_queue_remove(app, h, q, sid, qid):
    return app.lens.queue_remove(sid, qid)


@route("POST", "/api/sessions/(?P<sid>[^/]+)/interrupt")
def api_interrupt(app, h, q, sid):
    link, hub = _hub_running(app, sid)
    if hub:
        return link.interrupt(sid, hub)
    return app.lens.interrupt(sid)


def _hub_text(app, sid, text, mentions):
    """멘션한 파일·세션 안내를 이 앱이 턴을 열 때와 같게 글 뒤에 붙인다(agent-hub 는 글과 이미지만 받는다)."""
    return "".join(i["text"] for i in app.lens.build_input(text, mentions, sid) if i["type"] == "text")


def _hub_running(app, sid):
    """이 앱이 돌리는 턴이 없고 agent-hub·steward 가 그 세션의 턴을 돌리는 중이면 (그 연결, 채팅). 아니면 (None, None)."""
    if app.lens._running(sid):
        return None, None
    for link in (app.hub, app.steward):
        hub = link.chat_for(sid)
        if hub and hub["running"]:
            return link, hub
    return None, None


@route("GET", "/api/sessions/(?P<sid>[^/]+)/events")
def api_events(app, h, q, sid):
    h.sse(sid)
    return None


@route("POST", "/api/sessions/(?P<sid>[^/]+)/summaries")
def api_summary(app, h, q, sid):
    b = h.body()
    if not b.get("turn_id"):
        raise HttpError(400, "turn_id 가 필요합니다")
    return {"job": app.lens.jobs.start_summary(sid, b["turn_id"], b.get("request") or "", b.get("client_id"),
                                                b.get("effort") or None)}


@route("POST", "/api/sessions/(?P<sid>[^/]+)/questions")
def api_question(app, h, q, sid):
    b = h.body()
    if not b.get("turn_id"):
        raise HttpError(400, "turn_id 가 필요합니다")
    return {"job": app.lens.jobs.start_question(sid, b["turn_id"], b)}


@route("POST", "/api/refine")
def api_refine(app, h, q):
    return {"job": app.lens.jobs.start_refine(h.body())}


@route("GET", "/api/refine/prompts")
def api_refine_prompts(app, h, q):
    return {"prompts": {m: app.lens.refine_prompts.get(m) for m in refine.MODES}}


@route("POST", "/api/refine/prompts")
def api_refine_prompt_set(app, h, q):
    b = h.body()
    if b.get("mode") not in refine.MODES:
        raise ValueError("알 수 없는 다듬기 목적입니다")
    return {"prompt": app.lens.refine_prompts.set(b["mode"], b.get("text"))}


@route("POST", "/api/refine/preview")
def api_refine_preview(app, h, q):
    """다듬기로 함께 보내는 내용 — 실제 작업과 같은 조립(refine.parts). text·prompt 를 주지 않으면 지금 저장된 지시로 짓는다."""
    b = h.body()
    mode = refine.mode_of(b.get("mode"))
    forked = bool(b.get("thread_id") and b.get("turn_id"))
    cwd = (b.get("cwd") or "").strip()
    if b.get("thread_id"):
        cwd = app.lens.session_meta(b["thread_id"]).get("cwd") or cwd
    prompt = b.get("prompt") if isinstance(b.get("prompt"), str) and b["prompt"].strip() else app.lens.refine_prompts.text(mode)
    return {"mode": mode, "forked": forked,
            "parts": refine.parts(b.get("text") or "", forked, cwd, refine.answers(b.get("answers"), answer_limit=4000), mode, prompt)}


@route("GET", "/api/jobs/(?P<jid>[^/]+)")
def api_job(app, h, q, jid):
    return {"job": app.lens.jobs.public(app.lens.jobs.get(jid))}


@route("POST", "/api/jobs/(?P<jid>[^/]+)/cancel")
def api_job_cancel(app, h, q, jid):
    return {"job": app.lens.jobs.cancel(jid)}


@route("POST", "/api/jobs/(?P<jid>[^/]+)/retry")
def api_job_retry(app, h, q, jid):
    old = app.lens.jobs.get(jid)
    b = h.body()
    if old["kind"] == "refine":
        body = {k: old.get(k) for k in ("text", "thread_id", "turn_id", "cwd", "model", "draft_key", "effort", "agent",
                                        "mode", "prompt", "tier", "refine_model")}
        body["answers"] = old.get("context_answers")
        if b.get("effort"):
            body["effort"] = b["effort"]
        job = app.lens.jobs.start_refine(body)
    elif old["kind"] == "summary":
        job = app.lens.jobs.start_summary(old["thread_id"], old["turn_id"], old.get("request") or "",
                                          effort=b.get("effort") or old.get("effort"))
    else:
        body = {k: old.get(k) for k in ("quote", "start", "part", "parent_id", "anchor", "effort")}
        if b.get("effort"):
            body["effort"] = b["effort"]
        body["question"] = b.get("question") or old.get("question")
        job = app.lens.jobs.start_question(old["thread_id"], old["turn_id"], body)
    app.lens.jobs.delete(jid)
    return {"job": job, "replaced": jid}


@route("PATCH", "/api/jobs/(?P<jid>[^/]+)")
def api_job_patch(app, h, q, jid):
    return {"job": app.lens.jobs.patch(jid, h.body())}


@route("DELETE", "/api/jobs/(?P<jid>[^/]+)")
def api_job_delete(app, h, q, jid):
    return {"deleted": app.lens.jobs.delete(jid)}


@route("GET", "/api/sessions/(?P<sid>[^/]+)/comments")
def api_comments(app, h, q, sid):
    return {"comments": app.lens.list_comments(sid)}


@route("POST", "/api/sessions/(?P<sid>[^/]+)/comments")
def api_comment_add(app, h, q, sid):
    c = app.lens.save_comment(sid, h.body())
    app.lens.bus.publish(sid, {"type": "comments"})
    return {"comment": c}


@route("PATCH", "/api/comments/(?P<cid>[^/]+)")
def api_comment_patch(app, h, q, cid):
    c = app.lens.patch_comment(app.lens.comments, cid, h.body())
    return {"comment": c}


@route("DELETE", "/api/comments/(?P<cid>[^/]+)")
def api_comment_delete(app, h, q, cid):
    return {"deleted": app.lens.delete_comment(app.lens.comments, cid)}


# --- FDE 케이스(fde.py) — 작업 폴더의 fde/case.json ------------------------
@route("GET", "/api/fde")
def api_fde(app, h, q):
    return app.fde.view(q.get("cwd"))


@route("POST", "/api/fde")
def api_fde_create(app, h, q):
    b = h.body()
    return app.fde.create(b.get("cwd"), b)


@route("POST", "/api/fde/step")
def api_fde_step(app, h, q):
    b = h.body()
    return app.fde.step(b.get("cwd"), b)


@route("POST", "/api/fde/act/(?P<action>[a-z_]+)")
def api_fde_act(app, h, q, action):
    b = h.body()
    return app.fde.act(b.get("cwd"), action, b)


@route("POST", "/api/fde/run")
def api_fde_run(app, h, q):
    b = h.body()
    ids = b.get("ids") if isinstance(b.get("ids"), list) else []
    return app.fde.run(b.get("cwd"), [str(x) for x in ids])


@route("GET", "/api/files")
def api_files(app, h, q):
    return app.lens.list_files(q.get("cwd"), q.get("q") or "", _int(q.get("limit"), 30))


@route("GET", "/api/file")
def api_file(app, h, q):
    try:
        return app.lens.read_file(q.get("path"))
    except FileNotFoundError as e:
        raise HttpError(404, str(e))


@route("GET", "/api/tree")
def api_tree(app, h, q):
    return app.docs.tree(q.get("root"), q.get("dir") or "")


@route("GET", "/api/md-comments")
def api_md_comments(app, h, q):
    return {"comments": app.lens.list_md_comments(q.get("path") or "")}


@route("POST", "/api/md-comments")
def api_md_comment_add(app, h, q):
    b = h.body()
    if not b.get("path"):
        raise HttpError(400, "path 가 필요합니다")
    return {"comment": app.lens.save_md_comment(b["path"], b)}


@route("PATCH", "/api/md-comments/(?P<cid>[^/]+)")
def api_md_comment_patch(app, h, q, cid):
    return {"comment": app.lens.patch_comment(app.lens.md_comments, cid, h.body())}


@route("DELETE", "/api/md-comments/(?P<cid>[^/]+)")
def api_md_comment_delete(app, h, q, cid):
    return {"deleted": app.lens.delete_comment(app.lens.md_comments, cid)}


@route("GET", "/api/md-refines")
def api_md_refines(app, h, q):
    return {"refines": app.docs.list(q.get("path") or "")}


@route("POST", "/api/md-refines")
def api_md_refine(app, h, q):
    b = h.body()
    if not b.get("path"):
        raise HttpError(400, "path 가 필요합니다")
    out = app.docs.start(b)
    if b.get("session_id"):
        app.lens.bus.publish(b["session_id"], {"type": "comments"})
    return {"refine": out}


@route("POST", "/api/md-refines/(?P<rid>[^/]+)/revert")
def api_md_refine_revert(app, h, q, rid):
    return {"refine": app.docs.revert(rid)}


# --- 포트 관리 ------------------------------------------------------------
def _port_brief(e):
    return {k: e.get(k) for k in ("id", "name", "port", "state")}


def _port_out(app, e):
    out = {k: v for k, v in e.items() if k not in ("token", "pgid")}
    out["url"] = app.ports.url_of(e)
    tid = e.get("thread_id")
    if tid:
        m = app.lens.meta_cache.get(tid) or {}
        out["session_title"] = m.get("title")
    return out


def _thread_for_cwd(app, cwd):
    """CODEX_THREAD_ID 가 없을 때 — 이 앱이 만든 세션 중 작업 폴더가 가장 길게 겹치는 것."""
    if not cwd:
        return None
    cwd = os.path.realpath(cwd)
    best, n = None, 0
    for tid, e in (app.lens.registry.read().get("mine") or {}).items():
        c = os.path.realpath(e["cwd"]).rstrip("/") if e.get("cwd") else ""
        if c and (cwd == c or cwd.startswith(c + "/")) and len(c) > n:
            best, n = tid, len(c)
    return best


@route("GET", "/api/ports")
def api_ports(app, h, q):
    items = [_port_out(app, e) for e in app.ports.list(q.get("thread") or None)]
    return dict(app.ports.info(), items=items)


@route("POST", "/api/ports")
def api_port_run(app, h, q):
    b = h.body()
    tid = b.get("thread_id") or _thread_for_cwd(app, b.get("cwd"))
    e = app.ports.run(argv=b.get("argv"), sh=b.get("sh"), cwd=b.get("cwd"), env=b.get("env"), name=b.get("name"),
                      thread_id=tid, port=b.get("port"), replace=b.get("replace", True) is not False)
    return _port_out(app, e)


@route("GET", "/api/ports/free")
def api_port_free(app, h, q):
    return {"port": app.ports.free_port(), "bind": app.ports.bind}


@route("POST", "/api/ports/clear")
def api_port_clear(app, h, q):
    return app.ports.clear_done()


@route("GET", "/api/ports/(?P<pid>[^/]+)")
def api_port(app, h, q, pid):
    return _port_out(app, app.ports.get(pid))


@route("GET", "/api/ports/(?P<pid>[^/]+)/log")
def api_port_log(app, h, q, pid):
    return app.ports.log_tail(pid, _int(q.get("bytes"), 8000))


@route("POST", "/api/ports/(?P<pid>[^/]+)/stop")
def api_port_stop(app, h, q, pid):
    return _port_out(app, app.ports.stop(pid))


@route("DELETE", "/api/ports/(?P<pid>[^/]+)")
def api_port_delete(app, h, q, pid):
    return app.ports.remove(pid)


@route("POST", "/api/client-log")
def api_client_log(app, h, q):
    b = h.body()
    for ev in (b.get("events") or [])[:20]:
        if isinstance(ev, dict):
            applog.log("client_" + str(ev.get("type") or "event")[:40], level=str(ev.get("level") or "info")[:8],
                       **{k: (str(v)[:300] if isinstance(v, str) else v) for k, v in ev.items()
                          if k in ("ms", "route", "detail", "count", "error", "state", "agent", "bytes",
                                   "wait_ms", "read_ms", "download_ms", "render_ms")})
    return {"ok": True}


# --- HTTP -----------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "codex-lens/" + VERSION
    app = None

    def log_message(self, fmt, *args):
        pass

    def read_raw(self):
        """본문은 쓰든 안 쓰든 다 읽는다 — 남기면 keep-alive 연결의 다음 요청 앞에 붙는다."""
        n = int(self.headers.get("Content-Length") or 0)
        self.raw = self.rfile.read(n) if n > 0 else b""

    def body(self):
        raw = getattr(self, "raw", b"")
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise HttpError(400, "JSON 본문이 아닙니다")
        if not isinstance(data, dict):
            raise HttpError(400, "JSON 객체가 필요합니다")
        return data

    def send_json(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_bytes(self, status, data, ctype, cache="no-cache"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def sse(self, sid):
        lens = self.app.lens
        qu = lens.bus.subscribe(sid)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(b": hello\n\n")
            self.wfile.flush()
            with lens.lock:
                lt = lens.live.get(sid)
                snap = lt.snapshot() if lt and lt.turn_id else None
            if snap:
                self._event({"type": "live", "turn": snap})
            while True:
                try:
                    ev = qu.get(timeout=SSE_HEARTBEAT)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._event(ev)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            lens.bus.unsubscribe(sid, qu)

    def _event(self, ev):
        self.wfile.write(b"data: " + json.dumps(ev, ensure_ascii=False).encode("utf-8") + b"\n\n")
        self.wfile.flush()

    def do_GET(self):
        self.dispatch("GET")

    def do_HEAD(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        u = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(u.path)
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        self.read_raw()
        if not path.startswith("/api/"):
            return self.static(path)
        t0 = time.time()
        status, pattern = 200, None
        try:
            for m, rx, fn, pat in ROUTES:
                if m != method:
                    continue
                mt = rx.match(path)
                if mt:
                    pattern = pat
                    out = fn(self.app, self, q, **mt.groupdict())
                    if out is not None:
                        self.send_json(200, out)
                    return
            status = 404
            self.send_json(404, {"error": "없는 API 입니다: %s %s" % (method, path)})
        except HttpError as e:
            status = e.status
            self.send_json(e.status, {"error": str(e)})
        except HubError as e:
            status = e.status
            self.send_json(e.status, {"error": str(e)})
        except Busy as e:
            status = 409
            self.send_json(409, {"error": str(e)})
        except (ValueError, KeyError) as e:
            status = 400 if isinstance(e, ValueError) else 404
            msg = str(e) if isinstance(e, ValueError) else "찾지 못했습니다: %s" % e
            self.send_json(status, {"error": msg})
        except RpcError as e:
            status = 502
            self.send_json(502, {"error": "에이전트: %s" % e})
        except (BrokenPipeError, ConnectionResetError):
            status = 499
        except Exception as e:
            status = 500
            applog.log("api_error", level="error", path=pattern or path, error=repr(e)[:300],
                       trace=traceback.format_exc()[-1500:])
            try:
                self.send_json(500, {"error": "서버 오류: %s" % e})
            except OSError:
                pass
        finally:
            if pattern and not pattern.endswith("/events"):
                ms = int((time.time() - t0) * 1000)
                applog.log("api", level="warn" if status >= 500 or ms > 3000 else "info", method=method,
                           route=pattern, status=status, ms=ms)

    def static(self, path):
        if path in ("/", "/index.html"):
            return self.send_bytes(200, self.app.index_html().encode("utf-8"), "text/html; charset=utf-8")
        if path == "/manifest.webmanifest":
            return self.send_bytes(200, json.dumps(self.app.manifest(), ensure_ascii=False).encode("utf-8"),
                                   "application/manifest+json")
        if path == "/favicon.ico":
            path = "/icons/%sfavicon-32.png" % ("demo-" if DEMO else "")
        base = None
        if path.startswith("/static/"):
            base = os.path.join(ROOT, "static")
            rel = path[len("/static/"):]
        elif path.startswith("/icons/"):
            base = os.path.join(ROOT, "icons")
            rel = path[len("/icons/"):]
        if base:
            full = os.path.normpath(os.path.join(base, rel))
            if full.startswith(base + os.sep) and os.path.isfile(full):
                ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype in ("application/javascript",):
                    ctype += "; charset=utf-8"
                with open(full, "rb") as f:
                    return self.send_bytes(200, f.read(), ctype)
        self.send_bytes(404, "없는 파일입니다".encode("utf-8"), "text/plain; charset=utf-8")


def serve(app, host=HOST, port=PORT):
    Handler.app = app
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    applog.log("server_start", host=host, port=port, demo=DEMO, home=HOME)
    # bin/lens-port 가 CODEX_LENS_URL 없이 불려도 앱을 찾게 한다
    try:
        with open(os.path.join(HOME, "server.json"), "w") as f:
            json.dump({"url": api_base(host, port), "pid": os.getpid(), "version": VERSION, "demo": DEMO}, f)
    except OSError:
        pass
    print("%s %s — http://%s:%d (%s)" % (("[데모] " if DEMO else "") + APP_NAME, VERSION, host, port, HOME), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        applog.log("server_stop")
        app.ports.close()
        app.lens.client.close()


def main():
    has_codex = shutil.which(os.environ.get("CODEX_LENS_CODEX_BIN", "codex")) or os.environ.get("CODEX_LENS_CODEX_ARGV")
    has_claude = shutil.which(os.environ.get("CODEX_LENS_CLAUDE_BIN", "claude")) or os.environ.get("CODEX_LENS_CLAUDE_ARGV")
    if not has_codex:
        print("경고: codex 실행 파일을 찾지 못했습니다 — Codex 세션을 실행할 수 없습니다.", file=sys.stderr)
    if not has_claude:
        print("경고: claude 실행 파일을 찾지 못했습니다 — Claude Code 세션을 실행할 수 없습니다.", file=sys.stderr)
    serve(App())


if __name__ == "__main__":
    main()
