#!/usr/bin/env python3
"""실행 위치 회귀와 같은 이름의 원본·워크트리 데모(--demo 면 검사 뒤에도 켜 둔다)."""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
import seed  # noqa: E402
sys.path.insert(0, str(ROOT / 'scripts'))
import browser_runner  # noqa: E402


def run(*args):
    return subprocess.check_output(args, text=True, cwd=ROOT).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo', action='store_true', help='검사 뒤에도 데모를 켜 둔다(0.0.0.0 에 묶는다)')
    args = parser.parse_args()
    out = ROOT / 'output' / 'worktree'
    home = out / ('demo' if args.demo else 'qa')
    home.mkdir(parents=True, exist_ok=True)
    seed_path, repo = seed.build(str(home))
    template = json.loads(Path(seed_path).read_text())['threads'][0]
    cases = [{'id': template['id'], 'kind': 'repository', 'label': '기본 워크트리', 'cwd': repo,
              'short': 'sample-project', 'title': template['preview']}]
    extra = []
    for i, ident in enumerate(['review-1234', 'review-5678'], 1):
        root = home / 'worktrees' / ident / 'sample-project'
        run('git', '-C', repo, 'worktree', 'add', '-q', '-b', ident, str(root))
        s = copy.deepcopy(template)
        s.update(id='01a1aaaa0000000000000000000001%02d' % i, cwd=str(root),
                 name='같은 폴더명 · ' + ident, gitInfo={'branch': ident})
        extra.append(s)
        cases.append({'id': s['id'], 'kind': 'worktree', 'label': '워크트리', 'cwd': str(root),
                      'repo': repo, 'short': ident + '/sample-project', 'title': s['name']})
    for i, kind in enumerate(['folder', 'unknown'], 3):
        folder = home / ('일반 폴더' if kind == 'folder' else '사라진 폴더')
        if kind == 'folder':
            folder.mkdir()
        s = copy.deepcopy(template)
        s.update(id='01a1aaaa0000000000000000000001%02d' % i, cwd=str(folder),
                 name='일반 폴더' if kind == 'folder' else '위치 확인 전', gitInfo=None)
        extra.append(s)
        cases.append({'id': s['id'], 'kind': kind, 'label': '폴더' if kind == 'folder' else '위치 확인 전',
                      'cwd': str(folder), 'short': folder.name, 'title': s['name']})
    extra_path = home / 'extra.json'
    extra_path.write_text(json.dumps({'threads': extra}))
    with browser_runner.FakeDemo(home, args=['--extra-seed', str(extra_path)], env={'CODEX_LENS_BROKER_IDLE_SEC': '1'},
                                 host='0.0.0.0' if args.demo else '127.0.0.1') as demo:
        base = demo.url
        info = {'host': os.uname().nodename, 'port': demo.port, 'source': str(ROOT), 'home': str(home),
                'url': base + '#/s/' + cases[1]['id']}
        (out / ('demo.json' if args.demo else 'qa.json')).write_text(json.dumps(info, indent=2))
        print(json.dumps(info), flush=True)
        with urllib.request.urlopen(base, timeout=15) as response:
            assert response.status == 200
        script = out / 'run.mjs'
        script.write_text('const BASE=%s, OUT=%s, CASES=%s;\n' %
                          (json.dumps(base), json.dumps(str(out)), json.dumps(cases)) +
                          (ROOT / 'scripts/e2e_worktree.mjs').read_text())
        rc = browser_runner.run(str(script), '[데모] Codex Lens 실행 위치')
        if args.demo:
            print('데모를 켜 둡니다(Ctrl-C 로 끈다): ' + info['url'], flush=True)
            try:
                demo.proc.wait()
            except KeyboardInterrupt:
                pass
        return rc

if __name__ == '__main__':
    sys.exit(main())
