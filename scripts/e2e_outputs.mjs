// browser_check.py --outputs-only: agent-output/1 최종 답의 타입별 카드(할 일 · 정할 것 · 메인 반영)를 1280·412×915 에서 검사한다.
// 시드는 tests/seed.py output_threads — 두 세션 모두 가짜 app-server 라 보낸 글은 새 턴으로만 돌아온다.
const fs = await import('node:fs/promises');
const task = globalThis.browserTask, page = task.page('p1');
const results = [], shotErrors = [];
const S1 = '01a1aaaa0000000000000000000000d1', S2 = '01a1aaaa0000000000000000000000d2';
function must(ok, message) { if (!ok) throw new Error(message); results.push(message); }
async function wait(fn, arg, timeout = 15000) { await page.waitForFunction(fn, arg, { timeout }); }
async function shot(name) {
  if (shotErrors.length >= 1) return;
  try { await page.screenshot({ path: OUT + '/' + name + '.png' }); }
  catch (e) { shotErrors.push(String(e)); }
}
async function openSession(id) {
  await page.goto(BASE + '#/s/' + id);
  await wait(() => !!document.querySelector('.out-report'));
}
async function layout() {
  return page.evaluate(() => {
    const r = document.querySelector('.out-report'), W = document.documentElement.clientWidth;
    const over = [...r.querySelectorAll('*')].filter(e => e.getBoundingClientRect().right > W + 0.5).map(e => e.className || e.tagName);
    const wrapped = [...r.querySelectorAll('button')].filter(b => b.getBoundingClientRect().height > 44).map(b => b.textContent);
    return { kinds: [...r.querySelectorAll('.out-sec')].map(s => s.dataset.kind), over, wrapped,
      jsonShown: /"protocol"|agent-output/.test(document.querySelector('.turn .part.ans').textContent),
      body: document.querySelector('.turn .answer').textContent };
  });
}
// 같은 모양의 요소가 여럿이라 DOM 에서 직접 고른다(사용자 조작과 같은 click·input 이벤트)
async function clickAt(sel, i = 0) { await page.evaluate(([s, n]) => document.querySelectorAll(s)[n].click(), [sel, i]); }
async function typeAt(sel, i, v) {
  await page.evaluate(([s, n, val]) => { const e = document.querySelectorAll(s)[n]; e.focus(); e.value = val; e.dispatchEvent(new Event('input', { bubbles: true })); }, [sel, i, v]);
}
async function turnCount() { return page.evaluate(() => document.querySelectorAll('.turn').length); }
try {
  for (const [w, h, mobile] of [[1280, 900, false], [412, 915, true]]) {
    const tag = w + '';
    await page.cdp('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 1, mobile });
    await openSession(S1);
    const l = await layout();
    must(l.kinds.join() === 'decisions,merge,todo', tag + ': 정할 것 · 메인 반영 · 할 일 카드가 차례로 보인다 ' + l.kinds);
    must(!l.jsonShown && /진행 상태/.test(l.body), tag + ': 답변은 본문만 보이고 JSON 원문이 드러나지 않는다');
    must(!l.over.length, tag + ': 카드가 가로로 넘치지 않는다 ' + JSON.stringify(l.over));
    must(!l.wrapped.length, tag + ': 단추 글이 줄바꿈되지 않는다 ' + JSON.stringify(l.wrapped));
    const todo = await page.evaluate(() => [...document.querySelectorAll('.out-sec[data-kind="todo"] .out-item')].map(i => ({
      owner: i.querySelector('.badge').textContent, put: [...i.querySelectorAll('button')].some(b => b.textContent === '입력창에 넣기') })));
    must(todo.length === 2 && todo[0].owner === '에이전트' && todo[0].put && todo[1].owner === '직접' && !todo[1].put,
      tag + ': 할 일은 주인별로 표시되고 에이전트 몫만 입력창에 넣는다');
    const dec = await page.evaluate(() => ({
      boxes: [...document.querySelectorAll('.out-q')].map(q => q.querySelectorAll('input[type=checkbox]').length),
      free: document.querySelectorAll('.out-q .out-free').length,
      rec: document.querySelectorAll('.out-q .badge.run').length,
      send: document.querySelector('.out-sec[data-kind="decisions"] button.primary').disabled }));
    must(dec.boxes.join() === '3,3' && dec.free === 2 && dec.rec === 3 && dec.send,
      tag + ': 모든 질문이 여러 개 고르기·직접 입력을 받고, 추천이 표시되며, 고르기 전에는 보낼 수 없다 ' + JSON.stringify(dec));
    if (w === 1280) await shot('outputs-1280');
  }

  // 단일 선택 질문에서도 여러 개를 고르고, 직접 입력과 함께 새로고침 뒤에도 남는다
  // 첫 질문(단일 선택으로 낸 질문)의 앞 두 선택지 — 질문마다 선택지가 3개라 0·1 번
  await clickAt('.out-q .out-opt input', 0);
  await clickAt('.out-q .out-opt input', 1);
  await typeAt('.out-q .out-free', 1, '주말에는 보내지 않기');
  await openSession(S1);
  const kept = await page.evaluate(() => ({
    on: [...document.querySelectorAll('.out-q')].map(q => [...q.querySelectorAll('input:checked')].length),
    free: document.querySelectorAll('.out-q .out-free')[1].value,
    send: document.querySelector('.out-sec[data-kind="decisions"] button.primary').disabled }));
  must(kept.on.join() === '2,0' && kept.free === '주말에는 보내지 않기' && !kept.send, '고른 답·직접 입력이 다시 열어도 남고 보낼 수 있다 ' + JSON.stringify(kept));
  await shot('outputs-412-picked');

  // 할 일을 입력창에 넣는다(보내지 않는다)
  await page.evaluate(() => [...document.querySelectorAll('.out-sec[data-kind="todo"] button')].find(b => b.textContent === '입력창에 넣기').click());
  const ta = await page.evaluate(() => document.querySelector('.composer textarea').value);
  must(/이어서 해 줘: 확인이 끝나면 데모를 내린다/.test(ta), '에이전트 할 일을 입력창에 넣는다');
  await typeAt('.composer textarea', 0, '');

  // 답 보내기 → 같은 세션의 새 턴 요청으로 간다. 보낸 뒤 카드는 잠기고 다시 눌러도 턴이 늘지 않는다
  const before = await turnCount();
  await page.click('css=.out-sec[data-kind="decisions"] button.primary');
  await wait(n => document.querySelectorAll('.turn').length === n + 1, before);
  const req = await page.evaluate(() => [...document.querySelectorAll('.turn')].pop().querySelector('.req-body').textContent);
  must(/정할 것에 답합니다/.test(req) && /마지막 턴 상태를 모두 보인다 \/ 실행 중과 지연만 더한다/.test(req) && /직접 입력: 주말에는 보내지 않기/.test(req),
    '고른 답(여러 개)과 직접 입력이 질문과 함께 새 턴으로 간다');
  await wait(() => [...document.querySelectorAll('.turn')].pop().querySelector('.answer') !== null);
  const after = await page.evaluate(() => ({
    status: document.querySelector('.out-status').textContent,
    locked: [...document.querySelectorAll('.out-report button.primary, .out-report input')].every(e => e.disabled) }));
  must(/답을 보냄/.test(after.status) && after.locked, '보낸 카드는 보낸 기록을 보이고 모든 동작이 잠긴다 ' + JSON.stringify(after));
  must(await turnCount() === before + 1, '중복 전송이 없다');

  // 메인 반영 — 한 번 누르면 "메인 반영해 줘" 가 간다
  await openSession(S2);
  const kinds = await page.evaluate(() => [...document.querySelectorAll('.out-sec')].map(s => s.dataset.kind).join());
  must(kinds === 'merge', '메인 반영만 있는 답은 메인 반영 카드만 보인다');
  const mb = await page.evaluate(() => ({ text: document.querySelector('.out-sec[data-kind="merge"] .out-head').textContent,
    summary: document.querySelector('.out-sec[data-kind="merge"] .md').textContent.trim(),
    checks: document.querySelectorAll('.out-checks li').length }));
  must(/v0\.15\.2 · patch/.test(mb.text) && mb.checks === 1, '버전·종류와 끝낸 확인이 보인다 ' + JSON.stringify(mb));
  must(/^답변 속 표가/.test(mb.summary), '요약 앞의 버전은 배지와 겹치지 않게 뗀다 ' + mb.summary);
  await shot('outputs-412-merge');
  const b2 = await turnCount();
  await page.click('css=.out-sec[data-kind="merge"] button.primary');
  await wait(n => document.querySelectorAll('.turn').length === n + 1, b2);
  const mreq = await page.evaluate(() => [...document.querySelectorAll('.turn')].pop().querySelector('.req-body').textContent);
  must(/메인 반영해 줘 \(v0\.15\.2 · patch\)/.test(mreq), '메인 반영 단추가 같은 세션에 반영 요청을 보낸다');
  await wait(() => /메인 반영을 요청함/.test(document.querySelector('.out-status').textContent));
  must(await page.evaluate(() => document.querySelector('.out-sec[data-kind="merge"] button.primary').disabled), '반영 요청 뒤 단추가 잠긴다');
  await fs.writeFile(OUT + '/result.json', JSON.stringify({ ok: true, results, shotErrors }, null, 2));
  console.log(JSON.stringify({ ok: true, results, shotErrors }, null, 1));
} catch (e) {
  await shot('outputs-fail');
  await fs.writeFile(OUT + '/result.json', JSON.stringify({ ok: false, error: String(e), results, shotErrors }, null, 2));
  console.log(JSON.stringify({ ok: false, error: String(e), results, shotErrors }, null, 1));
  process.exitCode = 1;
}
