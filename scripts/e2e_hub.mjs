// browser_check.py --hub-only: agent-hub 가 돌리는 세션(가짜 허브)에서 바로 넣기·턴 끝나고·멈춤이 agent-hub 로 가는지 본다.
const fs = await import('node:fs/promises');
const task = globalThis.browserTask, page = task.page('p1');
const results = [], shotErrors = [];
function must(ok, message) { if (!ok) throw new Error(message); results.push(message); }
async function wait(fn, arg, timeout = 10000) { await page.waitForFunction(fn, arg, { timeout }); }
async function shot(name) {
  if (shotErrors.length >= 1) return;
  try { await page.screenshot({ path: OUT + '/' + name + '.png' }); }
  catch (e) { shotErrors.push(String(e)); }
}
async function calls() { return (await (await fetch(HUB + '/calls')).json()).calls; }
async function setHub(body) { await fetch(HUB + '/set', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); }
async function until(fn, label, timeout = 10000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) { const v = await fn(); if (v) return v; await new Promise((r) => setTimeout(r, 150)); }
  throw new Error('시간 안에 맞지 않음: ' + label);
}
const ui = () => page.evaluate(() => {
  const row = document.querySelector('.composer .steer-row'), send = document.querySelector('.composer-bar button.primary');
  const stop = document.querySelector('.composer-bar button.stop'), r = row.getBoundingClientRect();
  return { row: !row.hidden, lead: row.firstChild.textContent, send: send.textContent, stop: !stop.hidden,
    modes: [...row.querySelectorAll('.mode-seg button')].map((b) => b.textContent + ':' + b.getAttribute('aria-pressed')),
    fits: r.right <= innerWidth && row.scrollWidth <= row.clientWidth + 1,
    queue: document.querySelector('.composer-wrap .queue, .queue')?.hidden === false ? document.querySelector('.queue').textContent : '' };
});
try {
  await page.cdp('Emulation.setDeviceMetricsOverride', { width: 412, height: 915, deviceScaleFactor: 1, mobile: true });
  await page.goto(BASE + '#/s/' + HUB_SESSION);
  await page.evaluate(() => localStorage.removeItem('codex-lens.sendMode'));
  await wait(() => !!document.querySelector('.composer .steer-row') && !document.querySelector('.composer .steer-row').hidden);
  let s = await ui();
  must(s.lead.includes('agent-hub') && s.stop && s.send === '넣기', 'agent-hub 가 돌리는 세션은 응답 중 선택줄·멈춤·넣기가 보인다 ' + JSON.stringify(s));
  must(s.fits, '412 폭에서 선택줄이 넘치지 않는다');
  await shot('hub-1-running');

  await page.fill('css=.composer textarea', '바로 넣는 말');
  await page.click('css=.composer-bar button.primary', { label: '넣기' });
  const c1 = await until(async () => (await calls()).find((c) => c.path.endsWith('/send') && c.body.text.startsWith('바로 넣는 말')), '바로 넣기 전송');
  must(c1.body.mode === 'steer' && c1.body.expected_session_id === HUB_SESSION, 'agent-hub 로 바로 넣기(steer)가 같은 세션 ID 로 간다');
  await wait(() => document.querySelector('.composer textarea').value === '' && /agent-hub/.test(document.getElementById('toast').textContent));
  must(true, '보낸 뒤 입력창이 비고 agent-hub 로 넣었다고 알린다');

  await page.evaluate(() => [...document.querySelectorAll('.composer .mode-seg button')].find((b) => b.textContent === '턴 끝나고').click());
  await wait(() => document.querySelector('.composer-bar button.primary').textContent === '대기열에');
  await page.fill('css=.composer textarea', '끝나면 보낼 말');
  await page.click('css=.composer-bar button.primary', { label: '대기열에' });
  const c2 = await until(async () => (await calls()).find((c) => c.path.endsWith('/send') && c.body.text.startsWith('끝나면 보낼 말')), '대기열 전송');
  must(c2.body.mode === 'queue', '턴 끝나고는 agent-hub 대기열(queue)로 간다');
  await wait(() => /agent-hub 에서 턴이 끝나면 차례로 보냄 · 1/.test(document.querySelector('.queue')?.textContent || ''), undefined, 8000);
  must(true, 'agent-hub 대기열 개수가 입력창 위에 보인다');
  await shot('hub-2-queued');

  await page.click('css=.composer-bar button.stop', { label: '멈춤' });
  await until(async () => (await calls()).some((c) => c.path.endsWith('/kill')), '멈춤 전송');
  must(true, '멈춤이 agent-hub 의 kill 로 간다');
  await wait(() => document.querySelector('.composer .steer-row').hidden && document.querySelector('.composer-bar button.primary').textContent === '보내기', undefined, 8000);
  s = await ui();
  must(!s.row && !s.stop && !s.queue, 'agent-hub 턴이 끝나면 선택줄·멈춤·대기열 표시가 사라지고 보내기로 돌아온다 ' + JSON.stringify(s));
  await setHub({ status: 'running', pending: 0 });
  await wait(() => !document.querySelector('.composer .steer-row').hidden, undefined, 15000);
  must(true, '다시 agent-hub 에서 돌기 시작하면 몇 초 안에 선택줄이 돌아온다');
  await fs.writeFile(OUT + '/result.json', JSON.stringify({ ok: true, results, shotErrors }, null, 2));
  console.log('agent-hub 끼워 넣기 회귀 통과: ' + results.length + '개 단언');
} catch (e) {
  await shot('hub-fail');
  await fs.writeFile(OUT + '/result.json', JSON.stringify({ ok: false, error: String(e), results, shotErrors }, null, 2));
  throw e;
} finally {
  await task.finish({ keep: [] });
}
