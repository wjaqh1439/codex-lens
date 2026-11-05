// scripts/context_check.py가 합성 데모·BASE·OUT을 준비한다. 새 Space는 실행기가 닫는다.
const fs = await import('node:fs/promises');
const assert = (await import('node:assert/strict')).default;
const task = globalThis.browserTask, page = task.page('p1');
const checks = [], shotErrors = [];
const S1 = '01a1aaaa000000000000000000000001';
const S2 = '01a1aaaa000000000000000000000002';
const S3 = 'c1a0de00-0000-4000-8000-000000000003';
async function expectContext(text) {
  await page.waitForFunction(t => {
    const c = document.getElementById('session-context');
    return c && !c.hidden && c.textContent === t;
  }, text, {timeout:15000});
}
async function layout(label) {
  const v = await page.evaluate(() => {
    const c = document.getElementById('session-context'), r = c.getBoundingClientRect();
    const p = document.getElementById('pane').getBoundingClientRect();
    return {count: document.querySelectorAll('#session-context').length, left:r.left, right:r.right,
      top:r.top, bottom:r.bottom, paneLeft:p.left, paneRight:p.right,
      headBottom:document.getElementById('top').getBoundingClientRect().bottom,
      viewport:innerHeight, overflow:document.documentElement.scrollWidth-innerWidth,
      wrap:c.getBoundingClientRect().height > 24};
  });
  assert.ok(v.count === 1 && v.overflow <= 0 && v.left >= v.paneLeft && v.right <= v.paneRight &&
    v.top >= 0 && v.bottom <= v.headBottom && v.bottom < v.viewport && !v.wrap, JSON.stringify(v));
  checks.push(label + ': 컨텍스트 한 번 표시 · 줄바꿈·가로 넘침 없음');
}
try {
  await page.cdp('Emulation.setDeviceMetricsOverride', {width:412,height:915,deviceScaleFactor:2,mobile:true});
  await page.goto(BASE + '#/s/' + S1);
  await expectContext('컨텍스트 42% · 최대 258.4K');
  await layout('412×915');
  checks.push('저장된 마지막 호출 사용량과 실제 최대 용량 복원');
  await page.fill('.composer textarea', '천천히 컨텍스트 표시를 확인해 줘');
  await page.click('.composer-bar button.primary', {label:'컨텍스트 갱신 시작'});
  await expectContext('컨텍스트 21% · 최대 258.4K');
  assert.ok(await page.evaluate(() => !!document.querySelector('.turn:last-child .proc-spinner')));
  checks.push('턴이 진행 중일 때 21%로 실시간 갱신');
  await expectContext('컨텍스트 42% · 최대 258.4K');
  await page.reload();
  await expectContext('컨텍스트 42% · 최대 258.4K');
  const title = await page.evaluate(() => document.getElementById('session-context').title);
  assert.ok(title.includes('108,528') && title.includes('258,400'), title);
  checks.push('누적 999,999·캐시 중복 합산 없이 마지막 호출 108,528 / 258,400');
  await page.cdp('Emulation.setDeviceMetricsOverride', {width:1280,height:915,deviceScaleFactor:1,mobile:false});
  await layout('1280×915');
  try { await page.screenshot({path:OUT+'/context-desktop.png'}); } catch(e) {shotErrors.push(String(e.message));}
  await page.goto(BASE+'#/s/'+S2);
  await expectContext('컨텍스트 42K · 최대 확인 전');
  checks.push('최대 용량 미보고 시 %를 추정하지 않고 확인 전 표시');
  await page.goto(BASE+'#/s/'+S3);
  await expectContext('컨텍스트 확인 전');
  checks.push('다른 세션에 앞 세션의 사용량이 섞이지 않음');
  await page.goto(BASE+'#/s/'+S1);
  await expectContext('컨텍스트 42% · 최대 258.4K');
  await page.cdp('Emulation.setDeviceMetricsOverride', {width:412,height:915,deviceScaleFactor:2,mobile:true});
  await layout('재진입 412×915');
  try { await page.screenshot({path:OUT+'/context-mobile.png'}); } catch(e) {shotErrors.push(String(e.message));}
  checks.push('HTTPS 데모 또는 격리 회귀 화면에서 재진입·새로고침 복원');
  console.log('PASS context\n'+checks.join('\n'));
} catch(e) {
  checks.push('FAIL '+String(e.stack||e));
  throw e;
} finally {
  await fs.writeFile(OUT+'/result.json', JSON.stringify({checks,shotErrors},null,2));
  await task.finish({keep:[]});
}
