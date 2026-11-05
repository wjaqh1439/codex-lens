// steward 가 돌리는 세션(scripts/steward_demo.py 의 가짜 steward)에서 응답 중 선택줄·멈춤이 보이고 멈춤이 steward 로 가는지 본다(412×915).
// 앞에 BASE·OUT·SESSION 상수를 붙여 air-browser --script 로 돌린다.
const task = globalThis.airBrowserTask, page = task.page('p1');
const results = [], shotErrors = [];
function must(ok, message) { if (!ok) throw new Error(message); results.push(message); }
async function wait(fn, arg, timeout = 10000) { await page.waitForFunction(fn, arg, { timeout }); }
async function shot(name) {
  if (shotErrors.length >= 1) return;
  try { await page.screenshot({ path: OUT + '/' + name + '.png' }); } catch (e) { shotErrors.push(String(e)); }
}
const ui = () => page.evaluate(() => {
  const row = document.querySelector('.composer .steer-row'), stop = document.querySelector('.composer-bar button.stop');
  return { row: !row.hidden, lead: row.firstChild.textContent, stop: !stop.hidden, title: stop.title,
    fits: row.getBoundingClientRect().right <= innerWidth && row.scrollWidth <= row.clientWidth + 1 };
});
try {
  await page.cdp('Emulation.setDeviceMetricsOverride', { width: 412, height: 915, deviceScaleFactor: 1, mobile: true });
  await page.goto(BASE + '#/s/' + SESSION);
  await wait(() => !!document.querySelector('.composer .steer-row') && !document.querySelector('.composer .steer-row').hidden);
  const s = await ui();
  must(s.lead.includes('steward') && s.stop && s.title.includes('steward'), 'steward 가 돌리는 세션은 선택줄에 steward 가 적히고 멈춤이 보인다 ' + JSON.stringify(s));
  must(s.fits, '412 폭에서 선택줄이 넘치지 않는다');
  await shot('steward-1-running');
  if (KEEP) { console.log(JSON.stringify({ ok: true, results, shotErrors }, null, 1)); }
  else {
    await page.click('css=.composer-bar button.stop', { label: '멈춤' });
    await wait(() => /멈추라고 보냈습니다/.test(document.getElementById('toast').textContent));
    must(true, '멈춤을 누르면 steward 로 보내고 알린다');
    await wait(() => document.querySelector('.composer-bar button.stop').hidden, undefined, 15000);
    must(true, 'steward 세션이 멈춘 뒤 멈춤 단추가 사라진다');
    await shot('steward-2-stopped');
    console.log(JSON.stringify({ ok: true, results, shotErrors }, null, 1));
  }
} catch (e) {
  await shot('steward-fail');
  console.log(JSON.stringify({ ok: false, error: String(e && e.stack || e), results, shotErrors }, null, 1));
  throw e;
}
