// lens-port로 관리하는 격리된 가짜 데모에서만 실행한다.
const fs = await import('node:fs/promises');
const page = globalThis.browserTask.page('p1');
const results = [], shotErrors = [];
function must(ok, label) { if (!ok) throw new Error(label); results.push(label); }
async function wait(fn, arg) { await page.waitForFunction(fn, arg, { timeout: 15000 }); }
async function idle() { await wait(() => document.querySelector('.slist')?.getAttribute('aria-busy') === 'false'); }
async function screenshot(name) {
  try { await page.screenshot({ path: OUT + '/' + name + '.png' }); }
  catch (e) { shotErrors.push(String(e.message || e)); }
}
try {
  await page.cdp('Emulation.setDeviceMetricsOverride', { width: 1280, height: 915, deviceScaleFactor: 1, mobile: false });
  await page.goto(BASE);
  await idle();
  console.log(await page.snapshot());
  must(await page.evaluate(() => !!document.querySelector('button[aria-label="세션 목록 새로고침"]')), '목록 새로고침 버튼 표시');
  await page.click('css=.srow >> nth=0');
  await wait(() => !!document.querySelector('.composer textarea'));
  await idle();
  await page.fill('css=.composer textarea', '새로고침해도 남는 초안');
  const before = await page.evaluate(() => ({ hash: location.hash, sid: document.querySelector('.srow.on').dataset.sid }));
  const data = await page.fetch('/api/sessions');
  const cwd = JSON.parse(data.body).sessions[0].cwd;
  const externalText = '외부에서 생성한 새로고침 검증 세션 ' + Date.now();
  const created = await page.fetch('/api/sessions', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cwd, text: externalText, model: 'gpt-fake-sol' }) });
  must(created.ok, '별도 요청으로 세션 생성');
  const externalId = JSON.parse(created.body).id;
  // 요청을 잡아 둬서 갱신 중 반응과 중복 클릭 방지를 확인한다.
  await page.evaluate(() => {
    const original = window.fetch;
    window.fetch = function (url, options) {
      if (String(url).startsWith('/api/sessions?')) {
        window.fetch = original;
        return new Promise(resolve => { window.releaseListRefresh = () => resolve(original(url, options)); });
      }
      return original(url, options);
    };
  });
  await page.click('css=.list-refresh');
  must(await page.evaluate(() => document.querySelector('.list-refresh').disabled && document.querySelector('.list-refresh').textContent === '…'), '갱신 즉시 진행 표시·중복 클릭 방지');
  await page.evaluate(() => window.releaseListRefresh());
  await idle();
  const refreshed = await page.evaluate(id => ({ found: !!document.querySelector('.srow[data-sid="' + id + '"]'),
    hash: location.hash, active: document.querySelector('.srow.on')?.dataset.sid, draft: document.querySelector('.composer textarea').value }), externalId);
  must(refreshed.found && refreshed.hash === before.hash && refreshed.active === before.sid && refreshed.draft === '새로고침해도 남는 초안', '외부 새 세션 반영·열린 세션·초안 유지');
  await page.fill('css=.list-tools input', externalText);
  await wait(() => document.querySelectorAll('.srow').length === 1);
  await idle();
  await page.click('css=.list-refresh');
  await idle();
  must(await page.evaluate(text => document.querySelector('.list-tools input').value === text && document.querySelectorAll('.srow').length === 1, externalText), '검색어 유지 후 갱신');
  await page.evaluate(() => {
    const original = window.fetch;
    window.fetch = function (url, options) {
      if (String(url).startsWith('/api/sessions?')) {
        window.fetch = original;
        return Promise.resolve(new Response(JSON.stringify({ error: '갱신 실패 회귀' }), { status: 503, headers: { 'Content-Type': 'application/json' } }));
      }
      return original(url, options);
    };
  });
  await page.click('css=.list-refresh');
  await idle();
  must(await page.evaluate(() => document.querySelectorAll('.srow').length === 1 && !document.querySelector('.list-refresh').disabled && document.getElementById('toast').textContent.includes('갱신 실패 회귀')), '갱신 실패 시 목록 보존·실패 표시·재시도 가능');
  await page.click('css=.list-refresh');
  await idle();
  await page.fill('css=.list-tools input', '');
  await wait(() => document.querySelectorAll('.srow').length >= 3);
  await idle();
  await page.click('css=.list-tools button.primary');
  await wait(() => !!document.querySelector('input[aria-label="작업 폴더"]'));
  await page.fill('css=input[aria-label="작업 폴더"]', cwd);
  await page.fill('css=.composer textarea', '앱에서 생성한 자동 갱신 검증 세션');
  await page.click('css=.composer-bar button.primary');
  await wait(() => location.hash.startsWith('#/s/') && !!document.querySelector('.srow.on'));
  await idle();
  must(await page.evaluate(() => document.querySelector('.srow.on').dataset.sid === decodeURIComponent(location.hash.split('/s/')[1]) && document.querySelectorAll('.srow').length >= 4), '앱에서 생성한 세션이 왼쪽 목록에 즉시 표시');
  await screenshot('sessions-desktop');
  await page.cdp('Emulation.setDeviceMetricsOverride', { width: 412, height: 915, deviceScaleFactor: 1, mobile: true });
  await page.goto(BASE);
  await idle();
  must(await page.evaluate(() => {
    const r = document.querySelector('.list-refresh').getBoundingClientRect();
    return r.width >= 30 && r.left >= 0 && r.right <= innerWidth && document.documentElement.scrollWidth <= innerWidth;
  }), '412×915 목록에서 새로고침 버튼 표시·가로 넘침 없음');
  await page.click('css=.list-refresh');
  await idle();
  await screenshot('sessions-mobile');
  console.log(JSON.stringify({ results, shotErrors }));
} finally {
  await fs.writeFile(OUT + '/result.json', JSON.stringify({ results, shotErrors }, null, 2));
}
