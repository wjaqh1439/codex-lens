// browser_check.py --restart-only: 앱 서버가 내려간 사이에 저장하면 "서버 재시작 중" 띠가 뜨고,
// 서버가 다시 뜨면 띠가 사라지며 같은 요청이 자동으로 다시 나가 저장되는지 본다(412×915).
// 서버를 내리고 올리는 일은 browser_check.py 가 OUT/restart-ctl 파일을 보고 한다("stop" → "stopped", "start" → "started").
const fs = await import('node:fs/promises');
const task = globalThis.browserTask, page = task.page('p1');
const results = [], shotErrors = [];
function must(ok, message) { if (!ok) throw new Error('단언 실패: ' + message); results.push('ok ' + message); }
async function wait(fn, arg, timeout = 15000) { await page.waitForFunction(fn, arg, { timeout }); }
async function shot(name) {
  if (shotErrors.length >= 1) return;
  try { await page.screenshot({ path: OUT + '/' + name + '.png' }); }
  catch (e) { shotErrors.push(String(e)); }
}
const CTL = OUT + '/restart-ctl';
async function ctl(cmd, done, timeout = 20000) {
  await fs.writeFile(CTL, cmd);
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    if ((await fs.readFile(CTL, 'utf8').catch(() => '')).trim() === done) return;
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error('서버 ' + cmd + ' 응답 없음');
}
const settings = () => page.evaluate(async () => (await (await fetch('/api/models')).json()).settings);
try {
  await page.cdp('Emulation.setDeviceMetricsOverride', { width: 412, height: 915, deviceScaleFactor: 1, mobile: true });
  await page.goto(BASE + '#/models');
  await wait(() => !!document.querySelector('.model-row[data-model="gpt-fake-sol"]'));
  must(await page.evaluate(() => !document.querySelector('#server-down:not([hidden])')), '서버가 떠 있을 때는 재시작 띠가 없다');

  await ctl('stop', 'stopped');
  const tClick = Date.now();
  await page.evaluate(() => document.querySelector('.model-row[data-model="gpt-fake-sol"] .opt').click());
  await wait(() => { const b = document.getElementById('server-down'); return b && !b.hidden && /서버 재시작 중/.test(b.textContent); }, undefined, 5000);
  must(Date.now() - tClick < 3000, '서버가 내려간 사이 저장하면 3초 안에 "서버 재시작 중" 띠가 뜬다 (' + (Date.now() - tClick) + 'ms)');
  await wait(() => /요청 1건을 다시 보냅니다/.test(document.getElementById('server-down').textContent) &&
    /[1-9]초/.test(document.getElementById('server-down').textContent), undefined, 5000);
  const bar = await page.evaluate(() => { const b = document.getElementById('server-down'), r = b.getBoundingClientRect();
    return { text: b.textContent, top: r.top, left: r.left, right: r.right, height: r.height, cancelH: b.querySelector('.sd-cancel').getBoundingClientRect().height,
      toast: !document.getElementById('toast').hidden ? document.getElementById('toast').textContent : '' }; });
  must(bar.top >= 0 && bar.left >= 0 && bar.right <= 412, '띠가 화면 안에 있다 ' + JSON.stringify(bar));
  must(bar.right - bar.left >= 360 && bar.height <= 64, '412 폭에서 띠가 좁게 접히지 않고 두 줄 안에 든다 ' + JSON.stringify(bar));
  must(bar.cancelH <= 44, '"그만 기다리기" 단추가 줄바꿈되지 않는다 ' + bar.cancelH);
  must(!/저장하지 못했습니다/.test(bar.toast), '기다리는 동안 "저장하지 못했습니다"가 뜨지 않는다 ' + bar.toast);
  await shot('restart-1-waiting');

  await ctl('start', 'started', 30000);
  await wait(() => document.getElementById('server-down').hidden, undefined, 10000);
  must(true, '서버가 다시 뜨면 띠가 사라진다');
  await wait(() => document.querySelector('.model-row[data-model="gpt-fake-sol"]')?.classList.contains('on'), undefined, 10000);
  const st = await settings();
  must(st.defaults.codex.model === 'gpt-fake-sol', '서버가 다시 뜬 뒤 같은 저장 요청이 자동으로 다시 나가 저장된다 ' + JSON.stringify(st.defaults.codex));
  await shot('restart-2-recovered');

  await page.evaluate(() => fetch('/api/models/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ agent: 'codex', model: '', effort: '', tier: '', hidden: [] }) }));
  await fs.writeFile(OUT + '/result.json', JSON.stringify({ ok: true, results, shotErrors }, null, 2));
  console.log('재시작 회귀 통과: ' + results.length + '개 단언');
} catch (e) {
  await fs.writeFile(OUT + '/result.json', JSON.stringify({ ok: false, error: String(e), results, shotErrors }, null, 2));
  throw e;
} finally {
  await task.finish({ keep: [] });
}
