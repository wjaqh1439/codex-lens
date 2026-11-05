// browser_check.py --fde-only: FDE 케이스 화면(#/case)을 412×915 · 1280 에서 검사한다. 시드는 scripts/fde_seed.py(FDE_CWD).
const task = globalThis.browserTask, page = task.page('p1');
const results = [], shotErrors = [];
function must(ok, message) { if (!ok) throw new Error(message); results.push(message); }
async function wait(fn, arg, timeout = 20000) { await page.waitForFunction(fn, arg, { timeout }); }
async function shot(name) {
  if (shotErrors.length) return;
  try { await page.screenshot({ path: OUT + '/' + name + '.png' }); } catch (e) { shotErrors.push(String(e)); }
}
async function open(tab) {
  await page.goto(BASE + '#/case?cwd=' + encodeURIComponent(FDE_CWD) + (tab ? '&tab=' + tab : ''));
  await wait(() => !!document.querySelector('.fde-steps') && !!document.querySelector('.fde-panel'));
}
async function click(sel, text) {
  const ok = await page.evaluate(([s, t]) => {
    const b = [...document.querySelectorAll(s)].find(x => x.textContent.trim().startsWith(t) && !x.disabled && x.offsetParent !== null);
    if (b) b.click();
    return !!b;
  }, [sel, text]);
  if (!ok) throw new Error('누를 것을 찾지 못함: ' + sel + ' ' + text);
}
async function type(sel, value) {
  await page.evaluate(([s, v]) => {
    const e = [...document.querySelectorAll(s)].find(x => x.offsetParent);
    e.focus(); e.value = v; e.dispatchEvent(new Event('input', { bubbles: true }));
  }, [sel, value]);
}
async function check(tag) {
  const l = await page.evaluate(() => {
    const W = document.documentElement.clientWidth;
    return {
      over: [...document.querySelectorAll('#view *')].filter(e => e.offsetParent && !e.closest('.fde-table-wrap') && e.getBoundingClientRect().right > W + 0.5).map(e => String(e.className || e.tagName)).slice(0, 5),
      wrapped: [...document.querySelectorAll('#view button')].filter(b => b.offsetParent && !b.classList.contains('fde-opt') && b.getBoundingClientRect().height > 46).map(b => b.textContent).slice(0, 5),
      dup: [...document.querySelectorAll('#view .fde-headline')].map(h => h.textContent).filter((t, i, a) => a.indexOf(t) !== i) };
  });
  must(!l.over.length, tag + ': 가로로 넘치지 않는다 ' + JSON.stringify(l.over));
  must(!l.wrapped.length, tag + ': 단추 글이 줄바꿈되지 않는다 ' + JSON.stringify(l.wrapped));
  must(!l.dup.length, tag + ': 같은 요지가 두 번 나오지 않는다 ' + JSON.stringify(l.dup));
}
try {
  for (const [w, h, mobile] of [[412, 915, true], [1280, 900, false]]) {
    await page.cdp('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 1, mobile });
    for (const tab of ['understand', 'plan', 'build', 'verify', 'use']) {
      await open(tab);
      await check(w + ' ' + tab);
      if (w === 412) await shot('fde-' + tab + '-412');
    }
  }
  await page.cdp('Emulation.setDeviceMetricsOverride', { width: 412, height: 915, deviceScaleFactor: 1, mobile: true });

  // 이해 — 요지가 먼저, 선택지는 한 번 누르면 결정
  await open('understand');
  const u = await page.evaluate(() => ({ head: document.querySelector('.fde-card.main .fde-headline').textContent,
    open: document.querySelectorAll('.fde-choice:not(.done)').length, badge: (document.querySelector('.fde-step.on .fde-badge.n') || {}).textContent,
    detailClosed: [...document.querySelectorAll('.fde-fold')].some(d => d.querySelector('summary').textContent.includes('자세히') && !d.open) }));
  must(/점검원마다/.test(u.head) && u.open === 1 && u.badge === '1' && u.detailClosed, '이해: 요지 · 선택지가 먼저 보이고 세부는 접혀 있다 ' + JSON.stringify(u));
  await click('.fde-opt', '이동 시간 먼저');
  await wait(() => /이동 시간 먼저 \(D1\)/.test((document.querySelector('.fde-chosen') || {}).textContent || ''));
  must(await page.evaluate(() => !document.querySelector('.fde-step.on .fde-badge.n')), '이해: 한 번 누르면 결정(D1)이 되고 남은 수가 사라진다');

  // 피드백 — 초안이 새로고침 뒤에도 남고, 반영해 다시가 v2 를 만든다
  await click('.fde-talk button', '💬 피드백');
  await type('.fde-talk textarea', '야간 점검은 범위 밖이라고 적어 줘');
  await page.reload();
  await wait(() => !!document.querySelector('.fde-talk'));
  await click('.fde-talk button', '💬 피드백');
  must(await page.evaluate(() => [...document.querySelectorAll('.fde-talk textarea')].find(t => t.offsetParent).value) === '야간 점검은 범위 밖이라고 적어 줘',
    '피드백: 보내지 않은 초안이 새로고침 뒤에도 남는다');
  await click('.fde-talk button', '보내기');
  await wait(() => [...document.querySelectorAll('.fde-panel button')].some(b => b.textContent === '피드백 1개 반영해 다시'));
  await click('.fde-panel button', '피드백 1개 반영해 다시');
  await wait(() => !!document.querySelector('.fde-job') || /피드백 반영/.test(document.querySelector('.fde-card.main .fde-headline').textContent), null, 3000);
  results.push('진행: 누르면 바로 진행 줄(경과 · 취소)이 보인다');
  await wait(() => /^피드백 반영/.test(document.querySelector('.fde-card.main .fde-headline').textContent) && !document.querySelector('.fde-job'));
  results.push('피드백: 반영한 v2 가 생긴다');

  // 질문
  await click('.fde-talk button', '질문하기');
  await type('.fde-talk textarea', '우선순위는 몇 단계인가요?');
  await click('.fde-talk button', '보내기');
  await wait(() => [...document.querySelectorAll('.fde-fold > summary')].some(s => s.textContent === '질문과 답 1'));
  results.push('질문: 답이 그 결과 아래 "질문과 답"에 쌓인다');

  // 뒤로 가기 — 탭 · 펼침 · 스크롤 복원
  await click('.fde-fold > summary', '자세히');
  await page.evaluate(() => window.scrollTo(0, 300));
  await page.waitForTimeout(400);
  const y0 = await page.evaluate(() => window.scrollY);
  await page.evaluate(() => { location.hash = '#/files'; });
  await wait(() => location.hash.startsWith('#/files'));
  await page.evaluate(() => history.back());
  await wait(() => !!document.querySelector('.fde-panel') && [...document.querySelectorAll('.fde-fold')].some(d => d.open && d.querySelector('summary').textContent.includes('자세히')));
  await page.waitForTimeout(500);
  const back = await page.evaluate(() => ({ y: window.scrollY, tab: document.querySelector('.fde-step.on span').textContent }));
  must(back.tab === '이해' && Math.abs(back.y - y0) < 40, '뒤로 가기: 탭 · 펼침 · 스크롤이 그대로다 ' + JSON.stringify([y0, back]));

  // 계획 — 모듈 · 메인 알고리즘 · 비교표, 해결 방법 고르기
  await open('plan');
  const pl = await page.evaluate(() => ({ main: document.querySelectorAll('.fde-mod.main').length, algo: !!document.querySelector('.fde-algo'),
    rows: document.querySelectorAll('.fde-table tr').length }));
  must(pl.main === 1 && pl.algo && pl.rows === 4, '계획: 메인 알고리즘 모듈이 강조되고 기준 × 해결 방법 표가 있다 ' + JSON.stringify(pl));
  await click('.fde-opt', '탐욕 + 지역 개선');
  await wait(() => /탐욕 \+ 지역 개선 \(D/.test((document.querySelector('.fde-chosen') || {}).textContent || ''));
  results.push('계획: 해결 방법을 고르면 결정으로 남는다');

  // 검증 — 종료 코드로 판정, 계획 다시 만들기 = 알고리즘 v2, 지표가 v2 에 붙는다
  await open('verify');
  await click('.fde-panel button', '모두 실행');
  await wait(() => /통과 2 · 실패 1/.test(document.querySelector('.fde-card.main .fde-headline').textContent));
  results.push('검증: 실패한 명령은 실패로 보인다');
  await click('.fde-panel button', '다시 만들기');
  await wait(() => document.querySelectorAll('.fde-table tr').length === 3 && !document.querySelector('.fde-job'));
  await click('.fde-panel button', '모두 실행');
  await wait(() => [...document.querySelectorAll('.fde-table tr')].some(tr => tr.firstChild.textContent === 'v2' && /380/.test(tr.textContent)));
  results.push('검증: 계획을 다시 만들면 알고리즘 v2 가 되고 새 지표가 v2 행에 붙는다');
  await shot('fde-verify-after-412');

  // 사용 — 기록과 결정 기록
  await open('use');
  await type('.fde-panel textarea', '점검 사진을 첨부하고 싶다');
  await click('.fde-panel button', '남기기');
  await wait(() => [...document.querySelectorAll('.fde-dec')].some(d => /점검 사진을 첨부하고 싶다/.test(d.textContent)));
  const log = await page.evaluate(() => [...document.querySelectorAll('.fde-dec-a')].map(a => a.textContent).join('|'));
  must(/이동 시간 먼저/.test(log) && /탐욕 \+ 지역 개선/.test(log), '사용: 결정 기록에 이해 · 계획의 선택이 모두 있다');
  await check('412 use after');
  await shot('fde-use-after-412');
  console.log(JSON.stringify({ ok: true, results, shotErrors }, null, 1));
} catch (e) {
  await shot('fde-fail');
  console.log(JSON.stringify({ ok: false, error: String(e && e.stack || e), results, shotErrors }, null, 1));
  throw e;
}
