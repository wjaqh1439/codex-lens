// 실제 SSE를 통과하는 가짜 Codex·Claude 스트림. 3초 수신 공백 뒤 다시 생성한다.
// Codex 는 가짜 app-server 가 쓰는 기록(rollout)의 실제 출력 토큰으로, Claude 는 수신량 추정으로 보인다.
const fs = await import('node:fs/promises');
const page = globalThis.browserTask.page('p1');
const results = [], shotErrors = [];
function must(ok, label) { if (!ok) throw new Error(label); results.push(label); }
async function wait(fn) { await page.waitForFunction(fn, undefined, {timeout: 20000}); }
try {
  for (const agent of ['codex', 'claude']) {
    await page.cdp('Emulation.setDeviceMetricsOverride', {width:412, height:915, deviceScaleFactor:1, mobile:true});
    await page.goto(BASE);
    await wait(() => !!document.querySelector('.srow'));
    const response = await page.fetch('/api/sessions', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cwd:PROJ, agent, text:'텍스트를 생성하면서 속도를 보여 줘. ' + '계속 설명해 줘. '.repeat(8)})});
    must(response.ok, agent + ' 격리된 세션 생성');
    const sid = JSON.parse(response.body).id;
    await page.goto(BASE + '#/s/' + sid);
    if (agent === 'codex') {
      // Codex: 기록(rollout)의 실제 출력 토큰 ÷ 응답 시간. 진행 중엔 마지막 응답, 끝나면 턴 평균 — 수신량 추정은 쓰지 않는다.
      await wait(() => !!document.querySelector('.proc-spinner') && document.querySelector('.proc-speed.measured'));
      const live = await page.evaluate(() => {
        const speed = document.querySelector('.proc-speed.measured');
        const r = speed.getBoundingClientRect();
        return {text:speed.textContent, title:speed.title, estimates:document.querySelectorAll('.proc-speed:not(.measured)').length,
          collapsed:!speed.closest('.proc').classList.contains('open'),
          overflow:document.documentElement.scrollWidth > innerWidth, visible:r.width > 0 && r.left >= 0 && r.right <= innerWidth};
      });
      must(/^[1-9]\d* tok\/s$/.test(live.text) && live.title.includes('실제 출력 토큰') && live.title.includes('마지막 응답'), 'codex 진행 중 마지막 응답의 실측 tok/s와 설명');
      must(live.estimates === 0, 'codex 수신량 추정치를 함께 보이지 않는다');
      must(live.collapsed && live.visible && !live.overflow, 'codex 412×915 접힌 과정에서 속도 표시·가로 넘침 없음');
      await new Promise((r) => setTimeout(r, 2500));   // 수신 추정이라면 사라졌을 2초 공백을 넘긴다
      must(await page.evaluate(() => !!document.querySelector('.proc-spinner') && !!document.querySelector('.proc-speed.measured:not([hidden])')),
        'codex 수신 공백에도 실측 속도가 남는다');
      try { await page.screenshot({path:OUT + '/speed-codex.png'}); }
      catch (e) { shotErrors.push(String(e.message || e)); }
      await wait(() => !document.querySelector('.proc-spinner') && !!document.querySelector('.ans .answer'));
      await wait(() => /^평균 [1-9]\d* tok\/s$/.test((document.querySelector('.proc-speed.measured') || {}).textContent || ''));
      must(await page.evaluate(() => document.querySelector('.proc-speed.measured').title.includes('응답 2회')), 'codex 완료 뒤 턴 평균(응답 2회)으로 바뀐다');
      await page.cdp('Emulation.setDeviceMetricsOverride', {width:1280, height:800, deviceScaleFactor:1, mobile:false});
      await page.reload();
      await wait(() => !!document.querySelector('.ans .answer') && !!document.querySelector('.proc-speed.measured'));
      must(await page.evaluate(() => /^평균 [1-9]\d* tok\/s$/.test(document.querySelector('.proc-speed.measured').textContent)),
        'codex 다시 읽은 지난 턴도 기록에서 평균 tok/s를 보인다(외부 실행 세션과 같은 읽기 경로)');
      continue;
    }
    await wait(() => document.querySelector('.proc-speed:not([hidden])'));
    const sample = await page.evaluate(() => {
      const speed = document.querySelector('.proc-speed');
      const r = speed.getBoundingClientRect();
      return {text:speed.textContent, title:speed.title, collapsed:!speed.closest('.proc').classList.contains('open'),
        overflow:document.documentElement.scrollWidth > innerWidth, visible:r.width > 0 && r.left >= 0 && r.right <= innerWidth};
    });
    must(/^추정 [1-9]\d* tok\/s$/.test(sample.text) && sample.title.includes('실제 생성 속도와 다릅니다'), agent + ' 생성 중 추정 tok/s와 설명');
    must(sample.collapsed && sample.visible && !sample.overflow, agent + ' 412×915 접힌 과정에서 속도 표시·가로 넘침 없음');
    await wait(() => !!document.querySelector('.proc-spinner') && !document.querySelector('.proc-speed:not([hidden])'));
    must(true, agent + ' 텍스트 수신 공백에서 속도가 사라진다');
    await wait(() => document.querySelector('.proc-speed:not([hidden])'));
    must(true, agent + ' 생성이 재개되면 새 구간 속도를 표시한다');
    try { await page.screenshot({path:OUT + '/speed-' + agent + '.png'}); }
    catch (e) { shotErrors.push(String(e.message || e)); }
    await wait(() => !document.querySelector('.proc-spinner') && !!document.querySelector('.ans .answer'));
    must(await page.evaluate(() => !document.querySelector('.proc-speed')), agent + ' 완료 뒤 속도 표시 제거');
    await page.cdp('Emulation.setDeviceMetricsOverride', {width:1280, height:800, deviceScaleFactor:1, mobile:false});
    await page.reload();
    await wait(() => !!document.querySelector('.ans .answer'));
    must(await page.evaluate(() => !document.querySelector('.proc-speed')), agent + ' 다시 읽은 과거 답변에서 속도를 만들지 않는다');
  }
  console.log(JSON.stringify({ok:true, results, shotErrors}));
} finally {
  await fs.writeFile(OUT + '/result.json', JSON.stringify({results, shotErrors}, null, 2));
}
