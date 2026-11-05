// browser_check.py --resend-only: 앱이 다시 뜨는 동안(앞단 프록시 502) 보낸 글이 앱이 돌아오면 한 번만 다시 가는지 본다.
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
const qa = (path) => page.evaluate((p) => fetch(p, { cache: 'no-store' }).then((r) => r.json()), path);
const hint = () => page.evaluate(() => document.querySelector('.composer-hint').textContent);
try {
  await page.cdp('Emulation.setDeviceMetricsOverride', { width: 412, height: 915, deviceScaleFactor: 1, mobile: true });
  await page.goto(BASE + '#/s/01a1aaaa000000000000000000000002');
  await wait(() => !!document.querySelector('.composer textarea') && document.querySelectorAll('.turn').length >= 1);
  const before = await page.evaluate(() => document.querySelectorAll('.turn').length);

  // 1) 앱이 6초 동안 내려가 있다 — 보내기는 실패하지만 글을 지우지 않고, 돌아오면 같은 글을 한 번 보낸다
  await qa('/__qa/down?sec=6');
  await page.fill('css=.composer textarea', '재시작 중에 보낸 글');
  await page.click('css=.composer-bar button.primary');
  await wait(() => /자동으로 다시 보냅니다/.test(document.querySelector('.composer-hint').textContent));
  must(await page.evaluate(() => document.querySelector('.composer textarea').value === '재시작 중에 보낸 글'), '실패해도 글은 입력창에 그대로');
  await shot('resend-waiting');
  await wait((n) => document.querySelectorAll('.turn').length > n && document.querySelector('.composer textarea').value === '', before, 20000);
  must((await qa('/__qa/stats')).turns === 1, '앱이 돌아오면 턴 보내기가 정확히 한 번 간다');
  must(!/보내지 못했|자동으로/.test(await hint()), '다시 보낸 뒤 실패 안내가 사라진다');
  await wait(() => !document.querySelector('.composer-bar button.primary')?.textContent.includes('멈춤'), undefined, 15000);

  // 2) 기다리는 동안 글을 고치면 자동으로 보내지 않는다(고친 글은 사용자가 직접 보낸다)
  await qa('/__qa/down?sec=4');
  await page.fill('css=.composer textarea', '두 번째 글');
  await page.click('css=.composer-bar button.primary');
  await wait(() => /자동으로 다시 보냅니다/.test(document.querySelector('.composer-hint').textContent));
  await page.fill('css=.composer textarea', '두 번째 글 — 고침');
  must(/보내지 못했습니다/.test(await hint()), '글을 고치면 자동 재전송을 멈추고 실패를 보인다');
  await new Promise((r) => setTimeout(r, 7000));
  must((await qa('/__qa/stats')).turns === 1, '고친 글은 앱이 돌아와도 저절로 가지 않는다');
  must(await page.evaluate(() => document.querySelector('.composer textarea').value === '두 번째 글 — 고침'), '고친 글은 입력창에 남는다');

  // 3) 앱 자체가 낸 오류(JSON 본문)는 기다리지 않고 바로 실패로 보인다 — 프록시 502 와 가른다
  must(await page.evaluate(() => fetch('/api/sessions/없는세션/turns', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
    .then((r) => r.text()).then((t) => /^\s*\{/.test(t))), '앱의 오류 응답은 JSON 본문이다');

  await fs.writeFile(OUT + '/result.json', JSON.stringify({ ok: true, results, shotErrors }, null, 2));
  console.log('재전송 회귀 통과: ' + results.length + '개 단언');
} catch (e) {
  await fs.writeFile(OUT + '/result.json', JSON.stringify({ ok: false, error: String(e), results, shotErrors }, null, 2));
  throw e;
} finally {
  await task.finish({ keep: [] });
}
