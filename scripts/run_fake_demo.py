#!/usr/bin/env python3
"""가짜 app-server·가짜 claude 로 데모 서버를 띄운다 — 브라우저 회귀와 모델 없이 화면을 볼 때 쓴다.

python3 scripts/run_fake_demo.py --home /tmp/x --port 19123
데모 표시([데모])·데모 저장소 분리는 진짜 데모와 같다. 시드는 tests/seed.py(견본 프로젝트 + Codex CLI 세션 두 개 + Claude Code 기록 하나).
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import seed  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 18761))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--external", action="store_true", help="목록 상태 시드(밖에서 도는 세션·멈춤·끊김·실패·시스템 오류)를 더한다")
    ap.add_argument("--extra-seed", help="시드에 더할 Codex 스레드 목록(JSON 파일, threads 키)")
    ap.add_argument("--tailscale", action="store_true", help="세션 서버를 tailscale serve 로 내보내는 앱 기본값을 그대로 쓴다")
    a = ap.parse_args()
    home = os.path.abspath(a.home)
    os.makedirs(home, exist_ok=True)
    seed_path, proj = seed.build(home, external=a.external)
    if a.extra_seed:
        with open(seed_path) as f:
            data = json.load(f)
        with open(a.extra_seed) as f:
            data["threads"].extend(json.load(f).get("threads") or [])
        with open(seed_path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    env = dict(os.environ, CODEX_LENS_DEMO="1", CODEX_LENS_HOME=os.path.join(home, "lens"), CODEX_LENS_PORT=str(a.port),
               CODEX_LENS_HOST=a.host, CODEX_LENS_DEMO_CODEX_HOME_KEEP="1", CODEX_HOME=os.path.join(home, "fake-codex-home"),
               CODEX_LENS_CODEX_ARGV=json.dumps([sys.executable, os.path.join(ROOT, "tests", "fake_app_server.py")]),
               FAKE_CODEX_SEED=seed_path, FAKE_CODEX_ROLLOUT_DIR=os.path.join(home, "fake-rollouts"), FAKE_CODEX_DELAY=os.environ.get("FAKE_CODEX_DELAY", "0.4"),
               # 가짜 Claude Code — 시드 기록(seed.build_claude)이 있는 설정 폴더를 그대로 쓴다
               CLAUDE_CONFIG_DIR=os.path.join(home, "claude-home"), CODEX_LENS_DEMO_CLAUDE_HOME_KEEP="1",
               CODEX_LENS_CLAUDE_ARGV=json.dumps([sys.executable, os.path.join(ROOT, "tests", "fake_claude.py")]),
               FAKE_CLAUDE_DELAY=os.environ.get("FAKE_CLAUDE_DELAY", os.environ.get("FAKE_CODEX_DELAY", "0.4")),
               CODEX_LENS_BROKER_IDLE_SEC=os.environ.get("CODEX_LENS_BROKER_IDLE_SEC", "60"),
               # 밖에서 도는 세션(--external)의 턴을 끝내는 신호 파일 — 스레드 ID 를 한 줄 적으면 끝난 것으로 보인다(상태는 `ID:failed`·`ID:interrupted`)
               FAKE_CODEX_EXTERNAL_END=os.path.join(home, "external-end"),
               CODEX_LENS_EXTERNAL_WATCH_SEC=os.environ.get("CODEX_LENS_EXTERNAL_WATCH_SEC", "1"))
    # 가짜 데모·회귀는 진짜 tailscale serve 매핑을 만들지 않는다(값을 주면 그대로, --tailscale 이면 앱 기본값)
    if a.tailscale:
        env.pop("CODEX_LENS_PORT_EXPOSE", None)
    else:
        env.setdefault("CODEX_LENS_PORT_EXPOSE", "")
    env.setdefault("CODEX_LENS_FEEDBACK_ORIGIN", "")
    print(json.dumps({"project": proj, "port": a.port}), flush=True)
    os.execve(sys.executable, [sys.executable, os.path.join(ROOT, "app.py")], env)


if __name__ == "__main__":
    main()
