// 실행: browser_check.py --url <가짜 데모 주소> --archive-only. 데이터는 검사 후 복원한다.
const fs = await import('node:fs/promises');
const page = globalThis.browserTask.page('p1');
const sid = '01a1aaaa000000000000000000000001';
const row = '.srow[data-sid="' + sid + '"]';
const action = '.session-row:has(' + row + ') .session-archive';
const initialArchive = {};
const checks = [], shotErrors = [];
function must(ok, text) { if (!ok) throw new Error(text); checks.push(text); }
async function wait(fn, arg) { await page.waitForFunction(fn, arg, { timeout: 15000 }); }
async function exists(css) { await wait((s) => !!document.querySelector(s), css); }
async function absent(css) { await wait((s) => !document.querySelector(s), css); }
async function shot(name) {
  try { await page.screenshot({ path: OUT + '/' + name + '.png' }); }
  catch (e) { shotErrors.push(String(e.message || e).slice(0, 150)); }
}
async function filter(text) { await page.click('loc=role:button[name="' + text + '"]', { label: text + ' 목록 보기' }); }
async function checkButtons(label) {
  must(await page.evaluate(() => [...document.querySelectorAll('.session-archive')].filter(b => b.getClientRects().length).every(b => {
    const r = b.getBoundingClientRect();
    return r.left >= 0 && r.right <= innerWidth && r.width > 0 && r.height > 0 &&
      document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)?.closest('button') === b;
  })), label + ' 보관 버튼이 화면 안에 보이고 다른 요소에 가려지지 않는다');
}
try {
  await page.cdp('Emulation.setDeviceMetricsOverride', { width: 412, height: 915, deviceScaleFactor: 2, mobile: true });
  await page.goto(BASE);
  for (const id of [sid, '01a1aaaa000000000000000000000002']) {
    const response = await page.fetch(BASE + 'api/sessions/' + id);
    initialArchive[id] = JSON.parse(response.body).session.archived;
    await page.fetch(BASE + 'api/sessions/' + id + '/archive', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ archived: false }) });
  }
  await page.reload();
  await exists(row);
  must(await page.evaluate((sel) => {
    const b = document.querySelector(sel), r = b.getBoundingClientRect();
    return b.textContent === '보관' && r.width > 0 && r.height > 0 && !document.querySelector('.sheet');
  }, action), '목록에 보관 버튼이 메뉴 없이 바로 보인다');
  await checkButtons('모바일 목록');
  await shot('archive-direct-mobile');
  console.log(await page.snapshot());
  await page.click('css=' + row, { label: '보관할 세션 열기' });
  await exists('.turn');
  await checkButtons('모바일 세션 머리');
  must(await page.evaluate(() => document.querySelector('#top-actions .session-archive').textContent === '보관'), '세션 머리에도 보관 버튼이 바로 보인다');
  await page.fill('css=.composer textarea', '보관해도 남는 초안');
  await page.click('css=#top-actions .session-archive', { label: '모바일에서 열린 세션 보관' });
  await wait(() => document.querySelector('#top-actions .session-archive')?.textContent === '보관 해제');
  must(await page.evaluate((id) => location.hash.includes(id) && !!document.querySelector('.turn') && document.querySelector('.composer textarea').value === '보관해도 남는 초안' && document.querySelector('#subtitle').textContent.includes('보관됨'), sid), '모바일에서 보관해도 세션과 초안이 유지되고 상태가 바뀐다');
  await page.click('css=#top-actions .session-archive', { label: '모바일에서 보관 해제' });
  await wait(() => document.querySelector('#top-actions .session-archive')?.textContent === '보관');
  must(await page.evaluate((id) => location.hash.includes(id) && !document.querySelector('#subtitle').textContent.includes('보관됨'), sid), '보관 해제해도 세션을 유지하고 상태가 바뀐다');
  await page.click('css=#back', { label: '세션 목록으로 돌아가기' });
  await exists(action);
  // 저장 실패는 성공으로 표시하지 않으며, 같은 창에서 재시도할 수 있다.
  await page.evaluate(() => {
    const original = window.fetch;
    window.fetch = function (url, options) {
      if (String(url).endsWith('/archive') && options?.method === 'POST') {
        window.fetch = original;
        return Promise.resolve(new Response(JSON.stringify({ error: '보관 실패 시험' }), { status: 503, headers: { 'Content-Type': 'application/json' } }));
      }
      return original.apply(this, arguments);
    };
  });
  await page.click('css=' + action, { label: '목록에서 바로 보관하기' });
  await wait(() => document.querySelector('#toast').textContent.includes('저장하지 못했습니다'));
  must(await page.evaluate((sel) => document.querySelector(sel)?.textContent === '보관' && !document.querySelector(sel).disabled, action), '실패하면 세션과 재시도 버튼을 유지한다');
  await page.click('css=' + action, { label: '세션 보관 다시 시도' });
  await absent(row);
  must(await page.evaluate(() => !document.querySelector('.sheet')), '한 번 누르면 확인 창 없이 바로 보관한다');
  must(true, '보관한 세션이 일반 목록에서 사라진다');
  await filter('보관됨'); await exists(row);
  await page.fill('css=input[aria-label="세션 검색"]', '없는검색결과');
  await wait(() => document.querySelector('.slist .empty')?.textContent === '맞는 세션이 없습니다');
  await page.fill('css=input[aria-label="세션 검색"]', 'calc.py'); await exists(row);
  await page.reload(); await exists(row);
  await checkButtons('모바일 보관 목록');
  must(await page.evaluate(() => location.hash.includes('archived=1') && document.querySelector('input[aria-label="세션 검색"]').value === 'calc.py'), '새로고침 후 보관 필터와 검색어를 복원한다');
  await shot('archive-mobile');
  await page.click('css=' + row, { label: '보관한 세션 읽기' }); await exists('.turn');
  must(await page.evaluate(() => document.querySelector('.composer textarea').value === '보관해도 남는 초안' && document.querySelector('#subtitle').textContent.includes('보관됨')), '보관한 기록을 열어도 초안을 유지하고 보관 상태를 표시한다');
  await page.click('css=#back', { label: '보관 목록으로 돌아가기' }); await exists(row);
  must(await page.evaluate(() => location.hash.includes('archived=1') && document.querySelector('input[aria-label="세션 검색"]').value === 'calc.py'), '뒤로 가기에서 보관 필터와 검색어를 복원한다');
  await page.click('css=' + action, { label: '목록에서 바로 보관 해제' });
  await absent(row);
  await filter('세션'); await exists(row);
  must(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), '412×915에서 목록과 동작 버튼에 가로 넘침이 없다');
  // 넓은 화면에서 다른 세션을 숨겨도 읽던 화면은 유지한다.
  await page.fill('css=input[aria-label="세션 검색"]', '');
  await wait(() => document.querySelectorAll('.srow').length === 4);   // Codex 2 · Claude 1 · S1 아래 하위 에이전트 1
  await page.cdp('Emulation.setDeviceMetricsOverride', { width: 1280, height: 800, deviceScaleFactor: 1, mobile: false });
  await page.click('css=' + row, { label: '나란히 보기에서 세션 열기' }); await exists('#side .srow.on');
  console.log(await page.snapshot());
  await page.click('css=#side .srow[data-sid="01a1aaaa000000000000000000000002"] + .session-archive', { label: '다른 세션 바로 보관' });
  await wait(() => document.querySelectorAll('#side .srow').length === 3);
  must(await page.evaluate((id) => location.hash.includes(id) && !!document.querySelector('.turn'), sid), '다른 세션을 보관해도 현재 세션과 나란히 보기를 유지한다');
  await page.click('css=#side ' + action, { label: '목록에서 현재 세션 보관' });
  await wait(() => document.querySelector('#top-actions .session-archive')?.textContent === '보관 해제');
  must(await page.evaluate((id) => location.hash.includes(id) && document.documentElement.classList.contains('split') && !!document.querySelector('.turn') && document.querySelector('.composer textarea').value === '보관해도 남는 초안', sid), '목록에서 현재 세션을 보관해도 화면과 초안을 유지한다');
  await page.click('css=#top-actions .session-archive', { label: '열린 세션 보관 해제' });
  await wait(() => document.querySelector('#top-actions .session-archive')?.textContent === '보관');
  await page.click('css=#top-actions .session-archive', { label: '현재 세션 머리에서 보관' });
  await wait(() => document.querySelector('#top-actions .session-archive')?.textContent === '보관 해제');
  must(await page.evaluate((id) => location.hash.includes(id) && !!document.querySelector('.turn'), sid), '세션 머리에서 보관해도 나란히 보기를 유지한다');
  await filter('보관됨'); await exists(row);
  await exists('#side .srow.on');
  await page.reload(); await exists('#side .srow.on');
  await checkButtons('나란히 보기');
  must(await page.evaluate(() => document.querySelector('.list-filter .on').textContent === '보관됨' && document.querySelectorAll('#side .srow').length === 2), '세션 화면 새로고침에서도 왼쪽 보관 목록을 유지한다');
  must(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), '나란히 보기에서도 가로 넘침이 없다');
  await shot('archive-desktop');
  await page.fill('css=.composer textarea', '');
  console.log(JSON.stringify({ ok: true, checks, shotErrors }));
  await fs.writeFile(OUT + '/result.json', JSON.stringify({ ok: true, checks, shotErrors }, null, 2));
} catch (e) {
  await shot('archive-failure');
  await fs.writeFile(OUT + '/result.json', JSON.stringify({ ok: false, checks, shotErrors, error: String(e.stack || e), snapshot: await page.snapshot().catch(() => '') }, null, 2));
  throw e;
} finally {
  // 데모의 두 견본 세션을 되돌려 기존 화면 회귀에서도 같은 시드를 쓴다.
  for (const id of [sid, '01a1aaaa000000000000000000000002']) {
    if (id in initialArchive) await page.fetch(BASE + 'api/sessions/' + id + '/archive', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ archived: initialArchive[id] }) }).catch(() => {});
  }
}
