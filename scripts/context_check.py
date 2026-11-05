#!/usr/bin/env python3
"""컨텍스트 회귀 또는 지속 데모. 주소·데이터 위치를 output/context 에 남긴다(--demo 면 검사 뒤에도 켜 둔다)."""
import argparse
import json
import os
from pathlib import Path
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import browser_runner  # noqa: E402
S1 = '01a1aaaa000000000000000000000001'
S2 = '01a1aaaa000000000000000000000002'


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--demo', action='store_true', help='검사 뒤에도 데모를 켜 둔다(0.0.0.0 에 묶는다)')
    args = ap.parse_args()
    out = ROOT / 'output' / 'context'
    out.mkdir(parents=True, exist_ok=True)
    home = out / ('demo' if args.demo else 'qa')
    data = home / 'lens'
    data.mkdir(parents=True, exist_ok=True)
    # 운영 기록과 분리된 합성 사용량. 서버를 다시 띄워도 JsonStore가 복원하는 경로다.
    (data / 'context.json').write_text(json.dumps({'threads': {
        S1: {'tokens': 108528, 'window': 258400, 'percent': 42},
        S2: {'tokens': 42000, 'window': None, 'percent': None}}}))
    with browser_runner.FakeDemo(home, env={'FAKE_CODEX_CONTEXT_TOKENS': '108528', 'FAKE_CODEX_SLOW_SEC': '3',
                                            'CODEX_LENS_BROKER_IDLE_SEC': '1'},
                                 host='0.0.0.0' if args.demo else '127.0.0.1') as demo:
        base = demo.url
        info = {'host': os.uname().nodename, 'port': demo.port, 'home': str(home), 'source': str(ROOT),
                'url': base + '#/s/' + S1}
        (out / ('demo.json' if args.demo else 'qa.json')).write_text(json.dumps(info, indent=2))
        print(json.dumps(info), flush=True)
        with urllib.request.urlopen(base, timeout=15) as res:
            assert res.status == 200 and 'session-context' in res.read().decode()
        script = out / 'run.mjs'
        script.write_text('const BASE = %s, OUT = %s;\n' % (json.dumps(base), json.dumps(str(out))) +
                          (ROOT / 'scripts/e2e_context.mjs').read_text())
        rc = browser_runner.run(str(script), '[데모] Codex Lens 컨텍스트')
        if args.demo:
            print('데모를 켜 둡니다(Ctrl-C 로 끈다): ' + info['url'], flush=True)
            try:
                demo.proc.wait()
            except KeyboardInterrupt:
                pass
        return rc

if __name__ == '__main__':
    sys.exit(main())
