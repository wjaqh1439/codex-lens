// scripts/browser_check.py --web-only · ego-browser, 실패 시 종료 코드 1.
const fs = await import('node:fs/promises');
const task = globalThis.browserTask, page = task.page('p1'), results = [], shotErrors = [];
const sid = '01a1aaaa000000000000000000000001';
const session = '#/s/' + sid;
let expectedY = null;
function must(ok, label) { if (!ok) throw new Error(label); results.push(label); }
async function wait(fn, arg) { await page.waitForFunction(fn, arg, {timeout: 15000}); }
async function screenshot(name) {
  if (shotErrors.length) return;
  try { await page.screenshot({path: OUT + '/' + name + '.png'}); }
  catch (e) { shotErrors.push(String(e)); }
}
async function openLink() {
  await page.evaluate((base) => {
    const a = [...document.querySelectorAll('.annot[data-part=answer] a.ext')].find(a => /\/demo\/$/.test(a.href));
    a.href = base + 'api/info'; a.click();
  }, BASE);
  await wait(() => {
    const f = document.querySelector('.web-frame');
    try { return f && f.contentDocument.body.textContent.includes('"version"') && document.querySelector('.web-tools').hidden; }
    catch { return false; }
  });
}
async function controls(width) {
  const s = await page.evaluate(() => {
    const ret = document.getElementById('pane-back'), close = document.getElementById('back');
    const title = document.querySelector('#top .web-bar').getBoundingClientRect();
    const menu = document.querySelector('#top-actions [aria-label="페이지 메뉴"]');
    const f = document.querySelector('.web-frame').getBoundingClientRect();
    return {ret: ret.textContent, returnHidden: ret.hidden, close: close.textContent, closeHidden: close.hidden,
      left: ret.getBoundingClientRect().right <= title.left,
      right: close.hidden || close.getBoundingClientRect().left >= menu.getBoundingClientRect().right,
      extra: document.querySelectorAll('.web-tools button').length,
      count: [...document.querySelectorAll('#top button')].filter(b => !b.hidden && b.textContent.includes('세션으로')).length,
      overflow: document.documentElement.scrollWidth > innerWidth,
      frame: f.height > innerHeight * .6 && f.bottom <= innerHeight + 1};
  });
  must(s.ret === '세션으로' && !s.returnHidden && s.left && s.count === 1, width + ' 세션 복귀가 왼쪽 위, 주소창 앞에 한 번 보인다');
  must(width < 900 ? s.closeHidden : !s.closeHidden && s.close === '세션 닫기' && s.right,
    width + ' 세션 닫기는 넓은 화면 오른쪽 끝에 표시한다');
  must(!s.extra && !s.overflow && s.frame, width + ' 보조 단추 줄 없이 페이지가 남는 공간을 쓴다');
}
try {
  for (const width of [412, 1280]) {
    await page.cdp('Emulation.setDeviceMetricsOverride', {width, height: 915, deviceScaleFactor: 1, mobile: width < 900});
    await page.goto(BASE + session);
    await wait(() => !!document.querySelector('.composer textarea'));
    await page.fill('css=.composer textarea', '페이지를 보고 돌아와서 보낼 초안');
    const y = await page.evaluate(() => {
      [...document.querySelectorAll('.annot[data-part=answer] a.ext')].find(a => /\/demo\/$/.test(a.href)).scrollIntoView({block:'center'});
      return scrollY;
    });
    expectedY = y;
    await openLink(); await controls(width);
    await screenshot('web-navigation-' + width);
    await page.click('css=#top-actions [aria-label="페이지 메뉴"]', {label:'페이지 보조 메뉴 열기'});
    must(await page.evaluate(() => {
      const d = document.querySelector('.web-menu');
      return d && d.textContent === '페이지 새로고침브라우저에서 열기';
    }), width + ' 갱신과 브라우저 열기를 페이지 메뉴로 모은다');
    await page.evaluate(() => { document.querySelector('.web-frame').contentDocument.body.textContent = '갱신 전'; });
    await page.click('css=.web-menu button >> nth=0', {label:'열린 페이지 새로고침'});
    await wait(() => document.querySelector('.web-frame')?.contentDocument?.body.textContent.includes('"version"'));
    must(await page.evaluate(() => !document.querySelector('.web-menu')), width + ' 새로고침하면 페이지가 다시 로드되고 메뉴가 닫힌다');
    await page.click('css=#top-actions [aria-label="페이지 메뉴"]');
    const popupWait = page.waitForEvent('popup');
    await page.click('css=.web-menu button >> nth=1', {label:'페이지를 브라우저에서 열기'});
    const popup = await popupWait;
    await popup.waitForURL(BASE + 'api/info');
    must(await popup.url() === BASE + 'api/info', width + ' 브라우저 열기가 같은 페이지를 새 탭으로 연다');
    await popup.close();
    await page.click('css=#pane-back', {label:'원래 세션으로 돌아가기'});
    await wait(() => !!document.querySelector('.composer textarea'));
    await wait((y) => Math.abs(scrollY - y) < 4, y);
    must(await page.evaluate(() => document.querySelector('.composer textarea').value === '페이지를 보고 돌아와서 보낼 초안'), width + ' 복귀 시 읽던 위치와 미전송 초안을 유지한다');
    await page.evaluate(() => history.forward());
    await wait(() => !!document.querySelector('.web-frame'));
    await controls(width);
    if (width >= 900) {
      await page.click('css=#back', {label:'세션 패널 닫기'});
      await wait(() => location.hash === '#/' && !!document.querySelector('#view .srow'));
      must(await page.evaluate(() => !document.documentElement.classList.contains('split')), '세션 닫기는 페이지와 세션 패널을 닫고 목록으로 간다');
    }
  }
  // 주소를 직접 연 경우에는 이전 브라우저 기록 대신 from 세션을 연다.
  await page.goto(BASE + '#/web?url=' + encodeURIComponent(BASE + 'api/info') + '&from=' + sid);
  await wait(() => !!document.querySelector('.web-frame'));
  await page.click('css=#pane-back');
  await wait((hash) => location.hash === hash && !!document.querySelector('.turn'), session);
  must(true, '직접 연 웹 보기에서도 원래 세션으로 돌아간다');
  await fs.writeFile(OUT + '/result.json', JSON.stringify({ok:true, results, shotErrors}, null, 2));
  console.log(JSON.stringify({ok:true, results, shotErrors}));
} catch (e) {
  const debug = await page.evaluate(() => ({y:scrollY, height:document.documentElement.scrollHeight, state:history.state,
    scrolls:Object.fromEntries(Object.entries(sessionStorage).filter(([k]) => k.includes('scroll.')))}));
  console.log(JSON.stringify({expectedY, debug, results}));
  await fs.writeFile(OUT + '/result.json', JSON.stringify({ok:false, results, shotErrors, expectedY, debug, error:String(e.stack || e)}, null, 2));
  throw e;
}
