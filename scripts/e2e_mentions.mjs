// browser_check.py --mentions-only: 실제 콘텐츠·헤더·입력창으로 추천창 경계와 조작을 검증한다.
const fs = await import('node:fs/promises');
const task = globalThis.browserTask, page = task.page('p1');
const results = [], shotErrors = [];
function must(ok, message) { if (!ok) throw new Error(message); results.push(message); }
async function wait(fn, arg) { await page.waitForFunction(fn, arg, { timeout: 10000 }); }
async function shot(name) {
  if (shotErrors.length >= 1) return;
  try { await page.screenshot({ path: OUT + '/' + name + '.png' }); }
  catch (e) { shotErrors.push(String(e)); }
}
async function metrics() {
  return page.evaluate(() => {
    const p = document.querySelector('.mention-pop'), r = p.getBoundingClientRect();
    const h = document.getElementById('top').getBoundingClientRect(), vv = visualViewport;
    const a = p.querySelector('.on')?.getBoundingClientRect(), b = document.querySelector('.composer-box').getBoundingClientRect();
    const section = p.querySelector('.mention-sec')?.getBoundingClientRect();
    const hit = a && document.elementFromPoint((a.left + a.right) / 2, (a.top + a.bottom) / 2);
    return { top: r.top, bottom: r.bottom, left: r.left, right: r.right, height: r.height,
      minTop: Math.max(vv.offsetTop, h.bottom), maxBottom: vv.offsetTop + vv.height,
      width: innerWidth, down: r.top >= b.bottom, scroll: scrollY, popScroll: p.scrollTop,
      sectionTop: section?.top, selected: a && a.top >= r.top && a.bottom <= r.bottom,
      clickable: !!hit?.closest('.mention-item'), items: p.querySelectorAll('.mention-item').length };
  });
}
async function check(label, selected = true) {
  const m = await metrics();
  must(m.top >= m.minTop && m.bottom <= m.maxBottom && m.height >= (selected ? 38 : 20),
    label + ': 추천창이 헤더와 화면 경계 안에 있다 ' + JSON.stringify(m));
  must(m.left >= 0 && m.right <= m.width, label + ': 가로 잘림 없음');
  if (selected) must(m.selected && m.clickable, label + ': 선택 항목이 보이고 누를 수 있다');
  return m;
}
async function open() {
  await page.fill('css=.composer textarea', '@');
  await wait(() => document.querySelectorAll('.mention-pop .mention-item').length >= 4);
}
try {
  // 충분한 파일 수로 높이 제한과 목록 내부 스크롤을 재현한다. 운영 데이터는 쓰지 않는다.
  for (let i = 0; i < 12; i++) await fs.writeFile(PROJ + '/mention-' + i + '.txt', '추천창 회귀용 파일\n');
  await page.cdp('Emulation.setDeviceMetricsOverride', { width: 1600, height: 800, deviceScaleFactor: 1, mobile: false });
  await page.goto(BASE + '#/s/01a1aaaa000000000000000000000002');
  await wait(() => !!document.querySelector('.composer textarea'));
  await open();
  const desktop = await check('데스크톱 짧은 대화');
  must(desktop.down && desktop.popScroll === 0 && desktop.sectionTop >= desktop.top,
    '위쪽 입력창에서는 아래로 열리고 첫 파일 분류부터 보인다');
  await shot('mentions-desktop');
  for (let i = 0; i < desktop.items - 1; i++) {
    await page.press('css=.composer textarea', 'ArrowDown');
    const m = await check('추천 ' + (i + 2));
    must(Math.abs(m.scroll - desktop.scroll) < 1, '키보드 선택이 페이지를 스크롤하지 않는다');
  }
  await page.press('css=.composer textarea', 'ArrowDown');
  const wrapped = await check('처음으로 순환');
  must(wrapped.popScroll === 0 && wrapped.sectionTop >= wrapped.top, '처음으로 돌아오면 분류 제목도 보인다');

  // 추천창을 연 채 리사이즈한다. 입력을 다시 하지 않아도 위치와 높이가 갱신되어야 한다.
  await page.cdp('Emulation.setDeviceMetricsOverride', { width: 412, height: 915, deviceScaleFactor: 1, mobile: true });
  await wait(() => innerWidth === 412 && document.querySelector('.mention-pop').getBoundingClientRect().right <= 412);
  await check('모바일 412×915');
  await shot('mentions-mobile');
  await page.cdp('Emulation.setDeviceMetricsOverride', { width: 412, height: 520, deviceScaleFactor: 1, mobile: true });
  await wait(() => { const p = document.querySelector('.mention-pop').getBoundingClientRect(); return p.top >= document.getElementById('top').getBoundingClientRect().bottom && p.bottom <= 520; });
  await check('키보드로 줄어든 높이');
  await page.press('css=.composer textarea', 'Escape');
  must(await page.evaluate(() => !document.querySelector('.mention-pop')), 'Escape로 추천창 닫기');

  // 긴 대화의 하단에서는 위로 연다. 실제 클릭·초안 칩·Enter 선택도 지킨다.
  await page.cdp('Emulation.setDeviceMetricsOverride', { width: 412, height: 915, deviceScaleFactor: 1, mobile: true });
  await page.goto(BASE + '#/s/01a1aaaa000000000000000000000001');
  await wait(() => document.querySelectorAll('.turn').length === 2);
  await page.evaluate(() => scrollTo(0, document.documentElement.scrollHeight));
  await open();
  const bottom = await check('긴 대화 하단');
  must(!bottom.down, '화면 아래쪽 입력창에서는 위로 열린다');
  await page.click('css=.mention-item.on');
  must(await page.evaluate(() => !document.querySelector('.mention-pop') && document.querySelectorAll('.mchip').length === 1), '파일 클릭으로 멘션 칩 추가');
  await page.fill('css=.composer textarea', '@존재하지않는파일zzzz');
  await wait(() => document.querySelector('.mention-pop')?.textContent.includes('맞는 파일'));
  await check('검색 결과 없음', false);
  await page.fill('css=.composer textarea', '@dsg');
  await wait(() => document.querySelector('.mention-item .mi-sub')?.textContent === 'docs/design.md');
  await page.press('css=.composer textarea', 'Enter');
  must(await page.evaluate(() => document.querySelector('.composer textarea').value.includes('@docs/design.md') && !document.querySelector('.mention-pop')), 'Enter로 추천 파일 선택');

  await page.goto(BASE + '#/new?cwd=' + encodeURIComponent(PROJ));
  await wait(() => !!document.querySelector('.wt-row:not([hidden])'));
  await open();
  await check('새 세션 입력창');
  await shot('mentions-new-session');
  await fs.writeFile(OUT + '/result.json', JSON.stringify({ ok: true, results, shotErrors }, null, 2));
  console.log('멘션 회귀 통과: ' + results.length + '개 단언');
} catch (e) {
  await fs.writeFile(OUT + '/result.json', JSON.stringify({ ok: false, error: String(e), results, shotErrors }, null, 2));
  throw e;
} finally {
  await task.finish({ keep: [] });
}
