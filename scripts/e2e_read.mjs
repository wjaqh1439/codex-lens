// 세션 첫 읽기의 과정 표시 회귀 — 가짜 데모에서만 실행한다(python3 scripts/browser_check.py --read-only).
// 읽기 요청을 붙잡아 단계·경과·지연·멈추기·다시 읽기를 DOM 으로 확인한다.
const fs = await import('node:fs/promises');
const page = globalThis.browserTask.page('p1');
const results = [], shotErrors = [];
function must(ok, label) { if (!ok) throw new Error(label); results.push(label); }
async function wait(fn, arg, timeout = 15000) { await page.waitForFunction(fn, arg, { timeout }); }
async function screenshot(name) {
  try { await page.screenshot({ path: OUT + '/' + name + '.png' }); }
  catch (e) { shotErrors.push(String(e.message || e)); }
}
// 다음 세션 읽기 하나를 붙잡는다 — releaseRead() 로 보내고, 멈추기(abort)는 그대로 전한다
async function holdNextRead() {
  await page.evaluate(() => {
    const original = window.__origFetch || (window.__origFetch = window.fetch);
    window.fetch = function (url, options) {
      if (/^\/api\/sessions\/[^/?]+$/.test(String(url))) {
        window.fetch = original;
        return new Promise((resolve, reject) => {
          const sig = options && options.signal;
          if (sig) sig.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
          window.releaseRead = () => resolve(original(url, options));
        });
      }
      return original(url, options);
    };
  });
}
const proc = () => page.evaluate(() => {
  const b = document.querySelector('#view .read-proc');
  if (!b) return null;
  return {
    title: b.querySelector('.read-proc-title b').textContent,
    clock: b.querySelector('.read-proc-title .counts').textContent,
    stalled: !b.querySelector('.badge.warn').hidden,
    steps: [...b.querySelectorAll('.read-step')].map((s) => ({ key: s.dataset.step, cls: s.className, name: s.querySelector('.name').textContent,
      time: s.querySelector('.time').textContent, spin: s.querySelector('.mark').classList.contains('proc-spinner') })),
    buttons: [...b.querySelectorAll('button')].filter((x) => !x.hidden).map((x) => x.textContent),
  };
});
let sid = null;
try {
  for (const [w, h, mobile] of [[1280, 900, false], [412, 915, true]]) {
    const tag = w + 'x' + h;
    await page.cdp('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 1, mobile });
    await page.goto(BASE);
    await wait(() => document.querySelectorAll('.srow').length > 0);
    sid = sid || await page.evaluate(() => document.querySelector('.srow').dataset.sid);
    await holdNextRead();
    await page.goto(BASE + '#/s/' + encodeURIComponent(sid));
    await wait(() => !!document.querySelector('#view .read-proc'));
    let p = await proc();
    must(p.title === '세션 읽는 중' && p.steps.length === 3 && p.steps[0].cls.includes('now') && p.steps[0].spin
      && p.steps[1].cls.includes('todo') && p.steps[2].cls.includes('todo'), tag + ' 첫 단계(기록 읽기)가 돌고 나머지는 대기');
    const c0 = p.clock;
    await new Promise((r) => setTimeout(r, 1300));
    p = await proc();
    must(p.clock !== c0 && p.steps[0].time !== '', tag + ' 경과 시간이 오른다');
    must(await page.evaluate(() => {
      const b = document.querySelector('#view .read-proc');
      const wraps = [...b.querySelectorAll('button, .read-step, .read-proc-title')].some((x) => x.getBoundingClientRect().height > 44);
      return !wraps && document.documentElement.scrollWidth <= innerWidth;
    }), tag + ' 단추·단계 줄바꿈 없음·가로 넘침 없음');
    // 30초 무신호면 지연 — 시계를 앞당겨 본다
    await page.evaluate(() => { const real = Date.now.bind(Date); window.__dateNow = real; Date.now = () => real() + 31000; });
    await wait(() => !document.querySelector('#view .read-proc .badge.warn').hidden);
    must((await proc()).stalled, tag + ' 30초 무신호면 지연 표시');
    await page.evaluate(() => { Date.now = window.__dateNow; });
    await screenshot('read-waiting-' + tag);
    // 멈추기 → 다시 읽기
    await page.click('css=#view .read-proc button.link', { label: '읽기 멈추기' });
    await wait(() => document.querySelector('#view .read-proc b')?.textContent === '읽기를 멈췄습니다');
    p = await proc();
    must(p.steps[0].cls.includes('failed') && p.buttons.includes('다시 읽기') && !p.buttons.includes('멈추기'), tag + ' 멈추면 그 단계에 표시하고 다시 읽기를 준다');
    await holdNextRead();
    await page.click('css=#view .read-proc button:not(.link)', { label: '다시 읽기' });
    await wait(() => (document.querySelector('#view .read-proc b') || {}).textContent === '세션 읽는 중');
    await page.evaluate(() => window.releaseRead());
    await wait(() => !!document.querySelector('#view .turn'));
    must(await page.evaluate(() => !document.querySelector('#view .read-proc')), tag + ' 다 그리면 과정 표시가 사라지고 턴이 보인다');
    // 실패하면 오류와 다시 읽기
    await page.evaluate(() => {
      const original = window.__origFetch;
      window.fetch = function (url, options) {
        if (/^\/api\/sessions\/[^/?]+$/.test(String(url))) { window.fetch = original; return Promise.resolve(new Response(JSON.stringify({ error: '읽기 실패 회귀' }), { status: 503 })); }
        return original(url, options);
      };
    });
    await page.goto(BASE + '#/');
    await page.goto(BASE + '#/s/' + encodeURIComponent(sid));
    await wait(() => document.querySelector('#view .read-proc b')?.textContent === '세션을 읽지 못했습니다');
    p = await proc();
    must(p.steps[1].cls.includes('failed') && p.steps[0].cls.includes('done') && p.buttons.includes('다시 읽기')
      && await page.evaluate(() => document.querySelector('#view .read-proc-err').textContent.includes('읽기 실패 회귀')), tag + ' 실패하면 실패한 단계·오류·다시 읽기');
    await screenshot('read-failed-' + tag);
    await page.click('css=#view .read-proc button:not(.link)', { label: '실패 뒤 다시 읽기' });
    await wait(() => !!document.querySelector('#view .turn'));
    must(true, tag + ' 실패 뒤 다시 읽기로 열린다');
  }
  // 서버가 단계별 시간을 싣는다
  const r = JSON.parse((await page.fetch('/api/sessions/' + encodeURIComponent(sid))).body);
  must(r.timing && Number.isInteger(r.timing.read_ms) && Number.isInteger(r.timing.build_ms), '응답에 서버 단계 시간(read_ms·build_ms)');
  console.log(JSON.stringify({ results, shotErrors }));
} finally {
  await fs.writeFile(OUT + '/result.json', JSON.stringify({ results, shotErrors }, null, 2));
}
