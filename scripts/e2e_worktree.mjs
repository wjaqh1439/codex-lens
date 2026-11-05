// worktree_check.py가 실제 Git fixture·BASE·OUT·CASES를 준비한다.
const fs = await import('node:fs/promises');
const assert = (await import('node:assert/strict')).default;
const task = globalThis.browserTask, page = task.page('p1');
const checks = [], shotErrors = [];
async function shot(name) {
  if (shotErrors.length) return;
  try { await page.screenshot({path: OUT + '/' + name + '.png'}); }
  catch (e) { shotErrors.push(String(e.message)); }
}
try {
  for (const width of [1280, 412]) {
    await page.cdp('Emulation.setDeviceMetricsOverride', {width, height:915, deviceScaleFactor:1, mobile:width === 412});
    for (const c of CASES) {
      await page.goto(BASE + '#/s/' + c.id);
      await page.waitForFunction(id => document.getElementById('sid')?.dataset.id === id &&
        !!document.querySelector('.session-location'), c.id, {timeout:15000});
      const response = await page.fetch('/api/sessions/' + c.id);
      const meta = JSON.parse(response.body).session;
      assert.equal(meta.location.kind, c.kind);
      const view = await page.evaluate(() => {
        const b = document.querySelector('.session-location'), r = b.getBoundingClientRect();
        return {text:b.textContent, count:document.querySelectorAll('#top .session-location').length,
          top:r.top, left:r.left, right:r.right, height:r.height,
          visible:document.elementFromPoint(r.left+r.width/2,r.top+r.height/2)?.closest('button') === b,
          overflow:document.documentElement.scrollWidth-innerWidth};
      });
      assert.equal(view.text, c.label + ' · ' + c.short);
      assert.ok(view.count === 1 && view.visible && view.left >= 0 && view.right <= width && view.height < 25 && view.overflow <= 0, JSON.stringify(view));
      await page.click('.session-location', {label:'실행 위치 경로 확인'});
      await page.waitForSelector('.sheet[aria-label="실행 위치"]');
      const paths = await page.evaluate(() => [...document.querySelectorAll('.sheet .location-path')].map(p => p.textContent));
      assert.deepEqual(paths, c.repo ? [c.cwd,c.repo] : [c.cwd]);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth &&
        [...document.querySelectorAll('.location-path')].every(e => e.getBoundingClientRect().right <= innerWidth)));
      await shot('location-' + width + '-' + c.kind);
      await page.click('.sheet-head button', {label:'실행 위치 닫기'});
      checks.push(width + ': ' + c.label + ' ' + c.short + ' · 클릭 가능 · 전체 경로 확인 · 넘침 없음');
    }
    // 같은 이름의 두 워크트리는 상위 폴더로 구별하고, 새로고침해도 표시를 유지한다.
    await page.goto(BASE + '#/s/' + CASES[1].id);
    await page.waitForSelector('.session-location');
    await page.reload();
    await page.waitForFunction(text => document.querySelector('.session-location')?.textContent === text,
      '워크트리 · ' + CASES[1].short, {timeout:15000});
    await shot('header-' + width);
    checks.push(width + ': 재진입·새로고침 후 워크트리 위치 유지');
  }
  console.log('PASS worktree\n' + checks.join('\n'));
} catch(e) {
  checks.push('FAIL ' + String(e.stack || e));
  await shot('failure');
  throw e;
} finally {
  await fs.writeFile(OUT+'/result.json', JSON.stringify({checks,shotErrors},null,2));
  await task.finish({keep:[]});
}
