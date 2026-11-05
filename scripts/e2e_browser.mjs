// codex-lens 브라우저 회귀(412×915). scripts/browser_check.py 가 BASE·OUT·PROJ 를 앞에 붙여 실행한다.
// 단언이 하나라도 깨지면 예외로 끝나고(종료 코드 1) 그때까지의 스크린샷·result.json 을 남긴다.
const fs = await import("node:fs/promises");
const task = globalThis.browserTask;
const page = task.page("p1");
const results = [];
function must(cond, msg) { if (!cond) throw new Error("단언 실패: " + msg); results.push("ok " + msg); }
const shotErrors = [];
// 캡처는 보조 자료다 — 화면이 잠긴 기기에서는 시간 초과가 날 수 있어 실패해도 단언을 멈추지 않는다.
async function shot(name) {
  if (shotErrors.length >= 2) return;
  try { await page.screenshot({ path: OUT + "/" + name + ".png" }); } catch (e) { shotErrors.push(name + ": " + String(e.message || e).slice(0, 120)); }
}
async function save(extra) { await fs.writeFile(OUT + "/result.json", JSON.stringify({ results, shotErrors, ...extra }, null, 1)); }
async function waitFn(fn, arg, timeout = 15000) { await page.waitForFunction(fn, arg, { timeout }); }
async function waitSel(css, timeout = 15000) {
  await page.waitForFunction((c) => { const e = document.querySelector(c); return !!e && e.getClientRects().length > 0; }, css, { timeout });
}
async function checkAppHeader(label) {
  const a = await page.evaluate(() => {
    const app = document.getElementById("app-top"), r = app.getBoundingClientRect();
    const banner = document.getElementById("demo-banner").getBoundingClientRect();
    return { full: r.left === 0 && r.right === innerWidth, top: r.top, bannerBottom: banner.bottom,
      controls: ["app-home", "app-projects", "app-ports", "version"].every((id) => {
        const b = document.getElementById(id).getBoundingClientRect();
        return app.contains(document.getElementById(id)) && b.width > 0 && b.left >= 0 && b.right <= innerWidth;
      }), brand: document.getElementById("app-home").textContent === window.CODEX_LENS.name,
      oneVersion: document.querySelectorAll("#version").length === 1 && !document.querySelector("#pane #version") };
  });
  must(a.full && a.controls && a.brand && a.oneVersion && a.top >= a.bannerBottom - 1,
    label + " 앱 이름·프로젝트·포트·버전이 화면 전체 폭의 공통 상단 바에 남는다");
}
async function checkPaneHeader(label) {
  await checkAppHeader(label);
  const h = await page.evaluate(() => {
    const pane = document.getElementById("pane"), head = document.getElementById("top");
    const p = pane.getBoundingClientRect(), r = head.getBoundingClientRect(), v = document.getElementById("view").getBoundingClientRect();
    const side = document.getElementById("side").getBoundingClientRect(), app = document.getElementById("app-top").getBoundingClientRect();
    return { inside: head.parentElement === pane && pane.contains(document.getElementById("view")),
      aligned: Math.abs(r.left - v.left) < 1 && Math.abs(r.right - v.right) < 1,
      contained: ["back", "pane-back", "title", "subtitle", "sid", "top-actions"].every((id) => {
        const e = document.getElementById(id), b = e.getBoundingClientRect();
        return e.hidden || (b.left >= p.left && b.right <= p.right);
      }),
      top: r.top, appBottom: app.bottom, sideTop: side.top,
      overflow: document.documentElement.scrollWidth - innerWidth };
  });
  must(h.inside && h.aligned && h.contained, label + " 제목·메타·ID·버튼이 세션 본문과 같은 패널 안에 있다");
  must(h.top >= h.appBottom - 1 && h.overflow <= 0, label + " 공통 상단 바와 세션 머리가 겹치지 않고 가로 넘침이 없다");
  must(Math.abs(h.sideTop - h.appBottom) < 1, label + " 왼쪽 목록은 공통 상단 바 바로 아래부터 표시된다");
}
async function checkProcessToggle(label) {
  async function state(open, phase) {
    const s = await page.evaluate(() => {
      const p = document.querySelector(".turn .proc"), steps = p.querySelector(".steps");
      return { open: p.classList.contains("open"), expanded: p.querySelector(".proc-toggle").getAttribute("aria-expanded"),
        hidden: steps.hidden, display: getComputedStyle(steps).display, height: steps.getBoundingClientRect().height,
        count: steps.querySelectorAll(".step").length };
    });
    must(s.open === open && s.expanded === String(open) && s.hidden === !open &&
      (open ? s.display !== "none" && s.height > 0 && s.count > 0 : s.display === "none" && s.height === 0),
      label + " 과정 " + phase + " — 상태와 실제 표시가 일치 " + JSON.stringify(s));
    return s.count;
  }
  // 저장된 펼침 상태와 무관하게 시작한다.
  if (await page.evaluate(() => document.querySelector(".turn .proc-toggle").getAttribute("aria-expanded") === "true"))
    await page.click("css=.turn .proc-toggle >> nth=0");
  await state(false, "처음 접힘");
  await page.click("css=.turn .proc-toggle >> nth=0", { label: "과정 펼치기" });
  const count = await state(true, "펼치기");
  await page.click("css=.turn .proc-toggle >> nth=0", { label: "과정 다시 접기" });
  await state(false, "다시 접기");
  await page.reload();
  await waitSel(".turn");
  await state(false, "새로고침 후 접힘 유지");
  await page.click("css=.turn .proc-toggle >> nth=0", { label: "과정 다시 펼치기" });
  must(await state(true, "다시 펼치기") === count, label + " 다시 펼쳐도 과정 항목 수가 같다");
  const scrolling = await page.evaluate(() => {
    const p = document.querySelector('.turn .proc'), box = p.querySelector('.steps');
    const clones = Array.from({ length: 30 }, () => box.lastElementChild.cloneNode(true));
    clones.forEach(x => box.appendChild(x));
    p.querySelector('.proc-toggle').click(); p.querySelector('.proc-toggle').click();
    const b = box.getBoundingClientRect(), last = box.lastElementChild.getBoundingClientRect();
    const result = { height: b.height, limit: Math.min(400, innerHeight * .45), scrollable: box.scrollHeight > box.clientHeight,
      atEnd: box.scrollHeight - box.clientHeight - box.scrollTop <= 1, lastVisible: last.bottom <= b.bottom + 1 };
    box.scrollTop = 0; result.firstVisible = box.firstElementChild.getBoundingClientRect().top >= b.top - 1;
    clones.forEach(x => x.remove());
    return result;
  });
  must(scrolling.height <= scrolling.limit + 1 && scrolling.scrollable && scrolling.atEnd && scrolling.lastVisible && scrolling.firstVisible,
    label + ' 긴 과정은 제한된 내부 스크롤에서 마지막부터 보이고 처음까지 읽을 수 있다 ' + JSON.stringify(scrolling));
}

try {
  if (!LAYOUT_ONLY) {
  await page.cdp("Emulation.setDeviceMetricsOverride", { width: 412, height: 915, deviceScaleFactor: 2, mobile: true });
  await page.goto(BASE);
  await waitSel(".srow", 15000);
  const list = await page.evaluate(() => ({
    title: document.title, banner: getComputedStyle(document.getElementById("demo-banner")).display,
    rows: document.querySelectorAll(".srow").length, icon: document.querySelector("link[rel=icon]").getAttribute("href"),
    claude: [...document.querySelectorAll(".srow")].filter((r) => r.querySelector(".srow-meta > .agent-ico.claude[aria-label='Claude Code']:first-child")).map((r) => r.querySelector(".srow-title").textContent),
    codex: [...document.querySelectorAll(".srow")].filter((r) => r.querySelector(".srow-meta > .agent-ico.codex[aria-label=Codex]:first-child")).length,
    textBadge: [...document.querySelectorAll(".srow .badge")].filter((b) => /^(Claude|Codex)$/.test(b.textContent)).length,
    overflow: document.documentElement.scrollWidth - window.innerWidth,
  }));
  must(list.title.startsWith("[데모]"), "탭 제목 앞에 [데모]");
  must(list.banner !== "none", "데모 배너가 보인다");
  must(list.icon.includes("demo-"), "데모 아이콘");
  must(list.rows === 4, "기존 세션 4개(Codex 2 · Claude Code 1 · 하위 에이전트 1)가 목록에 보인다");
  must(list.claude.join() === "README 할 일 체크 목록", "Claude Code 세션 첫머리에 Claude 아이콘 " + list.claude.join());
  must(list.codex === 3 && list.textBadge === 0, "Codex 세션(하위 에이전트 포함) 첫머리에 Codex 아이콘, 글자 배지는 없다 " + JSON.stringify(list));
  must(list.overflow <= 0, "목록 가로 넘침 없음");
  await shot("01-list");
  // 세션 안에서 만든 세션(Codex 하위 에이전트)은 부모 아래에 들여 쓰고, 열면 머리에 부모로 가는 길이 있다
  const kids = await page.evaluate(() => {
    const tops = [...document.querySelectorAll(".slist .sgroup")].filter((g) => !g.parentElement.closest(".skids"));
    const parent = tops.find((g) => g.querySelector(":scope > .skids"));
    const kid = parent && parent.querySelector(":scope > .skids .srow");
    const pr = parent && parent.querySelector(":scope > .session-row > .srow").getBoundingClientRect(), kr = kid && kid.getBoundingClientRect();
    return { tops: tops.length, parentTitle: parent ? parent.querySelector(".srow-title").textContent : "", kid: kid ? kid.innerText : "",
      tog: parent ? parent.querySelector(":scope > .skids-tog").textContent : "", indent: kr && pr ? kr.left - pr.left : 0,
      kidRight: kr ? kr.right : 0, w: innerWidth };
  });
  must(kids.tops === 3, "맨 위 줄은 3개 — 하위 세션은 따로 서지 않는다(" + kids.tops + ")");
  must(/나눗셈 분기를 시험/.test(kids.kid) && /Pascal/.test(kids.kid), "하위 에이전트가 부모 아래에 이름표와 함께 보인다: " + kids.kid);
  must(kids.indent >= 12 && kids.kidRight <= kids.w, "하위 세션은 들여 쓰고 화면 폭을 넘지 않는다 " + JSON.stringify(kids));
  must(kids.tog === "▾ 하위 세션 1", "하위 세션 접기 단추: " + kids.tog);
  await page.evaluate(() => document.querySelector(".skids-tog").click());
  must(await page.evaluate(() => document.querySelector(".skids").hidden && document.querySelector(".skids-tog").textContent === "▸ 하위 세션 1"), "하위 세션을 접는다");
  await page.evaluate(() => document.querySelector(".skids-tog").click());
  await page.evaluate(() => document.querySelector(".skids .srow").click());
  await waitSel("#top .parent-link", 10000);
  const up = await page.evaluate(() => ({ text: document.querySelector("#top .parent-link").textContent, href: document.querySelector("#top .parent-link").getAttribute("href") }));
  must(up.text === "↑ " + kids.parentTitle && /^#\/s\//.test(up.href), "하위 세션 머리에 부모로 가는 길: " + JSON.stringify(up));
  await shot("01b-child-session");
  await page.goto(BASE);
  await waitSel(".srow", 15000);

  // 세션 열기 → 턴 구조
  await page.click("css=.srow >> nth=0", { label: "첫 세션 열기" });
  await waitSel(".turn", 15000);
  const turn = await page.evaluate(() => {
    const t = document.querySelector(".turn");
    return { labels: [...t.querySelectorAll(".part-label")].map((x) => x.textContent), turns: document.querySelectorAll(".turn").length,
      title: document.getElementById("title").textContent, headCount: document.getElementById("top").innerText.split(document.getElementById("title").textContent).length - 1,
      sid: document.getElementById("sid").hidden ? "" : document.getElementById("sid").textContent, hashSid: decodeURIComponent(location.hash.split("/s/")[1] || "") };
  });
  must(turn.turns === 2, "턴 2개");
  must(turn.labels.join() === "요청,답변", "요청·답변 머리말(과정은 접는 단추)");
  must(turn.headCount === 1, "머리 영역에 제목이 한 번만");
  must(turn.hashSid && turn.sid === "세션 " + turn.hashSid, "머리 영역에 세션 ID(" + turn.sid + ")");
  // 모델 칩은 "기본" 대신 지금 실제로 쓰일 값 — 고르지 않았으면 세션의 모델·effort
  await waitFn(() => /GPT-Fake-Sol/.test(document.querySelector(".composer-bar .chip")?.textContent || ""), undefined, 5000);
  const pickTxt = await page.evaluate(() => document.querySelector(".composer-bar .chip").textContent);
  must(pickTxt === "GPT-Fake-Solmedium", "세션 입력창 모델 칩은 세션의 실제 값(GPT-Fake-Sol · medium): " + pickTxt);
  // 의견 위젯(오른쪽 아래)이 입력창의 보내기 단추를 가리지 않는다
  const fbOn = await page.evaluate(() => !!document.querySelector('script[src*="feedback-widget.js"]'));   // CODEX_LENS_FEEDBACK_ORIGIN 을 줬을 때만
  if (fbOn) await page.waitForFunction(() => [...document.querySelectorAll("[id^=project-feedback-widget]")].some((h) => h.shadowRoot && h.shadowRoot.querySelector("button")), undefined, { timeout: 8000 }).catch(() => {});
  const fb = await page.evaluate(() => {
    const box = (r) => ({ l: r.left, t: r.top, r: r.right, b: r.bottom });
    let w = null;
    for (const h of document.querySelectorAll("[id^=project-feedback-widget]"))
      for (const b of h.shadowRoot ? h.shadowRoot.querySelectorAll("button") : []) {
        const r = b.getBoundingClientRect();
        if (r.width && r.height && r.bottom > innerHeight - 120) w = box(r);
      }
    return { cls: document.documentElement.className, w, send: box(document.querySelector(".composer-bar button.primary").getBoundingClientRect()) };
  });
  must(/has-fb/.test(fb.cls) === fbOn, "의견 위젯을 넣었을 때만 html.has-fb");
  if (!fbOn) results.push("note 의견 위젯을 넣지 않아(CODEX_LENS_FEEDBACK_ORIGIN 없음) 겹침 검사는 건너뜀");
  else if (fb.w) must(fb.send.r <= fb.w.l || fb.send.b <= fb.w.t || fb.send.l >= fb.w.r || fb.send.t >= fb.w.b, "보내기 단추와 의견 위젯이 겹치지 않는다 " + JSON.stringify(fb));
  else results.push("note 의견 위젯을 불러오지 못해 겹침 검사는 건너뜀");
  await checkProcessToggle("모바일 412×915");
  await waitFn(() => document.querySelectorAll(".turn .steps .step").length >= 5);
  must(true, "과정 항목 5개가 펼쳐진다");
  await shot("02-session");

  // 첨부는 장수 대신 실제 이미지를 보여 주며, 로컬 경로·끼워 넣기·읽기 실패도 다룬다.
  await waitFn(() => [...document.querySelectorAll('.turn:first-child .req-images img')].length === 2 &&
    [...document.querySelectorAll('.turn:first-child .req-images img')].every(i => i.complete && i.naturalWidth === 640));
  must(true, "요청의 data URL·로컬 파일 첨부가 실제 이미지로 디코딩된다");
  await page.click('css=.turn:first-child .req-image-preview >> nth=0', { label: "첨부 이미지 크게 보기" });
  await waitFn(() => document.querySelector('.image-sheet img')?.naturalWidth === 640);
  must(await page.evaluate(() => {
    const r = document.querySelector('.image-sheet').getBoundingClientRect();
    const body = document.querySelector('.image-view').getBoundingClientRect();
    return r.left >= 0 && r.right <= innerWidth && document.documentElement.scrollWidth <= innerWidth &&
      (!document.documentElement.classList.contains('has-fb') || body.bottom <= innerHeight - 68);
  }), "412px에서 이미지 확대 창이 화면 안에 있고 의견 버튼의 자리를 비운다");
  await shot("02-image-preview");
  await page.click('css=.image-sheet .sheet-head button', { label: "이미지 닫기" });
  await waitFn(() => !document.querySelector('.image-sheet'));
  await page.evaluate(() => document.querySelector('.req-steer .req-images').scrollIntoView({ block: 'center' }));
  await waitFn(() => document.querySelector('.req-steer .req-image-preview img')?.naturalWidth === 640 &&
    document.querySelectorAll('.req-steer .image-unavailable').length === 2);
  must(await page.evaluate(() => !document.querySelector('.req-steer .md').textContent.trim() &&
    document.documentElement.scrollWidth <= innerWidth), "이미지만 끼워 넣은 말도 표시하고 누락된 파일·파일 ID는 오류 안내로 남긴다");
  await page.reload();
  await waitFn(() => document.querySelector('.turn:first-child .req-image-preview img')?.naturalWidth === 640);
  must(true, "새로고침 뒤에도 첨부 미리보기가 유지된다");

  // 과정 요약 — effort 를 고르고 만든다
  await page.evaluate(() => document.querySelector(".turn .sum-eff").click());
  await waitFn(() => !!document.querySelector(".sheet .seg button"));
  const effs = await page.evaluate(() => [...document.querySelectorAll(".sheet .seg button")].map(b => b.textContent));
  must(effs.join(",") === "low,medium,high", "요약 effort 선택지는 세션 모델이 받는 값: " + effs.join(","));
  await page.evaluate(() => [...document.querySelectorAll(".sheet .seg button")].find(b => b.textContent === "medium").click());
  await waitFn(() => !document.querySelector(".sheet") && /^medium/.test(document.querySelector(".turn .sum-eff")?.textContent || ""));
  must(true, "요약 effort 칩이 고른 값(medium)을 보인다");
  await page.evaluate(() => document.querySelector(".turn .sum-btn").click());
  await waitFn(() => !!document.querySelector(".turn .summary-slot .iq-card"), undefined, 1500);
  must(true, "요약 단추를 누르면 1.5초 안에 진행 카드");
  await waitFn(() => !!document.querySelector(".turn .summary-slot .iq-meta") && /한 줄 요약/.test(document.querySelector(".turn .summary-slot .iq-answer")?.textContent || ""), undefined, 20000);
  const sumMeta = await page.evaluate(() => document.querySelector(".turn .summary-slot .iq-meta").textContent);
  must(/gpt-fake-sol/.test(sumMeta) && /캐시/.test(sumMeta) && /medium/.test(sumMeta), "요약은 세션 모델·고른 effort 로, 캐시를 읽었다: " + sumMeta);
  const sumIds = await page.evaluate(() => ({ ids: [...document.querySelectorAll(".turn .summary-slot .iq-ids .id-chip")].map((b) => b.textContent),
    sid: document.getElementById("sid").dataset.id }));
  must(sumIds.ids.length === 2 && sumIds.ids[0] === "세션" + sumIds.sid && /^포크\(일회용\).+/.test(sumIds.ids[1]),
    "요약 카드에 세션 ID 와 포크 ID(" + sumIds.ids.join(" | ") + ")");
  await shot("03-summary");

  // 답변 속 바깥 링크(데모 주소)는 새 탭이 아니라 앱 안의 웹 보기로 열리고, 뒤로 가면 떠난 자리로 돌아온다
  // 이 기기 주소(127.0.0.1)인데 그 포트에 서버가 없으면 빈 화면 대신 이유를 보인다
  await page.evaluate(() => [...document.querySelectorAll(".annot[data-part=answer] a.ext")].find((x) => /:9\/demo\/$/.test(x.href)).click());
  await waitFn(() => /응답하는 서버가 없습니다/.test(document.querySelector(".web-tools .grow")?.textContent || ""), undefined, 8000);
  must(await page.evaluate(() => !document.querySelector("iframe.web-frame")), "꺼진 로컬 주소는 iframe 대신 '응답하는 서버가 없습니다'");
  await page.evaluate(() => history.back());
  await waitSel(".turn", 10000);
  const link = await page.evaluate((base) => {
    const a = [...document.querySelectorAll(".annot[data-part=answer] a.ext")].find((x) => /\/demo\/$/.test(x.href));
    if (!a) return null;
    a.href = base + "api/info";   // 가짜 데모에서 실제로 읽히는 같은 출처 주소로 바꿔 끼운다
    a.scrollIntoView({ block: "center" });
    return { href: a.href, y: Math.round(scrollY) };
  }, BASE);
  must(link, "답변의 데모 주소가 바깥 링크(a.ext)로 그려진다");
  await page.evaluate(() => [...document.querySelectorAll(".annot[data-part=answer] a.ext")].find((x) => /api\/info$/.test(x.href)).click());
  await waitSel("iframe.web-frame", 5000);
  await waitFn(() => { const f = document.querySelector("iframe.web-frame"); try { return /"version"/.test(f.contentDocument.body.textContent); } catch (e) { return false; } }, undefined, 10000);
  await waitFn(() => document.querySelector(".web-tools .grow").textContent === "", undefined, 5000).catch(() => {});
  const web = await page.evaluate(() => {
    const f = document.querySelector("iframe.web-frame").getBoundingClientRect();
    const addr = document.querySelector("#top .web-addr"), ar = addr && addr.getBoundingClientRect();
    return { hash: location.hash, title: document.getElementById("title").textContent, status: document.querySelector(".web-tools .grow").textContent,
      addr: addr && addr.value, addrW: ar ? Math.round(ar.width) : 0, titleShown: !!document.getElementById("title").offsetParent,
      session: document.getElementById("pane-back").textContent, sessionShown: !document.getElementById("pane-back").hidden,
      pageBack: !!document.querySelector("#top .web-bar .web-back"),
      frameH: f.height, frameBottom: f.bottom, innerH: innerHeight, overflowX: document.documentElement.scrollWidth - innerWidth,
      scrollY: document.documentElement.scrollHeight - innerHeight,
      wraps: [...document.querySelectorAll(".web-tools button")].filter((b) => b.getBoundingClientRect().height > 40).map((b) => b.textContent),
      headCount: [...document.querySelectorAll("header")].map((h) => h.innerText).join("\n").split(document.getElementById("title").textContent).length - 1 };
  });
  const href0 = link.href;
  must(/^#\/web\?/.test(web.hash) && /from=/.test(web.hash), "링크를 누르면 앱 안의 웹 보기(#/web)로 연다 " + web.hash);
  must(web.addr === href0 && web.addrW >= 150 && !web.titleShown && web.headCount === 0,
    "머리에 주소창이 열린 주소를 보이고 제목 칸은 숨는다(" + web.addr + " · 폭 " + web.addrW + ")");
  must(web.sessionShown && web.session === "세션으로" && web.pageBack, "머리에 '세션으로'와 페이지 뒤로(‹)");
  must(web.status === "", "다 읽으면 불러오는 중 표시가 걷힌다(" + web.status + ")");
  must(web.frameH > web.innerH * 0.6 && web.frameBottom <= web.innerH + 0.5, "페이지가 남는 높이를 다 쓴다(" + Math.round(web.frameH) + "/" + web.innerH + ", 아래 끝 " + web.frameBottom + ", 창 스크롤 " + web.scrollY + ")");
  must(web.overflowX <= 0 && web.scrollY <= 1 && !web.wraps.length, "웹 보기 가로 넘침·창 스크롤·단추 줄바꿈 없음 " + JSON.stringify(web));
  await shot("03b-web-view");
  // 페이지 안에서 옮겨 가면 주소창이 따라가고, ‹ 는 페이지 안에서 뒤로 간다(웹 보기에 머문다)
  async function frameGo(href) {
    await page.evaluate((h) => { document.querySelector("iframe.web-frame").contentWindow.location.href = h; }, href);
    await waitFn((h) => document.querySelector("#top .web-addr")?.value === h, href, 8000);
  }
  const href1 = BASE + "api/info?page=2", href2 = BASE + "api/info?page=3";
  await frameGo(href1);
  await frameGo(href2);
  must(true, "페이지 안에서 옮겨 가면 주소창이 지금 주소(" + href2 + ")");
  await page.click("css=#top .web-back", { label: "페이지 뒤로" });
  await waitFn((h) => document.querySelector("#top .web-addr")?.value === h, href1, 8000);
  must(await page.evaluate(() => /^#\/web\?/.test(location.hash) && document.documentElement.classList.contains("web")),
    "‹ 는 페이지 안의 이전 화면으로 가고 웹 보기에 머문다");
  // 세션으로는 페이지 안의 기록과 상관없이 떠난 세션의 그 자리로 곧장 돌아간다
  await page.click("css=#pane-back", { label: "세션으로" });
  await waitSel(".turn", 10000);
  await page.waitForFunction((y) => Math.abs(scrollY - y) < 4, link.y, { timeout: 8000 }).catch(() => {});
  const backY = await page.evaluate(() => ({ y: Math.round(scrollY), web: document.documentElement.classList.contains("web"), hash: location.hash }));
  must(!backY.web && /^#\/s\//.test(backY.hash) && Math.abs(backY.y - link.y) < 4, "페이지 안에서 옮겨 다닌 뒤에도 세션으로를 누르면 세션의 떠난 위치로 돌아온다(" + link.y + " → " + backY.y + ")");
  // 브라우저 뒤로 가기도 그대로 — 웹 보기를 다시 열고 바로 뒤로 가면 세션으로
  await page.evaluate((base) => { const a = [...document.querySelectorAll(".annot[data-part=answer] a.ext")].find((x) => /\/demo\/$|api\/info$/.test(x.href)); a.href = base + "api/info"; a.click(); }, BASE);
  await waitSel("iframe.web-frame", 5000);
  await page.evaluate(() => history.back());
  await waitSel(".turn", 10000);
  must(await page.evaluate(() => /^#\/s\//.test(location.hash) && !document.documentElement.classList.contains("web")), "웹 보기에서 브라우저 뒤로 가기는 세션으로");

  // 답변에서 글을 골라 인라인 질문
  async function selectIn(selector, text) {
    return page.evaluate(({ selector, text }) => {
      const root = document.querySelector(selector);
      const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      while (w.nextNode()) {
        const n = w.currentNode, i = n.nodeValue.indexOf(text);
        if (i >= 0 && (root.closest(".iq-card") || !n.parentElement.closest(".iq-card"))) {
          const r = document.createRange(); r.setStart(n, i); r.setEnd(n, i + text.length);
          const s = getSelection(); s.removeAllRanges(); s.addRange(r); return true;
        }
      }
      return false;
    }, { selector, text });
  }
  must(await selectIn(".turn .annot[data-part=answer]", "0으로 나눌 수 없습니다"), "답변 글 고르기");
  await waitSel(".sel-pills button.q", 5000);
  await page.click("css=.sel-pills button.q", { label: "질문 메뉴" });
  await waitSel(".iq-card textarea.iq-input", 5000);
  await page.fill("css=.iq-card textarea.iq-input", "왜 예외 대신 문자열이야?");
  await page.press("css=.iq-card textarea.iq-input", "Enter");
  await waitFn(() => [...document.querySelectorAll(".annot[data-part=answer] .iq-card")].some((c) => c.querySelector(".iq-meta") && /계산기 예제/.test(c.textContent)), undefined, 20000);
  const iq = await page.evaluate(() => {
    const card = [...document.querySelectorAll(".annot[data-part=answer] .iq-card")].find((c) => /계산기 예제/.test(c.textContent));
    const mark = document.querySelector(".annot[data-part=answer] mark.iq-mark");
    return { mark: mark && mark.textContent, inAnswer: !!card.closest(".annot[data-part=answer]"),
      afterBlock: card.previousElementSibling ? card.previousElementSibling.tagName : card.parentElement.tagName,
      ids: [...card.querySelectorAll(".iq-ids .id-chip")].map((b) => b.textContent),
      sid: document.getElementById("sid").dataset.id };
  });
  must(iq.mark && iq.mark.includes("0으로"), "고른 글에 형광 표시");
  must(iq.inAnswer, "답 카드가 고른 글이 있는 답변 안에 붙는다(" + iq.afterBlock + " 뒤)");
  must(iq.ids.length === 2 && iq.ids[0] === "세션" + iq.sid && /^포크.+/.test(iq.ids[1]) && iq.ids[1] !== "포크" + iq.sid,
    "질문 카드에 세션 ID 와 포크 ID(" + iq.ids.join(" | ") + ")");
  await shot("04-inline-question");

  // 답 카드 안에서 다시 골라 이어 묻기
  must(await selectIn(".annot[data-part=answer] .iq-card .iq-answer", "계산기 예제"), "답 카드 글 고르기");
  await waitSel(".sel-pills button.q", 5000);
  await page.click("css=.sel-pills button.q", { label: "이어 묻기" });
  await waitSel(".iq-card[data-draft] textarea.iq-input", 5000);
  await page.fill("css=.iq-card[data-draft] textarea.iq-input", "예제를 하나 더");
  await page.press("css=.iq-card[data-draft] textarea.iq-input", "Enter");
  await waitFn(() => document.querySelectorAll(".iq-card[data-parent]").length >= 1 &&
    !!document.querySelector(".iq-card[data-parent] .iq-meta"), undefined, 20000);
  const indent = await page.evaluate(() => getComputedStyle(document.querySelector(".iq-card[data-parent]")).marginLeft);
  must(parseFloat(indent) >= 12, "이어 묻기 카드는 들여쓴다(" + indent + ")");

  // 코멘트 → 입력창에 넣기
  must(await selectIn(".turn .annot[data-part=answer]", "사용법"), "코멘트할 글 고르기");
  await waitSel(".sel-pills", 5000);
  await page.click("text=💬 코멘트", { label: "코멘트" });
  await waitSel("#cm-pop textarea", 5000);
  await page.fill("css=#cm-pop textarea", "예시 명령을 하나 더 넣어 줘");
  await page.click("css=#cm-pop button.primary", { label: "입력창에 넣기" });
  await waitFn(() => [...document.querySelectorAll(".cm-card")].some((c) => /입력창에 있음/.test(c.textContent)));
  const composerText = await page.evaluate(() => document.querySelector(".composer textarea").value);
  must(composerText.includes("> 사용법") && composerText.includes("예시 명령을"), "코멘트가 입력창에 인용으로 들어간다");
  await shot("05-comment");

  // 멘션: @ 로 파일 고르기
  await page.focus("css=.composer textarea");
  await page.evaluate(() => { const t = document.querySelector(".composer textarea"); t.setSelectionRange(t.value.length, t.value.length); });
  await page.keyboard.type("@dsg");
  await waitSel(".mention-pop .mention-item", 5000);
  const firstItem = await page.evaluate(() => document.querySelector(".mention-pop .mention-item .mi-sub").textContent);
  must(firstItem === "docs/design.md", "@dsg → docs/design.md 가 맨 위(" + firstItem + ")");
  const hasSessions = await page.evaluate(() => [...document.querySelectorAll(".mention-sec")].map((x) => x.textContent).join("|"));
  await page.press("css=.composer textarea", "Tab");
  const after = await page.evaluate(() => ({ v: document.querySelector(".composer textarea").value, chips: document.querySelectorAll(".mchip").length }));
  must(after.v.includes("@docs/design.md") && after.chips === 1, "멘션이 입력창·칩에 들어간다");
  await shot("06-mention");
  void hasSessions;

  // 다듬기 — 세션 맥락으로 포크해 다듬고, 결과는 입력창에 바로 들어간다
  const bar = await page.evaluate(() => {
    const b = document.querySelector(".composer-bar"), r = (e) => e.getBoundingClientRect();
    const btns = [...b.querySelectorAll("button")].filter((x) => !x.hidden);
    const main = b.querySelector(".chip .main");
    return { wrap: btns.some((x) => r(x).height > 44), chip: r(b.querySelector(".chip")).width, cut: main.scrollWidth > main.clientWidth + 1,
      rf: !!b.querySelector(".rf-btn:not([hidden])") };
  });
  must(bar.rf && !bar.wrap && !bar.cut, "입력창 단추 줄에 다듬기 단추 — 줄바꿈 없고 모델 칩이 잘리지 않는다 " + JSON.stringify(bar));
  // ✨ 는 판을 연다: 탭 · 목적 · 다듬기 모델 · 시작 단추 · 프롬프트 확인 · 함께 보내는 내용
  await page.click("css=.composer-bar .rf-btn", { label: "다듬기 판 열기" });
  await waitFn(() => { const p = document.querySelector(".rf-panel"); return p && !p.hidden && p.querySelector(".rf-run") && p.querySelector("select.rf-mode"); }, undefined, 1000);
  const rf0 = await page.evaluate(() => ({ tabs: [...document.querySelectorAll(".rf-tab")].map((b) => b.textContent), run: document.querySelector(".rf-run").textContent,
    mode: document.querySelector("select.rf-mode").value, chip: document.querySelector(".rf-panel .rf-chip .main").textContent }));
  must(rf0.tabs[0] === "✨ 다듬기" && rf0.tabs[1] === "버전" && rf0.run === "다듬기 시작" && rf0.mode === "request" && rf0.chip === "GPT-Fake-Sol",
    "✨ 를 누르면 1초 안에 판이 열린다 — 탭 · 목적 · 세션 모델 칩 · 시작 단추 " + JSON.stringify(rf0));
  const rfClick = (label) => page.evaluate((t) => [...document.querySelectorAll(".rf-panel button")].find((b) => b.textContent === t).click(), label);
  await page.evaluate(() => { const d = document.querySelectorAll(".rf-panel details.rf-det"); d[0].open = true; d[1].open = true; });
  await waitFn(() => /JSON 객체 하나만/.test(document.querySelector(".rf-full")?.textContent || "") && document.querySelector("textarea.rf-prompt").value.length > 50, undefined, 5000);
  const full = await page.evaluate(() => ({ prompt: document.querySelector("textarea.rf-prompt").value, st: document.querySelector(".rf-det .rf-status .grow").textContent,
    secs: [...document.querySelectorAll(".rf-full .sheet-sec")].map((x) => x.textContent), slot: document.querySelector(".rf-full").textContent }));
  must(/코딩 에이전트에게 전달하기 좋은/.test(full.prompt) && full.st === "기본 프롬프트", "다듬기 프롬프트를 확인한다: " + full.st);
  must(full.secs.includes("세션 맥락 안내") && full.secs.includes("편집 전용 지시") && /포크해/.test(full.slot) && /입력창 글이 이 자리에/.test(full.slot),
    "함께 보내는 내용 — 실제 조립 순서와 포크 안내: " + full.secs.join(","));
  await page.evaluate(() => { const d = document.querySelectorAll(".rf-panel details.rf-det"); d[0].open = false; d[1].open = false; });
  const pw = await page.evaluate(() => { const p = document.querySelector(".rf-panel"); return { sw: p.scrollWidth, cw: p.clientWidth, run: document.querySelector(".rf-run").getBoundingClientRect().height }; });
  must(pw.sw <= pw.cw + 1 && pw.run <= 40, "412 폭에서 판이 넘치지 않고 시작 단추가 한 줄 " + JSON.stringify(pw));
  await rfClick("다듬기 시작");
  await waitFn(() => { const p = document.querySelector(".rf-panel"); return p && /보내는 중|포크|기다리는|생각|답하는/.test(p.textContent) && /다듬기 취소/.test(p.textContent); }, undefined, 1000);
  must(true, "다듬기 시작을 누르면 1초 안에 진행 표시와 취소 단추");
  await waitFn(() => /입력창에 넣었습니다/.test(document.querySelector(".rf-panel")?.textContent || ""), undefined, 20000);
  const rf1 = await page.evaluate(() => ({ v: document.querySelector(".composer textarea").value, chips: document.querySelectorAll(".mchip").length,
    p: document.querySelector(".rf-panel").textContent, meta: document.querySelector(".rf-panel .iq-meta").textContent,
    tabs: [...document.querySelectorAll(".rf-tab")].map((b) => b.textContent), run: document.querySelector(".rf-run").textContent,
    opts: [...document.querySelectorAll(".rf-q .seg button")].map((b) => b.textContent) }));
  must(rf1.v.startsWith("목표: ") && rf1.v.includes("@docs/design.md") && rf1.chips === 1, "다듬은 글이 입력창에 들어가고 멘션은 남는다 " + JSON.stringify({ v: rf1.v.slice(0, 80), chips: rf1.chips }));
  must(/에이전트 의견/.test(rf1.p) && /범위가 분명해/.test(rf1.p), "에이전트 의견이 판에 따로 보인다");
  must(/캐시/.test(rf1.meta) && /세션 맥락/.test(rf1.meta) && !/GPT-Fake-Sol|gpt-fake-sol/.test(rf1.meta), "세션 모델로 포크해 캐시를 읽었고, 모델은 칩에만 보인다: " + rf1.meta);
  must(rf1.tabs[1] === "버전 2" && rf1.run === "다시 다듬기", "다듬기 전 글과 다듬은 글이 버전으로 남는다: " + rf1.tabs.join(","));
  must(rf1.opts.join() === "calc.py,README.md", "추가 질문의 선택지");
  await rfClick("원문 비교");
  await waitSel(".rf-diff ins", 3000);
  must(await page.evaluate(() => !!document.querySelector(".rf-diff del")), "원문 비교에 지운 말·더한 말");
  await page.evaluate(() => document.querySelector(".rf-q .seg button").scrollIntoView({ block: "nearest" }));   // 판 안에서 스크롤
  await page.click("css=.rf-q .seg button >> nth=0", { label: "선택지 calc.py" });
  await rfClick("답변 반영해 다듬기");
  await waitFn(() => /\(답변 반영\)/.test(document.querySelector(".composer textarea").value) && /입력창에 넣었습니다/.test(document.querySelector(".rf-panel").textContent), undefined, 20000);
  must(true, "답변을 반영해 다시 다듬는다");
  const answered = await page.evaluate(() => document.querySelector(".composer textarea").value);
  // 좋은 글 목적 — 본문은 입력창, 편집 메모는 판에
  await page.evaluate(() => { const s = document.querySelector("select.rf-mode"); s.value = "writing"; s.dispatchEvent(new Event("change", { bubbles: true })); });
  await rfClick("다시 다듬기");
  await waitFn(() => /^다듬은 글: /.test(document.querySelector(".composer textarea").value) && /편집 메모/.test(document.querySelector(".rf-panel").textContent), undefined, 20000);
  must(await page.evaluate(() => /좋은 글/.test(document.querySelector(".rf-panel .iq-meta").textContent) && !/에이전트 의견/.test(document.querySelector(".rf-panel").textContent)),
    "좋은 글로 다듬으면 편집 메모만 따로 보인다");
  // 버전 탭 — 최신이 위, 고르면 지금 글과 비교, 되돌리면 되돌리기 전 글도 버전으로 남는다
  await page.evaluate(() => [...document.querySelectorAll(".rf-tab")].find((b) => /^버전/.test(b.textContent)).click());
  await waitSel(".rf-ver-line", 3000);
  const vers = await page.evaluate(() => [...document.querySelectorAll(".rf-ver-line")].map((b) => b.textContent));
  must(vers.length === 4 && /^v4다듬음좋은 글/.test(vers[0]) && /지금$/.test(vers[0]) && /^v1다듬기 전/.test(vers[3]), "버전 목록: " + vers.join(" | "));
  await page.evaluate(() => [...document.querySelectorAll(".rf-ver-line")].find((b) => /^v3/.test(b.textContent)).click());
  await waitSel(".rf-ver.open .rf-diff", 3000);
  await rfClick("이 버전으로 되돌리기");
  await waitFn((t) => document.querySelector(".composer textarea").value === t, answered, 3000);
  const vb = await page.evaluate(() => ({ tab: [...document.querySelectorAll(".rf-tab")][1].textContent,
    now: [...document.querySelectorAll(".rf-ver-line")].filter((b) => /지금$/.test(b.textContent)).map((b) => b.textContent.slice(0, 2)) }));
  must(vb.tab === "버전 4" && vb.now.join() === "v3", "v3 로 되돌린다 — 되돌리기 전 글은 이미 v4 라 다시 남기지 않는다 " + JSON.stringify(vb));
  await page.evaluate(() => { const ta = document.querySelector(".composer textarea"); ta.value += " 손봄"; ta.dispatchEvent(new Event("input", { bubbles: true })); });
  await page.evaluate(() => [...document.querySelectorAll(".rf-ver-line")].find((b) => /^v4/.test(b.textContent)).click());
  await rfClick("이 버전으로 되돌리기");
  await page.evaluate(() => [...document.querySelectorAll(".rf-ver-line")].find((b) => /^v3/.test(b.textContent)).click());
  await rfClick("이 버전으로 되돌리기");
  await waitFn((t) => document.querySelector(".composer textarea").value === t, answered, 3000);
  const back2 = await page.evaluate(() => [...document.querySelectorAll(".rf-ver-line")].map((b) => b.textContent.slice(0, 8)));
  must(back2.length === 5 && /^v5되돌리기 전/.test(back2[0]), "손본 글을 되돌리면 그 글만 '되돌리기 전' 버전으로 남는다(이미 있는 글은 다시 남기지 않는다) " + back2.join("|"));
  await page.evaluate(() => [...document.querySelectorAll(".rf-tab")][0].click());
  await page.evaluate(() => { const s = document.querySelector("select.rf-mode"); s.value = "request"; s.dispatchEvent(new Event("change", { bubbles: true })); });
  await shot("06b-refine");

  // 보내기 → 실시간 턴 → 코멘트는 세션에 보냄(다듬어 문장이 바뀌었어도) → 다듬기 판은 닫힌다
  await page.click("css=.composer-bar button.primary", { label: "보내기" });
  await waitFn(() => document.querySelectorAll(".turn").length === 3, undefined, 10000);
  must(true, "보낸 턴이 바로 나타난다");
  await waitFn(() => /작업을 마쳤습니다/.test(document.querySelectorAll(".turn")[2]?.querySelector(".annot[data-part=answer]")?.textContent || ""), undefined, 20000);
  await waitFn(() => [...document.querySelectorAll(".cm-card")].some((c) => /세션에 보냄/.test(c.textContent)), undefined, 10000);
  must(true, "보낸 뒤 코멘트가 '세션에 보냄'");
  must(await page.evaluate(() => document.querySelector(".rf-panel").hidden), "보내면 다듬기 판이 닫힌다");
  must(await page.evaluate(() => !localStorage.getItem(Object.keys(localStorage).find((k) => /refineVers\./.test(k)) || "x")), "보내면 다듬기 버전을 비운다");
  const ctx = await page.evaluate(() => document.querySelectorAll(".turn")[2].querySelector(".req-ctx")?.textContent || "");
  must(/멘션 맥락/.test(ctx), "보낸 요청에 멘션 맥락이 접혀 있다");
  await shot("07-sent");

  // 위치 복원: 문서로 갔다가 뒤로
  await page.evaluate(() => window.scrollTo(0, 600));
  await page.waitForTimeout(400);
  const y0 = await page.evaluate(() => window.scrollY);
  // 프로젝트 탐색기 → docs 폴더 → design.md
  await page.evaluate(() => [...document.querySelectorAll("#top-actions button")].find((b) => b.textContent === "파일").click());
  await waitSel(".tree .frow", 5000);
  const tree = await page.evaluate(() => ({ rows: [...document.querySelectorAll(".tree .frow .fname")].map((x) => x.textContent),
    title: document.getElementById("title").textContent, overflow: document.documentElement.scrollWidth - window.innerWidth }));
  must(tree.rows[0] === "docs" && tree.rows.includes("README.md") && tree.rows.includes("calc.py"), "탐색기: 폴더가 먼저, 파일이 뒤에 " + tree.rows.join(","));
  must(tree.title === "sample-project", "탐색기 제목은 프로젝트 이름");
  must(tree.overflow <= 0, "탐색기 가로 넘침 없음");
  await shot("08a-explorer");
  await page.evaluate(() => [...document.querySelectorAll(".tree .frow")].find((b) => b.querySelector(".fname").textContent === "docs").click());
  await waitFn(() => [...document.querySelectorAll(".tree .frow .fname")].some((x) => x.textContent === "design.md"));
  const crumbs = await page.evaluate(() => document.querySelector(".crumbs").innerText.replace(/\s+/g, ""));
  must(crumbs === "⌂/docs", "폴더 경로 표시 " + crumbs);
  await page.evaluate(() => [...document.querySelectorAll(".tree .frow")].find((b) => b.querySelector(".fname").textContent === "design.md").click());
  await waitSel(".doc .md-line", 5000);
  // 줄 누르기 → 코멘트
  await page.click("css=.doc li .md-line >> nth=1", { label: "문서 줄 누르기" });
  await waitSel("#cm-pop textarea", 5000);
  await page.fill("css=#cm-pop textarea", "예외를 쓰는 편이 낫지 않을까?");
  await page.click("css=#cm-pop button.primary", { label: "세션 입력창에 넣기" });
  await waitFn(() => document.querySelectorAll(".doc .cm-card").length === 1 && /입력창에 있음/.test(document.querySelector(".doc .cm-card").textContent));
  const docCard = await page.evaluate(() => { const c = document.querySelector(".doc .cm-cards"); return c.previousElementSibling ? c.previousElementSibling.tagName : ""; });
  must(docCard === "UL", "문서 코멘트 카드가 그 목록 바로 뒤에 붙는다(" + docCard + ")");
  await shot("08-doc-comment");

  // 두 번째 코멘트는 저장만 — 이것만 AI 다듬기로 보낸다
  await page.click("css=.doc li .md-line >> nth=0", { label: "문서 첫 항목 줄 누르기" });
  await waitSel("#cm-pop textarea", 5000);
  await page.fill("css=#cm-pop textarea", "누가 검사하는지 더 분명하게");
  await page.evaluate(() => [...document.querySelectorAll("#cm-pop button")].find((b) => /^저장만/.test(b.textContent)).click());
  await waitFn(() => document.querySelectorAll(".doc .cm-card").length === 2);

  // AI 로 다듬기: 코멘트 고르기 → 요청 → 진행 → 바뀐 줄 표시 → 되돌리기
  const cardState = (re) => page.evaluate((re) => [...document.querySelectorAll(".doc .cm-card")].find((c) => new RegExp(re).test(c.textContent))?.querySelector(".cm-state")?.textContent, re);
  await page.evaluate(() => [...document.querySelectorAll(".doc-tools button")].find((b) => /AI로 다듬기/.test(b.textContent)).click());
  await waitSel(".sheet .ck-row input", 5000);
  const ck = await page.evaluate(() => [...document.querySelectorAll(".sheet .ck-row")].map((x) => [x.textContent, x.querySelector("input").checked]));
  must(ck.length === 2 && ck.every((x) => x[1]), "다듬기 시트에 열린 코멘트 2개가 골라진 채로 나온다");
  await page.evaluate(() => [...document.querySelectorAll(".sheet .ck-row")].find((x) => /예외를 쓰는/.test(x.textContent)).querySelector("input").click());
  await page.fill("css=.sheet textarea", "문장은 짧게");
  await shot("08b-refine-sheet");
  await page.evaluate(() => [...document.querySelectorAll(".sheet button.primary")].find((b) => b.textContent === "다듬기 요청").click());
  await waitFn(() => !!document.querySelector(".refine-panel:not([hidden]) .live-status"), undefined, 1500);
  must(true, "다듬기 요청 1.5초 안에 진행 표시");
  await waitFn(() => /다듬었습니다/.test(document.querySelector(".refine-panel")?.textContent || ""), undefined, 20000);
  const refined = await page.evaluate(() => ({ panel: document.querySelector(".refine-panel").innerText, chg: document.querySelectorAll(".doc .md-line.chg").length,
    text: document.querySelector(".doc .md").textContent, card: document.querySelector(".doc .cm-card .cm-state")?.textContent,
    overflow: document.documentElement.scrollWidth - window.innerWidth }));
  must(/\+1/.test(refined.panel) && refined.chg === 1, "다듬은 뒤 바뀐 줄 1개를 표시한다: " + refined.panel.replace(/\n/g, " "));
  must(refined.text.includes("호출하는 쪽이 합니다. (다듬음)") && !refined.text.includes("돌려줍니다. (다듬음)"), "고른 코멘트 부분만 고친 글이 다시 읽힌다");
  must(await cardState("누가 검사") === "AI 다듬기에 보냄", "보낸 코멘트는 'AI 다듬기에 보냄'");
  must(await cardState("예외를 쓰는") === "입력창에 있음", "빼 둔 코멘트는 그대로 입력창에 있음");
  void refined.card;
  must(refined.overflow <= 0, "문서 화면 가로 넘침 없음");
  await page.evaluate(() => [...document.querySelectorAll(".refine-panel button")].find((b) => b.textContent === "답 보기").click());
  must(await page.evaluate(() => /코멘트 1개를 반영했습니다/.test(document.querySelector(".rp-answer:not([hidden])")?.textContent || "")), "다듬기 답을 펼쳐 본다");
  await shot("08c-refined");
  for (let i = 0; i < 2; i++) await page.evaluate(() => [...document.querySelectorAll(".refine-panel button")].find((b) => /되돌리기/.test(b.textContent)).click());
  await waitFn(() => /되돌렸습니다/.test(document.querySelector(".refine-panel")?.textContent || "") && !/\(다듬음\)/.test(document.querySelector(".doc .md").textContent), undefined, 10000);
  must(true, "되돌리면 다듬기 전 글");
  must(await cardState("누가 검사") === "저장만", "되돌리면 보낸 코멘트가 다시 열린다");

  // 뒤로: 문서 → 탐색기(docs) 는 목록이 다시 보이고, 두 번 더 뒤로 가면 세션
  await page.evaluate(() => history.back());
  await waitFn(() => [...document.querySelectorAll(".tree .frow .fname")].some((x) => x.textContent === "design.md"), undefined, 10000);
  must(true, "뒤로 가면 탐색기 docs 폴더");
  await page.evaluate(() => history.go(-2));
  await waitSel(".turn", 10000);
  await page.waitForTimeout(800);
  const back = await page.evaluate(() => ({ y: window.scrollY, draft: document.querySelector(".composer textarea").value }));
  must(Math.abs(back.y - y0) < 80, "뒤로 오면 스크롤 위치 복원(" + y0 + " → " + back.y + ")");
  must(back.draft.includes("[문서 코멘트]") && back.draft.includes("예외를 쓰는 편이"), "문서 코멘트가 세션 입력창 초안에 들어 있다");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  must(overflow <= 0, "세션 화면 가로 넘침 없음");
  await shot("09-back");

  // 새 세션
  await page.goto(BASE + "#/new");
  await waitSel(".composer textarea", 5000);
  await page.fill("css=.composer textarea", "앞뒤");
  await page.evaluate(() => document.querySelector(".composer textarea").setSelectionRange(1, 1));
  await page.press("css=.composer textarea", "Tab");
  const tabDraft = await page.evaluate(() => ({ value: document.querySelector(".composer textarea").value,
    caret: document.querySelector(".composer textarea").selectionStart }));
  must(tabDraft.value === "앞\t뒤" && tabDraft.caret === 2, "Tab을 커서 위치에 입력한다");
  await page.goto(BASE + "#/sessions");
  await page.goto(BASE + "#/new");
  await waitSel(".composer textarea", 5000);
  must(await page.evaluate(() => document.querySelector(".composer textarea").value === "앞\t뒤"), "Tab이 들어간 초안을 복원한다");
  await page.fill("css=.field input", PROJ);
  await waitFn(() => /GPT-Fake-Sol/.test(document.querySelector(".composer-bar .chip")?.textContent || ""), undefined, 5000);
  const newPick = await page.evaluate(() => document.querySelector(".composer-bar .chip").textContent);
  must(newPick === "GPT-Fake-Solhigh", "새 세션 모델 칩은 Codex 설정값(GPT-Fake-Sol · high): " + newPick);
  await waitFn(() => { const r = document.querySelector(".wt-row"); return r && !r.hidden; }, undefined, 5000);
  const wt = await page.evaluate(() => ({ checked: document.querySelector(".wt-row input").checked, sub: document.querySelector(".wt-sub").textContent,
    wrap: [...document.querySelectorAll(".wt-row .wt-main")].every((e) => e.getBoundingClientRect().height < 30) }));
  must(wt.checked && /새 브랜치/.test(wt.sub) && wt.wrap, "git 작업 폴더면 '워크트리에서 작업'이 켜진 채 보인다");
  await page.fill("css=.composer textarea", "새 세션 시험");
  // 새 세션 화면의 다듬기 — 다듬는 중에 새로고침해도 작업을 다시 붙잡는다
  await page.click("css=.composer-bar .rf-btn", { label: "다듬기 판 열기" });
  await waitFn(() => !document.querySelector(".rf-panel").hidden && document.querySelector(".rf-run"), undefined, 1000);
  await page.click("css=.rf-panel .rf-run", { label: "다듬기 시작" });
  await waitFn(() => /다듬기 취소/.test(document.querySelector(".rf-panel").textContent), undefined, 1000);
  await page.reload();
  await waitFn(() => /입력창에 넣었습니다/.test(document.querySelector(".rf-panel")?.textContent || ""), undefined, 20000);
  const rfNew = await page.evaluate(() => document.querySelector(".composer textarea").value);
  must(rfNew.startsWith("목표: 새 세션 시험"), "새로고침 뒤에도 다듬은 글이 들어온다 " + JSON.stringify(rfNew.slice(0, 120)));
  await page.evaluate(() => [...document.querySelectorAll(".rf-panel button")].find((b) => b.textContent === "되돌리기").click());
  const reverted = await page.evaluate(() => ({ v: document.querySelector(".composer textarea").value, hidden: document.querySelector(".rf-panel").hidden }));
  must(reverted.v === "새 세션 시험" && !reverted.hidden, "되돌리기는 다듬기 전 글로(판은 남아 다듬은 글을 다시 넣을 수 있다)");
  must(await page.evaluate(() => /다듬기 전 글로 되돌렸습니다/.test(document.querySelector(".rf-panel").textContent) && [...document.querySelectorAll(".rf-panel button")].some((b) => b.textContent === "다듬은 글 다시 넣기")),
    "되돌린 뒤 '다듬은 글 다시 넣기'");
  await page.click("css=.composer-bar button.primary", { label: "시작" });
  await page.waitForFunction(() => /#\/s\//.test(location.hash), undefined, { timeout: 10000 });
  await waitSel(".turn", 2000);
  const pendingRead = await page.evaluate(async () => {
    const sid = decodeURIComponent(location.hash.split("/")[2]);
    const r = await fetch("/api/sessions/" + encodeURIComponent(sid));
    const d = await r.json();
    return { status: r.status, pending: d.history_pending, request: d.turns?.[0]?.request?.text,
      error: document.querySelector(".empty.err")?.textContent };
  });
  must(pendingRead.status === 200 && pendingRead.pending && /새 세션 시험/.test(pendingRead.request) && !pendingRead.error,
    "새 기록 파일이 비어 있어도 첫 요청이 바로 보이고 읽기 오류가 없다");
  await page.reload();
  await waitSel(".turn", 2000);
  must(!await page.evaluate(() => !!document.querySelector(".empty.err")), "첫 기록 저장 전 새로고침도 오류 없이 복원한다");
  await waitFn(() => /작업을 마쳤습니다/.test(document.querySelector(".annot[data-part=answer]")?.textContent || ""), undefined, 20000);
  must(true, "새 세션을 만들고 첫 답을 받는다");
  await waitFn(async () => {
    const sid = decodeURIComponent(location.hash.split("/")[2]);
    return !(await (await fetch("/api/sessions/" + encodeURIComponent(sid))).json()).history_pending;
  }, undefined, 10000);
  must(!await page.evaluate(() => !!document.querySelector(".empty.err")), "저장된 기록으로 전환한 뒤에도 답변을 유지한다");
  const made = await page.evaluate(async (proj) => {
    const sid = decodeURIComponent(location.hash.split("/")[2]);
    const d = await (await fetch("/api/sessions/" + encodeURIComponent(sid))).json();
    return { origin: d.session.worktree && d.session.worktree.origin_cwd, cwd: d.session.cwd, branch: d.session.worktree && d.session.worktree.branch, proj };
  }, PROJ);
  must(made.origin === PROJ && made.cwd !== PROJ && /^codex-lens\//.test(made.branch || ""), "새 세션은 새 브랜치 워크트리에서 돈다");
  await shot("10-new-session");

  // 응답 중에 보내기: 바로 넣기(진행 중인 턴에 끼워 넣기) · 턴 끝나고(대기열)
  async function sendText(t) {
    await page.fill("css=.composer textarea", t);
    await waitFn(() => document.getElementById('toast').hidden, undefined, 7000);
    await page.click("css=.composer .composer-bar button.primary", { label: "보내기" });
    await waitFn((t) => document.querySelector(".composer textarea").value === "", t, 10000);
  }
  // 답 글은 턴이 끝나기 전에도 보인다 — 첫 턴이 끝난 뒤 보내야 새 턴이 된다(아니면 끼워 넣기가 거절돼 대기열로 간다)
  await waitFn(() => document.querySelector(".composer .steer-row")?.hidden !== false, undefined, 10000);
  await sendText("천천히 두 번째 일");
  await waitSel(".composer .steer-row", 5000);
  await waitSel(".turn:last-child .proc-toggle .proc-spinner", 5000);
  const progress = await page.evaluate(() => {
    const t = [...document.querySelectorAll('.turn')].pop(), spin = t.querySelector('.proc-spinner');
    return { collapsed: !t.querySelector('.proc').classList.contains('open'),
      spinning: getComputedStyle(spin).animationName === 'proc-spin',
      label: spin.getAttribute('aria-label'), answer: !!t.querySelector('.ans'),
      placeholder: t.textContent.includes('답을 쓰는 중') };
  });
  must(progress.collapsed && progress.spinning && progress.label === '진행 중', '접힌 과정 제목 옆에 진행 스피너가 돈다');
  must(!progress.answer && !progress.placeholder, '과정 실행 중 빈 답변 칸이나 답변 작성 안내를 표시하지 않는다');
  const run = await page.evaluate(() => {
    const box = (e) => { const r = e.getBoundingClientRect(); return { l: r.left, t: r.top, r: r.right, b: r.bottom }; };
    const bar = document.querySelector(".composer-bar"), send = bar.querySelector("button.primary"), stop = bar.querySelector("button.stop");
    let w = null;
    for (const h of document.querySelectorAll("[id^=project-feedback-widget]"))
      for (const b of h.shadowRoot ? h.shadowRoot.querySelectorAll("button") : []) { const r = b.getBoundingClientRect(); if (r.width && r.bottom > innerHeight - 120) w = box(b); }
    return { modes: [...document.querySelectorAll(".mode-seg button")].map((b) => b.textContent + ":" + b.getAttribute("aria-pressed")),
      send: send.textContent, stop: !!stop && !stop.hidden, sendBox: box(send), w, barOver: bar.scrollWidth - bar.clientWidth,
      wraps: [...document.querySelectorAll(".composer button")].filter((b) => b.getClientRects().length && b.getBoundingClientRect().height > 44).map((b) => b.textContent),
      overflow: document.documentElement.scrollWidth - innerWidth };
  });
  must(run.modes.join() === "바로 넣기:true,턴 끝나고:false", "응답 중이면 보내는 방식을 고른다(기본 바로 넣기) " + run.modes.join());
  must(run.stop && run.send === "넣기", "응답 중에도 멈춤과 넣기 단추가 함께 있다");
  must(run.barOver <= 0 && run.overflow <= 0 && !run.wraps.length, "응답 중 입력창 단추가 넘치거나 줄바꿈되지 않는다 " + JSON.stringify(run));
  if (run.w) must(run.sendBox.r <= run.w.l || run.sendBox.b <= run.w.t, "응답 중 넣기 단추와 의견 위젯이 겹치지 않는다");
  await sendText("README 도 같이 봐");
  await waitFn(() => { const t = [...document.querySelectorAll(".turn")].pop(); return [...t.querySelectorAll(".req-steer")].some((e) => /README 도 같이 봐/.test(e.textContent)); }, undefined, 8000);
  must(true, "바로 넣은 말이 진행 중인 턴의 요청 아래에 '진행 중에 넣음'으로 보인다");
  await page.click("text=턴 끝나고", { label: "턴 끝나고 고르기" });
  must(await page.evaluate(() => document.querySelector(".composer-bar button.primary").textContent) === "대기열에", "턴 끝나고를 고르면 단추가 '대기열에'");
  await sendText("끝나면 할 일");
  await waitFn(() => [...document.querySelectorAll(".queue .qitem")].some((e) => /끝나면 할 일/.test(e.textContent)), undefined, 5000);
  must(true, "턴 끝나고 보낸 말이 입력창 위 대기열에 보인다");
  await shot("11-steer-queue");
  await waitFn(() => { const ts = document.querySelectorAll(".turn"); const last = ts[ts.length - 1];
    return ts.length === 3 && /끝나면 할 일/.test(last.querySelector(".req-body").textContent) && /작업을 마쳤습니다/.test(last.querySelector(".annot[data-part=answer]")?.textContent || ""); }, undefined, 45000);
  // 답은 스트리밍 중에도 보인다 — 턴이 끝나 응답 중 표시가 걷힐 때까지 기다린다
  await waitFn(() => document.querySelector(".composer .steer-row").hidden, undefined, 10000).catch(() => {});
  const fin = await page.evaluate(() => ({ queue: !document.querySelector(".queue") || document.querySelector(".queue").hidden,
    read: /끼워 넣은 말도 읽었습니다/.test(document.querySelectorAll(".turn")[1].querySelector(".annot[data-part=answer]").textContent),
    steerRow: document.querySelector(".composer .steer-row").hidden }));
  must(fin.queue, "턴이 끝나면 대기열의 말이 다음 턴으로 나가고 대기열이 빈다");
  must(fin.read, "끼워 넣은 말을 그 턴의 답이 읽었다");
  must(fin.steerRow, "응답이 끝나면 보내는 방식 줄이 사라진다");
  must(await page.evaluate(() => !document.querySelector('.proc-spinner')), '완료된 턴에서는 과정 스피너가 사라진다');
  await shot("12-queue-sent");
  // 멈춘 턴: 하던 중의 말("~하겠습니다")은 답변이 아니라 과정에 남고, 답변 자리에 "사용자가 멈춤" 안내와 이어서 하기
  await sendText("천천히 중간 말 남기고 멈출 일");
  await waitFn(() => { const t = [...document.querySelectorAll(".turn")].pop(); return document.querySelectorAll(".turn").length === 4 && /메시지 1/.test(t.querySelector(".proc .counts")?.textContent || ""); }, undefined, 10000);
  await page.click("css=.composer-bar button.stop", { label: "멈춤" });
  await waitSel(".turn:last-child .turn-halt", 15000);
  const halt = await page.evaluate(() => {
    const t = document.querySelector(".turn:last-child"), go = t.querySelector(".resume-btn");
    return { msg: t.querySelector(".turn-halt").textContent, labels: [...t.querySelectorAll(".part-label")].map((e) => e.textContent),
      answer: !!t.querySelector(".annot[data-part=answer]"), badge: [...t.querySelectorAll(".turn-meta .badge")].map((e) => e.textContent),
      procMsg: /메시지 1/.test(t.querySelector(".proc .counts").textContent), go: !!go && go.getClientRects().length > 0,
      goH: go ? go.getBoundingClientRect().height : 0, overflow: document.documentElement.scrollWidth - innerWidth,
      count: (document.body.innerText.match(/사용자가 중간에 멈췄습니다/g) || []).length };
  });
  must(/사용자가 중간에 멈췄습니다/.test(halt.msg) && halt.count === 1, "멈춘 턴은 답변 자리에 '사용자가 중간에 멈췄습니다'가 한 번 보인다 " + JSON.stringify(halt));
  must(!halt.answer && halt.labels.indexOf("답변") < 0 && halt.procMsg, "멈추기 전의 중간 말은 답변이 아니라 과정의 메시지로 남는다 " + JSON.stringify(halt));
  must(!halt.badge.length, "멈춤 표시는 머리 배지와 겹치지 않는다 " + halt.badge.join());
  must(halt.go && halt.goH <= 44 && halt.overflow <= 0, "마지막 멈춘 턴에 '이어서 하기' 단추가 줄바꿈·넘침 없이 보인다 " + JSON.stringify(halt));
  await page.evaluate(() => document.querySelector(".turn:last-child").scrollIntoView({ block: "start" }));
  await shot("12a-stopped-turn");
  await page.click("css=.turn:last-child .resume-btn", { label: "이어서 하기" });
  await waitFn(() => { const ts = document.querySelectorAll(".turn"); return ts.length === 5 && /이어서 진행해 주세요/.test(ts[4].querySelector(".req-body").textContent); }, undefined, 10000);
  must(await page.evaluate(() => [...document.querySelectorAll(".resume-btn")].every((b) => !b.getClientRects().length)), "이어서 하기를 누르면 새 턴이 열리고 앞 턴의 단추는 숨는다");
  await waitFn(() => /작업을 마쳤습니다/.test(document.querySelector(".turn:last-child .annot[data-part=answer]")?.textContent || "") && document.querySelector(".composer .steer-row").hidden, undefined, 20000);
  // 모델 관리: 공통 상단 바의 "모델" → 숨기기 · 기본 모델 · 기본 속도(Fast)(에이전트별) → 입력창 모델 칩에 반영
  await page.goto(BASE + "#/");
  await waitSel("#view .srow", 15000);
  await page.evaluate(() => document.getElementById("app-models").click());
  await waitSel(".model-row", 10000);
  const mv = await page.evaluate(() => ({
    hash: location.hash, rows: [...document.querySelectorAll(".model-row")].map((r) => r.dataset.model),
    headCount: [...document.querySelectorAll("header")].filter((h) => !h.hidden).map((h) => h.innerText).join("\n").split("모델 관리").length - 1,
    topWrap: [...document.querySelectorAll("#app-top button")].filter((b) => b.getBoundingClientRect().height > 44).map((b) => b.textContent),
    topOver: document.getElementById("app-top").scrollWidth - document.getElementById("app-top").clientWidth,
    tiers: [...document.querySelectorAll(".model-defaults .mdef-row:nth-child(2) .seg button")].map((b) => b.textContent),
    wraps: [...document.querySelectorAll(".models-view button.small, .models-view .seg button")].filter((b) => b.getBoundingClientRect().height > 44).map((b) => b.textContent),
    overflow: document.documentElement.scrollWidth - innerWidth }));
  must(mv.hash === "#/models", "모델 단추는 #/models 로 간다");
  must(mv.rows.join() === ",gpt-fake-sol,gpt-fake-luna", "Codex 기본 + 모델 2개(Codex 가 숨긴 모델은 없음): " + mv.rows.join());
  must(mv.headCount === 1, "머리 영역에 '모델 관리'가 한 번만");
  must(!mv.topWrap.length && mv.topOver <= 0, "공통 상단 바(프로젝트·모델·포트)가 줄바꿈·넘침 없이 한 줄 " + JSON.stringify({ w: mv.topWrap, o: mv.topOver }));
  must(mv.tiers.join() === "기본,표준,Fast", "기본 속도 선택지는 모델이 내놓는 등급에서: " + mv.tiers.join());
  must(!mv.wraps.length && mv.overflow <= 0, "모델 관리 단추 줄바꿈·가로 넘침 없음 " + JSON.stringify(mv));
  const settings = () => page.evaluate(async () => (await (await fetch("/api/models")).json()).settings);
  await page.evaluate(() => document.querySelector('.model-row[data-model="gpt-fake-luna"] .vis').click());
  await waitFn(() => document.querySelector('.model-row[data-model="gpt-fake-luna"]')?.classList.contains("hid"), undefined, 5000);
  must((await settings()).hidden.join() === "gpt-fake-luna", "숨기기를 누르면 바로 저장된다");
  await page.evaluate(() => document.querySelector('.model-row[data-model="gpt-fake-sol"] .opt').click());
  await waitFn(() => document.querySelector('.model-row[data-model="gpt-fake-sol"]')?.classList.contains("on"), undefined, 5000);
  must(await page.evaluate(() => document.querySelector('.model-row[data-model="gpt-fake-sol"] .vis').disabled), "기본 모델은 숨기기 단추가 꺼진다");
  await page.evaluate(() => [...document.querySelectorAll(".model-defaults .seg button")].find((b) => b.textContent === "Fast").click());
  await waitFn(() => [...document.querySelectorAll(".model-defaults .seg button.on")].some((b) => b.textContent === "Fast"), undefined, 5000);
  const st1 = await settings();
  must(st1.defaults.codex.model === "gpt-fake-sol" && st1.defaults.codex.tier === "priority" && !st1.defaults.claude.model,
    "Codex 기본 모델·기본 속도(Fast) 저장(Claude 기본값은 그대로): " + JSON.stringify(st1));
  // Claude Code 로 바꾸면 그 에이전트의 모델·기본값만 — 속도 등급이 없어 속도 줄이 없다
  await page.evaluate(() => [...document.querySelectorAll(".models-agent button")].find((b) => b.textContent === "Claude Code").click());
  await waitFn(() => /agent=claude/.test(location.hash) && document.querySelectorAll('.model-row[data-model^="claude-"]').length > 0, undefined, 5000);
  const cv = await page.evaluate(() => ({ rows: [...document.querySelectorAll(".model-row")].map((r) => r.dataset.model),
    defRows: document.querySelectorAll(".model-defaults .mdef-row").length }));
  must(cv.rows[0] === "" && cv.rows.slice(1).every((x) => /^claude-/.test(x)) && cv.defRows === 1, "Claude Code 모델 관리: Claude 모델만, 속도 줄 없음 " + JSON.stringify(cv));
  await page.evaluate(() => [...document.querySelectorAll(".models-agent button")].find((b) => b.textContent === "Codex").click());
  await waitFn(() => !/agent=claude/.test(location.hash) && !!document.querySelector('.model-row[data-model="gpt-fake-sol"].on'), undefined, 5000);
  await shot("14a-models");
  // 과정 요약 · 인라인 질문 기본 effort — 모델 관리에서 에이전트별로 정하면 세션 화면의 요약 effort 가 그 값을 보인다
  const fk = await page.evaluate(() => ({ rows: [...document.querySelectorAll(".fork-defaults .mdef-row")].map((r) => r.dataset.kind + ":" +
      [...r.querySelectorAll("button")].map((b) => b.textContent + (b.classList.contains("on") ? "*" : "")).join("/")),
    wraps: [...document.querySelectorAll(".fork-defaults button")].filter((b) => b.getBoundingClientRect().height > 44).length }));
  must(fk.rows.join() === "question:low*/medium/high,summary:low*/medium/high" && !fk.wraps,
    "포크 기본 effort 줄: 인라인 질문·과정 요약, Codex 모델이 받는 값, 처음은 low " + JSON.stringify(fk));
  await page.evaluate(() => [...document.querySelectorAll(".fork-defaults .mdef-row[data-kind=summary] button")].find((b) => b.textContent === "high").click());
  await waitFn(() => document.querySelector(".fork-defaults .mdef-row[data-kind=summary] button.on")?.textContent === "high", undefined, 5000);
  const st2 = await settings();
  must(st2.fork_efforts.codex.summary === "high" && st2.fork_efforts.codex.question === "low" && st2.fork_efforts.claude.summary === "low",
    "과정 요약 기본 effort 는 Codex 에만 바로 저장된다: " + JSON.stringify(st2.fork_efforts));
  await page.goto(BASE + "#/s/01a1aaaa000000000000000000000001");
  await waitFn(() => document.querySelectorAll(".turn .sum-eff").length > 0 &&
    [...document.querySelectorAll(".turn .sum-eff")].every((b) => /^high/.test(b.textContent)), undefined, 10000);
  must(true, "세션 화면의 과정 요약 effort 가 모델 관리의 기본값(high)을 보인다");
  await page.evaluate(() => fetch("/api/models/settings", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agent: "codex", fork_efforts: { summary: "", question: "" } }) }));
  // 새 세션 입력창: 숨긴 모델은 고르기 목록에 없고, "기본"은 정한 기본 모델을 보이며 속도를 고를 수 있다
  await page.goto(BASE + "#/new");
  await waitSel(".composer", 10000);
  await page.evaluate(() => { localStorage.removeItem("codex-lens.pick.new"); localStorage.removeItem("codex-lens.newAgent"); });
  await page.reload();
  await waitSel(".composer", 10000);
  await waitFn(() => [...document.querySelectorAll(".composer .chip")].some((c) => /GPT-Fake-Sol/.test(c.textContent)), undefined, 8000);
  await page.evaluate(() => [...document.querySelectorAll(".composer .chip")].find((c) => c.title === "모델 · effort · 속도 고르기").click());
  await waitSel(".sheet .pick-tier", 5000);
  const pk = await page.evaluate(() => ({ opts: [...document.querySelectorAll(".sheet .file-list .opt b")].map((b) => b.textContent),
    on: [...document.querySelectorAll(".sheet .file-list .opt.on b")].map((b) => b.textContent).join(),
    tiers: [...document.querySelectorAll(".sheet .pick-tier button")].map((b) => b.textContent) }));
  must(pk.opts.join() === "GPT-Fake-Sol" && pk.on === "GPT-Fake-Sol", "숨긴 모델은 고르기 목록에서 빠지고 '기본' 칸 없이 정한 기본 모델이 선택돼 있다: " + pk.opts.join() + " / " + pk.on);
  must(pk.tiers.join() === "기본,표준,Fast", "모델 고르기에서 속도(Fast)를 고른다: " + pk.tiers.join());
  await page.evaluate(() => [...document.querySelectorAll(".sheet .pick-tier button")].find((b) => b.textContent === "Fast").click());
  await page.evaluate(() => [...document.querySelectorAll(".sheet button")].find((b) => b.textContent === "적용").click());
  await waitFn(() => !document.querySelector(".sheet") && [...document.querySelectorAll(".composer-bar .chip .fast-mark")].some((m) => m.getAttribute("aria-label") === "Fast"), undefined, 5000);
  const chipFit = await page.evaluate(() => { const m = [...document.querySelectorAll(".composer-bar .chip")].find((c) => c.querySelector(".fast-mark")).querySelector(".main");
    return { text: m.textContent, cut: m.scrollWidth - m.clientWidth }; });
  must(true, "Fast 를 고르면 모델 칩에 ⚡(Fast)가 보인다");
  must(chipFit.text === "GPT-Fake-Sol" && chipFit.cut <= 1, "Fast 를 켜도 모델 칩의 모델 이름이 잘리지 않는다 " + JSON.stringify(chipFit));
  await shot("14b-pick-fast");
  await page.evaluate(() => fetch("/api/models/settings", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agent: "codex", model: "", effort: "", tier: "", hidden: [] }) }));
  await page.evaluate(() => localStorage.removeItem("codex-lens.pick.new"));

  // 최근 칩 — 보낼 때 쓴 모델 · effort · 속도가 입력창 위에 칩으로 남고, 누르면 바로 그 조합으로 바뀐다
  await page.evaluate(async (cwd) => {
    const post = (text, recent) => fetch("/api/sessions", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cwd, text, client_id: crypto.randomUUID(), recent }) });
    await post("최근 칩 시드 1", { agent: "codex", model: "gpt-fake-luna", effort: "low", tier: "" });
    await post("최근 칩 시드 2", { agent: "codex", model: "gpt-fake-sol", effort: "medium", tier: "priority" });
  }, PROJ);
  await page.goto(BASE + "#/new");
  await page.reload();
  await waitFn(() => document.querySelectorAll(".recent-picks .recent-chip").length >= 2, undefined, 8000);
  const rc = await page.evaluate(() => {
    const row = document.querySelector(".recent-picks"), chips = [...row.querySelectorAll(".recent-chip")];
    const main = document.querySelector(".composer-bar .chip");
    return { texts: chips.map((c) => c.textContent), fast: chips.map((c) => !!c.querySelector(".fast-mark")),
      main: main.textContent, h: row.getBoundingClientRect().height, wrap: chips.some((c) => c.getBoundingClientRect().height > 34),
      overflowX: getComputedStyle(row).overflowX };
  });
  must(rc.texts[0] === "GPT-Fake-Solmedium⚡", "가장 최근 조합이 맨 앞 칩: " + rc.texts.join(" | "));
  must(rc.fast[0], "Fast 로 보낸 조합은 칩에 ⚡ 가 붙는다");
  must(rc.texts.some((t) => /GPT-Fake-Lunalow/.test(t)), "앞서 쓴 다른 조합도 칩으로 남는다: " + rc.texts.join(" | "));
  must(!rc.texts.includes(rc.main), "지금 쓰일 조합(모델 칩)은 최근 칩에 다시 보이지 않는다: " + rc.main);
  must(rc.texts.every((t) => /(low|medium|high)/.test(t)) && new Set(rc.texts).size === rc.texts.length,
    "effort 를 비워 보낸 조합도 모델의 기본 effort 로 보이고 같은 칩이 두 번 나오지 않는다: " + rc.texts.join(" | "));
  must(rc.h <= 40 && !rc.wrap && rc.overflowX === "auto", "최근 칩은 한 줄(넘치면 옆으로 민다) " + JSON.stringify(rc));
  await shot("14c-recent-picks");
  await page.evaluate(() => [...document.querySelectorAll(".recent-picks .recent-chip")].find((c) => /GPT-Fake-Luna/.test(c.textContent)).click());
  await waitFn(() => /GPT-Fake-Luna/.test(document.querySelector(".composer-bar .chip").textContent), undefined, 3000);
  const rc2 = await page.evaluate(() => ({ main: document.querySelector(".composer-bar .chip").textContent,
    pick: JSON.parse(localStorage.getItem("codex-lens.pick.new") || "null"),
    texts: [...document.querySelectorAll(".recent-picks .recent-chip")].map((c) => c.textContent) }));
  must(/GPT-Fake-Luna/.test(rc2.main) && /low/.test(rc2.main) && rc2.pick && rc2.pick.model === "gpt-fake-luna" && rc2.pick.effort === "low",
    "최근 칩을 누르면 모델 칩·고른 값이 그 조합으로 바뀐다 " + JSON.stringify(rc2));
  must(!rc2.texts.some((t) => /GPT-Fake-Luna/.test(t)) && rc2.texts.some((t) => /GPT-Fake-Sol/.test(t)), "바꾼 조합은 최근 줄에서 빠지고 다른 조합이 남는다 " + rc2.texts.join(" | "));
  await page.evaluate(() => localStorage.removeItem("codex-lens.pick.new"));

  // Claude Code — 새 세션에서 에이전트를 고르면 모델·effort 선택지가 그 에이전트 것으로 바뀌고, Claude 세션이 돈다
  await page.goto(BASE + "#/new");
  await waitSel(".agent-seg button", 5000);
  const ag = await page.evaluate(() => ({ btns: [...document.querySelectorAll(".agent-seg button")].map((b) => b.textContent + ":" + b.getAttribute("aria-checked")),
    sub: document.getElementById("subtitle")?.textContent || "" }));
  must(ag.btns.join() === "Codex:true,Claude Code:false", "새 세션에 에이전트 고르기(기본 Codex) " + ag.btns.join());
  must(!/Codex/.test(ag.sub), "머리 영역에 에이전트 이름을 따로 적지 않는다(고르는 칸과 겹치지 않게) " + ag.sub);
  await page.evaluate(() => [...document.querySelectorAll(".agent-seg button")].find((b) => b.textContent === "Claude Code").click());
  await page.evaluate(() => document.querySelector(".composer-bar .chip").click());
  await waitSel(".sheet .file-list .opt", 5000);
  const cm = await page.evaluate(() => ({ title: document.querySelector(".sheet .sheet-head b").textContent,
    models: [...document.querySelectorAll(".sheet .file-list .opt b")].map((b) => b.textContent),
    note: document.querySelector(".sheet .file-list .opt .opt-note")?.textContent || "" }));
  must(/Claude Code/.test(cm.title) && cm.models.includes("Claude Opus 5.5") && !cm.models.some((m) => /GPT-Fake/.test(m)),
    "Claude Code 를 고르면 Claude 모델만 보인다 " + cm.models.join(","));
  must(!cm.models.includes("기본 모델"), "고르는 창에 '기본 모델' 칸 대신 실제 모델만 보인다 " + cm.models.join(","));
  await page.evaluate(() => [...document.querySelectorAll(".sheet .file-list .opt")].find((o) => /Claude Sonnet/.test(o.textContent)).click());
  const ce = await page.evaluate(() => [...document.querySelectorAll(".sheet .seg button")].map((b) => b.textContent));
  must(ce.join() === "기본,low,medium,high,xhigh,max", "Claude 모델의 effort 선택지 " + ce.join());
  await page.evaluate(() => [...document.querySelectorAll(".sheet .seg button")].find((b) => b.textContent === "high").click());
  await page.evaluate(() => [...document.querySelectorAll(".sheet button.primary")].find((b) => b.textContent === "적용").click());
  const chipTxt = await page.evaluate(() => document.querySelector(".composer-bar .chip").textContent);
  must(/Claude Sonnet 5\.5/.test(chipTxt) && /high/.test(chipTxt), "모델 칩에 고른 Claude 모델·effort " + chipTxt);
  await page.fill("css=.field input", PROJ);
  await page.fill("css=.composer textarea", "Claude 로 README 읽어 줘");
  await page.click("css=.composer-bar button.primary", { label: "Claude 세션 시작" });
  await page.waitForFunction(() => /#\/s\//.test(location.hash), undefined, { timeout: 10000 });
  await waitFn(() => /작업을 마쳤습니다/.test(document.querySelector(".annot[data-part=answer]")?.textContent || ""), undefined, 20000);
  const cs = await page.evaluate(async () => {
    const sid = decodeURIComponent(location.hash.split("/")[2]);
    const d = await (await fetch("/api/sessions/" + encodeURIComponent(sid))).json();
    return { agent: d.session.agent, model: d.session.model, steps: [...document.querySelectorAll(".turn .proc-toggle")].map((b) => b.textContent).join(" ") };
  });
  must(cs.agent === "claude" && cs.model === "claude-sonnet-5-5", "Claude Code 세션이 고른 모델로 돌아 첫 답을 받는다 " + JSON.stringify(cs));
  await shot("12b-claude-session");
  // 기존 Claude Code 기록도 턴(요청 · 과정 · 답변)으로 읽는다
  await page.goto(BASE + "#/");
  await waitSel(".srow .agent-ico.claude", 10000);
  await page.evaluate(() => [...document.querySelectorAll(".srow")].find((r) => /README 할 일 체크 목록/.test(r.textContent)).click());
  await waitSel(".turn", 10000);
  await page.evaluate(() => document.querySelector(".turn .proc-toggle").click());
  await waitFn(() => document.querySelectorAll(".turn .steps .step").length >= 4);
  const hist = await page.evaluate(() => ({ req: document.querySelector(".turn .req-body").textContent,
    ans: document.querySelector(".turn .annot[data-part=answer]").textContent,
    steps: [...document.querySelectorAll(".turn .steps .step")].map((e) => e.textContent.slice(0, 40)).join(" | "),
    overflow: document.documentElement.scrollWidth - innerWidth }));
  must(/체크 목록으로 바꿔/.test(hist.req) && /체크 목록/.test(hist.ans), "Claude 기록의 요청·답변");
  must(/Read · README\.md/.test(hist.steps) && /README\.md/.test(hist.steps), "Claude 기록의 과정(도구·파일 변경) " + hist.steps);
  must(hist.overflow <= 0, "Claude 세션 화면 가로 넘침 없음");
  await shot("12c-claude-history");
  // 포트: 세션이 lens-port 로 띄운 서버를 목록 머리의 "포트 N" → 포트 화면에서 보고, 로그를 펼치고, 끈다(412×915)
  await page.cdp("Emulation.setDeviceMetricsOverride", { width: 412, height: 915, deviceScaleFactor: 2, mobile: true });
  await page.goto(BASE + "#/");
  await waitSel("#view .srow", 15000);
  const portSid = await page.evaluate(() => document.querySelector("#view .srow").dataset.sid);
  const started = await page.evaluate(async ({ cwd, sid }) => {
    const r = await fetch("/api/ports", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "web", cwd, thread_id: sid, sh: "exec python3 -m http.server $PORT --bind $CODEX_LENS_BIND" }) });
    return r.json();
  }, { cwd: PROJ, sid: portSid });
  must(started.port >= 20000 && started.port <= 20999 && started.state === "starting", "lens-port API 가 대역 안의 빈 포트를 골라 띄운다(" + started.port + ")");
  await page.reload();
  await page.waitForFunction(() => document.getElementById("app-ports").textContent === "포트 1", undefined, { timeout: 8000 });
  await page.click("css=#app-ports", { label: "공통 상단 바에서 포트 열기" });
  await waitSel(".prow", 8000);
  await page.waitForFunction((p) => { const r = document.querySelector('.prow'); return r && r.querySelector(".pnum").textContent === ":" + p && /열림/.test(r.querySelector(".badge").textContent); }, started.port, { timeout: 15000 });
  const pv = await page.evaluate(() => {
    const r = document.querySelector(".prow"), a = r.querySelector("a.btnlike");
    return { hash: location.hash, href: a && a.href, title: document.getElementById("title").textContent, sub: document.getElementById("subtitle").textContent,
      overflow: document.documentElement.scrollWidth - innerWidth, session: (r.querySelector(".plink") || {}).textContent || "",
      wraps: [...document.querySelectorAll(".prow button, .prow a.btnlike, #top-actions button")].filter((b) => b.getBoundingClientRect().height > 40).map((b) => b.textContent),
      headH: r.querySelector(".prow-head").getBoundingClientRect().height };
  });
  must(/^#\/ports/.test(pv.hash) && pv.title === "포트", "포트 화면이 열린다(" + pv.hash + ")");
  must(pv.href === "http://127.0.0.1:" + started.port + "/", "열기 주소는 지금 보는 호스트의 그 포트(" + pv.href + ")");
  must(/실행 중 1/.test(pv.sub) && /대역 20000–20999/.test(pv.sub), "머리에 실행 중 개수·대역(" + pv.sub + ")");
  must(pv.session.length > 0, "띄운 세션이 행에 보인다(" + pv.session + ")");
  must(pv.overflow <= 0 && !pv.wraps.length && pv.headH < 44, "포트 화면 가로 넘침·단추 줄바꿈 없음 " + JSON.stringify(pv));
  const served = await page.evaluate(async (u) => { try { const r = await fetch(u, { mode: "no-cors" }); return true; } catch (e) { return false; } }, pv.href);
  must(served, "띄운 서버가 실제로 응답한다");
  // 로그 펼치기 — 주소에 남아 새로 고쳐도 펼쳐져 있다
  await page.evaluate(() => [...document.querySelectorAll(".prow button")].find((b) => b.textContent === "로그").click());
  await page.waitForFunction(() => { const p = document.querySelector(".plog"); return p && /http\.server/.test(p.textContent); }, undefined, { timeout: 8000 });
  must(await page.evaluate(() => /log=p/.test(location.hash)), "펼친 로그가 주소에 남는다");
  await page.reload();
  await page.waitForFunction(() => { const p = document.querySelector(".plog"); return p && /http\.server/.test(p.textContent); }, undefined, { timeout: 8000 });
  must(true, "새로 고쳐도 로그가 펼쳐져 있다");
  await shot("14-ports");
  // 세션 머리의 "포트 1" → 그 세션 것만
  await page.goto(BASE + "#/s/" + encodeURIComponent(portSid));
  await waitSel(".turn", 15000);
  await page.waitForFunction(() => [...document.querySelectorAll("#top-actions button")].some((b) => b.textContent === "포트 1"), undefined, { timeout: 8000 });
  await page.evaluate(() => [...document.querySelectorAll("#top-actions button")].find((b) => b.textContent === "포트 1").click());
  await waitSel(".prow", 8000);
  must(await page.evaluate((sid) => location.hash.indexOf("thread=" + sid) >= 0 && document.querySelector(".port-tools button.on").textContent === "이 세션만", portSid), "세션에서 열면 그 세션이 띄운 것만 보인다");
  // 끄기: 한 번 누르면 확인, 두 번째에 끈다 → 꺼짐, 서버가 응답하지 않는다, 지우기로 정리
  await page.evaluate(() => document.querySelector(".prow button.danger").click());
  must(await page.evaluate(() => document.querySelector(".prow button.danger").textContent === "정말 끄기"), "끄기는 한 번 더 눌러 확인한다");
  await page.evaluate(() => document.querySelector(".prow button.danger").click());
  await page.waitForFunction(() => /꺼짐/.test(document.querySelector(".prow .badge").textContent), undefined, { timeout: 12000 });
  const gone = await page.evaluate(async (u) => { try { await fetch(u, { mode: "no-cors" }); return false; } catch (e) { return true; } }, pv.href);
  must(gone, "끈 서버는 더 응답하지 않는다");
  must(await page.evaluate(() => !document.querySelector(".prow a.btnlike") && /실행 중 없음/.test(document.getElementById("subtitle").textContent)), "꺼진 행에는 열기가 없고 머리가 실행 중 없음");
  await shot("15-ports-stopped");
  await page.evaluate(() => [...document.querySelectorAll(".prow button")].find((b) => b.textContent === "지우기").click());
  await page.waitForFunction(() => !document.querySelector(".prow") && document.querySelector(".ports .empty"), undefined, { timeout: 8000 });
  must(true, "지우기로 기록이 사라진다");
  }
  // 넓은 화면: 세션은 새 페이지가 아니라 목록 오른쪽에 열리고, 손잡이로 폭을 바꾼다
  await page.cdp("Emulation.setDeviceMetricsOverride", { width: 1280, height: 800, deviceScaleFactor: 1, mobile: false });
  if (LAYOUT_ONLY) await page.goto(BASE);
  await page.evaluate(() => localStorage.removeItem("codex-lens.sessRatio"));
  await page.goto(BASE + "#/");
  await waitSel("#view .srow", 15000);
  must(await page.evaluate(() => !document.documentElement.classList.contains("split")), "넓은 화면 목록은 전체 폭(나란히 보기 아님)");
  await checkAppHeader("목록 화면");
  must(await page.evaluate(() => document.getElementById("top").hidden), "목록에서는 앱 제목 머리글을 두 번 표시하지 않는다");
  const listedIds = await page.evaluate(async () => {
    const flat = (xs) => xs.flatMap((s) => [s.id, ...flat(s.children || [])]);
    return flat((await (await fetch("/api/sessions")).json()).sessions).sort().join();
  });
  await page.click("css=.srow >> nth=0", { label: "넓은 화면에서 세션 열기" });
  await waitSel("#view .turn", 15000);
  const wide = await page.evaluate(() => {
    const s = document.getElementById("side").getBoundingClientRect(), v = document.getElementById("view").getBoundingClientRect();
    const on = document.querySelector("#side .srow.on");
    return { split: document.documentElement.classList.contains("split"), rows: document.querySelectorAll("#side .srow").length,
      ids: [...document.querySelectorAll("#side .srow")].map(r => r.dataset.sid).sort().join(),
      titles: [...document.querySelectorAll("#side .srow")].map((r) => r.innerText.split("\n")[0]).join(" | "),
      sideRight: s.right, viewLeft: v.left, viewW: v.width, on: on ? on.dataset.sid : "", hashSid: decodeURIComponent(location.hash.split("/s/")[1] || ""),
      back: document.getElementById("back").getAttribute("aria-label"), overflow: document.documentElement.scrollWidth - window.innerWidth };
  });
  must(wide.split && wide.ids === listedIds, "세션을 열어도 목록이 왼쪽에 모두 남는다(" + wide.rows + "행: " + wide.titles + ")");
  must(wide.sideRight <= wide.viewLeft && wide.viewW >= 420, "세션이 목록 오른쪽에 열린다");
  must(wide.on && wide.on === wide.hashSid, "열린 세션이 목록에 표시된다");
  must(wide.back === "세션 닫기", "머리 단추는 세션 닫기로 표시한다");
  must(wide.overflow <= 0, "나란히 보기 가로 넘침 없음");
  await checkPaneHeader("나란히 보기");
  await checkProcessToggle("넓은 화면 1280×800");
  await shot("13-split");
  // 손잡이를 왼쪽으로 200px 끌면 세션이 넓어지고, 새로 고쳐도 폭이 남는다
  const sp = await page.evaluate(() => { const r = document.getElementById("splitter").getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + 200 }; });
  const mouse = (type, x, buttons) => page.cdp("Input.dispatchMouseEvent", { type, x, y: sp.y, button: "left", buttons, clickCount: 1, pointerType: "mouse" });
  await mouse("mousePressed", sp.x, 1);
  for (let i = 1; i <= 5; i++) await mouse("mouseMoved", sp.x - 40 * i, 1);
  await mouse("mouseReleased", sp.x - 200, 0);
  const viewW2 = await page.evaluate(() => document.getElementById("view").getBoundingClientRect().width);
  must(Math.abs(viewW2 - (wide.viewW + 200)) < 12, "손잡이를 끌면 세션 폭이 바뀐다(" + Math.round(wide.viewW) + " → " + Math.round(viewW2) + ")");
  await page.reload();
  await waitSel("#view .turn", 15000);
  const viewW3 = await page.evaluate(() => document.getElementById("view").getBoundingClientRect().width);
  must(Math.abs(viewW3 - viewW2) < 4, "고친 세션 폭이 다시 열어도 남는다");
  await checkPaneHeader("폭 조절·새로고침 뒤");
  await page.evaluate(() => window.scrollTo(0, 250));
  await checkPaneHeader("본문 스크롤 뒤");
  await page.evaluate(() => window.scrollTo(0, 0));
  // 모바일로 줄였다가 돌아와도 같은 머리와 입력창을 유지한다.
  await page.fill("css=.composer textarea", "패널 전환 중 초안");
  await page.cdp("Emulation.setDeviceMetricsOverride", { width: 412, height: 915, deviceScaleFactor: 2, mobile: true });
  await waitFn(() => !document.documentElement.classList.contains("split"));
  must(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth && document.querySelectorAll("#title").length === 1 &&
    document.querySelector(".composer textarea").value === "패널 전환 중 초안"), "모바일 전환 시 머리가 중복되지 않고 초안과 화면 폭을 유지한다");
  await checkAppHeader("모바일 세션 화면");
  await shot("16-mobile-global-header");
  await page.cdp("Emulation.setDeviceMetricsOverride", { width: 1280, height: 800, deviceScaleFactor: 1, mobile: false });
  await waitFn(() => document.documentElement.classList.contains("split"));
  await checkPaneHeader("모바일에서 돌아온 뒤");
  await page.fill("css=.composer textarea", "");
  await page.click("css=#app-projects", { label: "공통 프로젝트 메뉴 열기" });
  await waitFn(() => /^#\/files/.test(location.hash) && document.getElementById("title").textContent === "프로젝트");
  await checkAppHeader("프로젝트 화면");
  await page.click("css=#pane-back", { label: "프로젝트에서 세션으로 돌아가기" });
  await waitSel("#view .turn");
  await page.click("css=#app-ports", { label: "공통 포트 메뉴 열기" });
  await waitSel("#view .ports");
  await checkAppHeader("포트 화면");
  await page.click("css=#pane-back", { label: "포트에서 세션으로 돌아가기" });
  await waitSel("#view .turn");
  await page.click("css=#version", { label: "공통 버전 이력 열기" });
  await waitFn(() => document.querySelector(".sheet .changelog")?.textContent.includes(window.CODEX_LENS.version));
  must(true, "세션을 읽는 중에도 공통 상단 바의 변경 이력이 열린다");
  await page.click("css=.sheet-head button", { label: "변경 이력 닫기" });
  // 다른 세션을 누르면 오른쪽만 바뀐다
  await page.click("css=#side .srow:not(.on) >> nth=0", { label: "다른 세션 열기" });
  await page.waitForFunction((sid) => decodeURIComponent(location.hash.split("/s/")[1] || "") !== sid && document.querySelector("#side .srow.on"), wide.hashSid, { timeout: 10000 });
  must(await page.evaluate((ids) => document.documentElement.classList.contains("split") &&
    [...document.querySelectorAll("#side .srow")].map(r => r.dataset.sid).sort().join() === ids, listedIds), "다른 세션을 눌러도 목록은 그대로");
  // 넓은 화면의 웹 보기: 목록 옆 오른쪽 창에 열리고, 창 높이를 넘지 않는다
  await page.evaluate((base) => { location.hash = "#/web?url=" + encodeURIComponent(base + "api/info"); }, BASE);
  await waitFn(() => { const f = document.querySelector("#view iframe.web-frame"); return f && f.getBoundingClientRect().height > 0; }, undefined, 10000);
  const ww = await page.evaluate(() => { const f = document.querySelector("#view iframe.web-frame").getBoundingClientRect(), s = document.getElementById("side").getBoundingClientRect();
    return { split: document.documentElement.classList.contains("split"), left: f.left, side: s.right, bottom: f.bottom, h: f.height, innerH: innerHeight,
      scroll: document.documentElement.scrollHeight - innerHeight }; });
  must(ww.split && ww.left >= ww.side && ww.bottom <= ww.innerH + 0.5 && ww.h > ww.innerH * 0.6 && ww.scroll <= 1, "넓은 화면 웹 보기는 목록 옆에 창 높이만큼 " + JSON.stringify(ww));
  await page.evaluate(() => history.back());
  await waitSel("#view .turn", 10000);
  // 오른쪽 창 안에서 파일 → 폴더로 들어가면 닫기 옆에 뒤로(‹)가 생기고, 누르면 한 단계씩 돌아온다
  await page.evaluate(() => [...document.querySelectorAll("#top-actions button")].find((b) => b.textContent === "파일").click());
  await waitSel("#view .tree .frow", 5000);
  await page.evaluate(() => [...document.querySelectorAll("#view .tree .frow")].find((b) => b.querySelector(".fname").textContent === "docs").click());
  await waitFn(() => [...document.querySelectorAll("#view .tree .frow .fname")].some((x) => x.textContent === "design.md"));
  const pb = await page.evaluate(() => ({ pane: !document.getElementById("pane-back").hidden, close: document.getElementById("back").getAttribute("aria-label") }));
  must(pb.pane && pb.close === "세션 닫기", "나란히 보기에서 폴더에 들어가면 세션 닫기와 뒤로 단추");
  await page.click("css=#pane-back", { label: "오른쪽 창에서 뒤로" });
  await waitFn(() => /^#\/files/.test(location.hash) && !/[?&]dir=/.test(location.hash) && [...document.querySelectorAll("#view .tree .frow .fname")].some((x) => x.textContent === "README.md"));
  must(true, "뒤로를 누르면 상위 폴더로 돌아온다");
  await page.click("css=#pane-back", { label: "오른쪽 창에서 뒤로" });
  await waitSel("#view .turn", 10000);
  must(await page.evaluate(() => /^#\/s\//.test(location.hash) && document.documentElement.classList.contains("split")), "한 번 더 뒤로를 누르면 세션으로 돌아온다");
  await page.click("css=#back", { label: "오른쪽 창 닫기" });
  await waitSel("#view .srow", 5000);
  must(await page.evaluate(() => !document.documentElement.classList.contains("split") && location.hash.replace(/^#/, "") === "/"), "닫으면 목록만 전체 폭으로");
  await checkAppHeader("세션을 닫은 뒤");
  await save({ ok: true });
  console.log(JSON.stringify({ ok: true, results }, null, 1));
} catch (e) {
  await shot("zz-failure").catch(() => {});
  const dom = await page.evaluate(() => ({ panel: document.querySelector(".refine-panel")?.innerText, toast: document.getElementById("toast")?.innerText,
    doc: document.querySelector(".doc .md")?.textContent?.slice(0, 600) })).catch(() => null);
  await save({ ok: false, error: String(e && e.stack || e), url: await page.url().catch(() => ""), dom });
  console.error(String(e && e.stack || e));
  throw e;
}
