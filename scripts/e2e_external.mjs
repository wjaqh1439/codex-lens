// codex-lens 브라우저 회귀(412×915) — 이 앱 밖(Codex 앱·CLI 등)에서 돌고 있는 세션의 진행 상태.
// scripts/browser_check.py --external 이 BASE·OUT 을 앞에 붙여 실행한다(시드: tests/seed.py external_threads).
const fs = await import("node:fs/promises");
const task = globalThis.browserTask;
const page = task.page("p1");
const results = [];
function must(cond, msg) { if (!cond) throw new Error("단언 실패: " + msg); results.push("ok " + msg); }
const shotErrors = [];
async function shot(name) {
  if (shotErrors.length >= 2) return;
  try { await page.screenshot({ path: OUT + "/" + name + ".png" }); } catch (e) { shotErrors.push(name + ": " + String(e.message || e).slice(0, 120)); }
}
async function save(extra) { await fs.writeFile(OUT + "/result.json", JSON.stringify({ results, shotErrors, ...extra }, null, 1)); }
async function waitSel(css, timeout = 15000) {
  await page.waitForFunction((c) => { const e = document.querySelector(c); return !!e && e.getClientRects().length > 0; }, css, { timeout });
}
const S = (n) => "01a1aaaa00000000000000000000000" + n;
const S1 = S(1), S3 = S(3), S4 = S(4);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

try {
  await page.cdp("Emulation.setDeviceMetricsOverride", { width: 412, height: 915, deviceScaleFactor: 2, mobile: true });
  await page.goto(BASE);
  await waitSel(".srow", 15000);
  const list = await page.evaluate((ids) => {
    const badge = (id) => { const r = document.querySelector('.srow[data-sid="' + id + '"]'); const b = r && r.querySelector(".badge.run, .badge.warn, .badge.dim:not(:empty), .badge.bad");
      return r ? { state: r.dataset.state || "", text: [...r.querySelectorAll(".srow-meta .badge")].map((x) => x.textContent).join(","), title: b ? b.title : "" } : null; };
    const out = {}; ids.forEach((id) => { out[id] = badge(id); });
    return { rows: out, first: document.querySelector(".srow").dataset.sid, overflow: document.documentElement.scrollWidth - innerWidth,
      wraps: [...document.querySelectorAll(".srow-meta .badge")].filter((b) => b.getClientRects().length > 1).length };
  }, [S(1), S(3), S(4), S(5), S(6), S(7)]);
  const r = list.rows;
  must(r[S(3)].state === "running" && /실행 중/.test(r[S(3)].text) && r[S(3)].title.includes("이 앱 밖"), "다른 곳에서 도는 세션이 '실행 중'(" + r[S(3)].text + ")");
  must(r[S(4)].state === "interrupted" && r[S(4)].text.includes("멈춤"), "마지막 턴을 멈춘 세션은 '멈춤'");
  must(r[S(5)].state === "unfinished" && r[S(5)].text.includes("끝나지 않음"), "끝 기록 없이 끊긴 세션은 '끝나지 않음'");
  must(r[S(6)].state === "failed" && r[S(6)].text.includes("실패"), "실패한 세션은 '실패'");
  must(r[S(7)].state === "error" && r[S(7)].text.includes("시스템 오류"), "시스템 오류 세션은 '시스템 오류'");
  must(!r[S(1)].state, "완료된 세션은 상태 표시가 없다");
  must(list.first === S3 && list.overflow <= 0 && list.wraps === 0, "기록이 방금 바뀐 세션이 맨 위, 가로 넘침·배지 줄바꿈 없음");
  await shot("ext-01-list");
  // 진행 중인 행이 있으면 목록을 10초마다 다시 읽는다
  const n0 = await page.evaluate(() => performance.getEntriesByType("resource").filter((e) => /\/api\/sessions\?/.test(e.name)).length);
  await sleep(11500);
  const n1 = await page.evaluate(() => performance.getEntriesByType("resource").filter((e) => /\/api\/sessions\?/.test(e.name)).length);
  must(n1 > n0, "진행 중인 행이 있으면 목록을 다시 읽는다(" + n0 + " → " + n1 + ")");

  await page.click('css=.srow[data-sid="' + S3 + '"]', { label: "다른 곳에서 도는 세션 열기" });
  await waitSel(".turn .live-status", 15000);
  const t = await page.evaluate(() => {
    const turn = [...document.querySelectorAll(".turn")].pop();
    const send = document.querySelector(".composer .composer-bar button.primary:last-child") || [...document.querySelectorAll(".composer .composer-bar button")].pop();
    return { live: turn.querySelector(".live-status").innerText, badges: [...turn.querySelectorAll(".turn-meta .badge")].map((b) => b.textContent),
      ans: turn.querySelector(".part.ans") ? turn.querySelector(".part.ans").innerText : "", halt: !!turn.querySelector(".turn-halt"), sum: !!turn.querySelector(".sum-btn"),
      send: send ? send.textContent + ":" + send.disabled : "", hint: document.querySelector(".composer-hint").textContent,
      steer: (() => { const r = document.querySelector(".composer .steer-row"); return r && !r.hidden ? r.innerText.replace(/\n/g, " ") : ""; })(),
      stop: (() => { const b = document.querySelector(".composer-bar button.stop"); return !!b && !b.hidden; })() };
  });
  // 이 앱에서 도는 턴과 같은 모양 — 어디서 도는지는 머리에 따로 적지 않는다
  must(t.live.includes("명령 실행 중") && !t.live.includes("다른 곳"), "턴에 '명령 실행 중'과 경과·마지막 신호만 보이고 '다른 곳' 접두어는 없다(" + t.live.replace(/\n/g, " ") + ")");
  // 답이 아직 없는 진행 중 턴은 답 자리를 비우고 과정 제목 옆 스피너로 진행을 보인다(b342ffe)
  must(!t.badges.includes("멈춤") && !t.halt && !t.ans, "진행 중인 턴을 '멈춤'으로 보이지 않고 빈 답 자리를 두지 않는다");
  must(!t.sum, "진행 중인 턴에는 과정 요약 단추가 없다");
  must(t.steer.includes("응답 중에 보내면") && t.steer.includes("바로 넣기") && t.steer.includes("턴 끝나고") && t.send === "넣기:false" && !t.hint.includes("다른 곳"),
    "다른 곳에서 진행 중이어도 응답 중과 같은 '바로 넣기 · 턴 끝나고' 선택줄과 넣기 단추가 켜져 있고 안내 문구는 없다(" + t.steer + " · " + t.send + " · " + t.hint + ")");
  must(!t.stop, "이 앱이 멈출 수 없는 다른 곳의 턴에는 멈춤 단추를 두지 않는다");
  await shot("ext-02-running");
  const sig0 = await page.evaluate(() => +document.querySelector(".live-status").dataset.lastSignal);
  await sleep(7000);
  const sig1 = await page.evaluate(() => { const l = document.querySelector(".live-status"); return l ? +l.dataset.lastSignal : 0; });
  must(sig1 > sig0, "다시 읽으면 마지막 신호가 새로 바뀐다(" + sig0 + " → " + sig1 + ")");

  // CLI 의 Claude 세션: 하위 에이전트가 1분째 긴 명령(제한 10분)을 돌린다 — 조용해도 '지연'이 아니다(seed.build_claude_subagent)
  const C9 = "c1a0de00-0000-4000-8000-000000000009";
  await page.goto(BASE);
  await waitSel('.srow[data-sid="' + C9 + '"]', 15000);
  const c9row = await page.evaluate((id) => { const r = document.querySelector('.srow[data-sid="' + id + '"]'); const b = r.querySelector(".badge[data-signal]");
    return { state: r.dataset.state || "", text: b ? b.textContent : "", title: b ? b.title : "" }; }, C9);
  must(c9row.state === "running" && c9row.text === "실행 중" && c9row.title.includes("명령이 끝나기를 기다리는 중"),
    "하위 에이전트가 긴 명령을 돌리는 세션은 1분 조용해도 '실행 중'(" + c9row.text + " · " + c9row.title + ")");
  await page.click('css=.srow[data-sid="' + C9 + '"]', { label: "하위 에이전트가 일하는 Claude 세션 열기" });
  await waitSel(".turn .live-status", 15000);
  const c9 = await page.evaluate(() => { const l = document.querySelector(".turn .live-status"); return { text: l.innerText, stalled: l.classList.contains("stalled") }; });
  must(!c9.stalled && !c9.text.includes("지연") && c9.text.includes("하위 에이전트 진행 중 · 명령 실행 중 · nice -n 5")
    && c9.text.includes("명령이 끝나야 기록됨") && c9.text.includes("제한 시간까지"),
    "진행 표시는 하위 에이전트가 돌리는 명령과 제한 시간을 보이고 '지연'이 아니다(" + c9.text.replace(/\n/g, " ") + ")");
  await shot("ext-02b-subagent-quiet");

  // Claude 하위 에이전트 단계의 머리에서 바로 그 하위 기록(agent-<ID>)을 세션으로 연다 — 부모 링크가 있고 읽기만 된다
  await page.evaluate(() => { const t = document.querySelector(".turn"); const s = t.querySelector(".steps"); if (!s || s.hidden) { const b = [...t.querySelectorAll("[aria-expanded=false]")].find((x) => !x.closest(".step")); if (b) b.click(); } });
  await waitSel("details.step .step-open", 5000);
  const c9open = await page.evaluate(() => { const a = document.querySelector("details.step .step-open"); const sm = a.closest("summary");
    return { href: a.getAttribute("href"), text: a.textContent, wrap: a.getClientRects().length > 1, fits: sm.scrollWidth <= sm.clientWidth + 1 }; });
  must(c9open.href === "#/s/agent-c9" && c9open.text === "세션 ›" && !c9open.wrap && c9open.fits, "Claude 하위 에이전트 단계 머리에 '세션 ›' 링크(" + c9open.href + ")");
  await page.click("css=details.step .step-open", { label: "하위 에이전트 세션으로" });
  await page.waitForFunction(() => location.hash === "#/s/agent-c9" && document.querySelector(".turn") && document.querySelector(".parent-link"), null, { timeout: 10000 });
  const c9sub = await page.evaluate(() => ({ parent: document.querySelector(".parent-link").getAttribute("href"), head: document.querySelector(".parent-link").textContent,
    hint: (document.querySelector(".composer-hint") || {}).textContent || "",
    turns: document.querySelectorAll(".turn").length,
    sumBtn: !!document.querySelector(".sum-btn") }));
  must(c9sub.parent === "#/s/" + C9 && c9sub.turns === 1 && c9sub.hint.includes("읽기만") && !c9sub.sumBtn,
    "하위 에이전트 세션: 상위 세션 링크·턴 1개·보내기 막힘 안내·과정 요약 없음(" + c9sub.head + " · " + c9sub.hint + ")");
  await shot("ext-02c-subagent-session");
  await page.click("css=.parent-link", { label: "상위 세션으로" });
  await page.waitForFunction((id) => location.hash.includes(id), C9, { timeout: 10000 });

  await page.goto(BASE + "#/s/" + S4);
  await waitSel(".turn", 15000);
  const s4 = await page.evaluate(() => ({ halts: [...document.querySelectorAll(".turn")].map((x) => { const h = x.querySelector(".turn-halt .halt-msg b"); return h ? h.textContent : ""; }),
    badges: [...document.querySelectorAll(".turn-meta .badge")].map((b) => b.textContent),
    live: !!document.querySelector(".live-status") }));
  // 멈춤·끝나지 않음은 머리 배지가 아니라 답변 자리의 안내 줄이 알린다(0.18.0)
  must(s4.halts[0] === "끝나지 않고 멈췄습니다" && s4.halts[1] === "사용자가 중간에 멈췄습니다" && !s4.badges.length && !s4.live,
    "끝 기록 없이 끊긴 턴은 '끝나지 않고 멈췄습니다', 사용자가 멈춘 턴은 '사용자가 중간에 멈췄습니다'(" + s4.halts.join(" | ") + " · 배지 " + s4.badges.join() + ")");
  await shot("ext-03-unfinished");

  // 하위 에이전트 — 대상을 비운 wait 도 그 턴의 하위 에이전트 이름으로 보이고, 펼치면 하위 세션의 마지막 말과 '하위 세션 열기'가 있다
  await page.goto(BASE + "#/s/" + S(8));
  await waitSel(".turn", 15000);
  await page.evaluate(() => { const t = document.querySelector(".turn"); const s = t.querySelector(".steps"); if (!s || s.hidden) [...t.querySelectorAll("[aria-expanded=false]")].find((b) => !b.closest(".step")).click(); });
  await waitSel("details.step", 5000);
  const ag = await page.evaluate(() => [...document.querySelectorAll("details.step")].map((d) => d.querySelector(".skind").textContent + "|" + d.querySelector(".st").textContent + "|" + d.querySelector(".sx").textContent));
  const codexOpen = await page.evaluate(() => [...document.querySelectorAll("details.step .step-open")].map((a) => a.getAttribute("href")));
  must(codexOpen.length >= 1 && codexOpen.every((h) => h.startsWith("#/s/")), "Codex 하위 에이전트 단계 머리에도 '세션 ›' 링크(" + codexOpen.join() + ")");
  must(ag.includes("하위 에이전트|시작 · integrate_archive|gpt-fake-sol high") && ag.includes("하위 에이전트|기다림 · integrate_archive|"), "하위 에이전트 단계가 동작·대상 이름으로 보인다(" + ag.join(" / ") + ")");
  await page.evaluate(() => { const d = [...document.querySelectorAll("details.step")].find((x) => x.querySelector(".st").textContent.startsWith("기다림")); d.open = true; d.scrollIntoView({ block: "center" }); });
  await page.waitForFunction(() => { const p = document.querySelector("details.step[open] .agent-peek"); return p && !/읽는 중/.test(p.textContent); }, null, { timeout: 10000 });
  const peek = await page.evaluate(() => { const b = document.querySelector("details.step[open] .step-body");
    return { text: b.innerText, open: [...b.querySelectorAll("button")].map((x) => x.textContent).join(), wrap: [...b.querySelectorAll(".agent-head button")].filter((x) => x.getClientRects().length > 1).length,
      overflow: document.documentElement.scrollWidth - innerWidth }; });
  must(peek.text.includes("통합 완료 — 테스트 92개 통과") && peek.text.includes("끝남") && peek.open.includes("하위 세션 열기"), "펼친 기다림에 하위 세션의 상태·마지막 말·열기 단추(" + peek.text.replace(/\n/g, " ") + ")");
  must(peek.wrap === 0 && peek.overflow <= 0, "하위 에이전트 칸에 단추 줄바꿈·가로 넘침이 없다");
  await shot("ext-04-subagent");
  await page.evaluate(() => [...document.querySelectorAll("details.step[open] .agent-head button")].find((b) => b.textContent === "하위 세션 열기").click());
  await page.waitForFunction((id) => location.hash.includes(id) && document.querySelector(".turn"), S(9), { timeout: 10000 });
  const childTitle = await page.evaluate(() => document.title + " | " + document.body.innerText.slice(0, 400));
  must(childTitle.includes("하위 에이전트 · integrate_archive (Lorentz)"), "하위 세션 열기로 하위 세션에 가고 제목이 '하위 에이전트 · 이름 (별명)'");
  // --- 다른 곳에서 도는 턴 뒤로 미룬 보내기: 바로 넣기·턴 끝나고 모두 대기열로 가고, 턴이 끝나면 서버가 한 번씩 보낸다
  const path = await import("node:path");
  const endFile = path.join(path.dirname(PROJ), "external-end");   // 가짜 app-server 가 읽는 '밖의 턴이 끝났다' 신호(browser_check.py 가 같은 경로를 준다)
  await fs.rm(endFile, { force: true });
  await page.goto(BASE + "#/s/" + S3);
  await waitSel(".composer .steer-row", 15000);
  await page.evaluate(() => localStorage.removeItem("codex-lens.sendMode"));
  await page.goto(BASE + "#/s/" + S3);
  await page.waitForFunction(() => { const r = document.querySelector(".composer .steer-row"); return r && !r.hidden; }, null, { timeout: 15000 });
  const turnsBefore = await page.evaluate(() => document.querySelectorAll(".turn").length);
  await page.fill("css=.composer textarea", "끼워 넣고 싶은 말");
  await page.click("css=.composer-bar button.primary", { label: "넣기" });
  await page.waitForFunction(() => /다음 턴으로 보냅니다/.test(document.getElementById("toast").textContent), null, { timeout: 8000 });
  await page.waitForFunction(() => /턴이 끝나면 차례로 보냄 · 1/.test((document.querySelector(".queue") || {}).textContent || ""), null, { timeout: 8000 });
  must(true, "다른 곳의 턴에 '넣기'를 누르면 끼워 넣지 못해 대기열로 가고 그렇게 알린다");
  await page.evaluate(() => [...document.querySelectorAll(".composer .mode-seg button")].find((b) => b.textContent === "턴 끝나고").click());
  await page.waitForFunction(() => document.querySelector(".composer-bar button.primary").textContent === "대기열에", null, { timeout: 5000 });
  await page.fill("css=.composer textarea", "턴 끝나고 보낼 말");
  await page.click("css=.composer-bar button.primary", { label: "대기열에" });
  await page.waitForFunction(() => /턴이 끝나면 차례로 보냄 · 2/.test((document.querySelector(".queue") || {}).textContent || ""), null, { timeout: 8000 });
  const q = await page.evaluate(() => {
    const bar = document.querySelector(".composer-bar"), btns = [...bar.querySelectorAll("button")].filter((b) => !b.hidden && b.getClientRects().length);
    return { head: document.querySelector(".queue-head").textContent, go: [...document.querySelectorAll(".qitem button")].map((b) => b.textContent),
      wraps: btns.filter((b) => b.getBoundingClientRect().height > 48).length, fits: bar.scrollWidth <= bar.clientWidth + 1,
      rowFits: (() => { const r = document.querySelector(".composer .steer-row").getBoundingClientRect(); return r.right <= innerWidth + 1; })() };
  });
  must(q.head === "턴이 끝나면 차례로 보냄 · 2" && !q.go.includes("바로 넣기") && q.go.every((x) => x === "빼기"),
    "대기열 머리글은 응답 중과 같은 문구이고 끼워 넣을 수 없는 항목에는 '바로 넣기'가 없다(" + q.head + " · " + q.go.join() + ")");
  must(q.wraps === 0 && q.fits && q.rowFits, "412 폭에서 입력창 단추 줄이 줄바꿈·넘침 없이 한 줄이다");
  await shot("ext-05-queued");
  await sleep(4000);
  must((await page.evaluate(() => document.querySelectorAll(".turn").length)) === turnsBefore, "다른 곳의 턴이 도는 동안에는 이 앱이 새 턴을 열지 않는다");
  await fs.writeFile(endFile, S3);   // 밖의 턴이 끝났다
  await page.waitForFunction(() => { const t = [...document.querySelectorAll(".turn .part.req")].map((x) => x.innerText).join("\n"); return t.includes("끼워 넣고 싶은 말") && t.includes("턴 끝나고 보낼 말"); }, null, { timeout: 40000 });
  await page.waitForFunction(() => !document.querySelector(".queue .qitem"), null, { timeout: 40000 });
  await sleep(4000);
  const sent = await page.evaluate(() => { const t = [...document.querySelectorAll(".turn .part.req")].map((x) => x.innerText).join("\n"); const n = (x) => t.split(x).length - 1;
    return { a: n("끼워 넣고 싶은 말"), b: n("턴 끝나고 보낼 말"), turns: document.querySelectorAll(".turn").length }; });
  must(sent.a === 1 && sent.b === 1 && sent.turns === turnsBefore + 2, "턴이 끝나면 화면 조작 없이 두 메시지가 차례로 한 번씩 보내진다(" + JSON.stringify(sent) + ")");
  await shot("ext-06-sent");
  await fs.rm(endFile, { force: true });
  await save({ ok: true });
  console.log(JSON.stringify({ ok: true, results }, null, 1));
} catch (e) {
  await shot("ext-zz-failure").catch(() => {});
  await save({ ok: false, error: String(e && e.stack || e) });
  console.log(JSON.stringify({ ok: false, error: String(e && e.message || e), results }, null, 1));
  throw e;
}
