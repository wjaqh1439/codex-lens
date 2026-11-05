"""웹 보기 주소 풀기 — 답변 속 `http://127.0.0.1:<포트>` 같은 이 기기 주소를 다른 기기에서도 열리는 주소로 바꾼다.

127.0.0.1·localhost 는 화면을 보는 기기 자신을 가리켜, 휴대폰에서 누르면 앱 서버 기기의 데모 서버에 닿지 않는다.
이 기기(앱 서버)의 포트면 tailscale serve 의 HTTPS 매핑(`https://<호스트>:<포트> → http://127.0.0.1:<포트>`)을 찾아 그 주소로 바꾼다.
매핑을 새로 만들지는 않는다. 매핑이 없으면(tailscale 이 없는 기기 등) 화면이 앱을 연 주소를 본다 —
e2b `https://8761-<샌드박스>.e2b.app` 처럼 호스트 이름에 앱 포트가 들어 있으면 그 자리를 대상 포트로 바꾸고,
아니면 같은 호스트 이름으로 그 포트에 닿을 때 `http://<그 호스트>:<포트>` 로 바꾼다(앱을 0.0.0.0 에 묶으면 lens-port
서버도 0.0.0.0 에 묶인다). 그것도 안 되면 바꾸지 않고 이유만 돌려준다.
"""
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse

CACHE_SEC = 10
_cache = {"at": 0.0, "val": None}
_lock = threading.Lock()


def tailscale_bin():
    for p in (shutil.which("tailscale"), "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
        if p and os.path.exists(p):
            return p
    return None


def _run_json(args):
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=3)
        return json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def tailscale_info():
    """(serve 상태, 이 기기 정보) — 10초 캐시. tailscale 이 없으면 (None, None)."""
    with _lock:
        if _cache["val"] is not None and time.time() - _cache["at"] < CACHE_SEC:
            return _cache["val"]
    ts = tailscale_bin()
    val = (None, None)
    if ts:
        status = _run_json([ts, "status", "--json"]) or {}
        me = status.get("Self") or {}
        serve = _run_json([ts, "serve", "status", "--json"]) or {}
        val = (serve, {"dns": (me.get("DNSName") or "").rstrip("."), "ips": me.get("TailscaleIPs") or []})
    with _lock:
        _cache.update(at=time.time(), val=val)
    return val


def forget_cache():
    with _lock:
        _cache.update(at=0.0, val=None)


def port_up(port, timeout=0.5):
    """이 기기에서 그 포트가 받는가 — IPv4·IPv6 루프백을 다 본다(Vite 같은 Node 서버는 localhost 를 ::1 에만 붙이기도 한다)."""
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def is_loopback(host):
    h = (host or "").strip("[]").lower()
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def is_unspecified(host):
    """0.0.0.0·:: — `python3 -m http.server` 가 'http://0.0.0.0:8000/' 으로 알려 주는 주소. 브라우저는 열지 않는다."""
    try:
        return ipaddress.ip_address((host or "").strip("[]")).is_unspecified
    except ValueError:
        return False


def is_self(host, me):
    """이 기기를 가리키는 호스트인가 — 루프백, 0.0.0.0, 이 기기의 tailscale IP·이름."""
    h = (host or "").strip("[]").lower()
    if is_loopback(h) or is_unspecified(h):
        return True
    if not me:
        return False
    dns = (me.get("dns") or "").lower()
    return h in [ip.lower() for ip in me.get("ips") or []] or (dns and (h == dns or h == dns.split(".")[0]))


def https_for_port(serve, port):
    """serve 상태에서 `/` 를 http://127.0.0.1:<port>(또는 localhost)로 넘기는 HTTPS 주소의 origin."""
    targets = {"http://127.0.0.1:%d" % port, "http://localhost:%d" % port, "http://[::1]:%d" % port}
    tcp = (serve or {}).get("TCP") or {}
    for hostport, web in sorted(((serve or {}).get("Web") or {}).items()):
        root = ((web or {}).get("Handlers") or {}).get("/") or {}
        if (root.get("Proxy") or "").rstrip("/") not in targets:
            continue
        sport = hostport.rsplit(":", 1)[-1]
        if not (tcp.get(sport) or {}).get("HTTPS"):
            continue
        return "https://" + (hostport if sport != "443" else hostport.rsplit(":", 1)[0])
    return None


def reachable(host, port, timeout=0.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def split_host(host_header):
    """`Host` 헤더 → 호스트 이름(대괄호 없는 IPv6 포함). 이상하면 None."""
    try:
        return urllib.parse.urlsplit("//" + (host_header or "").strip()).hostname
    except ValueError:
        return None


def _is_ip(h):
    try:
        ipaddress.ip_address((h or "").strip("[]"))
        return True
    except ValueError:
        return False


def proxied_origin(via_origin, app_port, port):
    """앱을 연 주소가 포트 번호를 호스트 이름에 담는 프록시면 그 자리만 바꾼 origin — 아니면 None.
    e2b `https://8761-<샌드박스>.e2b.app`, Codespaces `https://<이름>-8761.app.github.dev`,
    Gitpod `https://8761-<id>.<지역>.gitpod.io` 처럼 첫 이름 칸에 앱 포트가 숫자 낱말로 들어 있을 때만 본다."""
    try:
        v = urllib.parse.urlsplit(via_origin or "")
        host = v.hostname
    except ValueError:
        return None
    if v.scheme not in ("http", "https") or not host or not app_port or _is_ip(host) or is_loopback(host):
        return None
    first, dot, rest = host.partition(".")
    pat = r"(?<![0-9])%d(?![0-9])" % int(app_port)
    if not dot or len(re.findall(pat, first)) != 1:
        return None
    netloc = re.sub(pat, str(int(port)), first) + "." + rest + (":%d" % v.port if v.port else "")
    return "%s://%s" % (v.scheme, netloc)


def resolve(url, info=None, up=None, public_for=None, via=None, app_port=None, reach=None):
    """→ {"url": 열 주소, "changed": bool, "reason": None|"bad"|"down"|"no_https"|"no_tailscale", "local": bool}.
    public_for(port) 는 포트 관리(ports.py)가 그 포트에 붙인 바깥 주소 — 있으면 tailscale 매핑보다 먼저 쓴다.
    via 는 화면이 앱을 연 origin(location.origin), app_port 는 앱이 듣는 포트 — HTTPS 매핑이 없으면
    ① 포트를 호스트 이름에 담는 프록시(e2b 등)면 그 자리를 바꾼 주소 ② 아니면 같은 호스트 이름으로 그 포트에 닿는지 본다.
    info·up·reach 는 시험용으로 바꿔 끼운다(기본은 tailscale CLI·실제 포트 접속)."""
    orig = url
    try:
        u = urllib.parse.urlsplit(url or "")
        port = u.port
    except ValueError:
        return {"url": url, "changed": False, "reason": "bad", "local": False}
    if u.scheme not in ("http", "https") or not u.hostname:
        return {"url": url, "changed": False, "reason": "bad", "local": False}
    serve, me = info if info is not None else tailscale_info()
    if not is_self(u.hostname, me):
        return {"url": url, "changed": False, "reason": None, "local": False}
    if port is None:
        port = 443 if u.scheme == "https" else 80
    # 0.0.0.0 은 이 기기 루프백으로 바꿔 둔다(HTTPS 주소를 못 찾았을 때 이 주소로 연다)
    if is_unspecified(u.hostname):
        url = urllib.parse.urlunsplit((u.scheme, "127.0.0.1:%d" % port, u.path or "/", u.query, u.fragment))
    # 이미 tailscale HTTPS 이름으로 된 주소면 그대로 연다
    if u.scheme == "https" and me and u.hostname.lower() == (me.get("dns") or "").lower():
        return {"url": url, "changed": False, "reason": None, "local": True}
    if not (up or port_up)(port):
        return {"url": url, "changed": url != orig, "reason": "down", "local": True}
    rest = urllib.parse.urlunsplit(("", "", u.path or "/", u.query, u.fragment))
    pub = public_for(port) if public_for else None
    if pub:
        p = urllib.parse.urlsplit(pub)
        if p.scheme in ("http", "https") and p.netloc:
            return {"url": "%s://%s%s" % (p.scheme, p.netloc, rest), "changed": True, "reason": None, "local": True}
    origin = https_for_port(serve, port) if serve is not None else None
    if origin:
        return {"url": origin + rest, "changed": True, "reason": None, "local": True}
    prox = proxied_origin(via, app_port, port)
    if prox:
        return {"url": prox + rest, "changed": True, "reason": None, "local": True, "via": "proxy"}
    try:
        vh = urllib.parse.urlsplit(via or "").hostname or ""
    except ValueError:
        vh = ""
    if vh and not is_loopback(vh) and not is_unspecified(vh) and (reach or reachable)(vh, port):
        netloc = ("[%s]" % vh if ":" in vh else vh) + ":%d" % port
        return {"url": "http://" + netloc + rest, "changed": True, "reason": None, "local": True, "via": "host"}
    reason = "no_tailscale" if serve is None else "no_https"
    return {"url": url, "changed": url != orig, "reason": reason, "local": True}
