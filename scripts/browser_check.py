#!/usr/bin/env python3
"""브라우저 회귀 — 가짜 app-server 데모를 띄우고 ego-browser 로 412×915 화면에서 핵심 흐름을 확인한다.

실행:  python3 scripts/browser_check.py   (ego-browser 가 PATH 에 있어야 한다)
배치만: 위 명령 끝에 --layout-only · 외부 허브 끼워 넣기만: --hub-only
세션 읽기 과정만: --read-only
산출물: output/qa/*.png, output/qa/result.json. 실패하면 종료 코드 1. 서버는 성공·실패와 관계없이 내린다.
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "output", "qa")
HUB_SESSION = "c1a0de00-0000-4000-8000-000000000003"   # tests/seed.py 의 Claude Code 기록
sys.path.insert(0, os.path.join(ROOT, "tests"))
import seed  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import browser_runner  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class DownProxy:
    """앱 앞의 작은 역방향 프록시. /__qa/down?sec=N 이면 N초 동안 모든 요청에 502(글자 본문)를 돌려주고,
    /__qa/stats 는 그동안 앱에 실제로 넘긴 턴 보내기(POST …/turns) 수를 알려 준다. 이벤트 스트림도 조각째 흘려보낸다."""

    def __init__(self, target, trigger=None, trigger_sec=10.0, port=0):
        import http.client
        import http.server
        import threading
        import urllib.parse
        self.target = target
        self.down_until = 0.0
        self.turns = 0
        self.tripped = set()
        up = urllib.parse.urlsplit(target)
        me = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def reply(self, code, body, ctype="text/plain; charset=utf-8"):
                b = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def handle_any(self):
                n = int(self.headers.get("Content-Length") or 0)
                data = self.rfile.read(n) if n else None
                if self.path.startswith("/__qa/down"):
                    sec = float(urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("sec", ["5"])[0])
                    me.down_until = time.time() + sec
                    return self.reply(200, json.dumps({"ok": True}), "application/json")
                if self.path.startswith("/__qa/stats"):
                    return self.reply(200, json.dumps({"turns": me.turns}), "application/json")
                if time.time() < me.down_until:
                    return self.reply(502, "Bad Gateway")
                is_turn = self.command == "POST" and self.path.split("?")[0].endswith("/turns")
                # 데모: 처음 보낸 글에 trigger 낱말이 있으면 앱이 그 순간 다시 뜨기 시작한 것처럼 trigger_sec 초 내려간다
                if is_turn and trigger and data and trigger in data.decode("utf-8", "replace"):
                    cid = (json.loads(data or b"{}").get("client_id") or "")
                    if cid not in me.tripped:
                        me.tripped.add(cid)
                        me.down_until = time.time() + trigger_sec
                        return self.reply(502, "Bad Gateway")
                if is_turn:
                    me.turns += 1
                c = http.client.HTTPConnection(up.hostname, up.port, timeout=600)
                hdr = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection", "accept-encoding")}
                c.request(self.command, self.path, body=data, headers=hdr)
                r = c.getresponse()
                self.send_response(r.status)
                chunked = r.getheader("Content-Length") is None
                for k, v in r.getheaders():
                    if k.lower() not in ("connection", "transfer-encoding", "keep-alive"):
                        self.send_header(k, v)
                if chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    while True:
                        b = r.read1(65536)
                        if not b:
                            break
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(b), b) if chunked else b)
                        self.wfile.flush()
                    if chunked:
                        self.wfile.write(b"0\r\n\r\n")
                except OSError:
                    self.close_connection = True
                c.close()

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = handle_any

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
        self.srv.daemon_threads = True
        self.url = "http://127.0.0.1:%d/" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layout-only", action="store_true", help="나란히 보기·머리글·모바일 전환만 검사")
    parser.add_argument("--images-only", action="store_true")
    parser.add_argument("--archive-only", action="store_true")
    parser.add_argument("--speed-only", action="store_true", help="Codex·Claude 텍스트 생성 속도·대기·완료 표시 검사")
    parser.add_argument("--external", action="store_true", help="목록 상태·이 앱 밖에서 도는 세션만 검사")
    parser.add_argument("--sessions-only", action="store_true", help="세션 목록 새로고침·신규 세션 갱신만 검사")
    parser.add_argument("--web-only", action="store_true", help="웹 보기의 세션 복귀·닫기·페이지 메뉴 검사")
    parser.add_argument("--url", help="이미 lens-port로 띄운 격리된 가짜 데모 주소")
    parser.add_argument("--read-only", action="store_true", help="세션 첫 읽기의 과정 표시(단계·지연·멈추기·다시 읽기)만 검사")
    parser.add_argument("--mentions-only", action="store_true", help="멘션 추천창의 위치·스크롤·화면 크기 변경 검사")
    parser.add_argument("--hub-only", action="store_true", help="agent-hub 가 돌리는 세션의 바로 넣기·턴 끝나고·멈춤 검사(가짜 허브)")
    parser.add_argument("--outputs-only", action="store_true", help="최종 출력 카드(할 일·정할 것·메인 반영)의 선택·보존·전송 검사")
    parser.add_argument("--restart-only", action="store_true", help="서버 재시작 중 띠·요청 자동 재전송 검사(서버를 내렸다 올린다)")
    parser.add_argument("--fde-only", action="store_true", help="FDE 케이스 화면(#/case)의 단계 · 선택지 · 피드백 · 검증 · 뒤로 가기 검사")
    parser.add_argument("--resend-only", action="store_true", help="앱이 다시 뜨는 동안(앞단 502) 보낸 글이 돌아오면 한 번만 다시 가는지 검사")
    args = parser.parse_args()
    os.makedirs(OUT, exist_ok=True)
    for f in os.listdir(OUT):
        if f.endswith(".png") or f in ("result.json", "restart-ctl"):
            os.unlink(os.path.join(OUT, f))
    port = None if args.url else free_port()
    home = tempfile.mkdtemp(prefix="codex-lens-qa-")
    # CODEX_LENS_FEEDBACK_ORIGIN 을 주면 그 의견 위젯을 붙여 입력창 단추와 겹치지 않는지 본다(없으면 겹침 검사만 건너뛴다)
    env = dict(os.environ, FAKE_CODEX_DELAY="0.3", FAKE_CODEX_SLOW_SEC="20", FAKE_CODEX_PERSIST_DELAY="4",
               FAKE_CLAUDE_DELAY="0.3", FAKE_CLAUDE_SLOW_SEC="20")
    if args.speed_only:
        env.update(FAKE_STREAM_DELAY='0.2', FAKE_STREAM_PAUSE_SEC='3', FAKE_STREAM_REPEAT='3')
    log = open(os.path.join(OUT, "server.log"), "w")
    if args.url and args.restart_only:
        parser.error("--restart-only 는 서버를 직접 내렸다 올려야 해서 --url 과 함께 쓸 수 없다")
    srv = hub = None
    hub_url = ""
    if args.hub_only and not args.url:
        # 시드의 Claude Code 기록을 agent-hub 가 돌리는 중이라고 알려 주는 가짜 허브
        hub = subprocess.Popen([sys.executable, os.path.join(ROOT, "tests", "fake_hub.py"), "--session", HUB_SESSION],
                               stdout=subprocess.PIPE, stderr=log, text=True)
        hub_url = json.loads(hub.stdout.readline())["url"]
        env["CODEX_LENS_HUB_URL"] = hub_url

    def start_server():
        cmd = [sys.executable, os.path.join(ROOT, "scripts", "run_fake_demo.py"), "--home", home, "--port", str(port)]
        if args.external:
            cmd.append("--external")
        if args.outputs_only:
            extra = os.path.join(home, "output-seed.json")
            with open(extra, "w") as f:
                json.dump({"threads": seed.output_threads(os.path.join(home, "sample-project"))}, f, ensure_ascii=False)
            cmd += ["--extra-seed", extra]
        p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env)
        print("서버 pid %d · 포트 %d · 홈 %s" % (p.pid, port, home), flush=True)
        return p

    def wait_up():
        for _ in range(200):
            try:
                urllib.request.urlopen(url + "api/info", timeout=1).read()
                return True
            except OSError:
                time.sleep(0.1)
        return False

    def stop_server(p):
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()

    if not args.url:
        srv = start_server()
    stop_ctl = threading.Event()

    def restart_ctl():
        # e2e_restart.mjs 가 OUT/restart-ctl 에 "stop"·"start" 를 쓰면 앱 서버를 내리고 올린 뒤 "stopped"·"started" 로 답한다
        nonlocal srv
        path = os.path.join(OUT, "restart-ctl")
        while not stop_ctl.is_set():
            try:
                with open(path) as f:
                    cmd = f.read().strip()
            except OSError:
                cmd = ""
            if cmd == "stop":
                stop_server(srv)
                with open(path, "w") as f:
                    f.write("stopped")
            elif cmd == "start":
                srv = start_server()
                with open(path, "w") as f:
                    f.write("started" if wait_up() else "failed")
            time.sleep(0.1)

    try:
        url = args.url.rstrip("/") + "/" if args.url else "http://127.0.0.1:%d/" % port
        wait_up()
        if args.restart_only:
            threading.Thread(target=restart_ctl, daemon=True).start()
        proxy = None
        if args.resend_only:
            # tailscale serve 처럼 앞단에 서서, 앱이 내려간 동안은 글자 본문의 502 를 돌려준다
            proxy = DownProxy(url)
            url = proxy.url
        fde_cwd = os.path.join(home, "plant-inspection")
        if args.fde_only:
            subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "fde_seed.py"), fde_cwd], check=True)
        script_name = "e2e_fde.mjs" if args.fde_only else "e2e_hub.mjs" if args.hub_only else "e2e_speed.mjs" if args.speed_only else "e2e_outputs.mjs" if args.outputs_only else "e2e_archive.mjs" if args.archive_only else "e2e_restart.mjs" if args.restart_only else "e2e_resend.mjs" if args.resend_only else "e2e_external.mjs" if args.external else "e2e_web_navigation.mjs" if args.web_only else "e2e_sessions.mjs" if args.sessions_only else "e2e_mentions.mjs" if args.mentions_only else "e2e_read.mjs" if args.read_only else "e2e_images.mjs" if args.images_only else "e2e_browser.mjs"
        with open(os.path.join(ROOT, "scripts", script_name)) as f:
            body = f.read()
        head = "const BASE = %s;\nconst OUT = %s;\nconst PROJ = %s;\nconst LAYOUT_ONLY = %s;\nconst HUB = %s;\nconst HUB_SESSION = %s;\nconst FDE_CWD = %s;\n" % (
            json.dumps(url), json.dumps(OUT), json.dumps(os.path.join(home, "sample-project")), json.dumps(args.layout_only),
            json.dumps(hub_url), json.dumps(HUB_SESSION), json.dumps(fde_cwd))
        script = os.path.join(OUT, "run.mjs")
        with open(script, "w") as f:
            f.write(head + body)
        rc = browser_runner.run(script, "[데모] codex-lens 회귀")
        print("브라우저 회귀 종료 코드", rc, flush=True)
        return 1 if rc else 0
    finally:
        stop_ctl.set()
        if args.resend_only and proxy:
            url = proxy.target
            proxy.close()
        # 회귀가 띄운 포트(lens-port)는 앱과 따로 돌므로 앱보다 먼저 끈다
        if srv:
            try:
                with urllib.request.urlopen(url + "api/ports", timeout=3) as r:
                    for e in json.load(r)["items"]:
                        if e["state"] in ("starting", "listening"):
                            urllib.request.urlopen(urllib.request.Request(url + "api/ports/%s/stop" % e["id"], data=b"{}", method="POST"), timeout=10).read()
            except (OSError, ValueError, KeyError):
                pass
            srv.terminate()
            try:
                srv.wait(5)
            except subprocess.TimeoutExpired:
                srv.kill()
        if hub:
            hub.terminate()
            hub.wait(5)
        log.close()
        # 실패를 조사할 수 있게 앱 로그(app.jsonl)는 산출물로 남긴다
        applog = os.path.join(home, "lens", "logs", "app.jsonl")
        if os.path.exists(applog):
            shutil.copy(applog, os.path.join(OUT, "app.jsonl"))
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
