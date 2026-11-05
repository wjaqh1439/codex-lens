/* Codex Lens 화면 — 목록 · 새 세션 · 세션(턴: 요청·과정·답변) · 프로젝트 탐색기 · 문서(.md) 보기·AI 다듬기.
   고른 글에는 인라인 질문·코멘트를 남긴다. */
(function () {
  'use strict';
  var BOOT = window.CODEX_LENS || {};
  var md = window.CodexMd;
  var view = document.getElementById('view');
  var titleEl = document.getElementById('title');
  var subEl = document.getElementById('subtitle');
  var sidEl = document.getElementById('sid');
  var actionsEl = document.getElementById('top-actions');
  var topEl = document.getElementById('top');
  var backBtn = document.getElementById('back');
  var paneBackBtn = document.getElementById('pane-back');
  var appHome = document.getElementById('app-home'), appPorts = document.getElementById('app-ports');
  appHome.textContent = BOOT.name || 'Codex Lens';
  appHome.addEventListener('click', function () { go('#/'); });
  document.getElementById('app-projects').addEventListener('click', function () { go('#/files'); });
  document.getElementById('app-case').addEventListener('click', function () {
    var c = lsGet('fde.lastCwd', '');
    if (parseHash().path !== '/case') go('#/case' + (c ? '?cwd=' + encodeURIComponent(c) : ''));
  });
  document.getElementById('app-models').addEventListener('click', function () { if (parseHash().path !== '/models') go('#/models'); });
  appPorts.addEventListener('click', function () { go(portsUrl()); });
  function refreshAppPorts() {
    api('GET', '/api/ports').then(function (r) {
      var n = (r.items || []).filter(portRunning).length;
      appPorts.textContent = '포트' + (n ? ' ' + n : '');
    }, function () {});
  }
  var MAC = /Mac|iPhone|iPad|iPod/.test(navigator.platform);
  var MOD = MAC ? '⌘' : 'Ctrl+';
  var STALL_SEC = 30;
  var LS = 'codex-lens.';
  var RESEND_MAX_SEC = window.CODEX_LENS_RESEND_MAX_SEC || 180;   // 보내기 실패 뒤 앱이 돌아오길 기다리는 최대 시간
  var STAGE = { starting: '보내는 중', forking: '포크를 띄우는 중', waiting: '답을 기다리는 중', thinking: '생각하는 중',
    reading: '읽는 중', answering: '답하는 중', compacting: '대화 압축 중', done: '완료', failed: '실패', cancelled: '멈춤' };
  var STAGE_SHORT = { starting: '보냄', forking: '포크', waiting: '대기', thinking: '생각', reading: '읽기', answering: '답', compacting: '압축' };
  var KIND = { command: '명령', file_change: '파일', reasoning: '생각', message: '메시지', tool: '도구', web_search: '검색', plan: '계획',
    image: '이미지', compaction: '압축', agent: '하위 에이전트', review: '리뷰', other: '기타' };
  var AGENT_STATE = { pendingInit: '준비 중', running: '실행 중', interrupted: '멈춤', completed: '끝남', errored: '오류', shutdown: '닫힘', notFound: '없음' };
  var STATE_LABEL = { none: '저장만', input: '입력창에 있음', sent: '세션에 보냄', memo: '메모로 둠' };
  var MENTION_MARK = '[codex-lens · 멘션 맥락]';

  // ---------------------------------------------------------------- 기본 도구
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function btn(label, cls, fn, title) {
    var b = el('button', cls || null, label); b.type = 'button';
    if (title) b.title = title;
    if (fn) b.addEventListener('click', fn);
    return b;
  }
  // 앱 서버가 내려가 있을 때(재시작 중) — 접속 자체가 안 되거나, 앞단 프록시(tailscale serve)가 JSON 이 아닌 502·503·504 를
  // 돌려줄 때 — 는 실패로 끝내지 않고 "서버 재시작 중" 띠를 띄운 채 /api/info 가 다시 답할 때까지 기다렸다가 같은 요청을 다시 보낸다.
  // 앱이 스스로 낸 502(에이전트 오류)는 JSON 이라 그대로 실패로 돌린다. 새 세션·턴 보내기는 client_id 로 서버가 중복을 거른다.
  var DOWN_GIVEUP_SEC = 120;
  var down = null;   // { t0, waiters: [{resolve, reject}], timer, tick, bar }
  function proxyDown(r) {
    return (r.status === 502 || r.status === 503 || r.status === 504) && !/json/i.test(r.headers.get('Content-Type') || '');
  }
  function downBar() {
    var b = document.getElementById('server-down');
    if (b) return b;
    b = el('div', 'server-down'); b.id = 'server-down'; b.setAttribute('role', 'status'); b.setAttribute('aria-live', 'polite');
    b.appendChild(el('span', 'sd-msg'));
    b.appendChild(btn('그만 기다리기', 'sd-cancel', function () { downGiveUp('기다리기를 멈췄습니다'); }));
    document.body.appendChild(b);
    return b;
  }
  function downRender() {
    if (!down) return;
    var s = Math.floor((Date.now() - down.t0) / 1000), n = down.waiters.length;
    var head = s < STALL_SEC ? '서버 재시작 중' : '서버 재시작이 늦어지고 있습니다(지연)';
    down.bar.querySelector('.sd-msg').textContent = head + ' · ' + s + '초' +
      (n ? ' · 연결되면 요청 ' + n + '건을 다시 보냅니다' : ' · 연결을 기다리는 중');
    down.bar.classList.toggle('stalled', s >= STALL_SEC);
    if (s >= DOWN_GIVEUP_SEC) downGiveUp('서버가 ' + DOWN_GIVEUP_SEC + '초째 답하지 않습니다');
  }
  function downEnd() {
    var d = down; down = null;
    clearInterval(d.timer); clearInterval(d.tick);
    d.bar.hidden = true;
    return d;
  }
  function downGiveUp(why) {
    if (!down) return;
    var d = downEnd();
    clientLog({ type: 'server_down', level: 'warn', ms: Date.now() - d.t0, count: d.waiters.length, detail: 'gave_up' });
    d.waiters.forEach(function (w) { var e = new Error(why); e.status = 0; w.reject(e); });
  }
  function waitServerUp() {
    return new Promise(function (resolve, reject) {
      if (!down) {
        down = { t0: Date.now(), waiters: [], bar: downBar() };
        down.bar.hidden = false;
        down.tick = setInterval(downRender, 1000);
        down.timer = setInterval(function () {
          fetch('/api/info', { cache: 'no-store' }).then(function (r) {
            if (!r.ok || !down) return;
            var d = downEnd();
            clientLog({ type: 'server_down', ms: Date.now() - d.t0, count: d.waiters.length, detail: 'recovered' });
            toast('서버에 다시 연결했습니다' + (d.waiters.length ? ' — 요청을 다시 보냅니다' : ''));
            d.waiters.forEach(function (w) { w.resolve(); });
          }, function () {});
        }, 1000);
      }
      down.waiters.push({ resolve: resolve, reject: reject });
      downRender();
    });
  }
  // noWait: 재시작 띠로 기다리지 않고 끊기면 err.unreachable 로 바로 돌려준다(입력창이 글을 지키며 스스로 다시 보낸다)
  function api(method, url, body, noWait) {
    var opt = { method: method, headers: {} };
    if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    var t0 = Date.now();
    function retry() {
      if (noWait) { var u = new Error('서버에 닿지 않습니다 — 다시 뜨는 중일 수 있습니다'); u.status = 0; u.unreachable = true; throw u; }
      if (Date.now() - t0 > DOWN_GIVEUP_SEC * 1000) {
        var e = new Error('서버에 닿지 않습니다 — ' + DOWN_GIVEUP_SEC + '초 동안 다시 연결하지 못했습니다'); e.status = 0; throw e;
      }
      return waitServerUp().then(attempt);
    }
    function attempt() {
      return fetch(url, opt).then(function (r) {
        if (proxyDown(r)) return retry();
        return r.text().then(function (t) {
          var j = null;
          try { j = t ? JSON.parse(t) : {}; } catch (e) { j = { error: t }; }
          if (!r.ok) { var err = new Error((j && j.error) || ('HTTP ' + r.status)); err.status = r.status; throw err; }
          return j;
        });
      }, function () { return retry(); });
    }
    return attempt();
  }
  function uuid4() {
    var b = new Uint8Array(16); crypto.getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
    var h = Array.prototype.map.call(b, function (x) { return (x + 0x100).toString(16).slice(1); }).join('');
    return h.slice(0, 8) + '-' + h.slice(8, 12) + '-' + h.slice(12, 16) + '-' + h.slice(16, 20) + '-' + h.slice(20);
  }
  function lsGet(k, d) { try { var v = localStorage.getItem(LS + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } }
  function lsSet(k, v) { try { if (v == null) localStorage.removeItem(LS + k); else localStorage.setItem(LS + k, JSON.stringify(v)); } catch (e) {} }
  var toastTimer = null;
  function toast(msg, action) {
    var t = document.getElementById('toast');
    t.innerHTML = ''; t.appendChild(document.createTextNode(msg));
    if (action) { var a = el('a', null, action.label); a.href = action.href || '#'; a.addEventListener('click', function (e) { if (action.fn) { e.preventDefault(); action.fn(); } }); t.appendChild(a); }
    t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { t.hidden = true; }, action ? 6000 : 3200);
  }
  function copyText(s) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(s).then(function () { toast('복사했습니다'); });
    var ta = el('textarea'); ta.value = s; ta.style.position = 'fixed'; ta.style.opacity = '0'; document.body.appendChild(ta);
    ta.select(); try { document.execCommand('copy'); toast('복사했습니다'); } catch (e) { toast('복사하지 못했습니다'); }
    ta.remove(); return Promise.resolve();
  }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function fmtTime(sec) {
    if (!sec) return '';
    var d = new Date(sec * 1000), now = new Date();
    var hm = pad(d.getHours()) + ':' + pad(d.getMinutes());
    if (d.toDateString() === now.toDateString()) return '오늘 ' + hm;
    return (d.getFullYear() !== now.getFullYear() ? d.getFullYear() + '-' : '') + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + hm;
  }
  function ago(sec) {
    if (!sec) return '';
    var s = Date.now() / 1000 - sec;
    if (s < 60) return '방금';
    if (s < 3600) return Math.floor(s / 60) + '분 전';
    if (s < 86400) return Math.floor(s / 3600) + '시간 전';
    if (s < 86400 * 7) return Math.floor(s / 86400) + '일 전';
    return fmtTime(sec);
  }
  function dur(ms) {
    if (ms == null) return '';
    var s = ms / 1000;
    if (s < 10) return s.toFixed(1) + '초';
    if (s < 60) return Math.round(s) + '초';
    var m = Math.floor(s / 60);
    if (m < 60) return m + '분 ' + Math.round(s % 60) + '초';
    return Math.floor(m / 60) + '시간 ' + (m % 60) + '분';
  }
  // 단어 단위 차이(LCS) — 다듬기 원문 비교. 너무 길면 통째로 바뀐 것으로 본다
  function wordDiff(a, b) {
    var A = a.split(/(\s+)/), B = b.split(/(\s+)/), n = A.length, m = B.length, out = [], i, j;
    function push(t, s) { var l = out[out.length - 1]; if (l && l.t === t) l.s += s; else if (s) out.push({ t: t, s: s }); }
    if (n * m > 4e6) { push('del', a); push('ins', b); return out; }
    var dp = []; for (i = 0; i <= n; i++) dp.push(new Uint32Array(m + 1));
    for (i = n - 1; i >= 0; i--) for (j = m - 1; j >= 0; j--) dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    i = 0; j = 0;
    while (i < n && j < m) {
      if (A[i] === B[j]) { push('eq', A[i]); i++; j++; }
      else if (dp[i + 1][j] >= dp[i][j + 1]) push('del', A[i++]);
      else push('ins', B[j++]);
    }
    while (i < n) push('del', A[i++]);
    while (j < m) push('ins', B[j++]);
    // 이어진 변경(사이의 빈칸 포함)은 지운 말 묶음 → 더한 말 묶음으로 — 낱말마다 번갈아 나오면 읽기 어렵다
    var res = [], del = '', ins = '', gap = '';
    function flush() { if (del) res.push({ t: 'del', s: del }); if (ins) res.push({ t: 'ins', s: ins }); if (gap) res.push({ t: 'eq', s: gap }); del = ins = gap = ''; }
    out.forEach(function (d) {
      if (d.t === 'eq' && (del || ins) && /^\s+$/.test(d.s)) { gap += d.s; return; }
      if (d.t === 'eq') { flush(); res.push(d); return; }
      if (gap) { if (del) del += gap; if (ins) ins += gap; gap = ''; }
      if (d.t === 'del') del += d.s; else ins += d.s;
    });
    flush();
    return res;
  }
  function kfmt(n) { return n == null ? '' : n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n); }
  function base(p) { return (p || '').replace(/\/+$/, '').split('/').pop() || p || ''; }
  function squash(s) { return (s || '').replace(/\s+/g, ''); }
  function clientLog(ev) {
    try { navigator.sendBeacon ? navigator.sendBeacon('/api/client-log', new Blob([JSON.stringify({ events: [ev] })], { type: 'application/json' }))
      : api('POST', '/api/client-log', { events: [ev] }); } catch (e) {}
  }
  window.addEventListener('error', function (e) { clientLog({ type: 'error', level: 'error', error: String(e.message || e).slice(0, 300), route: location.hash }); });


  // 설치된 앱(standalone)에서는 바깥 링크를 같은 창에서 연다 — 뒤로 가기로 돌아오게
  function standalone() { return (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) || navigator.standalone === true; }
  document.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
    var a = e.target.closest && e.target.closest('a[target="_blank"]');
    if (!a || a.hasAttribute('download')) return;
    var href = a.href || '';
    if (!/^https?:/i.test(href)) return;
    // 답변·문서 속 바깥 링크(md.js 의 a.ext)는 앱 안의 웹 보기로 연다 — 데모 페이지를 세션 옆에서 바로 본다
    if (a.classList.contains('ext') && view.contains(a)) { e.preventDefault(); go(webUrl(href, S.X && S.X.sid || (S.D && S.D.from) || null)); return; }
    if (!standalone()) return;
    e.preventDefault(); location.assign(href);
  });
  var origOpen = window.open;
  window.open = function (url) {
    if (standalone() && url && /^https?:/i.test(String(url))) { location.assign(url); return null; }
    return origOpen.apply(window, arguments);
  };

  // ---------------------------------------------------------------- 라우터 · 위치 복원
  history.scrollRestoration = 'manual';
  var S = { cleanup: [], seq: 0 };
  var navKey = (history.state && history.state.k) || ('k' + Date.now());
  if (!history.state || !history.state.k) history.replaceState({ k: navKey }, '', location.href);
  function saveScroll() {
    try { sessionStorage.setItem(LS + 'scroll.' + navKey, JSON.stringify({ y: window.scrollY })); } catch (e) {}
  }
  function savedScroll() {
    try { var v = JSON.parse(sessionStorage.getItem(LS + 'scroll.' + navKey) || 'null'); return v ? v.y : null; } catch (e) { return null; }
  }
  // 오른쪽 창 화면(세션·새 세션·문서·파일) 사이를 몇 번 들어왔는지 — 나란히 보기의 ‹ 단추가 이걸 보고 나온다(history state 의 d)
  function isPanePath(path) { return /^\/s\//.test(path) || path === '/new' || path === '/md' || path === '/files' || path === '/models' || path === '/ports' || path === '/web' || path === '/case'; }
  function paneDepth() { return (history.state && history.state.d) || 0; }
  function go(hash, replace) {
    clearTimeout(scrollSaveTimer);
    saveScroll();
    var from = parseHash().path, to = hash.replace(/^#/, '').split('?')[0] || '/';
    var d = !isPanePath(to) ? 0 : replace ? paneDepth() : isPanePath(from) ? paneDepth() + 1 : 0;
    navKey = 'k' + Date.now() + Math.random().toString(36).slice(2, 6);
    var state = { k: navKey, d: d, p: location.hash, sessionList: history.state && history.state.sessionList };
    if (replace) history.replaceState(state, '', hash); else history.pushState(state, '', hash);
    render(false);
  }
  window.addEventListener('popstate', function (e) {
    clearTimeout(scrollSaveTimer);
    navKey = (e.state && e.state.k) || ('k' + Date.now() + Math.random().toString(36).slice(2, 6));
    if (!e.state || !e.state.k) history.replaceState({ k: navKey }, '', location.href);
    render(true);
  });
  window.addEventListener('pagehide', saveScroll);
  var scrollSaveTimer = null;
  window.addEventListener('scroll', function () { clearTimeout(scrollSaveTimer); scrollSaveTimer = setTimeout(saveScroll, 200); }, { passive: true });
  function restoreScroll(pop) {
    var y = pop ? savedScroll() : null;
    window.scrollTo(0, y == null ? 0 : y);
  }
  function parseHash() {
    var h = location.hash.replace(/^#/, '') || '/';
    var qi = h.indexOf('?'), path = qi >= 0 ? h.slice(0, qi) : h, params = {};
    if (qi >= 0) new URLSearchParams(h.slice(qi + 1)).forEach(function (v, k) { params[k] = v; });
    return { path: path, params: params };
  }
  function setHeader(title, sub, back) {
    document.getElementById('session-context').hidden = true;
    document.getElementById('top').hidden = !isPanePath(parseHash().path);
    titleEl.textContent = title;
    document.title = (BOOT.demo ? '[데모] ' : '') + (title === BOOT.name ? BOOT.name : title + ' · ' + BOOT.name);
    subEl.innerHTML = '';
    (sub || []).filter(Boolean).forEach(function (s, i) {
      if (i) subEl.appendChild(el('span', 'sep', '·'));
      subEl.appendChild(typeof s === 'string' ? document.createTextNode(s) : s);
    });
    backBtn.hidden = !back || (!isSplit() && parseHash().path === '/web' && !!parseHash().params.from);
    actionsEl.innerHTML = '';
    sidEl.hidden = true;
  }
  sidEl.addEventListener('click', function () { copyText(sidEl.dataset.id); });
  // 세션·포크 ID — 누르면 전체 ID 를 복사한다
  function idChip(label, id, title) {
    var b = btn('', 'id-chip', function (e) { e.stopPropagation(); copyText(id); }, (title ? title + ' — ' : '') + '눌러서 복사');
    b.appendChild(el('span', 'id-label', label)); b.appendChild(el('span', 'id-val', id));
    return b;
  }
  // 나란히 보기에서는 목록이 왼쪽에 있으니 "뒤로" 대신 오른쪽 창을 닫는다
  function paintBack() {
    var split = isSplit(), r = parseHash(), webSession = r.path === '/web' && !!r.params.from;
    backBtn.textContent = split ? '세션 닫기' : '‹';
    backBtn.classList.toggle('icon-btn', !split);
    backBtn.classList.toggle('small', split);
    backBtn.setAttribute('aria-label', split ? '세션 닫기' : '뒤로');
    backBtn.title = split ? '세션 패널을 닫고 목록 보기 — 실행 중인 작업은 계속됩니다' : '뒤로';
    backBtn.hidden = !isPanePath(r.path) || (!split && webSession);
    // 창 안의 뒤로 가기는 왼쪽, 세션 패널 닫기는 오른쪽에 둔다.
    paneBackBtn.hidden = !(webSession || (split && paneDepth() > 0));
    paneBackBtn.textContent = webSession ? '세션으로' : '‹';
    paneBackBtn.classList.toggle('icon-btn', !webSession);
    paneBackBtn.classList.toggle('small', webSession);
    paneBackBtn.setAttribute('aria-label', webSession ? '세션으로' : '뒤로');
    paneBackBtn.title = webSession ? '페이지를 닫고 원래 세션으로 돌아가기' : '오른쪽 창에서 뒤로';
  }
  paneBackBtn.addEventListener('click', function () {
    var r = parseHash();
    if (r.path === '/web' && r.params.from) { webToSession(r.params.from, 'session'); return; }
    history.back();
  });
  // 웹 보기에서 떠난 세션으로 곧장 돌아간다. iframe 안에서 옮겨 다닌 기록은 앱의 방문 기록에도 쌓이므로
  // history.back() 은 페이지 안의 이전 화면으로 갈 뿐이다 — 앱의 기록 칸만 보는 Navigation API 로 세션 칸까지 건너뛴다.
  // 그 API 가 없으면 페이지 안에서 옮겨 다니지 않았을 때만 back, 아니면 세션을 새로 연다.
  function webToSession(from, action) {
    var session = '#/s/' + encodeURIComponent(from), nav = window.navigation, how = 'push';
    clientLog({ type: 'web_nav', action: action, depth: S.webDepth || 0 });
    try {
      if (nav && nav.currentEntry && nav.entries) {
        var list = nav.entries();
        for (var i = nav.currentEntry.index - 1; i >= 0; i--) {
          var h = new URL(list[i].url).hash;
          if (h === session) { nav.traverseTo(list[i].key); return; }
          if (!/^#\/web(\?|$)/.test(h)) break;   // 웹 보기 칸(주소창으로 옮겨 간 것)만 건너뛴다
        }
        how = 'push';
      } else if (!S.webDepth && history.state && history.state.p === session) how = 'back';
    } catch (e) {}
    if (how === 'back') history.back(); else go(session);
  }
  backBtn.addEventListener('click', function () {
    if (isSplit()) go(sessionList().url());
    else if (history.length > 1 && S.cameFromApp) history.back(); else go(sessionList().url(), true);
  });
  // FDE 케이스 화면(static/fde.js)에 넘기는 공용 도우미
  function fdeCtx() {
    return { el: el, btn: btn, api: api, toast: toast, setHeader: setHeader, go: go, restoreScroll: restoreScroll, S: S, view: view,
      md: md, lsGet: lsGet, lsSet: lsSet, uuid4: uuid4, clientLog: clientLog };
  }
  function render(pop) {
    S.cleanup.forEach(function (f) { try { f(); } catch (e) {} });
    S.cleanup = [];
    S.seq++;
    hidePills(); closeComment();
    var r = parseHash();
    refreshAppPorts();
    view.innerHTML = '';
    applyLayout(r);
    var m, sid = null;
    if ((m = /^\/s\/([^/]+)$/.exec(r.path))) viewSession((sid = decodeURIComponent(m[1])), pop);
    else if (r.path === '/new') viewNew(r.params, pop);
    else if (r.path === '/md') viewDoc(r.params, pop);
    else if (r.path === '/files') viewFiles(r.params, pop);
    else if (r.path === '/models') viewModels(r.params, pop);
    else if (r.path === '/ports') viewPorts(r.params, pop);
    else if (r.path === '/web') viewWeb(r.params, pop);
    else if (r.path === '/case') window.CodexFde.view(fdeCtx(), r.params, pop);
    else viewList(r.params, pop);
    document.documentElement.classList.toggle('web', r.path === '/web');
    if (LIST) LIST.setActive(sid || ((r.path === '/md' || r.path === '/files' || r.path === '/ports' || r.path === '/web') && r.params.from) || null);
    S.cameFromApp = S.cameFromApp || !pop;
  }

  // ---------------------------------------------------------------- 나란히 보기(넓은 화면)
  // 넓은 화면에서 세션·새 세션·문서는 새 페이지가 아니라 목록 오른쪽에 열린다. 가운데 손잡이를 끌어 세션 폭을 바꾼다.
  // 목록은 왼쪽에서 따로 스크롤되고, 오른쪽(세션)은 지금처럼 창 스크롤을 쓴다 — 위치 복원·입력창 고정이 그대로 동작한다.
  var shell = document.getElementById('shell');
  var side = document.getElementById('side');
  var splitter = document.getElementById('splitter');
  var bannerEl = document.getElementById('demo-banner'), appTopEl = document.getElementById('app-top');
  var wideMq = window.matchMedia('(min-width: 900px)');
  var SESS_MIN = 420, LIST_MIN = 240, SESS_RATIO = 0.62;
  var sessRatio = lsGet('sessRatio', SESS_RATIO);
  function isSplit() { return document.documentElement.classList.contains('split'); }
  function applyLayout(r) {
    r = r || parseHash();
    var split = wideMq.matches && isPanePath(r.path);
    document.documentElement.classList.toggle('split', split);
    if (split) {
      var L = sessionList();
      var changed = L.setState((history.state && history.state.sessionList) || {});
      if (L.el.parentNode !== side) { side.appendChild(L.el); L.load(true); }
      else if (changed) L.load(true);
      measureChrome(); sizeSplit();
    }
    paintBack();
  }
  function measureChrome() {
    document.documentElement.style.setProperty('--banner-h', bannerEl.offsetHeight + 'px');
    document.documentElement.style.setProperty('--chrome-h', (bannerEl.offsetHeight + appTopEl.offsetHeight) + 'px');
  }
  measureChrome();
  if (window.ResizeObserver) {
    var chromeObserver = new ResizeObserver(measureChrome);
    chromeObserver.observe(bannerEl); chromeObserver.observe(appTopEl);
  }
  // 세션 폭 = 전체 × 비율. 세션 420px · 목록 240px 아래로는 줄지 않는다. 실제로 쓴 비율을 돌려준다.
  function sizeSplit(ratio) {
    if (ratio == null) ratio = sessRatio;
    var total = shell.clientWidth - splitter.offsetWidth;
    var w = Math.round(Math.max(SESS_MIN, Math.min(total - LIST_MIN, total * ratio)));
    document.documentElement.style.setProperty('--sess-w', w + 'px');
    splitter.setAttribute('aria-valuenow', String(Math.round(w / total * 100)));
    return w / total;
  }
  function setRatio(ratio, keep) {
    sessRatio = sizeSplit(ratio);
    if (keep) lsSet('sessRatio', sessRatio);
  }
  splitter.addEventListener('pointerdown', function (e) {
    if (e.button !== 0) return;
    e.preventDefault();
    try { splitter.setPointerCapture(e.pointerId); } catch (err) {}
    document.documentElement.classList.add('resizing');
    var box = shell.getBoundingClientRect(), half = splitter.offsetWidth / 2, t0 = performance.now();
    function move(ev) { setRatio((box.right - ev.clientX - half) / (box.width - splitter.offsetWidth), false); }
    function up() {
      splitter.removeEventListener('pointermove', move); splitter.removeEventListener('pointerup', up); splitter.removeEventListener('pointercancel', up);
      document.documentElement.classList.remove('resizing');
      lsSet('sessRatio', sessRatio);
      clientLog({ type: 'split_resize', ms: Math.round(performance.now() - t0), ratio: Math.round(sessRatio * 100) / 100 });
    }
    splitter.addEventListener('pointermove', move); splitter.addEventListener('pointerup', up); splitter.addEventListener('pointercancel', up);
  });
  splitter.addEventListener('dblclick', function () { setRatio(SESS_RATIO, true); });
  splitter.addEventListener('keydown', function (e) {
    var total = shell.clientWidth - splitter.offsetWidth, step = (e.shiftKey ? 96 : 24) / total;
    if (e.key === 'ArrowLeft') setRatio(sessRatio + step, true);        // 손잡이를 왼쪽으로 = 세션이 넓어진다
    else if (e.key === 'ArrowRight') setRatio(sessRatio - step, true);
    else if (e.key === 'Home') setRatio(1, true);
    else if (e.key === 'End') setRatio(0, true);
    else return;
    e.preventDefault();
  });
  window.addEventListener('resize', function () { if (isSplit()) sizeSplit(); });
  // 폭이 바뀌어 나란히 보기가 켜지거나 꺼져도 열린 화면은 그대로 둔다(다시 그리지 않는다)
  (wideMq.addEventListener ? wideMq.addEventListener.bind(wideMq, 'change') : wideMq.addListener.bind(wideMq))(function () { applyLayout(); });

  // ---------------------------------------------------------------- 변경 이력
  document.getElementById('version').addEventListener('click', function () {
    var sh = sheet('변경 이력 · v' + BOOT.version, true);
    var box = el('div', 'md changelog', '불러오는 중…'); sh.body.appendChild(box);
    api('GET', '/api/changelog').then(function (r) { box.innerHTML = md.render(r.text); }, function (e) { box.textContent = e.message; });
  });
  function sheet(title, wide) {
    var bd = el('div', 'backdrop'), sh = el('div', 'sheet' + (wide ? ' wide' : ''));
    sh.setAttribute('role', 'dialog'); sh.setAttribute('aria-label', title);
    var head = el('div', 'sheet-head'); head.appendChild(el('b', null, title));
    var x = btn('✕', 'link', close, '닫기'); head.appendChild(x); sh.appendChild(head);
    var body = el('div'); sh.appendChild(body);
    function close() { bd.remove(); sh.remove(); document.removeEventListener('keydown', esc, true); }
    function esc(e) { if (e.key === 'Escape') { e.preventDefault(); close(); } }
    bd.addEventListener('click', close);
    document.addEventListener('keydown', esc, true);
    document.body.appendChild(bd); document.body.appendChild(sh);
    S.cleanup.push(close);
    return { el: sh, body: body, close: close };
  }

  function attachmentSource(image, cwd) {
    var url = image.url || '', path = image.path;
    if (url.indexOf('file:') === 0) {
      try { path = decodeURIComponent(new URL(url).pathname); } catch (e) { return ''; }
    } else if (/^https?:\/\//i.test(url) || /^data:image\/(png|jpe?g|gif|webp|avif|bmp);/i.test(url)) return url;
    if (!path) return '';
    if (path[0] !== '/' && path.indexOf('~/') !== 0) path = (cwd || '') + '/' + path;
    return '/api/image?path=' + encodeURIComponent(path);
  }
  function requestImages(parent, images, cwd) {
    if (!Array.isArray(images) || !images.length) return;
    var gallery = el('div', 'req-images');
    images.forEach(function (image, i) {
      var src = attachmentSource(image, cwd), label = '첨부 이미지 ' + (i + 1);
      var card = el('div', 'req-image');
      var fallback = el('div', 'image-unavailable', label + ' · 이미지를 불러올 수 없습니다');
      if (src) {
        var open = btn('', 'req-image-preview', function () {
          var sh = sheet(label, true); sh.el.classList.add('image-sheet');
          var full = el('img'); full.alt = label; full.referrerPolicy = 'no-referrer';
          full.addEventListener('error', function () { full.replaceWith(el('div', 'image-unavailable', '이미지를 불러올 수 없습니다')); });
          full.src = src; sh.body.className = 'image-view'; sh.body.appendChild(full);
          sh.el.querySelector('button').focus();
        });
        open.setAttribute('aria-label', label + ' 크게 보기');
        open.setAttribute('aria-haspopup', 'dialog');
        var img = el('img', 'request-image'); img.alt = label; img.loading = 'lazy'; img.decoding = 'async'; img.referrerPolicy = 'no-referrer';
        img.addEventListener('error', function () { open.replaceWith(fallback); });
        img.src = src; open.appendChild(img); card.appendChild(open);
      } else card.appendChild(fallback);
      gallery.appendChild(card);
    });
    parent.appendChild(gallery);
  }

  // ---------------------------------------------------------------- 모델 · effort · 속도
  // 두 에이전트(Codex · Claude Code)의 모델 — 항목마다 agent 가 있다. 목록·선택지는 /api/models 한 곳에서 읽고,
  // settings 는 모델 관리 탭의 숨김(모델 ID)과 에이전트별 기본값({model, effort, tier})이다.
  var modelsP = null, modelD = null;
  function modelData(fresh) {
    if (!modelsP || fresh) modelsP = api('GET', '/api/models' + (fresh ? '?fresh=1' : '')).then(function (r) {
      var st = r.settings || {};
      // currents: 아무것도 고르지 않았을 때 실제로 쓰일 {model, effort} — 에이전트별(모델 관리 기본값 → 에이전트 설정 → 목록의 기본)
      modelD = { models: r.models || [], settings: { hidden: st.hidden || [], defaults: st.defaults || {}, fork_efforts: st.fork_efforts || {} }, standard: r.standard_tier || 'default',
        currents: r.currents || {}, recent: r.recent || {} };
      return modelD;
    }, function (e) { modelsP = null; throw e; });
    return modelsP;
  }
  function models(agent) {
    return modelData().then(function (d) { return agent ? agentModels(d, agent) : d.models; });
  }
  function agentModels(d, agent) { return d.models.filter(function (m) { return (m.agent || 'codex') === (agent || 'codex'); }); }
  function defaultsOf(d, agent) { return (d && d.settings.defaults[agent || 'codex']) || { model: '', effort: '', tier: '' }; }
  // 포크 작업(과정 요약 · 인라인 질문)의 기본 effort — 모델 관리에서 에이전트마다 정한다. 세션 모델이 받지 않는 값이면
  // 가장 가까운 값(같은 거리면 약한 쪽)으로 맞춘다 — 서버 lens.fit_effort 와 같은 규칙.
  var EFFORT_ORDER = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'];
  function fitEffort(e, effs) {
    if (!e || !effs || !effs.length || effs.indexOf(e) >= 0 || EFFORT_ORDER.indexOf(e) < 0) return e;
    var r = EFFORT_ORDER.indexOf(e), best = null;
    effs.forEach(function (x) {
      var i = EFFORT_ORDER.indexOf(x); if (i < 0) return;
      if (best === null || Math.abs(i - r) < Math.abs(best - r) || (Math.abs(i - r) === Math.abs(best - r) && i < best)) best = i;
    });
    return best === null ? e : EFFORT_ORDER[best];
  }
  function forkEffort(d, kind, agent, modelId) {
    var e = (d && ((d.settings.fork_efforts || {})[agent || 'codex'] || {})[kind]) || 'low';
    var m = d && findModel(d, modelId);
    return fitEffort(e, m ? m.efforts : []);
  }
  // 그 에이전트의 모델들이 받는 effort 를 모두(세기 순)
  function agentEfforts(d, agent) {
    var all = {};
    agentModels(d, agent).forEach(function (m) { (m.efforts || []).forEach(function (e) { all[e] = 1; }); });
    return Object.keys(all).sort(function (a, b) { return EFFORT_ORDER.indexOf(a) - EFFORT_ORDER.indexOf(b); });
  }
  var AGENTS = [{ id: 'codex', label: 'Codex' }, { id: 'claude', label: 'Claude Code' }];
  function agentLabel(a) { return a === 'claude' ? 'Claude Code' : 'Codex'; }
  // 에이전트 아이콘(목록 행 첫머리). Claude 는 주황 방사형 별, Codex 는 그라데이션 구름 안 >_.
  var AGENT_SVG = (function () {
    var rays = '';
    for (var i = 0; i < 12; i++) {
      var a = i * Math.PI / 6, r = i % 2 ? 7.4 : 10;
      rays += 'M' + (12 + 2.6 * Math.cos(a)).toFixed(2) + ' ' + (12 + 2.6 * Math.sin(a)).toFixed(2) +
        'L' + (12 + r * Math.cos(a)).toFixed(2) + ' ' + (12 + r * Math.sin(a)).toFixed(2);
    }
    return {
      claude: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="' + rays + '" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>',
      codex: (function () {
        // Codex 앱 아이콘: 파랑·보라 그라데이션 구름 + 흰 >_
        var bumps = '';
        for (var j = 0; j < 8; j++) {
          var b = j * Math.PI / 4 + Math.PI / 8;
          bumps += '<circle cx="' + (12 + 6.9 * Math.cos(b)).toFixed(2) + '" cy="' + (12 + 6.9 * Math.sin(b)).toFixed(2) + '" r="4.6"/>';
        }
        return '<svg viewBox="0 0 24 24" aria-hidden="true"><defs><linearGradient id="cxg" gradientUnits="userSpaceOnUse" x1="4" y1="2" x2="18" y2="23">' +
          '<stop offset="0" stop-color="#c7c2ff"/><stop offset="0.45" stop-color="#6d6cf6"/><stop offset="1" stop-color="#2f3fe0"/></linearGradient></defs>' +
          '<g fill="url(#cxg)"><circle cx="12" cy="12" r="8"/>' + bumps + '</g>' +
          '<path d="M8 9.2l2.8 2.8L8 14.8M13 15h3.4" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
      })()
    };
  })();
  function agentIcon(a) {
    a = a === 'claude' ? 'claude' : 'codex';
    var i = el('span', 'agent-ico ' + a);
    i.innerHTML = AGENT_SVG[a];
    i.title = agentLabel(a); i.setAttribute('role', 'img'); i.setAttribute('aria-label', agentLabel(a));
    return i;
  }
  // 에이전트 고르기(새 세션 · 문서 다듬기의 새 세션 · 모델 관리). 모델 칩은 고른 에이전트의 모델만 보인다.
  function agentSeg(get, set) {
    var seg = el('div', 'seg agent-seg'); seg.setAttribute('role', 'radiogroup'); seg.setAttribute('aria-label', '에이전트');
    function paint() {
      seg.querySelectorAll('button').forEach(function (b) { var on = b.dataset.agent === get(); b.classList.toggle('on', on); b.setAttribute('aria-checked', String(on)); });
    }
    AGENTS.forEach(function (a) {
      var b = btn(a.label, '', function () { if (get() !== a.id) { set(a.id); paint(); } });
      b.dataset.agent = a.id; b.setAttribute('role', 'radio'); seg.appendChild(b);
    });
    paint();
    return { el: seg, paint: paint };
  }
  // 에이전트별로 따로 기억하는 {model, effort, tier} — Codex 는 예전 키를 그대로 쓴다
  function pickKey(prefix, agent) { return prefix + (agent === 'claude' ? '.claude' : ''); }
  function modelLabel(list, id) {
    var m = (list || []).filter(function (x) { return x.id === id; })[0];
    return m ? m.label : id;
  }
  function findModel(d, id) { return id ? d.models.filter(function (x) { return x.id === id; })[0] : null; }
  // 값 없음(기본)일 때 실제로 쓰일 모델 — 그 에이전트의 모델 관리 기본 모델, 없으면 에이전트 설정의 기본
  function defaultModel(d, agent) {
    return findModel(d, defaultsOf(d, agent).model) || agentModels(d, agent).filter(function (x) { return x.default; })[0] || null;
  }
  // 모델을 모를 때(기본 모델을 알 수 없음) 그 에이전트의 모든 모델이 받는 effort 만
  function commonEfforts(list) {
    return list.reduce(function (acc, x) { return acc === null ? x.efforts.slice() : acc.filter(function (e) { return x.efforts.indexOf(e) >= 0; }); }, null) || [];
  }
  // 속도 선택지 — 표준 · 모델이 내놓는 등급(Fast 등). 등급이 없는 모델(Claude 등)은 빈 배열(칸을 숨긴다)
  function tierOpts(d, m) {
    if (!m || !m.tiers || !m.tiers.length) return [];
    return [{ id: d.standard, name: '표준', description: '기본 속도' }].concat(m.tiers);
  }
  function tierName(d, m, id) {
    if (!id) return '';
    var t = tierOpts(d, m).filter(function (x) { return x.id === id; })[0];
    return t ? t.name : id;
  }
  // pick = {model, effort, tier} — 빈 값은 넘기지 않는다(속도만 "기본"이 남는다). 화면에는 "기본" 같은 말 대신 그때 실제로 쓰일 값을 보인다:
  // opts.base() 가 그 값({model, effort}, 세션이면 세션의 값)이고, 없으면 에이전트의 실제 기본(/api/models 의 currents)이다.
  // 실제 값과 같은 것을 고르면 빈 값으로 둔다 — 세션·모델 관리·설정이 바뀌면 그대로 따라간다.
  function pickChip(getPick, setPick, opts) {
    opts = opts || {};
    var agentOf = function () { return (opts.agent && opts.agent()) || 'codex'; };
    var chip = btn('', 'chip'); chip.setAttribute('aria-haspopup', 'dialog');
    function baseVal(d) { return (opts.base && opts.base()) || (d && d.currents[agentOf()]) || {}; }
    function baseOf(d) { return findModel(d, baseVal(d).model) || defaultModel(d, agentOf()); }
    // 지금 실제로 쓰일 모델·effort — 고른 값, 없으면 base. 모델이 받지 않는 effort 는 그 모델의 기본으로
    function resolved(d) {
      var b = baseVal(d), p = getPick(), model = p.model || b.model || '', effort = p.effort || b.effort || '';
      var m = d && findModel(d, model);
      if (m && effort && m.efforts.indexOf(effort) < 0) effort = m.default_effort || '';
      return { model: model, effort: effort };
    }
    function paintChip(d) {
      var p = getPick(), r = resolved(d);
      chip.innerHTML = '';
      chip.appendChild(el('span', 'main', r.model ? modelLabel(d && d.models, r.model) : '…'));
      var tn = d && p.tier && p.tier !== d.standard ? tierName(d, p.model ? findModel(d, p.model) : baseOf(d), p.tier) : '';
      // 빠른 등급(Fast)은 칩이 좁아 ⚡ 하나로 보이고, effort 를 알 수 없으면(Claude) "기본"도 생략한다 — 이름은 aria-label·시트에
      if (r.effort || !tn) chip.appendChild(el('span', 'sub', r.effort || '기본'));
      if (tn) { var fm = el('span', 'fast-mark', '⚡'); fm.setAttribute('aria-label', tn); fm.title = tn; chip.appendChild(fm); }
      chip.title = '모델 · effort · 속도 고르기';
    }
    // 최근 칩 — 보낼 때 실제로 쓰인 {model, effort, tier}(서버 recent-picks.json, 기기 공통). 누르면 바로 그 값으로 바꾼다.
    // 지금 쓰일 값과 같은 조합은 모델 칩이 이미 보이므로 빼고, 없어진 모델 · 숨긴 모델 · 받지 않는 값은 건너뛴다.
    var recentEl = el('div', 'recent-picks'); recentEl.hidden = true;
    recentEl.setAttribute('role', 'group'); recentEl.setAttribute('aria-label', '최근 쓴 모델 · effort · 속도');
    function tierUsed(d) {
      var p = getPick(), b = opts.base && opts.base();
      return p.tier || (b ? '' : defaultsOf(d, agentOf()).tier || '');
    }
    // effort 를 비워 둔(기본) 조합은 그 모델의 기본 effort 로 적는다 — 같은 조합이 "기본"과 실제 값으로 두 번 남지 않게
    function effortOf(d, model, effort) { var m = findModel(d, model); return effort || (m && m.default_effort) || ''; }
    function used(d) { var r = resolved(d); return { agent: agentOf(), model: r.model, effort: effortOf(d, r.model, r.effort), tier: tierUsed(d) }; }
    function fastOf(d, t) { return t && t !== d.standard ? t : ''; }
    function sameKey(d, a, b) { return a.model === b.model && (a.effort || '') === (b.effort || '') && fastOf(d, a.tier) === fastOf(d, b.tier); }
    function applyRecent(d, e) {
      var b = baseVal(d), same = e.model === b.model;
      setPick({ model: same ? '' : e.model, effort: same && e.effort === (b.effort || '') ? '' : e.effort, tier: e.tier || '' });
      paint(d);
    }
    function paintRecent(d) {
      recentEl.innerHTML = '';
      if (!d || opts.recent === false) { recentEl.hidden = true; return; }
      var cur = used(d), hidden = d.settings.hidden || [], seen = [];
      var items = ((d.recent || {})[agentOf()] || []).map(function (e) {
        return { model: e.model, effort: effortOf(d, e.model, e.effort), tier: e.tier || '' };
      }).filter(function (e) {
        var m = findModel(d, e.model);
        if (!m || (m.agent || 'codex') !== agentOf() || hidden.indexOf(m.id) >= 0) return false;
        if (e.effort && m.efforts.indexOf(e.effort) < 0) return false;
        if (e.tier && !tierOpts(d, m).some(function (t) { return t.id === e.tier; })) return false;
        if (sameKey(d, e, cur) || seen.some(function (x) { return sameKey(d, x, e); })) return false;
        seen.push(e); return true;
      }).slice(0, 4);
      recentEl.hidden = !items.length;
      if (!items.length) return;
      recentEl.appendChild(el('span', 'recent-k', '최근'));
      items.forEach(function (e) {
        var m = findModel(d, e.model), tn = fastOf(d, e.tier) ? tierName(d, m, e.tier) : '';
        var c = btn('', 'chip recent-chip', function () { applyRecent(d, e); });
        c.appendChild(el('span', 'main', m.label));
        if (e.effort) c.appendChild(el('span', 'sub', e.effort));
        if (tn) { var fm = el('span', 'fast-mark', '⚡'); fm.setAttribute('aria-label', tn); c.appendChild(fm); }
        c.title = '최근 쓴 조합으로 바꾸기 — ' + [m.label, e.effort, tn].filter(Boolean).join(' · ');
        recentEl.appendChild(c);
      });
    }
    function paint(d) { paintChip(d); paintRecent(d); }
    paint(null);
    modelData().then(paint, function () {});
    chip.addEventListener('click', function () {
      modelData().then(function (d) {
        var agent = agentOf(), list = agentModels(d, agent);
        var sh = sheet('모델 · effort · 속도 — ' + agentLabel(agent));
        var cur = Object.assign({ model: '', effort: '', tier: '' }, getPick(), resolved(d));
        var hidden = d.settings.hidden || [];
        var secM = el('div', 'sheet-sec', '모델'); sh.body.appendChild(secM);
        var box = el('div', 'file-list'); sh.body.appendChild(box);
        var secE = el('div', 'sheet-sec', 'effort(추론 강도)'); sh.body.appendChild(secE);
        var seg = el('div', 'seg'); sh.body.appendChild(seg);
        var note = el('div', 'iq-hint'); sh.body.appendChild(note);
        var secT = el('div', 'sheet-sec', '속도'); sh.body.appendChild(secT);
        var segT = el('div', 'seg pick-tier'); sh.body.appendChild(segT);
        var noteT = el('div', 'iq-hint'); sh.body.appendChild(noteT);
        function modelOf(mid) { return mid ? findModel(d, mid) : baseOf(d); }
        function draw() {
          box.innerHTML = '';
          // 모델 관리에서 숨긴 모델은 빼되, 지금 고른 모델은 남긴다(고른 값을 잃지 않게)
          var shown = list.filter(function (m) { return hidden.indexOf(m.id) < 0 || m.id === cur.model; });
          shown.forEach(function (m) {
            var o = btn('', 'opt' + (cur.model === m.id ? ' on' : ''));
            o.appendChild(el('span', 'radio')); o.appendChild(el('b', null, m.label));
            if (m.description) { o.appendChild(el('span')); o.appendChild(el('span', 'opt-note', m.description)); }
            o.addEventListener('click', function () {
              cur.model = m.id;
              var mm = modelOf(cur.model);
              if (!mm || mm.efforts.indexOf(cur.effort) < 0) cur.effort = mm && mm.efforts.indexOf(mm.default_effort) >= 0 ? mm.default_effort : '';
              if (cur.tier && !tierOpts(d, mm).some(function (t) { return t.id === cur.tier; })) cur.tier = '';
              draw();
            });
            box.appendChild(o);
          });
          var mm = modelOf(cur.model);
          seg.innerHTML = '';
          var effs = mm ? mm.efforts : commonEfforts(list);
          // 기본 effort 를 알 수 없는 모델(Claude)은 실제 값을 보일 수 없으니 "기본"(CLI 가 정함)을 남긴다
          (mm && !mm.default_effort && effs.length ? [''] : []).concat(effs).forEach(function (e) {
            var b = btn(e || '기본', (cur.effort || '') === e ? 'on' : '');
            b.addEventListener('click', function () { cur.effort = e; draw(); });
            seg.appendChild(b);
          });
          note.textContent = effs.length ? '' : '이 모델은 effort 선택을 받지 않습니다';
          segT.innerHTML = '';
          var tiers = tierOpts(d, mm);
          secT.hidden = segT.hidden = !tiers.length;
          (tiers.length ? [{ id: '', name: '기본' }].concat(tiers) : []).forEach(function (t) {
            var b = btn(t.name, (cur.tier || '') === t.id ? 'on' : '');
            if (t.description) b.title = t.description;
            b.addEventListener('click', function () { cur.tier = t.id; draw(); });
            segT.appendChild(b);
          });
          var ct = tiers.filter(function (t) { return t.id === cur.tier; })[0];
          noteT.textContent = !tiers.length ? '' : ct ? ct.description || '' : (opts.baseLabel ? '이 세션의 속도를 그대로 쓴다' : '모델 관리의 기본 속도를 쓴다');
          noteT.hidden = !tiers.length;
        }
        draw();
        var done = btn('적용', 'primary', function () {
          var b = baseVal(d), same = cur.model === b.model;
          setPick({ model: same ? '' : cur.model, effort: same && cur.effort === (b.effort || '') ? '' : cur.effort, tier: cur.tier || '' }); paint(d); sh.close();
        });
        done.style.marginTop = '6px'; sh.body.appendChild(done);
      }, function (e) { toast('모델 목록을 읽지 못했습니다: ' + e.message); });
    });
    return { el: chip, recentEl: recentEl, repaint: function () { modelData().then(paint, function () {}); },
      // 보내기 직전에 실제로 쓰일 조합 — 목록을 아직 못 읽었으면 null
      used: function () { return modelD ? used(modelD) : null; },
      // 보내기가 성공하면 서버와 같은 규칙(같은 조합은 하나, 최근 것 먼저)으로 화면 사본도 고친다
      noteUsed: function (u) {
        if (!modelD || !u || !u.model) return;
        var all = modelD.recent || (modelD.recent = {}), key = function (e) { return [e.model, e.effort || '', e.tier || ''].join('|'); };
        all[u.agent] = [{ model: u.model, effort: u.effort || '', tier: u.tier || '', at: Date.now() / 1000 }]
          .concat((all[u.agent] || []).filter(function (e) { return key(e) !== key(u); })).slice(0, 6);
        paint(modelD);
      } };
  }

  // ---------------------------------------------------------------- 모델 관리
  // 새 세션의 기본 모델 · effort · 속도와, 고르기 목록에서 숨길 모델. 바꾸면 바로 저장한다.
  // 기본값은 에이전트(Codex · Claude Code)마다 따로, 숨김은 모델 ID 단위. 보고 있는 에이전트는 주소(?agent=)에 둔다.
  function viewModels(params, pop) {
    setHeader('모델 관리', [], true);
    var seq = S.seq, agent = params.agent === 'claude' ? 'claude' : 'codex', last = null;
    var aseg = agentSeg(function () { return agent; }, function (a) {
      agent = a;
      history.replaceState(history.state, '', '#/models' + (a === 'claude' ? '?agent=claude' : ''));
      if (last) draw(last);
    });
    aseg.el.classList.add('models-agent');
    view.appendChild(aseg.el);
    var box = el('div', 'models-view'); view.appendChild(box);
    box.appendChild(el('div', 'empty', '모델 목록을 읽는 중…'));
    function save(patch, okMsg) {
      var t0 = performance.now();
      return api('POST', '/api/models/settings', Object.assign({ agent: agent }, patch)).then(function (r) {
        modelsP = null;
        clientLog({ type: 'model_settings', ms: Math.round(performance.now() - t0) });
        toast(okMsg || '저장했습니다');
        return load(false);
      }, function (e) { toast('저장하지 못했습니다: ' + e.message); return load(false); });
    }
    function load(fresh) {
      return modelData(fresh).then(function (d) {
        if (seq !== S.seq) return;
        last = d; draw(d);
      }, function (e) {
        if (seq !== S.seq) return;
        box.innerHTML = ''; box.appendChild(el('div', 'empty err', '모델 목록을 읽지 못했습니다: ' + e.message));
        box.appendChild(btn('다시 읽기', null, function () { load(true); }));
      });
    }
    function draw(d) {
      var st = defaultsOf(d, agent), hidden = d.settings.hidden || [], dm = defaultModel(d, agent), mine = agentModels(d, agent);
      var others = {};   // 다른 에이전트의 기본 모델도 숨길 수 없다
      AGENTS.forEach(function (a) { var x = defaultsOf(d, a.id).model; if (x) others[x] = 1; });
      box.innerHTML = '';
      // 기본값 — 새 세션(입력창 모델 칩의 "기본")에 쓰인다
      box.appendChild(el('div', 'sheet-sec', '새 세션 기본값'));
      var card = el('div', 'model-defaults');
      var effs = dm ? dm.efforts : commonEfforts(mine);
      var rowE = el('div', 'mdef-row'); rowE.appendChild(el('span', 'mdef-k', 'effort'));
      var segE = el('div', 'seg'); rowE.appendChild(segE);
      [''].concat(effs).forEach(function (e) {
        segE.appendChild(btn(e || '기본', (st.effort || '') === e ? 'on' : '', function () {
          if ((st.effort || '') !== e) save({ effort: e });
        }, e ? '' : agentLabel(agent) + ' 설정(또는 모델 기본값)을 따른다'));
      });
      card.appendChild(rowE);
      var tiers = tierOpts(d, dm);
      if (tiers.length) {
        var rowT = el('div', 'mdef-row'); rowT.appendChild(el('span', 'mdef-k', '속도'));
        var segT = el('div', 'seg'); rowT.appendChild(segT);
        [{ id: '', name: '기본', description: agentLabel(agent) + ' 설정을 따른다' }].concat(tiers).forEach(function (t) {
          var b = btn(t.name, (st.tier || '') === t.id ? 'on' : '', function () {
            if ((st.tier || '') !== t.id) save({ tier: t.id });
          }, t.description || '');
          if (t.id && t.id !== d.standard) b.classList.add('fast');
          segT.appendChild(b);
        });
        card.appendChild(rowT);
        var ct = tiers.filter(function (t) { return t.id === st.tier; })[0];
        if (ct && ct.description) card.appendChild(el('div', 'iq-hint', ct.name + ' — ' + ct.description));
      }
      box.appendChild(card);
      // 포크 작업 기본 effort — 과정 요약 · 인라인 질문을 고르지 않고 시작할 때
      box.appendChild(el('div', 'sheet-sec', '과정 요약 · 인라인 질문 기본 effort'));
      var fcard = el('div', 'fork-defaults');
      var fe = (d.settings.fork_efforts || {})[agent] || {}, offered = agentEfforts(d, agent);
      [['question', '인라인 질문'], ['summary', '과정 요약']].forEach(function (kv) {
        var row = el('div', 'mdef-row'); row.dataset.kind = kv[0]; row.appendChild(el('span', 'mdef-k', kv[1]));
        var sg = el('div', 'seg'); row.appendChild(sg);
        var curE = fe[kv[0]] || 'low';
        (offered.indexOf(curE) >= 0 ? offered : offered.concat([curE])).forEach(function (e) {
          sg.appendChild(btn(e, curE === e ? 'on' : '', function () {
            if (curE === e) return;
            var p = {}; p[kv[0]] = e;
            save({ fork_efforts: p }, kv[1] + ' 기본 effort: ' + e);
          }));
        });
        fcard.appendChild(row);
      });
      fcard.appendChild(el('div', 'iq-hint', '모델은 세션과 같다 — 세션 모델이 받지 않는 값이면 가장 가까운 값을 쓴다.'));
      box.appendChild(fcard);
      // 모델 목록 — 누르면 기본 모델, 오른쪽 단추로 숨김/보임
      box.appendChild(el('div', 'sheet-sec', '모델 — 눌러서 기본 모델로'));
      var list = el('div', 'model-list');
      var agentDefault = mine.filter(function (x) { return x.default; })[0], al = agentLabel(agent);
      [{ id: '', label: al + ' 기본', description: agentDefault ? al + ' 설정을 따른다 — 지금은 ' + agentDefault.label : al + ' 설정을 따른다' }].concat(mine).forEach(function (m) {
        var isDef = (st.model || '') === m.id, isHid = !!m.id && hidden.indexOf(m.id) >= 0, locked = isDef || !!others[m.id];
        var row = el('div', 'model-row' + (isDef ? ' on' : '') + (isHid ? ' hid' : ''));
        row.dataset.model = m.id;
        var pickB = btn('', 'opt' + (isDef ? ' on' : ''), function () {
          if (isDef) return;
          var patch = { model: m.id };
          var nm = m.id ? m : agentDefault;
          // 새 기본 모델이 받지 않는 effort·속도는 "기본"으로 돌린다
          if (st.effort && (!nm || nm.efforts.indexOf(st.effort) < 0)) patch.effort = '';
          if (st.tier && !tierOpts(d, nm).some(function (t) { return t.id === st.tier; })) patch.tier = '';
          save(patch, '기본 모델: ' + m.label);
        }, isDef ? '지금 기본 모델' : '기본 모델로 정하기');
        pickB.setAttribute('aria-pressed', String(isDef));
        pickB.appendChild(el('span', 'radio'));
        var nameLine = el('span', 'model-name'); nameLine.appendChild(el('b', null, m.label));
        if (m.tiers && m.tiers.length) nameLine.appendChild(el('span', 'badge dim', m.tiers.map(function (t) { return t.name; }).join(' · ')));
        if (isHid) nameLine.appendChild(el('span', 'badge dim', '숨김'));
        pickB.appendChild(nameLine);
        var sub = [m.description, m.efforts && m.efforts.length ? 'effort ' + m.efforts.join('/') : ''].filter(Boolean).join(' · ');
        if (sub) { pickB.appendChild(el('span')); pickB.appendChild(el('span', 'opt-note', sub)); }
        row.appendChild(pickB);
        if (m.id) {
          var vis = btn(isHid ? '보이기' : '숨기기', 'small vis', function () {
            var next = isHid ? hidden.filter(function (h) { return h !== m.id; }) : hidden.concat([m.id]);
            save({ hidden: next }, (isHid ? '다시 보입니다: ' : '숨겼습니다: ') + m.label);
          }, locked ? '기본 모델은 숨길 수 없습니다' : (isHid ? '모델 고르기 목록에 다시 보이기' : '모델 고르기 목록에서 숨기기'));
          vis.disabled = locked;
          vis.setAttribute('aria-pressed', String(isHid));
          row.appendChild(vis);
        }
        list.appendChild(row);
      });
      box.appendChild(list);
      var foot = el('div', 'models-foot');
      foot.appendChild(btn('목록 새로 읽기', 'small', function () { load(true).then(function () { toast('모델 목록을 다시 읽었습니다'); }); }));
      box.appendChild(foot);
    }
    load(false).then(function () { restoreScroll(pop); });
  }

  // ---------------------------------------------------------------- 실행 중 턴의 진행 표시(세션 · 문서 다듬기)
  // 30초 무신호면 '지연' — 단, 돌고 있는 명령의 제한 시간(quiet_until)까지는 조용한 것이 정상이다.
  // Claude Code 는 명령이 끝나야 결과를 적어서, 긴 시험·빌드 동안은 기록이 멈춰 있다.
  function isStalled(sig, quiet, now) {
    return now - Math.max(sig || 0, quiet || 0) > STALL_SEC;
  }
  function liveStage(t) {
    if (t.answer && t.answer.text) return '답하는 중';
    var last = t.process[t.process.length - 1];
    if (!last) return '응답을 기다리는 중';
    if (last.kind === 'agent' && last.status === 'inProgress' && last.activity) {
      // 하위 에이전트가 지금 돌리는 도구 — 돌고 있는 명령이면 제한 시간까지 조용한 것이 정상이다
      var a = last.activity;
      return '하위 에이전트 진행 중 · ' + (a.kind === 'command' ? '명령 실행 중 · ' : (KIND[a.kind] || a.kind) + ' · ') + a.title;
    }
    if (last.kind === 'agent' && last.status === 'inProgress') {
      // 앞 하위 에이전트가 도는 동안 본 기록은 조용하다 — 그 에이전트의 마지막 단계를 보인다
      var pg = last.progress;
      return '하위 에이전트 진행 중 · ' + (pg && pg.last ? pg.last : last.title.replace(/^하위 에이전트 · /, ''));
    }
    if (last.kind === 'command' && last.status === 'inProgress') return '명령 실행 중 · ' + last.title;
    if (last.kind === 'file_change' && last.status === 'inProgress') return '파일 고치는 중 · ' + last.title;
    if (last.kind === 'reasoning') return '생각하는 중';
    if (last.kind === 'message') return '중간 메시지 · ' + (last.detail || '').split('\n')[0].slice(0, 60);
    return (KIND[last.kind] || last.kind) + ' · ' + last.title;
  }
  function paintLiveStatus(ls) {
    var since = +ls.dataset.liveSince, sig = +ls.dataset.lastSignal, quiet = +ls.dataset.quietUntil || 0, now = Date.now() / 1000;
    var idle = now - sig, stalled = isStalled(sig, quiet, now);
    ls.classList.toggle('stalled', stalled);
    ls.innerHTML = '';
    ls.appendChild(el('span', 'dot'));
    ls.appendChild(el('span', 'stage', (stalled ? '지연 — ' : '') + ls.dataset.stage));
    // 명령이 도는 동안 조용한 것은 정상 — 언제까지 기다리는지 같은 줄에 적는다
    var wait = quiet > now && idle > STALL_SEC ? ' · 명령이 끝나야 기록됨 · 제한 시간까지 ' + dur((quiet - now) * 1000) : '';
    ls.appendChild(el('span', null, '경과 ' + dur((now - since) * 1000) + ' · 마지막 신호 ' + Math.round(idle) + '초 전' + wait));
  }
  function paintTokenSpeed(node) {
    node.hidden = Date.now() / 1000 >= +node.dataset.speedExpires;
  }

  // ---------------------------------------------------------------- 입력창(멘션 · 초안)
  // draftKey 별로 {text, mentions} 를 localStorage 에 보관하고 보내기 성공·직접 지움 때만 비운다.
  var imageDrafts = {}; // 저장 공간이 차도 화면 이동으로 초안을 잃지 않는다
  function composer(opts) {
    var wrap = el('div', 'composer'), boxEl = el('div', 'composer-box');
    wrap.style.position = 'sticky';
    var chips = el('div', 'mention-chips');
    var ta = el('textarea'); ta.rows = 2; ta.placeholder = opts.placeholder || '메시지 — @ 로 파일·세션 멘션';
    ta.setAttribute('aria-label', '메시지');
    var hint = el('div', 'composer-hint');
    var bar = el('div', 'composer-bar');
    var draft = imageDrafts[opts.draftKey] || lsGet('draft.' + opts.draftKey, null) || { text: '', mentions: [] };
    ta.value = draft.text || '';
    var mentions = draft.mentions || [];
    var images = draft.images || [], readingImages = 0;
    var previews = el('div', 'image-previews');
    var files = el('input'); files.type = 'file'; files.accept = 'image/png,image/jpeg,image/webp,image/gif'; files.multiple = true; files.hidden = true;
    var attach = btn('＋', 'small', function () { files.click(); }, '이미지 첨부');
    attach.setAttribute('aria-label', '이미지 첨부');
    files.addEventListener('change', function () { addImages(Array.from(files.files)); files.value = ''; });
    ta.addEventListener('paste', function (e) {
      var fs = Array.from(e.clipboardData && e.clipboardData.items || []).filter(function (it) { return it.kind === 'file' && /^image\//.test(it.type); }).map(function (it) { return it.getAsFile(); }).filter(Boolean);
      if (!fs.length) return;
      e.preventDefault();
      var text = e.clipboardData.getData('text/plain');
      if (text) { ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, 'end'); onInput(); }
      addImages(fs);
    });
    function addImages(fs) {
      if (readingImages) { toast('이미지를 읽고 있습니다. 잠시 뒤 다시 붙여넣어 주세요'); return; }
      // 한 번에 하나씩 읽어 붙여넣기 순서와 용량 제한을 지킨다.
      readingImages++; paint(); hint.textContent = '이미지 읽는 중…';
      fs.reduce(function (chain, f) { return chain.then(function () {
        if (!/^image\/(png|jpeg|webp|gif)$/.test(f.type)) throw new Error('PNG·JPEG·WebP·GIF 이미지만 첨부할 수 있습니다');
        if (images.length >= 8 || images.reduce(function (n, im) { return n + im.size; }, 0) + f.size > 2 * 1024 * 1024) throw new Error('이미지는 최대 8장, 합계 2MB까지 첨부할 수 있습니다');
        return new Promise(function (resolve, reject) {
          var reader = new FileReader(); reader.onerror = function () { reject(new Error('이미지를 읽지 못했습니다. 다시 붙여넣어 주세요')); };
          reader.onload = function () { images.push({ id: uuid4(), url: reader.result, size: f.size, name: f.name || '붙여넣은 이미지' }); save(); resolve(); }; reader.readAsDataURL(f);
        });
      }); }, Promise.resolve()).then(function () { hint.textContent = ''; }, function (e) { hint.textContent = e.message; }).then(function () { readingImages--; paint(); });
    }
    function paintImages() {
      previews.innerHTML = ''; previews.hidden = !images.length;
      images.forEach(function (im) {
        var cell = el('div', 'image-preview'), img = el('img'); img.src = im.url; img.alt = im.name;
        cell.appendChild(img); cell.appendChild(btn('×', 'small', function () { images = images.filter(function (x) { return x.id !== im.id; }); save(); }, '이미지 삭제: ' + im.name)); previews.appendChild(cell);
      });
    }
    var pick = opts.pick;
    var chip = pickChip(function () { return pick.get(); }, function (p) { pick.set(p); }, Object.assign({ agent: opts.agent }, opts.pickOpts));
    var at = btn('@', 'small at-btn', function () { insertAt('@'); }, '파일·세션 멘션');
    var send = btn(opts.sendLabel || '보내기', 'primary');
    var rfBtn = btn('', 'small rf-btn', function () { rfToggle(); }, '다듬기 판 열기 — 입력한 글을 에이전트 요청이나 좋은 글로 다듬는다');
    rfBtn.setAttribute('aria-label', '다듬기');
    rfBtn.appendChild(el('span', 'rf-ico', '✨')); rfBtn.appendChild(el('span', 'rf-label', '다듬기'));
    rfBtn.hidden = !opts.refine;
    var rfPanel = el('div', 'rf-panel'); rfPanel.hidden = true;
    var stop = btn('멈춤', 'stop', function () { if (opts.onStop) opts.onStop(); }, '진행 중인 턴을 멈춘다 — 기다리는 메시지는 남는다');
    bar.appendChild(chip.el); bar.appendChild(at); bar.appendChild(attach); bar.appendChild(files); bar.appendChild(rfBtn); bar.appendChild(el('span', 'grow')); bar.appendChild(stop); bar.appendChild(send);
    // 응답 중에 보내는 메시지: 바로 넣기(진행 중인 턴에 끼워 넣는다 — turn/steer) 또는 턴 끝나고(대기열). 고른 값은 기기에 남는다.
    var steerRow = el('div', 'steer-row'), steerLead = el('span', null, '응답 중에 보내면'); steerRow.appendChild(steerLead);
    var modeSeg = el('span', 'mode-seg'); steerRow.appendChild(modeSeg);
    var modeBtns = {
      steer: btn('바로 넣기', null, function () { setMode('steer'); }, '진행 중인 턴에 끼워 넣는다 — 에이전트가 다음 도구 결과를 받을 때 읽는다'),
      queue: btn('턴 끝나고', null, function () { setMode('queue'); }, '지금 턴이 끝나면 다음 턴으로 보낸다')
    };
    modeSeg.appendChild(modeBtns.steer); modeSeg.appendChild(modeBtns.queue);
    var sendMode = lsGet('sendMode', 'steer') === 'queue' ? 'queue' : 'steer';
    function setMode(m) { sendMode = m; lsSet('sendMode', m); paint(); }
    var queueEl = el('div', 'queue'); queueEl.hidden = true;
    // 다듬기 판은 화면에서 입력창 위에 보이지만(CSS order) DOM 에서는 입력창 뒤에 둔다 — '.composer textarea' 가 늘 입력창을 가리키게
    boxEl.appendChild(chips); boxEl.appendChild(previews); boxEl.appendChild(ta); boxEl.appendChild(rfPanel); boxEl.appendChild(hint); boxEl.appendChild(steerRow); boxEl.appendChild(chip.recentEl); boxEl.appendChild(bar);
    wrap.appendChild(queueEl); wrap.appendChild(boxEl);
    var running = false, sending = false, pop = null, popItems = [], popIdx = 0, popQuery = null, popSeq = 0;
    function save() {
      var keep = mentions.filter(function (m) { return ta.value.indexOf(m.label) >= 0; });
      mentions = keep;
      var value = !ta.value.trim() && !keep.length && !images.length ? null : { text: ta.value, mentions: keep, images: images.slice(), at: Date.now() };
      imageDrafts[opts.draftKey] = value;
      lsSet('draft.' + opts.draftKey, value);
      if (value && images.length && JSON.stringify((lsGet('draft.' + opts.draftKey, {}) || {}).images) !== JSON.stringify(images)) toast('이미지 초안 저장 공간이 부족합니다. 새로고침 전에 전송해 주세요');
      paintChips(); paintImages();
    }
    function paintChips() {
      chips.innerHTML = '';
      mentions.forEach(function (m) {
        var c = el('span', 'mchip', (m.kind === 'session' ? '세션 · ' : '파일 · ') + (m.kind === 'session' ? m.title : m.rel));
        c.title = m.kind === 'session' ? m.id : m.path; chips.appendChild(c);
      });
    }
    paintChips(); paintImages();
    function grow() { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight + 2, window.innerHeight * 0.4) + 'px'; }
    function insertAt(s) {
      var p = ta.selectionStart || ta.value.length;
      var pre = ta.value.slice(0, p), needSpace = pre && !/\s$/.test(pre);
      ta.value = pre + (needSpace ? ' ' : '') + s + ta.value.slice(p);
      var np = p + (needSpace ? 1 : 0) + s.length; ta.focus(); ta.setSelectionRange(np, np);
      onInput();
    }
    // 응답 중에도 보낼 수 있으면(opts.steer) 보내기와 멈춤을 함께 두고 보내는 방식을 고른다. 아니면 보내기 자리가 멈춤이 된다.
    function paint() {
      var both = running && !!opts.steer;
      steerRow.hidden = !both;
      Object.keys(modeBtns).forEach(function (k) { modeBtns[k].setAttribute('aria-pressed', String(sendMode === k)); });
      send.textContent = sending ? '보내는 중…' : (running && !both) ? '멈춤' : both ? (sendMode === 'queue' ? '대기열에' : '넣기') : (opts.sendLabel || '보내기');
      send.classList.toggle('primary', !running || both);
      send.disabled = sending || !!readingImages;
      stop.hidden = !both || !canStop;
    }
    var canStop = true;
    send.addEventListener('click', function () {
      if (running && !opts.steer) { if (opts.onStop) opts.onStop(); return; }
      doSend();
    });
    // 앱이 다시 뜨는 동안(운영 반영 재시작 등) 보내기가 실패하면, 글을 고치지 않는 한 돌아오는 대로 같은 client_id 로 다시 보낸다.
    var retry = null;
    function stopRetry() { if (retry) { clearTimeout(retry.timer); retry = null; } }
    function waitAndResend(e, sentText) {
      if (!retry) retry = { since: Date.now(), text: sentText, error: e.message };
      var sec = Math.round((Date.now() - retry.since) / 1000);
      if (ta.value !== retry.text || !wrap.isConnected || sec >= RESEND_MAX_SEC) {
        hint.textContent = '보내지 못했습니다: ' + retry.error + ' — 글은 그대로 있습니다'; stopRetry(); return;
      }
      hint.textContent = '서버에 닿지 않습니다(다시 뜨는 중일 수 있음) — 돌아오면 자동으로 다시 보냅니다 · ' + sec + '초' + (sec >= 30 ? ' · 지연' : '');
      retry.timer = setTimeout(function () {
        fetch('/api/info', { cache: 'no-store' }).then(function (r) { return r.ok; }, function () { return false; }).then(function (ok) {
          if (!retry) return;
          if (ok && ta.value === retry.text && !sending) { stopRetry(); doSend(); } else waitAndResend(e, sentText);
        });
      }, 2000);
    }
    ta.addEventListener('input', function () { if (retry && ta.value !== retry.text) { hint.textContent = '보내지 못했습니다: ' + retry.error + ' — 글은 그대로 있습니다'; stopRetry(); } });
    function doSend() {
      var text = ta.value.trim();
      if ((!text && !images.length) || sending || readingImages) { ta.focus(); return; }
      stopRetry();
      sending = true; paint(); hint.textContent = '보내는 중…';
      var ms = mentions.filter(function (m) { return ta.value.indexOf(m.label) >= 0; });
      var p = pick.get();
      // 다듬은 글이면 다듬기 전 글에 있던 코멘트도 보낸 것으로 센다(다듬으며 문장이 바뀌어도 내용은 갔다)
      var cmText = ta.value + (rf && rf.applied === true && rf.src ? '\n' + rf.src : '');
      var body = { text: text, images: images.map(function (im) { return { url: im.url }; }), mentions: ms, model: p.model || null, effort: p.effort || null, tier: p.tier || null,
        client_id: (draft.client_id && draft.text === ta.value && draft.imageKey === images.map(function (im) { return im.id; }).join(',')) ? draft.client_id : uuid4(),
        comment_ids: opts.commentIds ? opts.commentIds(cmText) : [] };
      if (running && opts.steer) body.mode = sendMode;
      var usedPick = chip.used();
      if (usedPick && usedPick.model) body.recent = usedPick;
      draft.client_id = body.client_id; draft.text = ta.value; draft.imageKey = images.map(function (im) { return im.id; }).join(',');
      var sentText = ta.value, sentImages = images.slice(), draftBeforeSend = imageDrafts[opts.draftKey] || null;
      opts.onSend(body).then(function () {
        sending = false; hint.textContent = '';
        chip.noteUsed(usedPick);
        images = images.filter(function (im) { return sentImages.indexOf(im) < 0; });
        if (ta.value === sentText) { ta.value = ''; mentions = []; draft = { text: '', mentions: [] }; grow(); rfClear(); }
        if (wrap.isConnected || JSON.stringify(imageDrafts[opts.draftKey] || null) === JSON.stringify(draftBeforeSend)) save();
        paint();
      }, function (e) {
        sending = false; paint();
        if (e.unreachable && ta.value === sentText) { waitAndResend(e, sentText); return; }
        hint.textContent = '보내지 못했습니다: ' + e.message + ' — 글은 그대로 있습니다';
      });
    }
    // 멘션 창
    function closePop() { if (pop) { pop.remove(); pop = null; } popQuery = null; }
    // 짧은 대화는 입력창이 위쪽에 있다. 헤더와 보이는 영역을 기준으로 여유 있는 쪽에 연다.
    function positionPop() {
      if (!pop || !boxEl.isConnected) return;
      var vv = window.visualViewport, top = vv ? vv.offsetTop : 0, height = vv ? vv.height : window.innerHeight;
      var bottom = top + height, rect = boxEl.getBoundingClientRect(), head = document.getElementById('top').getBoundingClientRect();
      top = Math.max(top, head.bottom) + 4; bottom -= 8;
      var above = Math.max(0, rect.top - 4 - top), below = Math.max(0, bottom - rect.bottom - 4);
      var desired = Math.min(height * 0.46, pop.scrollHeight + 2);
      var down = above < desired && below > above;
      pop.style.top = down ? 'calc(100% + 4px)' : 'auto';
      pop.style.bottom = down ? 'auto' : 'calc(100% + 4px)';
      pop.style.maxHeight = Math.min(height * 0.46, down ? below : above) + 'px';
    }
    var popFrame = null;
    function schedulePopPosition() {
      if (!pop || popFrame !== null) return;
      popFrame = requestAnimationFrame(function () { popFrame = null; positionPop(); });
    }
    window.addEventListener('resize', schedulePopPosition);
    window.addEventListener('scroll', schedulePopPosition, true);
    if (window.visualViewport) {
      window.visualViewport.addEventListener('resize', schedulePopPosition);
      window.visualViewport.addEventListener('scroll', schedulePopPosition);
    }
    var popObserver = window.ResizeObserver ? new ResizeObserver(schedulePopPosition) : null;
    if (popObserver) popObserver.observe(boxEl);
    S.cleanup.push(function () {
      closePop(); clearTimeout(popTimer); cancelAnimationFrame(popFrame);
      window.removeEventListener('resize', schedulePopPosition);
      window.removeEventListener('scroll', schedulePopPosition, true);
      if (window.visualViewport) {
        window.visualViewport.removeEventListener('resize', schedulePopPosition);
        window.visualViewport.removeEventListener('scroll', schedulePopPosition);
      }
      if (popObserver) popObserver.disconnect();
    });
    function mentionQuery() {
      var p = ta.selectionStart, pre = ta.value.slice(0, p);
      var m = /(^|\s)@([^\s@]*)$/.exec(pre);
      return m ? { q: m[2], from: p - m[2].length - 1, to: p } : null;
    }
    function openPop(mq) {
      popQuery = mq;
      var seq = ++popSeq;
      var cwd = opts.cwd();
      var pf = cwd ? api('GET', '/api/files?cwd=' + encodeURIComponent(cwd) + '&q=' + encodeURIComponent(mq.q) + '&limit=8') : Promise.resolve({ files: [] });
      var ps = api('GET', '/api/sessions?limit=6' + (mq.q ? '&q=' + encodeURIComponent(mq.q) : ''));
      Promise.all([pf.catch(function () { return { files: [] }; }), ps.catch(function () { return { sessions: [] }; })]).then(function (rs) {
        if (seq !== popSeq || !popQuery) return;
        popItems = [];
        (rs[0].files || []).forEach(function (f) { popItems.push({ kind: 'file', path: f.path, rel: f.rel }); });
        (rs[1].sessions || []).filter(function (s) { return s.id !== opts.selfId; }).slice(0, 6).forEach(function (s) {
          popItems.push({ kind: 'session', id: s.id, title: s.title, cwd: s.cwd, updated_at: s.updated_at });
        });
        popIdx = 0; drawPop();
      });
    }
    function drawPop() {
      if (!pop) { pop = el('div', 'mention-pop'); pop.setAttribute('role', 'listbox'); boxEl.style.position = 'relative'; boxEl.appendChild(pop); }
      pop.innerHTML = '';
      if (!popItems.length) { pop.appendChild(el('div', 'mention-sec', cwdHint())); positionPop(); return; }
      var lastKind = null;
      popItems.forEach(function (it, i) {
        if (it.kind !== lastKind) { pop.appendChild(el('div', 'mention-sec', it.kind === 'file' ? '파일 · ' + base(opts.cwd()) : '세션')); lastKind = it.kind; }
        var b = btn('', 'mention-item' + (i === popIdx ? ' on' : ''));
        b.setAttribute('role', 'option');
        b.appendChild(el('span', 'mi-main', it.kind === 'file' ? base(it.rel) : it.title));
        b.appendChild(el('span', 'mi-sub', it.kind === 'file' ? it.rel : base(it.cwd) + ' · ' + ago(it.updated_at)));
        b.addEventListener('pointerdown', function (e) { e.preventDefault(); });
        b.addEventListener('click', function () { choose(i); });
        pop.appendChild(b);
      });
      positionPop();
      // 선택 항목만 스크롤한다. scrollIntoView는 대화와 페이지까지 움직인다.
      var on = pop.querySelector('.on');
      if (on) {
        var pr = pop.getBoundingClientRect(), r = on.getBoundingClientRect();
        if (popIdx === 0) pop.scrollTop = 0;
        else if (r.top < pr.top + 4) pop.scrollTop -= pr.top + 4 - r.top;
        else if (r.bottom > pr.bottom - 4) pop.scrollTop += r.bottom - pr.bottom + 4;
      }
    }
    function cwdHint() { return opts.cwd() ? '맞는 파일·세션이 없습니다' : '작업 폴더를 정하면 파일도 고를 수 있습니다'; }
    function choose(i) {
      var it = popItems[i], mq = popQuery;
      if (!it || !mq) return;
      var label = it.kind === 'file' ? '@' + it.rel : '@세션:' + it.title.replace(/\s+/g, '_').slice(0, 40);
      ta.value = ta.value.slice(0, mq.from) + label + ' ' + ta.value.slice(mq.to);
      var np = mq.from + label.length + 1; ta.setSelectionRange(np, np); ta.focus();
      it.label = label;
      mentions = mentions.filter(function (m) { return m.label !== label; }).concat([it]);
      closePop(); save(); grow();
    }
    var popTimer = null;
    function onInput() {
      grow(); save();
      var mq = mentionQuery();
      clearTimeout(popTimer);
      if (!mq) { closePop(); return; }
      popTimer = setTimeout(function () { openPop(mq); }, 120);
    }
    ta.addEventListener('input', onInput);
    ta.addEventListener('blur', function () { setTimeout(closePop, 150); });
    ta.addEventListener('keydown', function (e) {
      if (e.isComposing || e.keyCode === 229) return;
      if (pop && popItems.length) {
        if (e.key === 'ArrowDown') { e.preventDefault(); popIdx = (popIdx + 1) % popItems.length; drawPop(); return; }
        if (e.key === 'ArrowUp') { e.preventDefault(); popIdx = (popIdx + popItems.length - 1) % popItems.length; drawPop(); return; }
        if (e.key === 'Enter' || (e.key === 'Tab' && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey)) { e.preventDefault(); choose(popIdx); return; }
      }
      if (e.key === 'Escape' && pop) { e.preventDefault(); closePop(); return; }
      if (e.key === 'Tab' && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault(); ta.setRangeText('\t', ta.selectionStart, ta.selectionEnd, 'end'); onInput(); return;
      }
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); doSend(); }
    });
    // 다듬기(refine.py) — ✨ 단추는 입력창 위에 판을 연다: [✨ 다듬기 | 버전 N] 탭,
    // 목적(에이전트 요청 · 좋은 글) · 다듬기 모델 · effort · 시작 단추, 다듬기 프롬프트 확인·수정, 함께 보내는 내용(서버가 실제와 같은
    // 조립으로 짓는다). 결과 글은 입력창에 바로 들어가고 의견 · 답 · 추가 질문 · 편집 메모는 판에 따로 보인다. 다듬기 직전 글과
    // 결과는 버전으로 남아 미리 보고 되돌린다(보내면 비운다). 상태 {open, tab, job, base(보낸 글), src(다듬기 전 글), applied,
    // answers, cmp, ver} 와 버전은 draftKey 별로 localStorage 에 두고, 작업은 서버가 쥐어 화면을 떠났다 와도 GET /api/jobs/<id> 로 다시 붙잡는다.
    var rfKey = 'refine.' + opts.draftKey, verKey = 'refineVers.' + opts.draftKey;
    var rf = opts.refine ? lsGet(rfKey, null) : null, rfJob = null, rfTimer = null, rfMsg = '', rfProg = null;
    if (rf && rf.open == null) rf.open = !!rf.job;   // 예전 판의 상태
    var rfVers = (opts.refine && lsGet(verKey, null)) || [];
    var rfMode = lsGet('rfMode', 'request') === 'writing' ? 'writing' : 'request';
    var rfDet = { prompt: false, full: false }, rfPromptsP = null, rfPrompts = {};
    var RF_MODES = [['request', '에이전트 요청'], ['writing', '좋은 글']];
    var RF_VER_KIND = { 'before-refine': '다듬기 전', refine: '다듬음', 'before-restore': '되돌리기 전' };
    S.cleanup.push(function () { clearTimeout(rfTimer); });
    function rfSave() { lsSet(rfKey, rf); }
    function rfRunning() { return !!(rfJob && rfJob.status === 'running'); }
    function rfCtx() { return opts.refine(); }
    function rfAgent() { return rfCtx().agent || 'codex'; }
    // 다듬기 모델 — 기준은 세션이면 세션 모델(포크가 원본 캐시를 읽는다), 새 세션이면 입력창 모델 칩이 실제로 쓸 모델이고
    // effort 기준은 low 다. 다른 모델을 고르면 다듬기에만 쓴다(에이전트별로 기기에 남는다).
    function rfPickKey() { return pickKey('pick.refine.input', rfAgent()); }
    function rfPick() { return lsGet(rfPickKey(), null) || { model: '', effort: '', tier: '' }; }
    function rfBase() {
      var c = rfCtx(), u = chip.used(), model = c.model || (u && u.model) || '';
      var m = modelD && findModel(modelD, model);
      return { model: model, effort: fitEffort('low', m ? m.efforts : []) };
    }
    var rfChip = pickChip(rfPick, function (p) { lsSet(rfPickKey(), p); rfPaint(); },
      { agent: rfAgent, base: rfBase, recent: false, baseLabel: '세션 모델' });
    rfChip.el.classList.add('rf-chip');
    function rfUsed() {
      var u = rfChip.used(), p = rfPick(), b = rfBase();
      return { model: (u && u.model) || b.model, effort: (u && u.effort) || b.effort || null, tier: p.tier || null,
        refine_model: p.model && p.model !== b.model ? p.model : null };
    }
    function rfLoadPrompts() {
      if (!rfPromptsP) rfPromptsP = api('GET', '/api/refine/prompts').then(function (r) { rfPrompts = r.prompts; return rfPrompts; },
        function (e) { rfPromptsP = null; throw e; });
      return rfPromptsP;
    }
    function rfPromptDraft(mode) { return lsGet('rfPromptDraft.' + mode, null); }
    // 버전 — 다듬기 직전 글·다듬은 글·되돌리기 직전 글. 이미 버전에 있는 글은 다시 남기지 않는다.
    function rfVersion(kind, text, meta) {
      var last = rfVers[rfVers.length - 1];
      if (!text || rfVers.some(function (v) { return v.text === text; })) return;
      rfVers.push(Object.assign({ n: last ? last.n + 1 : 1, kind: kind, text: text, at: Date.now() / 1000 }, meta || {}));
      if (rfVers.length > 30) rfVers = rfVers.slice(-30);
      lsSet(verKey, rfVers);
    }
    function rfSetText(text) { ta.value = text; grow(); save(); }
    function rfRestore(v, label) {
      if (rfRunning()) { toast('다듬는 동안에는 되돌릴 수 없습니다'); return; }
      rfVersion('before-restore', ta.value);
      rfSetText(v.text);
      if (rf) { rf.ver = null; rfSave(); }
      rfPaint();
      toast(label || 'v' + v.n + ' 글로 되돌렸습니다');
    }
    // 보내면 비운다(판 · 결과 · 버전). ✕ 는 판만 닫는다 — 결과와 버전은 다시 열면 그대로 있다.
    function rfClear() {
      if (rfRunning() && rfJob.id) api('POST', '/api/jobs/' + rfJob.id + '/cancel', {}).catch(function () {});
      rf = null; rfJob = null; rfVers = []; rfMsg = ''; clearTimeout(rfTimer); rfSave(); lsSet(verKey, null); rfPaint();
    }
    function rfClose() {
      if (rfRunning()) rfCancel();
      if (rf) { rf.open = false; rfSave(); }
      rfPaint();
    }
    function rfToggle() {
      if (!opts.refine) return;
      if (rf && rf.open) { rfClose(); return; }
      rf = rf || { job: null, answers: {} };
      rf.open = true; rf.tab = 'refine'; rfMsg = ''; rfSave(); rfPaint();
      var go = rfPanel.querySelector('.rf-run');
      if (go) go.focus({ preventScroll: true });
    }
    function rfCancel() {
      var j = rfJob;
      if (!j || !j.id) return;
      api('POST', '/api/jobs/' + j.id + '/cancel', {}).then(function (r) { if (rf && rf.job === j.id) { rfJob = r.job; rfPaint(); } }, function (e) { toast(e.message); });
    }
    function startRefine(answers) {
      if (!opts.refine || rfRunning()) return;
      var text = ta.value.trim();
      if (!text) { rfMsg = '다듬을 글을 먼저 쓰세요'; rfPaint(); ta.focus(); return; }
      rfMsg = '';
      var ctx = rfCtx(), use = rfUsed(), id = uuid4().replace(/-/g, '').slice(0, 16), pd = rfPromptDraft(rfMode);
      var body = Object.assign({ text: text, draft_key: opts.draftKey, mode: rfMode, effort: use.effort, tier: use.tier,
        refine_model: use.refine_model, answers: rfMode === 'request' ? answers || [] : [], client_id: id }, ctx.body);
      if (pd != null && pd.trim()) {
        body.prompt = pd;
        // 고친 프롬프트는 다듬기를 시작할 때 저장한다(이 작업은 위 글을 그대로 쓴다)
        api('POST', '/api/refine/prompts', { mode: rfMode, text: pd }).then(function (r) {
          rfPrompts[r.prompt.mode] = r.prompt;
          if (rfPromptDraft(r.prompt.mode) === pd) lsSet('rfPromptDraft.' + r.prompt.mode, null);
          rfPaint();
        }, function (e) { toast('프롬프트를 저장하지 못했습니다: ' + e.message); });
      }
      var prev = rf || {};
      rf = { open: true, tab: 'refine', job: id, base: ta.value, src: prev.applied === true && prev.src != null ? prev.src : ta.value, answers: {} };
      var now = Date.now() / 1000;
      rfJob = { id: id, status: 'running', stage: 'starting', steps: [], started_at: now, last_signal_at: now, model: use.model, effort: use.effort, mode: rfMode };
      rfSave(); rfPaint();
      api('POST', '/api/refine', body).then(function (r) {
        if (rf && rf.job === id) { rfJob = r.job; rfPaint(); rfPoll(); }
      }, function (e) {
        if (rf && rf.job === id) { rfJob = Object.assign({}, rfJob, { status: 'failed', stage: 'failed', error: e.message }); rfPaint(); }
      });
    }
    function rfPoll() {
      clearTimeout(rfTimer);
      if (!rf || !rfRunning()) return;
      var id = rf.job;
      rfTimer = setTimeout(function () {
        api('GET', '/api/jobs/' + id).then(function (r) {
          if (!rf || rf.job !== id) return;
          var was = rfJob && rfJob.status;
          rfJob = r.job; rfLanded();
          if (was !== rfJob.status) rfPaint(); else rfPaintProgress();
          rfPoll();
        }, function (e) {
          if (!rf || rf.job !== id) return;
          if (e.status === 404) { rfJob = { id: id, status: 'failed', stage: 'failed', error: '작업을 찾지 못했습니다 — 다시 다듬어 주세요' }; rfPaint(); }
          else rfPoll();
        });
      }, 700);
    }
    // 끝난 결과를 입력창에 넣는다 — 다듬는 동안 글을 고쳤으면 넣지 않고 '바꾸기'로 고르게 한다
    function rfApply(res) {
      rfVersion('before-refine', ta.value);
      rfSetText(res.request);
      rfVersion('refine', res.request, { model: rfJob.model || '', effort: rfJob.effort || '', mode: rfJob.mode || 'request' });
    }
    function rfLanded() {
      if (!rf || !rfJob || rfJob.status !== 'done' || rf.applied != null) return;
      if (ta.value === rf.base) { rfApply(rfJob.result); rf.applied = true; }
      else rf.applied = false;
      clientLog({ type: 'refine_done', ms: rfJob.duration_ms, applied: rf.applied, mode: rfJob.mode || 'request' });
      rfSave();
    }
    function rfAnswers() {
      var out = [], qs = (rfJob && rfJob.result && rfJob.result.missing_context) || [];
      qs.forEach(function (q, i) {
        var a = ((rf && rf.answers) || {})[i] || {}, parts = (a.sel || []).slice();
        if ((a.text || '').trim()) parts.push(a.text.trim());
        if (parts.length) out.push({ question: q.question, answer: parts.join(', ') });
      });
      return out;
    }
    function rfModelText(model, effort) {
      return [model ? modelLabel(modelD && modelD.models, model) : '', effort].filter(Boolean).join(' · ');
    }
    // 진행 줄만 다시 그린다(1초마다) — 판 전체를 다시 그리면 프롬프트 · 답 입력이 끊긴다
    function rfPaintProgress() {
      if (!rfProg || !rfRunning()) return;
      var j = rfJob, now = Date.now() / 1000, idle = now - (j.last_signal_at || now), stalled = idle > STALL_SEC;
      rfProg.stage.className = 'iq-stage' + (stalled ? ' stalled' : '');
      rfProg.stage.textContent = (stalled ? '지연 — ' : '') + (STAGE[j.stage] || j.stage || '확인하는 중') + (j.stage_detail ? ' · ' + j.stage_detail : '');
      rfProg.meta.className = 'iq-progmeta' + (stalled ? ' stalled' : '');
      rfProg.meta.textContent = [rfModelText(j.model, j.effort), '경과 ' + Math.round(now - (j.started_at || now)) + '초', '마지막 신호 ' + Math.round(idle) + '초 전'].filter(Boolean).join(' · ');
    }
    function rfPaint() {
      var open = !!(rf && rf.open);
      rfBtn.classList.toggle('on', open);
      rfBtn.setAttribute('aria-expanded', String(open));
      boxEl.classList.toggle('rf-open', open);
      var keep = rfPanel.querySelector('.rf-body'), top = keep ? keep.scrollTop : 0;
      rfPanel.hidden = !open;
      rfPanel.innerHTML = ''; rfProg = null;
      if (!open) return;
      var running = rfRunning(), j = rfJob;
      var head = el('div', 'rf-head'), tabs = el('div', 'rf-tabs');
      tabs.setAttribute('role', 'tablist');
      [['refine', '✨ 다듬기'], ['versions', '버전' + (rfVers.length ? ' ' + rfVers.length : '')]].forEach(function (t) {
        var b = btn(t[1], 'rf-tab' + ((rf.tab || 'refine') === t[0] ? ' on' : ''), function () { rf.tab = t[0]; rfSave(); rfPaint(); });
        b.setAttribute('role', 'tab'); b.setAttribute('aria-selected', String((rf.tab || 'refine') === t[0]));
        tabs.appendChild(b);
      });
      head.appendChild(tabs); head.appendChild(el('span', 'grow'));
      head.appendChild(btn('✕', 'iq-icon', rfClose, running ? '다듬기 멈추고 닫기' : '닫기 — 입력창 글과 버전은 그대로'));
      rfPanel.appendChild(head);
      var body = el('div', 'rf-body'); rfPanel.appendChild(body);
      if (rf.tab === 'versions') { rfPaintVersions(body); body.scrollTop = top; return; }
      // 목적 · 모델 · 시작
      var ctl = el('div', 'rf-controls');
      var modeSel = el('select', 'rf-mode');
      modeSel.setAttribute('aria-label', '다듬기 목적');
      RF_MODES.forEach(function (o) { var op = el('option', null, o[1]); op.value = o[0]; modeSel.appendChild(op); });
      modeSel.value = running && j.mode ? j.mode : rfMode;
      modeSel.disabled = running;
      modeSel.addEventListener('change', function () { rfMode = modeSel.value; lsSet('rfMode', rfMode); rfPaint(); });
      ctl.appendChild(modeSel);
      rfChip.el.disabled = running;
      ctl.appendChild(rfChip.el); rfChip.repaint();
      ctl.appendChild(el('span', 'grow'));
      var again = j && j.status !== 'running';
      ctl.appendChild(running ? btn('다듬기 취소', 'small danger rf-run', rfCancel)
        : btn(again ? '다시 다듬기' : '다듬기 시작', 'small primary rf-run', function () { startRefine(j && j.status !== 'done' ? j.context_answers : []); },
          again ? '지금 입력창 글을 다시 다듬는다' : '입력창 글을 다듬는다'));
      body.appendChild(ctl);
      var c = rfCtx(), p = rfPick();
      if (c.forked && p.model && p.model !== rfBase().model) body.appendChild(el('div', 'iq-hint', '세션과 다른 모델은 세션 캐시를 읽지 못해 더 느리고 비쌉니다'));
      // 다듬기 프롬프트 확인 · 수정 — 고친 글은 이 기기에 초안으로 두고 다듬기를 시작할 때 서버에 저장한다
      var dp = el('details', 'rf-det'); dp.open = rfDet.prompt;
      dp.appendChild(el('summary', null, '다듬기 프롬프트 확인·수정'));
      var pta = el('textarea', 'rf-prompt'); pta.rows = 6; pta.spellcheck = false; pta.disabled = running;
      pta.setAttribute('aria-label', '다듬기 프롬프트');
      var prow = el('div', 'rf-status'), pst = el('span', 'grow iq-hint');
      var preset = btn('기본 프롬프트로', 'small', function () {
        lsSet('rfPromptDraft.' + rfMode, null);
        api('POST', '/api/refine/prompts', { mode: rfMode, text: null }).then(function (r) {
          rfPrompts[r.prompt.mode] = r.prompt; pta.value = r.prompt.text; fillPrompt(); toast('기본 프롬프트로 돌렸습니다');
        }, function (e) { toast('되돌리지 못했습니다: ' + e.message); });
      });
      prow.appendChild(pst); prow.appendChild(preset);
      dp.appendChild(pta); dp.appendChild(prow);
      function fillPrompt() {
        var sp = rfPrompts[rfMode];
        if (!sp) { pst.textContent = '읽는 중…'; preset.disabled = true; return; }
        var d = rfPromptDraft(rfMode);
        if (document.activeElement !== pta) pta.value = d != null ? d : sp.text;
        var dirty = d != null && d !== sp.text;
        pst.textContent = dirty ? '고친 프롬프트 · 다듬기를 시작하면 저장됩니다' : sp.custom ? '고친 프롬프트' : '기본 프롬프트';
        preset.disabled = running || (!sp.custom && !dirty);
      }
      pta.addEventListener('input', function () {
        var sp = rfPrompts[rfMode];
        lsSet('rfPromptDraft.' + rfMode, sp && pta.value === sp.text ? null : pta.value);
        fillPrompt();
      });
      function loadPrompt() { fillPrompt(); rfLoadPrompts().then(fillPrompt, function (e) { pst.textContent = '읽지 못했습니다: ' + e.message; }); }
      dp.addEventListener('toggle', function () { rfDet.prompt = dp.open; if (dp.open) loadPrompt(); });
      if (dp.open) loadPrompt();
      body.appendChild(dp);
      // 함께 보내는 내용 — 실제 작업과 같은 조립(서버 refine.parts). 입력창 글과 다듬기 지시는 이미 보이므로 자리만 적는다
      var df = el('details', 'rf-det'); df.open = rfDet.full;
      df.appendChild(el('summary', null, '다듬기 프롬프트 외에 함께 보내는 내용'));
      var fbody = el('div', 'rf-full'); df.appendChild(fbody);
      function loadFull() {
        fbody.innerHTML = ''; fbody.appendChild(el('div', 'iq-hint', '읽는 중…'));
        var cc = rfCtx();
        api('POST', '/api/refine/preview', Object.assign({ mode: rfMode, answers: rfMode === 'request' ? rfAnswers() : [] }, cc.body)).then(function (r) {
          fbody.innerHTML = '';
          fbody.appendChild(el('div', 'iq-hint', r.forked
            ? '이 세션을 마지막으로 끝난 턴까지 일회용으로 포크해 아래 글을 다음 메시지로 보냅니다 — 세션의 지침과 지금까지의 대화가 앞에 함께 들어갑니다.'
            : '작업 폴더에서 일회용 새 세션을 열어 아래 글을 첫 메시지로 보냅니다 — 에이전트가 그 폴더의 지침(AGENTS.md 등)을 함께 읽습니다.'));
          r.parts.forEach(function (pt) {
            var sec = el('div', 'rf-part');
            sec.appendChild(el('div', 'sheet-sec', pt.label));
            var slot = pt.key === 'source' ? '입력창 글이 이 자리에 들어갑니다.' : pt.key === 'prompt' ? '“다듬기 프롬프트 확인·수정”의 내용이 이 자리에 들어갑니다.' : '';
            sec.appendChild(slot ? el('div', 'iq-hint', slot) : el('pre', 'rf-pre', pt.text));
            fbody.appendChild(sec);
          });
        }, function (e) { fbody.innerHTML = ''; fbody.appendChild(el('div', 'iq-formerr', '읽지 못했습니다: ' + e.message)); });
      }
      df.addEventListener('toggle', function () { rfDet.full = df.open; if (df.open) loadFull(); });
      if (df.open) loadFull();
      body.appendChild(df);
      if (rfMsg) body.appendChild(el('div', 'iq-formerr', rfMsg));
      if (!j) { body.scrollTop = top; return; }
      if (running) {
        var pr = el('div', 'iq-progress');
        pr.appendChild(el('span', 'dot'));
        var stage = el('span', 'iq-stage'); pr.appendChild(stage);
        body.appendChild(pr);
        var pm = el('div', 'iq-progmeta'); body.appendChild(pm);
        rfProg = { stage: stage, meta: pm };
        rfPaintProgress();
        return;
      }
      if (j.status !== 'done') {
        body.appendChild(el('div', 'iq-errline' + (j.status === 'cancelled' ? ' dim' : ''),
          j.status === 'cancelled' ? '멈췄습니다 — 입력창 글은 그대로입니다' : '다듬지 못했습니다 — ' + (j.error || '알 수 없는 오류') + ' (입력창 글은 그대로)'));
        body.scrollTop = top;
        return;
      }
      var res = j.result || {};
      var st = el('div', 'rf-status');
      if (rf.applied === false) {
        st.appendChild(el('span', 'grow', '다듬는 동안 글이 바뀌어 넣지 않았습니다'));
        st.appendChild(btn('바꾸기', 'small primary', function () {
          rf.src = ta.value; rfApply(res); rf.applied = true; rf.base = ta.value; rfSave(); rfPaint();
        }, '다듬은 글로 입력창을 바꾼다(지금 글은 버전에 남는다)'));
      } else if (rf.applied === 'reverted') {
        st.appendChild(el('span', 'grow', '다듬기 전 글로 되돌렸습니다'));
        st.appendChild(btn('다듬은 글 다시 넣기', 'small', function () {
          rfVersion('before-restore', ta.value); rfSetText(res.request); rf.applied = true; rfSave(); rfPaint();
        }));
      } else {
        st.appendChild(el('span', 'grow', '다듬은 글을 입력창에 넣었습니다'));
        st.appendChild(btn(rf.cmp ? '비교 닫기' : '원문 비교', 'small', function () { rf.cmp = !rf.cmp; rfSave(); rfPaint(); }));
        st.appendChild(btn('되돌리기', 'small', function () {
          rf.applied = 'reverted'; rf.cmp = false; rfSave();
          rfRestore({ text: rf.src }, '다듬기 전 글로 되돌렸습니다');
        }, '다듬기 전 글로 되돌린다(지금 글은 버전에 남는다)'));
      }
      body.appendChild(st);
      // 실제로 쓴 모델 · effort 는 위 칩과 다를 때만 적는다(같은 값을 두 번 보이지 않는다)
      var u = j.usage || {}, chipNow = rfUsed();
      var usedModel = j.model && (j.model !== chipNow.model || (j.effort || '') !== (chipNow.effort || '')) ? rfModelText(j.model, j.effort) : '';
      var meta = el('div', 'iq-meta', [j.mode === 'writing' ? '좋은 글' : '', usedModel, dur(j.duration_ms), u.cached ? '캐시 ' + kfmt(u.cached) + ' 읽음' : '', j.turn_id ? '세션 맥락' : ''].filter(Boolean).join(' · '));
      meta.title = '입력 ' + kfmt(u.input) + ' · 캐시 ' + kfmt(u.cached) + ' · 출력 ' + kfmt(u.output) + (j.fork_thread ? '\n일회용 ' + (j.turn_id ? '포크' : '세션') + ' ' + j.fork_thread : '');
      body.appendChild(meta);
      if ((res.lost_mentions || []).length) body.appendChild(el('div', 'iq-formerr', '다듬은 글에서 멘션이 빠졌습니다: ' + res.lost_mentions.join(' ') + ' — 되돌리거나 다시 넣어 주세요'));
      if (rf.cmp && rf.applied === true) {
        var diff = el('div', 'rf-diff');
        wordDiff(rf.src || '', ta.value).forEach(function (d) { diff.appendChild(d.t === 'eq' ? document.createTextNode(d.s) : el(d.t, null, d.s)); });
        body.appendChild(diff);
      }
      if ((res.editing_notes || []).length) {
        body.appendChild(el('div', 'sheet-sec', '편집 메모'));
        res.editing_notes.forEach(function (n) {
          var box = el('div', 'rf-qa');
          if (n.quote) box.appendChild(el('blockquote', 'rf-quote', n.quote));
          box.appendChild(el('div', 'rf-text', n.note));
          body.appendChild(box);
        });
      }
      if (res.opinion) {
        body.appendChild(el('div', 'sheet-sec', '에이전트 의견'));
        var op = el('div', 'md rf-text'); op.innerHTML = md.render(res.opinion); body.appendChild(op);
      }
      if ((res.answers || []).length) {
        body.appendChild(el('div', 'sheet-sec', '궁금한 것에 대한 답'));
        res.answers.forEach(function (a) {
          var box = el('div', 'rf-qa'); box.appendChild(el('b', null, a.question));
          var ab = el('div', 'md rf-text'); ab.innerHTML = md.render(a.answer); box.appendChild(ab);
          body.appendChild(box);
        });
      }
      var qs = res.missing_context || [];
      if (qs.length) {
        body.appendChild(el('div', 'sheet-sec', '더 알려 주면 좋은 것'));
        rf.answers = rf.answers || {};
        var goBtn = btn('답변 반영해 다듬기', 'small primary', function () { startRefine(rfAnswers()); });
        function syncGo() { goBtn.disabled = !rfAnswers().length; }
        qs.forEach(function (q, i) {
          var box = el('div', 'rf-q');
          box.appendChild(el('b', null, q.question));
          if (q.purpose) box.appendChild(el('div', 'iq-hint', q.purpose));
          var a = rf.answers[i] = rf.answers[i] || { sel: [], text: '' };
          if ((q.options || []).length) {
            var seg = el('div', 'seg');
            q.options.forEach(function (o) {
              var b = btn(o, a.sel.indexOf(o) >= 0 ? 'on' : '', function () {
                var k = a.sel.indexOf(o);
                if (k >= 0) a.sel.splice(k, 1); else a.sel.push(o);
                b.classList.toggle('on', k < 0); b.setAttribute('aria-pressed', String(k < 0)); rfSave(); syncGo();
              });
              b.setAttribute('aria-pressed', String(a.sel.indexOf(o) >= 0));
              seg.appendChild(b);
            });
            box.appendChild(seg);
          }
          var inp = el('input'); inp.placeholder = '직접 답하기 (선택과 함께 가능)'; inp.value = a.text || ''; inp.setAttribute('aria-label', q.question + ' — 직접 답하기');
          inp.addEventListener('input', function () { a.text = inp.value; rfSave(); syncGo(); });
          inp.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.isComposing && rfAnswers().length) { e.preventDefault(); startRefine(rfAnswers()); } });
          box.appendChild(inp);
          body.appendChild(box);
        });
        syncGo();
        var row = el('div', 'rf-status'); row.appendChild(el('span', 'grow')); row.appendChild(goBtn); body.appendChild(row);
      }
      body.scrollTop = top;
    }
    // 버전 탭 — 최신이 위. 누르면 지금 글과 비교해 보고 그 버전으로 되돌린다(되돌리기 직전 글도 버전으로 남는다).
    function rfPaintVersions(body) {
      if (!rfVers.length) {
        body.appendChild(el('div', 'iq-hint', '아직 버전이 없습니다 — 다듬으면 다듬기 전 글과 다듬은 글이 여기에 남습니다. 보내면 비웁니다.'));
        return;
      }
      var cur = ta.value;
      rfVers.slice().reverse().forEach(function (v) {
        var item = el('div', 'rf-ver' + (rf.ver === v.n ? ' open' : ''));
        var line = btn('', 'rf-ver-line', function () { rf.ver = rf.ver === v.n ? null : v.n; rfSave(); rfPaint(); });
        line.setAttribute('aria-expanded', String(rf.ver === v.n));
        line.appendChild(el('b', null, 'v' + v.n));
        line.appendChild(el('span', 'rf-ver-kind', RF_VER_KIND[v.kind] || v.kind));
        line.appendChild(el('span', 'rf-ver-meta', [v.kind === 'refine' ? (v.mode === 'writing' ? '좋은 글' : '에이전트 요청') : '', v.kind === 'refine' ? rfModelText(v.model, v.effort) : '', ago(v.at)].filter(Boolean).join(' · ')));
        if (v.text === cur) line.appendChild(el('span', 'iq-badge', '지금'));
        item.appendChild(line);
        if (rf.ver === v.n) {
          var pv = el('div', 'rf-diff');
          if (v.text === cur) pv.textContent = v.text;
          else wordDiff(cur, v.text).forEach(function (d) { pv.appendChild(d.t === 'eq' ? document.createTextNode(d.s) : el(d.t, null, d.s)); });
          item.appendChild(el('div', 'iq-hint', v.text === cur ? '지금 입력창 글과 같습니다' : '지금 글과 비교 — 지워질 말 · 더해질 말'));
          item.appendChild(pv);
          if (v.text !== cur) {
            var r = el('div', 'rf-status'); r.appendChild(el('span', 'grow'));
            r.appendChild(btn('이 버전으로 되돌리기', 'small primary', function () { rfRestore(v); }));
            item.appendChild(r);
          }
        }
        body.appendChild(item);
      });
    }
    if (rf && rf.job) {
      // 화면을 떠났다 와도 작업을 다시 붙잡는다. 닫아 둔 판의 옛 작업이 서버에서 지워졌으면 조용히 잊는다
      rfPaint();
      api('GET', '/api/jobs/' + rf.job).then(function (r) { if (rf && rf.job === r.job.id) { rfJob = r.job; rfLanded(); rfPaint(); rfPoll(); } },
        function () {
          if (!rf) return;
          if (!rf.open) { rf.job = null; rfSave(); return; }
          rfJob = { id: rf.job, status: 'failed', stage: 'failed', error: '작업을 찾지 못했습니다 — 다시 다듬어 주세요' }; rfPaint();
        });
    } else rfPaint();
    var rfTick = setInterval(rfPaintProgress, 1000);
    S.cleanup.push(function () { clearInterval(rfTick); });

    setTimeout(grow, 0);
    paint();
    return {
      el: wrap, ta: ta,
      setRunning: function (r) { running = !!r; paint(); },
      // agent-hub 가 돌리는 턴이면 보내기·멈춤이 그쪽으로 간다는 것을 선택줄 앞에 적는다
      setVia: function (via) {
        steerLead.textContent = via ? via + ' 응답 중 · 보내면' : '응답 중에 보내면';
        stop.title = via === 'agent-hub' ? via + ' 에서 진행 중인 턴을 멈춘다 — ' + via + ' 대기열도 비운다'
          : via ? via + ' 에서 진행 중인 턴을 멈춘다' : '진행 중인 턴을 멈춘다 — 기다리는 메시지는 남는다';
      },
      // 멈출 수 없는 턴(이 앱이 쥐지 않은 다른 곳의 턴)이면 멈춤을 숨긴다 — 바로 넣기·턴 끝나고는 그대로 쓴다
      setCanStop: function (can) { can = !!can; if (can !== canStop) { canStop = can; paint(); } },
      queueEl: queueEl,
      append: function (block) {
        var cur = ta.value.replace(/\s+$/, '');
        ta.value = (cur ? cur + '\n\n' : '') + block + '\n\n';
        grow(); save(); ta.focus({ preventScroll: true }); ta.setSelectionRange(ta.value.length, ta.value.length);
      },
      remove: function (block) {
        if (ta.value.indexOf(block) < 0) return false;
        ta.value = ta.value.replace(block, '').replace(/\n{3,}/g, '\n\n').replace(/^\s+/, '');
        grow(); save(); return true;
      },
      setHint: function (s) { hint.textContent = s || ''; },
      repaintChip: chip.repaint
    };
  }

  // ---------------------------------------------------------------- 목록
  // 목록은 한 번 만들어 계속 쓴다 — 좁은 화면·목록 화면에서는 본문에, 나란히 보기에서는 왼쪽에 붙는다.
  // 그래서 세션을 바꿔 열어도 왼쪽 목록의 검색어·스크롤·더 보기가 그대로 남는다.
  var LIST = null;
  function archiveButton(s) {
    var archived = !!s.archived;
    var label = archived ? '보관 해제' : '보관';
    var action = btn(label, 'small session-archive', function () {
      action.disabled = true; action.textContent = archived ? '복원 중…' : '보관 중…';
      api('POST', '/api/sessions/' + encodeURIComponent(s.id) + '/archive', { archived: !archived }).then(function () {
        toast(archived ? '세션을 복원했습니다' : '세션을 보관했습니다');
        if (S.X && S.X.sid === s.id && S.X.archiveChanged) S.X.archiveChanged(!archived);
        sessionList().load(true);
      }, function (e) {
        action.disabled = false; action.textContent = label;
        toast('저장하지 못했습니다: ' + e.message);
      });
    }, archived ? '세션 목록에 다시 표시' : '목록에서 숨기고 보관 — 기록과 실행 중인 작업은 유지됩니다');
    action.setAttribute('aria-label', label);
    return action;
  }
  // 목록 행의 마지막 턴 상태 — [글, 배지 모양, 설명]. 완료는 표시하지 않는다.
  var LIST_STATE = {
    running: ['실행 중', 'run', ''],
    interrupted: ['멈춤', 'dim', '마지막 턴을 멈췄습니다'],
    failed: ['실패', 'bad', '마지막 턴이 실패했습니다'],
    unfinished: ['끝나지 않음', 'dim', '마지막 턴이 끝 기록 없이 끊겼습니다(프로세스가 꺼졌거나 오래 소식이 없음)'],
    error: ['시스템 오류', 'bad', 'Codex 가 이 세션을 시스템 오류 상태로 알려 줍니다'],
  };
  function paintRunBadge(sb) {
    var sig = +sb.dataset.signal, quiet = +sb.dataset.quiet || 0, now = Date.now() / 1000;
    var idle = sig ? now - sig : 0, stalled = sig ? isStalled(sig, quiet, now) : false;
    sb.textContent = stalled ? '지연' : '실행 중';
    sb.className = 'badge ' + (stalled ? 'warn' : 'run');
    sb.title = (sb.dataset.external ? '이 앱 밖(Codex 앱·CLI 등)에서 진행 중' : '이 앱에서 진행 중') +
      (sig ? ' · 마지막 신호 ' + Math.round(idle) + '초 전' : '') + (quiet > now ? ' · 명령이 끝나기를 기다리는 중' : '');
  }
  function sessionList() {
    if (LIST) return LIST;
    var L = { el: el('div', 'slist'), active: null };
    var tools = el('div', 'list-tools');
    tools.classList.add('session-tools');
    var filters = el('div', 'seg list-filter');
    var archived = false;
    var activeBtn = btn('세션', 'on', function () { select(false); });
    var archivedBtn = btn('보관됨', '', function () { select(true); });
    filters.appendChild(activeBtn); filters.appendChild(archivedBtn); tools.appendChild(filters);
    var q = el('input'); q.type = 'search'; q.placeholder = '세션 제목·폴더 검색';
    q.setAttribute('aria-label', '세션 검색');
    tools.appendChild(q);
    var refresh = btn('↻', 'small list-refresh', function () { clearTimeout(timer); load(true); }, '세션 목록 새로고침');
    refresh.setAttribute('aria-label', '세션 목록 새로고침');
    tools.appendChild(refresh);
    tools.appendChild(btn('새 세션', 'primary small', function () { if (parseHash().path !== '/new') go('#/new'); }));
    var list = el('div'), more = el('div', 'more-row');
    L.el.appendChild(tools); L.el.appendChild(list); L.el.appendChild(more);
    var cursor = null, timer = null, loadSeq = 0, loading = false, shownQ = null, pages = 0, liveTimer = null;
    // 진행 중인 세션이 보이면 상태가 바뀌는지 10초마다 따라 읽는다(첫 쪽만 — 더 보기로 늘린 목록은 그대로 둔다)
    function followLive() {
      clearTimeout(liveTimer);
      if (pages !== 1 || !list.querySelector('.srow[data-state="running"]')) return;
      liveTimer = setTimeout(function () {
        if (L.el.isConnected && document.visibilityState === 'visible' && pages === 1 && !loading) load(true); else followLive();
      }, 10000);
    }
    // 진행 중인 행의 '지연'(30초 무신호)은 시계로 다시 칠한다
    setInterval(function () {
      if (L.el.isConnected) list.querySelectorAll('.srow[data-state="running"] .badge[data-signal]').forEach(paintRunBadge);
    }, 5000);
    function load(reset) {
      if (!reset && loading) return Promise.resolve();
      var my = ++loadSeq;
      clearTimeout(liveTimer);
      loading = true; refresh.disabled = true; refresh.textContent = '…';
      L.el.setAttribute('aria-busy', 'true');
      more.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
      var url = '/api/sessions?limit=40' + (archived ? '&archived=1' : '') + (q.value.trim() ? '&q=' + encodeURIComponent(q.value.trim()) : '') + (!reset && cursor ? '&cursor=' + encodeURIComponent(cursor) : '');
      if (reset && !list.children.length) list.appendChild(el('div', 'empty', '세션을 읽는 중…'));
      // 검색어를 바꾸면 결과가 올 때까지 이전 목록을 흐리게 둔다 — 그대로 두면 검색이 안 된 것처럼 보인다
      var wantQ = q.value.trim();
      if (reset && shownQ !== null && wantQ !== shownQ) list.classList.add('busy');
      return api('GET', url).then(function (r) {
        if (my !== loadSeq) return;
        list.classList.remove('busy');
        if (reset) { list.innerHTML = ''; shownQ = wantQ; }
        r.sessions.forEach(function (s) { place(s); });
        if (reset && !r.sessions.length) list.appendChild(el('div', 'empty', q.value ? '맞는 세션이 없습니다' : archived ? '보관한 세션이 없습니다' : '세션이 없습니다 — 새 세션을 시작해 보세요'));
        cursor = r.next_cursor;
        pages = reset ? 1 : pages + 1;
        followLive();
        more.innerHTML = '';
        if (cursor) more.appendChild(btn('더 보기', null, function () { load(false); }));
      }, function (e) {
        if (my !== loadSeq) return;
        list.classList.remove('busy');
        if (list.querySelector('.srow')) toast('세션 목록을 읽지 못했습니다: ' + e.message);
        else { list.innerHTML = ''; list.appendChild(el('div', 'empty err', '세션 목록을 읽지 못했습니다: ' + e.message)); }
      }).then(function () {
        if (my !== loadSeq) return;
        loading = false; refresh.disabled = false; refresh.textContent = '↻';
        L.el.setAttribute('aria-busy', 'false');
        more.querySelectorAll('button').forEach(function (b) { b.disabled = false; });
      });
    }
    function row(s, parentCwd) {
      var wrap = el('div', 'session-row' + (s.archived ? ' archived' : ''));
      var b = btn('', 'srow' + (s.id === L.active ? ' on' : ''));
      b.dataset.sid = s.id; b.dataset.agent = s.agent || 'codex';
      if (s.id === L.active) b.setAttribute('aria-current', 'true');
      b.appendChild(el('div', 'srow-title', s.title));
      var meta = el('div', 'srow-meta');
      meta.appendChild(agentIcon(s.agent));
      var st = s.state || ((s.status === 'running' || s.status === 'active') ? { kind: 'running' } : null);
      if (st) {
        b.dataset.state = st.kind;
        var sb = el('span', 'badge ' + (LIST_STATE[st.kind] || ['', 'dim'])[1], (LIST_STATE[st.kind] || [st.kind])[0]);
        if (st.kind === 'running') {
          if (st.last_signal) sb.dataset.signal = st.last_signal;
          if (st.quiet_until) sb.dataset.quiet = st.quiet_until;
          if (st.external) sb.dataset.external = '1';
          paintRunBadge(sb);
        } else sb.title = LIST_STATE[st.kind] ? LIST_STATE[st.kind][2] : '';
        meta.appendChild(sb);
      }
      if (s.agent_name) meta.appendChild(el('span', 'badge dim', s.agent_name));
      if (s.mine) meta.appendChild(el('span', 'badge', '이 앱'));
      if (s.forked_from) meta.appendChild(el('span', 'badge dim', '포크'));
      if (s.worktree) meta.appendChild(el('span', 'badge dim', '워크트리'));
      // 자식이 부모와 같은 폴더면 폴더 이름은 부모 줄에만 둔다
      meta.appendChild(el('span', 'grow', [s.cwd === parentCwd ? '' : base(s.cwd), s.model, ago(s.updated_at)].filter(Boolean).join(' · ')));
      b.appendChild(meta);
      b.title = s.cwd || '';
      b.addEventListener('click', function () { if (!(isSplit() && L.active === s.id)) go('#/s/' + encodeURIComponent(s.id)); });
      wrap.appendChild(b);
      wrap.appendChild(archiveButton(s));
      return wrap;
    }
    // 세션 하나 = 줄 + (있으면) 하위 세션 묶음. 하위 세션은 부모 아래에 들여 쓰고 접을 수 있다.
    // 접힘은 사용자가 고른 것만 기억한다(sessionStorage) — 고르기 전에는 넷 이상이고 돌고 있는 것이 없을 때만 접는다.
    function kidsPref(id, v) {
      var all = {};
      try { all = JSON.parse(sessionStorage.getItem(LS + 'kids') || '{}') || {}; } catch (e) {}
      if (v === undefined) return all[id];
      all[id] = v;
      try { sessionStorage.setItem(LS + 'kids', JSON.stringify(all)); } catch (e) {}
    }
    function group(s, parentCwd) {
      var g = el('div', 'sgroup');
      g.dataset.sid = s.id; g.dataset.cwd = s.cwd || '';
      g.appendChild(row(s, parentCwd));
      (s.children || []).forEach(function (c) { addKid(g, c); });
      return g;
    }
    function addKid(g, c) {
      var kids = g.querySelector(':scope > .skids'), tog = g.querySelector(':scope > .skids-tog');
      if (!kids) {
        tog = btn('', 'skids-tog', function () {
          var open = kids.hidden; kidsPref(g.dataset.sid, open); paintKids(g, open);
        });
        kids = el('div', 'skids');
        g.appendChild(tog); g.appendChild(kids);
      }
      kids.appendChild(group(c, g.dataset.cwd));
      var pref = kidsPref(g.dataset.sid);
      paintKids(g, pref !== undefined ? pref : (kids.children.length < 4 || !!kids.querySelector('.srow[data-state="running"], .srow.on')));
    }
    function paintKids(g, open) {
      var kids = g.querySelector(':scope > .skids'), tog = g.querySelector(':scope > .skids-tog');
      var n = kids.querySelectorAll('.srow').length, run = kids.querySelectorAll('.srow[data-state="running"]').length;
      kids.hidden = !open;
      tog.textContent = (open ? '▾ ' : '▸ ') + '하위 세션 ' + n + (run && !open ? ' · 실행 중 ' + run : '');
      tog.setAttribute('aria-expanded', String(open));
      tog.title = open ? '하위 세션 접기' : '하위 세션 펼치기';
    }
    function cssEsc(v) { return window.CSS && CSS.escape ? CSS.escape(v) : String(v).replace(/["\\]/g, '\\$&'); }
    // 앞 쪽에서 이미 그린 부모의 자식(더 보기)은 그 부모 아래에 붙이고, 이미 있는 세션은 다시 그리지 않는다
    function place(s) {
      if (list.querySelector('.sgroup[data-sid="' + cssEsc(s.id) + '"]')) return;
      var parent = s.parent && list.querySelector('.sgroup[data-sid="' + cssEsc(s.parent) + '"]');
      if (parent) addKid(parent, s); else list.appendChild(group(s));
    }
    // 오른쪽에 열린 세션을 목록에서 표시한다. 목록에 없으면(방금 만든 세션 등) 다시 읽는다.
    L.setActive = function (id) {
      L.active = id;
      var found = null;
      list.querySelectorAll('.srow').forEach(function (b) {
        var on = !!id && b.dataset.sid === id;
        b.classList.toggle('on', on);
        if (on) { b.setAttribute('aria-current', 'true'); found = b; } else b.removeAttribute('aria-current');
      });
      for (var k = found && found.closest('.skids'); k; k = k.parentElement.closest('.skids')) {
        if (k.hidden) paintKids(k.parentElement, true);
      }
      if (!isSplit() || !id) return;
      if (!found) { if (!loading && !q.value.trim() && list.querySelector('.srow')) load(true); return; }
      // 왼쪽 목록만 움직인다(scrollIntoView 는 창까지 스크롤할 수 있다)
      var top = found.getBoundingClientRect().top - side.getBoundingClientRect().top + side.scrollTop, toolsH = tools.offsetHeight;
      if (top - toolsH < side.scrollTop) side.scrollTop = top - toolsH - 8;
      else if (top + found.offsetHeight > side.scrollTop + side.clientHeight) side.scrollTop = top + found.offsetHeight - side.clientHeight + 8;
    };
    L.url = function () { return '#/' + (archived || q.value.trim() ? '?' + (archived ? 'archived=1' : '') + (q.value.trim() ? (archived ? '&' : '') + 'q=' + encodeURIComponent(q.value.trim()) : '') : ''); };
    L.setState = function (v) {
      var changed = q.value !== (v.q || '') || archived !== (v.archived === '1');
      q.value = v.q || ''; archived = v.archived === '1';
      activeBtn.classList.toggle('on', !archived); archivedBtn.classList.toggle('on', archived);
      activeBtn.setAttribute('aria-pressed', String(!archived)); archivedBtn.setAttribute('aria-pressed', String(archived));
      return changed;
    };
    function saveListState() {
      history.replaceState(Object.assign({}, history.state, { sessionList: { q: q.value, archived: archived ? '1' : '' } }), '', parseHash().path === '/' ? L.url() : location.href);
    }
    function select(value) {
      archived = value;
      L.setState({ q: q.value, archived: archived ? '1' : '' });
      saveListState();
      if (isSplit()) side.scrollTop = 0;
      load(true);
    }
    L.setState((history.state && history.state.sessionList) || {});
    L.load = load;
    q.addEventListener('input', function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        saveListState();
        load(true);
      }, 250);
    });
    document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'visible' && L.el.isConnected && !cursor) load(true); });
    LIST = L;
    return L;
  }
  function viewList(params, pop) {
    setHeader(BOOT.name, ['Codex · Claude Code 세션'], false);
    var L = sessionList();
    L.setState(params);
    history.replaceState(Object.assign({}, history.state, { sessionList: params }), '', location.href);
    view.appendChild(L.el);
    L.load(true).then(function () { restoreScroll(pop); });
  }

  // ---------------------------------------------------------------- 새 세션
  function viewNew(params, pop) {
    setHeader('새 세션', [], true);
    var form = el('div', 'form');
    // 에이전트 — 고른 값은 기억하고, 모델·effort 도 에이전트마다 따로 기억한다
    var agent = lsGet('newAgent', 'codex') === 'claude' ? 'claude' : 'codex';
    var fA = el('div', 'field'); fA.appendChild(el('label', null, '에이전트'));
    var aseg = agentSeg(function () { return agent; }, function (a) {
      agent = a; lsSet('newAgent', a);
      pickState = lsGet(pickKey('pick.new', a), { model: '', effort: '' });
      comp.repaintChip();
    });
    fA.appendChild(aseg.el); form.appendChild(fA);
    var f1 = el('div', 'field'); f1.appendChild(el('label', null, '작업 폴더'));
    var cwd = el('input'); cwd.placeholder = '/절대/경로'; cwd.value = params.cwd || lsGet('lastCwd', '') || '';
    cwd.setAttribute('aria-label', '작업 폴더');
    f1.appendChild(cwd);
    var chips = el('div', 'cwd-chips'); f1.appendChild(chips);
    form.appendChild(f1);
    // git 저장소면 원본을 건드리지 않게 새 브랜치 워크트리에서 돌린다(기본 켬, 고른 값은 기억한다)
    var wtRow = el('label', 'wt-row'); wtRow.hidden = true;
    var wtBox = el('input'); wtBox.type = 'checkbox'; wtBox.checked = lsGet('newWorktree', true);
    var wtSub = el('span', 'wt-sub');
    wtRow.appendChild(wtBox); wtRow.appendChild(el('span', 'wt-main', '워크트리에서 작업')); wtRow.appendChild(wtSub);
    wtBox.addEventListener('change', function () { lsSet('newWorktree', wtBox.checked); });
    form.appendChild(wtRow);
    var gitSeq = 0, gitTimer = null, git = null;
    function checkGit() {
      var my = ++gitSeq, c = cwd.value.trim();
      if (!c) { git = null; wtRow.hidden = true; return; }
      api('GET', '/api/git?cwd=' + encodeURIComponent(c)).then(function (r) {
        if (my !== gitSeq) return;
        git = r.git && r.has_head ? r : null;
        wtRow.hidden = !git;
        if (git) wtSub.textContent = (git.branch || 'HEAD') + ' 에서 새 브랜치' + (git.dirty ? ' · 커밋 안 된 변경 ' + git.dirty + '개는 따라가지 않음' : '');
      }, function () { if (my === gitSeq) { git = null; wtRow.hidden = true; } });
    }
    view.appendChild(form);
    api('GET', '/api/cwds').then(function (r) {
      if (!cwd.value) { cwd.value = (r.cwds && r.cwds[0]) || r.home || ''; checkGit(); }
      (r.cwds || []).slice(0, 8).forEach(function (c) {
        var b = btn(base(c), null, function () { cwd.value = c; lsSet('lastCwd', c); checkGit(); }, c); chips.appendChild(b);
      });
    }, function () {});
    cwd.addEventListener('change', function () { lsSet('lastCwd', cwd.value.trim()); });
    cwd.addEventListener('input', function () { clearTimeout(gitTimer); gitTimer = setTimeout(checkGit, 300); });
    S.cleanup.push(function () { clearTimeout(gitTimer); });
    if (cwd.value) checkGit();
    var pickState = lsGet(pickKey('pick.new', agent), { model: '', effort: '' });
    var comp = composer({
      draftKey: 'new', placeholder: '무엇을 할까요? — @ 로 파일·세션 멘션 (' + MOD + 'Enter 보내기)', sendLabel: '시작',
      cwd: function () { return cwd.value.trim(); },
      agent: function () { return agent; },
      pick: { get: function () { return pickState; }, set: function (p) { pickState = p; lsSet(pickKey('pick.new', agent), p); } },
      refine: function () { return { body: { cwd: cwd.value.trim(), model: pickState.model || null, agent: agent }, model: pickState.model || '', agent: agent }; },
      commentIds: mdCommentIdsIn,
      onSend: function (body) {
        body.cwd = cwd.value.trim();
        body.agent = agent;
        body.worktree = !!(git && wtBox.checked);
        lsSet('lastCwd', body.cwd);
        return api('POST', '/api/sessions', body, true).then(function (r) {
          if (LIST) LIST.load(true);
          go('#/s/' + encodeURIComponent(r.id), true);
        });
      }
    });
    comp.el.style.position = 'static';
    view.appendChild(comp.el);
    setTimeout(function () { comp.ta.focus(); }, 50);
    restoreScroll(pop);
  }
  function mdCommentIdsIn(text) {
    var ids = lsGet('mdInputIds', {});
    return Object.keys(ids).filter(function (id) { return text.indexOf(ids[id]) >= 0; });
  }

  // ---------------------------------------------------------------- 세션 읽기 과정
  var AGENT_NAME = { codex: 'Codex', claude: 'Claude Code' };
  function rowAgent(sid) {
    var r = document.querySelector('.srow[data-sid="' + (window.CSS && CSS.escape ? CSS.escape(sid) : sid) + '"]');
    return r ? r.dataset.agent : null;
  }
  function secs(ms) { return ms < 10000 ? (ms / 1000).toFixed(1) + '초' : ms < 60000 ? Math.round(ms / 1000) + '초' : Math.floor(ms / 60000) + '분 ' + Math.round(ms % 60000 / 1000) + '초'; }
  function kb(n) { return n >= 1048576 ? (n / 1048576).toFixed(1) + 'MB' : Math.max(1, Math.round(n / 1024)) + 'KB'; }
  // 세션 첫 읽기를 턴의 '과정'처럼 단계로 보인다 — 지금 단계·경과, 30초 무신호면 지연, 멈추기·다시 읽기.
  // 진행 표시와 운영 로그(client_session_render)는 같은 단계 기록에서 나온다.
  function readProcess(container, agent) {
    var STALL_MS = 30000;
    var t0 = Date.now(), lastSignal = t0, finished = false;
    var ctrl = window.AbortController ? new AbortController() : null;
    var box = el('div', 'part proc open read-proc');
    box.setAttribute('role', 'status'); box.setAttribute('aria-live', 'polite');
    var head = el('div', 'proc-head'), spin = el('span', 'proc-spinner');
    var title = el('b', null, '세션 읽는 중'), clock = el('span', 'counts'), stall = el('span', 'badge warn', '지연');
    stall.hidden = true;
    var tl = el('div', 'read-proc-title'); tl.appendChild(spin); tl.appendChild(title); tl.appendChild(clock); tl.appendChild(stall);
    var stop = btn('멈추기', 'small link', function () { if (ctrl) ctrl.abort(); }, '읽기를 그만둔다 — 다시 읽기로 이어 할 수 있다');
    if (!ctrl) stop.hidden = true;
    head.appendChild(tl); head.appendChild(stop); box.appendChild(head);
    var ol = el('ol', 'read-steps'); box.appendChild(ol);
    function stepLabel() { return (AGENT_NAME[agent] ? AGENT_NAME[agent] + ' ' : '') + '기록 읽기'; }
    var steps = [
      { key: 'read', label: stepLabel() },
      { key: 'recv', label: '받기' },
      { key: 'paint', label: '화면 그리기' }
    ];
    steps.forEach(function (st) {
      st.el = el('li', 'read-step todo'); st.el.dataset.step = st.key;
      st.mark = el('span', 'mark'); st.name = el('span', 'name', st.label); st.sub = el('span', 'sub'); st.time = el('span', 'time');
      st.el.appendChild(st.mark); st.el.appendChild(st.name); st.el.appendChild(st.sub); st.el.appendChild(st.time);
      ol.appendChild(st.el);
    });
    container.innerHTML = ''; container.appendChild(box);
    var cur = -1;
    function begin(i) {
      var now = Date.now();
      if (cur >= 0) { steps[cur].ms = now - steps[cur].at; steps[cur].state = 'done'; }
      cur = i; if (i < steps.length) { steps[i].at = now; steps[i].state = 'now'; }
      lastSignal = now; render();
    }
    function render() {
      var now = Date.now();
      clock.textContent = secs(now - t0);
      var stalled = !finished && now - lastSignal > STALL_MS;
      stall.hidden = !stalled; box.classList.toggle('stalled', stalled);
      steps.forEach(function (st) {
        var state = st.state || 'todo';
        st.el.className = 'read-step ' + state;
        st.name.textContent = st.label;
        st.mark.textContent = state === 'done' ? '✓' : state === 'failed' ? '✕' : '';
        st.mark.className = 'mark' + (state === 'now' && !finished ? ' proc-spinner' : '');
        st.time.textContent = state === 'done' ? secs(st.ms) : state === 'now' || state === 'failed' ? secs((st.ms != null ? st.ms : now - st.at)) : '';
      });
    }
    var timer = setInterval(render, 500);
    S.cleanup.push(function () { clearInterval(timer); if (!finished && ctrl) ctrl.abort(); });
    begin(0);
    var bytes = 0, total = 0;
    function log(state, extra) {
      var ev = { type: 'session_render', state: state, ms: Date.now() - t0, agent: agent || '', bytes: bytes };
      steps.forEach(function (st) { if (st.ms != null) ev[{ read: 'wait_ms', recv: 'download_ms', paint: 'render_ms' }[st.key]] = st.ms; });
      Object.keys(extra || {}).forEach(function (k) { ev[k] = extra[k]; });
      clientLog(ev);
    }
    function end() { finished = true; clearInterval(timer); spin.hidden = true; stop.hidden = true; }
    return {
      fetch: function (url) {
        return fetch(url, ctrl ? { signal: ctrl.signal } : {}).then(function (r) {
          begin(1);
          total = +r.headers.get('Content-Length') || 0;
          function done(text) {
            var j = null;
            try { j = text ? JSON.parse(text) : {}; } catch (e) { j = { error: text }; }
            if (!r.ok) { var err = new Error((j && j.error) || ('HTTP ' + r.status)); err.status = r.status; throw err; }
            return j;
          }
          if (!r.body || !r.body.getReader || !window.TextDecoder) return r.text().then(function (t) { bytes = t.length; return done(t); });
          var reader = r.body.getReader(), dec = new TextDecoder(), parts = [];
          function pump() {
            return reader.read().then(function (x) {
              if (x.done) { parts.push(dec.decode()); return done(parts.join('')); }
              bytes += x.value.length; parts.push(dec.decode(x.value, { stream: true }));
              lastSignal = Date.now();
              steps[1].sub.textContent = total ? kb(bytes) + ' / ' + kb(total) : kb(bytes);
              return pump();
            });
          }
          return pump();
        }, function (e) {
          if (e && e.name === 'AbortError') throw e;
          throw new Error('서버에 닿지 않습니다 — ' + e.message);
        });
      },
      paint: function (d, apply) {
        steps[1].sub.textContent = kb(bytes);
        var tm = d.timing || {}, ag = d.session && d.session.agent;
        if (ag && ag !== agent) { agent = ag; steps[0].label = stepLabel(); }
        if (tm.read_ms != null) {
          steps[0].sub.textContent = '서버 ' + secs(tm.read_ms + (tm.build_ms || 0));
          steps[0].el.title = '에이전트 기록 읽기 ' + tm.read_ms + 'ms · 정리 ' + (tm.build_ms || 0) + 'ms · 나머지는 전달';
        }
        steps[2].sub.textContent = '턴 ' + (d.turns || []).length + '개';
        begin(2);
        // 단계가 화면에 그려진 뒤 무거운 그리기를 시작한다
        return new Promise(function (res) { requestAnimationFrame(function () { setTimeout(res, 0); }); }).then(function () {
          try { apply(); } catch (e) { begin(3); end(); log('failed', { error: String(e.message || e).slice(0, 200) }); throw e; }
          begin(3); end();
          log('ok', { count: (d.turns || []).length, read_ms: tm.read_ms });
          if (box.isConnected) box.remove();
        });
      },
      fail: function (e, retry) {
        var cancelled = e && e.name === 'AbortError';
        if (cur >= 0 && cur < steps.length) { steps[cur].ms = Date.now() - steps[cur].at; steps[cur].state = 'failed'; }
        end(); render();
        title.textContent = cancelled ? '읽기를 멈췄습니다' : '세션을 읽지 못했습니다';
        box.classList.add(cancelled ? 'cancelled' : 'failed');
        if (!cancelled) { var msg = el('div', 'read-proc-err', e.message); msg.setAttribute('role', 'alert'); box.appendChild(msg); }
        head.appendChild(btn('다시 읽기', 'small', retry));
        log(cancelled ? 'cancelled' : 'failed', cancelled ? null : { level: 'warn', error: String(e.message || e).slice(0, 200) });
      },
      drop: function () { end(); if (ctrl) ctrl.abort(); }
    };
  }

  // ---------------------------------------------------------------- 세션
  function viewSession(sid, pop) {
    var seq = S.seq;
    var contextSeq = 0, latestContext;
    setHeader('세션', [], true);
    var ui = lsGet('ui.' + sid, { open: {}, folded: {} });
    function saveUi() { lsSet('ui.' + sid, ui); }
    var X = { sid: sid, data: null, turns: {}, turnEls: {}, jobs: {}, comments: [], sumEff: {}, drafts: lsGet('iqDrafts.' + sid, []),
      running: false, liveId: null, first: true, reports: {} };
    S.X = X;
    S.cleanup.push(function () { if (S.X === X) S.X = null; });
    var list = el('div', 'turns'); view.appendChild(list);
    var pickState = lsGet('pick.' + sid, { model: '', effort: '' });
    var comp = composer({
      draftKey: sid, selfId: sid,
      pickOpts: { baseLabel: '세션 모델',   // 고르지 않으면 이 세션의 모델·effort·속도가 이어진다(칩도 그 값을 보인다)
        base: function () { return X.data && { model: X.data.session.model, effort: X.data.session.effort }; } },
      cwd: function () { return X.data && X.data.session.cwd; },
      agent: function () { return X.data && X.data.session.agent; },
      pick: { get: function () { return pickState; }, set: function (p) { pickState = p; lsSet('pick.' + sid, p); } },
      // 다듬기는 마지막으로 끝난 턴까지 포크한다(진행 중인 턴은 포크할 수 없다) — 세션 맥락과 캐시를 그대로 쓴다
      refine: function () {
        var done = (X.data ? X.data.turns : []).filter(function (t) { return t.status !== 'inProgress'; });
        var last = done[done.length - 1];
        return { body: { thread_id: sid, turn_id: last ? last.id : null }, model: (X.data && X.data.session.model) || '', forked: !!last,
          agent: X.data && X.data.session.agent };
      },
      commentIds: function (text) {
        var ids = X.comments.filter(function (c) { return c.state === 'input' && text.indexOf(commentBlock(c)) >= 0; }).map(function (c) { return c.id; });
        return ids.concat(mdCommentIdsIn(text));
      },
      steer: true,
      onSend: function (body) {
        return api('POST', '/api/sessions/' + encodeURIComponent(sid) + '/turns', body, true).then(function (r) {
          setRunningUi(true);
          if (r.hub) {
            var via = r.via || 'agent-hub';
            toast(r.steered ? via + ' 의 진행 중인 턴에 넣었습니다' : body.mode === 'queue' ? via + ' 에서 이 턴이 끝나면 보냅니다' : '지금 턴에 넣지 못해 ' + via + ' 가 다음 턴으로 보냅니다');
            clearTimeout(pollTimer); pollTimer = setTimeout(function () { load(false); }, 1200);
          } else if (r.queued) toast(body.mode === 'queue' ? '이 턴이 끝나면 보냅니다' : '지금 턴에 넣지 못해 다음 턴으로 보냅니다');
          var ids = lsGet('mdInputIds', {}); body.comment_ids.forEach(function (id) { delete ids[id]; }); lsSet('mdInputIds', ids);
          return r;
        });
      },
      onStop: function () {
        api('POST', '/api/sessions/' + encodeURIComponent(sid) + '/interrupt', {}).then(function () { toast('멈추라고 보냈습니다'); },
          function (e) { toast('멈추지 못했습니다: ' + e.message); });
      }
    });
    view.appendChild(comp.el);
    X.comp = comp;
    // 기다리는 메시지("턴 끝나고") — 입력창 위에 쌓인다. 응답 중이면 골라 바로 넣고, 쉬는 중(멈춤·실패 뒤)이면 지금 보낸다.
    X.queue = [];
    function paintQueue() {
      var q = X.queue || [], box = comp.queueEl, hubQ = X.hubRun ? X.data.hub.pending : 0;
      box.innerHTML = ''; box.hidden = !q.length && !hubQ;
      // agent-hub 대기열은 글을 받지 않고 개수만 안다 — 꺼내기·빼기는 agent-hub 에서 한다
      if (hubQ) box.appendChild(el('div', 'queue-head', (X.data.hub.via || 'agent-hub') + ' 에서 턴이 끝나면 차례로 보냄 · ' + hubQ));
      if (!q.length) return;
      box.appendChild(el('div', 'queue-head', X.running ? '턴이 끝나면 차례로 보냄 · ' + q.length : '보내지 않고 기다리는 중 · ' + q.length));
      q.forEach(function (it) {
        var row = el('div', 'qitem'); row.dataset.qid = it.id;
        var tx = el('span', 'qtext', (it.text || '').split('\n')[0] + (it.images && it.images.length ? ' · 이미지 ' + it.images.length + '장' : '')); tx.title = it.text || ''; row.appendChild(tx);
        var go = btn(X.running ? '바로 넣기' : '지금 보내기', 'small', function () {
          go.disabled = true; go.textContent = X.running ? '넣는 중' : '보내는 중';
          api('POST', '/api/sessions/' + encodeURIComponent(sid) + '/queue/' + encodeURIComponent(it.id) + '/send', {}).then(function (r) {
            if (r.queued && r.queue_id === it.id) { toast('다른 곳에서 도는 턴이라 끝난 뒤에 보냅니다'); go.disabled = false; paintQueue(); return; }
            X.queue = X.queue.filter(function (x) { return x.id !== it.id; });
            if (!r.queued) setRunningUi(true);
            paintQueue();
          }, function (e) { toast('보내지 못했습니다: ' + e.message); go.disabled = false; paintQueue(); });
        }, X.running ? '기다리지 않고 진행 중인 턴에 끼워 넣는다' : '이 메시지로 새 턴을 연다');
        var rm = btn('빼기', 'small link', function () {
          api('DELETE', '/api/sessions/' + encodeURIComponent(sid) + '/queue/' + encodeURIComponent(it.id)).then(function (r) {
            X.queue = r.queue; paintQueue();
            if (!comp.ta.value.trim()) { comp.append(it.text); }
          }, function (e) { toast('빼지 못했습니다: ' + e.message); });
        }, '대기열에서 빼고 글은 입력창으로 돌려놓는다(입력창이 비어 있을 때)');
        if (!X.extRun) row.appendChild(go);   // 다른 프로세스의 턴에는 끼워 넣을 수 없어 "바로 넣기" 를 두지 않는다(턴이 끝나면 자동으로 간다)
        row.appendChild(rm); box.appendChild(row);
      });
    }
    function setRunningUi(r) { X.running = r; comp.setRunning(r); list.classList.toggle('running', !!r); paintQueue(); if (X.reports) refreshReports(); }
    var jump = btn('↓ 마지막 턴', 'small jump-last', function () {
      var ids = X.data ? X.data.turns : [];
      var last = ids.length && X.turnEls[ids[ids.length - 1].id];
      if (last) last.scrollIntoView({ block: 'start', behavior: 'smooth' });
    });
    jump.hidden = true; document.body.appendChild(jump);
    function onScroll() { jump.hidden = !X.data || X.data.turns.length < 2 || (window.innerHeight + window.scrollY > document.body.scrollHeight - 400); }
    window.addEventListener('scroll', onScroll, { passive: true });
    S.cleanup.push(function () { jump.remove(); window.removeEventListener('scroll', onScroll); });

    function load(first) {
      var t0 = performance.now();
      var requestedContextSeq = contextSeq;
      var url = '/api/sessions/' + encodeURIComponent(sid);
      // 처음 읽기는 과정(기록 읽기 → 받기 → 화면 그리기)을 보인다. 다시 읽기(복귀·턴 끝·따라 읽기)는 조용히 바꾼다.
      var rp = first ? readProcess(list, rowAgent(sid)) : null;
      return (rp ? rp.fetch(url) : api('GET', url)).then(function (d) {
        if (seq !== S.seq) { if (rp) rp.drop(); return; }
        if (rp) return rp.paint(d, function () { if (seq === S.seq) apply(d); });
        apply(d);
      }, function (e) {
        if (seq !== S.seq) return;
        if (rp) { rp.fail(e, function () { load(first); }); return; }
        // 이미 그린 턴이 있으면 지우지 않는다 — 다음 다시 읽기에서 따라잡는다
        if (X.data && list.querySelector('.turn')) { toast('세션을 다시 읽지 못했습니다: ' + e.message); externalPoll(X.data.turns[X.data.turns.length - 1]); return; }
        list.innerHTML = ''; list.appendChild(el('div', 'empty err', '세션을 읽지 못했습니다: ' + e.message));
        var retry = btn('다시 읽기', null, function () { load(first); }); list.appendChild(retry);
      });
      function apply(d) {
        var prev = X.data, prevJobs = X.jobs, prevComments = JSON.stringify(X.comments);
        if (requestedContextSeq !== contextSeq) d.session.context = latestContext;
        X.data = d;
        X.jobs = {};
        d.jobs.forEach(function (j) { X.jobs[j.id] = j; });
        Object.keys(prevJobs).forEach(function (k) { if (!X.jobs[k] && prevJobs[k].status === 'running' && !prevJobs[k].fork_thread) X.jobs[k] = prevJobs[k]; });
        X.comments = d.comments || [];
        var sameHead = prev && JSON.stringify(prev.session) === JSON.stringify(d.session) && prev.turns.length === d.turns.length;
        if (!sameHead) paintHeader();
        var ids = d.turns.map(function (t) { return t.id; }).join();
        var reuse = !first && prev && prev.turns.length && ids.indexOf(prev.turns.map(function (t) { return t.id; }).join()) === 0;
        if (reuse) {
          // 다시 읽기(복귀·턴 끝)는 바뀐 턴만 다시 그린다 — 쓰던 질문·코멘트·스크롤을 지킨다
          var commentsChanged = prevComments !== JSON.stringify(X.comments);
          d.turns.forEach(function (t) {
            var old = X.turns[t.id], el0 = X.turnEls[t.id];
            var jobsChanged = Object.keys(X.jobs).concat(Object.keys(prevJobs)).some(function (k) {
              var a = X.jobs[k], b = prevJobs[k], j = a || b;
              return j && j.turn_id === t.id && (!a || !b || a.version !== b.version || a.status !== b.status);
            });
            X.turns[t.id] = t;
            if (!el0) { var empty = list.querySelector('.empty'); if (empty) empty.remove(); var added = renderTurn(t); list.appendChild(added); restoreProcessScroll(added, null); }
            else if (commentsChanged || jobsChanged || JSON.stringify(old) !== JSON.stringify(t)) refreshTurn(t.id);
          });
        } else {
          list.innerHTML = '';
          X.turnEls = {};
          if (!d.turns.length) list.appendChild(el('div', 'empty', '아직 턴이 없습니다'));
          d.turns.forEach(function (t) { X.turns[t.id] = t; var e = renderTurn(t); list.appendChild(e); restoreProcessScroll(e, null); });
        }
        var last = d.turns[d.turns.length - 1];
        // 다른 곳에서 도는 턴(agent-hub 채팅이거나 CLI·Codex 앱) — 이 앱에서 도는 턴과 같이 바로 넣기·턴 끝나고를 고른다.
        // agent-hub·steward 면 그쪽으로 넣고 멈출 수 있고, 아니면 다른 프로세스의 턴이라 끼워 넣거나 멈추지 못해 대기열에 둔다(서버가 정한다).
        var ext = !!(last && last.status === 'inProgress' && last.external && !last.live);
        X.hubRun = !!(d.hub && d.hub.running) && !(last && last.live);
        X.running = !!(last && last.status === 'inProgress') || X.hubRun;
        list.classList.toggle('running', X.running);
        comp.setRunning((X.running && !!(last && last.live)) || X.hubRun || ext);
        X.extRun = ext && !X.hubRun;   // 이 앱이 쥐지 않은 턴 — 끼워 넣기·멈춤을 못 한다
        comp.setCanStop(!X.extRun);
        comp.setVia(X.hubRun ? (d.hub.via || 'agent-hub') : '');
        comp.setBlocked(d.session.readonly ? '하위 에이전트 기록 — 읽기만 됩니다. 이어서 시키려면 상위 세션에서 말하세요' : null);
        X.queue = d.queue || []; paintQueue();
        refreshReports();
        if (first) restoreScroll(pop);
        onScroll();
        externalPoll(last);
      }
    }
    function paintHeader() {
      var s = X.data.session;
      var up = null;
      if (s.parent) {
        // 세션 안에서 만든 세션 — 만든 세션으로 가는 길
        up = el('a', 'parent-link', '↑ ' + (s.parent_title || '상위 세션'));
        up.href = '#/s/' + encodeURIComponent(s.parent);
        up.title = '이 세션을 만든 세션 열기' + (s.parent_title ? ' — ' + s.parent_title : '');
      }
      var loc = s.location || {}, kind = loc.kind || 'unknown';
      var label = { worktree: '워크트리', repository: '기본 워크트리', folder: '폴더', unknown: '위치 확인 전' }[kind];
      var root = loc.root || s.cwd || '', shortPath = base(root);
      if (kind === 'worktree') shortPath = base(root.replace(/\/$/, '').replace(/\/[^/]+$/, '')) + '/' + shortPath;
      if (s.cwd && s.cwd.indexOf(root + '/') === 0) shortPath += s.cwd.slice(root.length);
      var where = btn(label + ' · ' + shortPath, 'session-location', function () {
        var sh = sheet('실행 위치');
        sh.body.appendChild(el('div', 'sheet-sec', label));
        function pathRow(name, path) {
          if (!path) return;
          var row = el('div', 'location-row');
          row.appendChild(el('div', 'sheet-sec', name));
          row.appendChild(el('code', 'location-path', path));
          row.appendChild(btn('복사', 'small', function () { copyText(path); }, name + ' 복사'));
          sh.body.appendChild(row);
        }
        pathRow('작업 폴더', s.cwd);
        pathRow('기본 워크트리', loc.repo);
      }, '실행 위치 자세히 보기 — ' + (s.cwd || '경로 확인 전'));
      where.setAttribute('aria-label', '실행 위치: ' + label + ' · ' + shortPath);
      var branch = s.branch ? el('span', 'session-branch', s.branch) : '';
      if (branch) branch.title = '세션 기록의 브랜치';
      setHeader(s.title, [up, s.archived ? '보관됨' : '', where, branch, s.model ? s.model + (s.effort ? ' · ' + s.effort : '') : '', X.data.turns.length + '턴'], true);
      subEl.title = '';
      sidEl.textContent = '세션 ' + sid; sidEl.dataset.id = sid; sidEl.hidden = false;
      paintContext();
      actionsEl.appendChild(btn('파일', 'small', function () { go(filesUrl(s.cwd, '', sid)); }, '작업 폴더 탐색기 — .md 문서에 코멘트를 남기고 AI 로 다듬는다'));
      actionsEl.appendChild(archiveButton(s));
      var ports = X.data.ports || [];
      if (ports.length) {
        actionsEl.appendChild(btn('포트 ' + ports.length, 'small', function () { go(portsUrl({ thread: sid, from: sid })); },
          '이 세션이 띄운 서버 — ' + ports.map(function (p) { return ':' + p.port + ' ' + p.name; }).join(', ')));
      }
      comp.repaintChip();
    }
    X.archiveChanged = function (archived) {
      if (!X.data) return;
      X.data.session.archived = archived;
      paintHeader();
    };
    function paintContext() {
      var chip = document.getElementById('session-context'), c = X.data.session.context;
      function K(n) { return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'K'; }
      chip.hidden = false;
      chip.className = 'context-chip' + (c && c.percent >= 90 ? ' crit' : c && c.percent >= 80 ? ' warn' : '');
      chip.textContent = c ? '컨텍스트 ' + (c.window ? Math.round(c.percent) + '% · 최대 ' + K(c.window) : K(c.tokens) + ' · 최대 확인 전') : '컨텍스트 확인 전';
      chip.title = c ? '마지막 API 호출 기준 · 사용 ' + c.tokens.toLocaleString() + ' 토큰' + (c.window ? ' / 최대 ' + c.window.toLocaleString() + ' 토큰' : ' · 최대 용량이 아직 보고되지 않았습니다') : '다음 API 호출에서 사용량이 보고되면 표시됩니다';
    }
    // 이 세션이 띄운 서버가 열리거나 꺼지면 머리의 "포트 N" 을 다시 그린다
    function reloadPorts() {
      api('GET', '/api/ports?thread=' + encodeURIComponent(sid)).then(function (r) {
        if (seq !== S.seq || !X.data) return;
        X.data.ports = (r.items || []).filter(portRunning);
        paintHeader();
      }, function () {});
    }
    // 이 앱 밖(CLI 등)에서 돌고 있는 세션은 알림이 오지 않으니 읽어서 따라간다
    var pollTimer = null;
    function externalPoll(last) {
      clearTimeout(pollTimer);
      if (X.data && X.data.history_pending) pollTimer = setTimeout(function () { load(false); }, 1000);
      else if (X.hubRun) pollTimer = setTimeout(function () { load(false); }, 3000);
      else if (last && last.status === 'inProgress' && !last.live) pollTimer = setTimeout(function () { load(false); }, 5000);
      // agent-hub 채팅으로 이어지는 세션 — 거기서 새 턴이 시작되면 알림이 없으니 느리게 따라 읽는다
      else if (X.data && X.data.hub) pollTimer = setTimeout(function () { load(false); }, 10000);
    }
    S.cleanup.push(function () { clearTimeout(pollTimer); });

    // 실시간
    var es = null, reloadTimer = null, esOpened = false;
    function connect() {
      es = new EventSource('/api/sessions/' + encodeURIComponent(sid) + '/events');
      // 앞단 프록시가 스트림을 주기적으로 끊으면 브라우저가 다시 붙는다 — 그 사이 놓친 턴·코멘트·대기열을 다시 읽는다
      es.onopen = function () { if (esOpened) { load(false); reloadComments(); } esOpened = true; };
      es.onmessage = function (m) {
        var ev; try { ev = JSON.parse(m.data); } catch (e) { return; }
        if (ev.type === 'live') onLive(ev.turn);
        else if (ev.type === 'context') {
          contextSeq++; latestContext = ev.context;
          if (X.data) { X.data.session.context = ev.context; paintContext(); }
        }
        else if (ev.type === 'queue') { X.queue = ev.queue || []; paintQueue(); }
        else if (ev.type === 'ports') reloadPorts();
        else if (ev.type === 'session_changed') { load(false); if (LIST) LIST.load(true); }
        else if (ev.type === 'turn_done') { setRunningUi(false); clearTimeout(reloadTimer); reloadTimer = setTimeout(function () { load(false); }, 400); }
        else if (ev.type === 'job') onJob(ev.job);
        else if (ev.type === 'job_removed') { delete X.jobs[ev.id]; redecorateAll(); }
        else if (ev.type === 'comments') reloadComments();
      };
    }
    connect();
    function onVis() { if (document.visibilityState === 'visible') { load(false); reloadComments(); } }
    document.addEventListener('visibilitychange', onVis);
    S.cleanup.push(function () { if (es) es.close(); clearTimeout(reloadTimer); document.removeEventListener('visibilitychange', onVis); });
    function onLive(t) {
      if (!X.data || !t.id) return;
      var old = X.turnEls[t.id];
      if (!old) {
        t.index = X.data.turns.length + 1;
        X.data.turns.push(t);
        var empty = list.querySelector('.empty'); if (empty) empty.remove();
      } else {
        t.index = X.turns[t.id] ? X.turns[t.id].index : t.index;
        X.data.turns = X.data.turns.map(function (x) { return x.id === t.id ? t : x; });
      }
      X.turns[t.id] = t;
      var pos = processScroll(old), ne = renderTurn(t);
      if (old) old.replaceWith(ne); else list.appendChild(ne);
      restoreProcessScroll(ne, pos);
      var live = t.status === 'inProgress';
      if (live !== X.running) setRunningUi(live);
      if (!old && live) ne.scrollIntoView({ block: 'start', behavior: 'smooth' });
    }
    var liveTick = setInterval(function () {
      document.querySelectorAll('[data-live-since]').forEach(paintLiveStatus);
      document.querySelectorAll('[data-speed-expires]').forEach(paintTokenSpeed);
      document.querySelectorAll('.iq-card[data-running]').forEach(function (c) { var j = X.jobs[c.dataset.jid]; if (j) paintProgress(c, j); });
    }, 1000);
    S.cleanup.push(function () { clearInterval(liveTick); });

    // ------------------------------------------------ 턴 그리기
    // Claude Code 백그라운드 작업 알림(사람이 보낸 요청이 아니다) — 한 줄 요약 + 접힌 결과
    var NOTICE_STATUS = { completed: ['완료', 'dim'], failed: ['실패', 'bad'], killed: ['중지', 'bad'], stopped: ['중지', 'bad'] };
    function renderNotices(box, list) {
      list.forEach(function (n) {
        var row = el('div', 'notice-row');
        var stt = NOTICE_STATUS[n.status] || (n.status ? [n.status, 'dim'] : null);
        if (stt) row.appendChild(el('span', 'badge ' + stt[1], stt[0]));
        row.appendChild(el('span', 'notice-sum', n.summary || '백그라운드 작업 알림'));
        box.appendChild(row);
        if (n.result) {
          var d = el('details', 'req-ctx'); d.appendChild(el('summary', null, '결과'));
          var m = el('div', 'md'); m.innerHTML = md.render(n.result); d.appendChild(m); box.appendChild(d);
        }
      });
    }
    function renderTurn(t) {
      var sec = el('section', 'turn'); sec.dataset.turn = t.id; sec.id = 'turn-' + t.id;
      X.turnEls[t.id] = sec;
      var meta = el('div', 'turn-meta');
      meta.appendChild(el('span', null, '턴 ' + (t.index || '')));
      if (t.started_at) meta.appendChild(el('span', null, fmtTime(t.started_at)));
      if (t.duration_ms) meta.appendChild(el('span', null, dur(t.duration_ms)));
      // 멈춤·실패·끝나지 않음은 답변 자리의 안내 줄이 알린다(머리에 또 적지 않는다)
      var halted = t.status === 'interrupted' || t.status === 'failed' || t.status === 'unfinished';
      if (t.status && t.status !== 'completed' && t.status !== 'inProgress' && !halted) meta.appendChild(el('span', 'badge dim', t.status === 'unfinished' ? '끝나지 않음' : t.status));
      sec.appendChild(meta);
      // 요청
      var req = el('div', 'part req'); req.appendChild(el('div', 'part-label', t.request.notice ? '알림' : '요청'));
      var rb = el('div', 'req-body');
      if (t.request.notice) { rb.classList.add('req-notice'); renderNotices(rb, t.request.notice); }
      var rtext = t.request.notice ? '' : (t.request.text || ''), ctx = '';
      var mi = rtext.indexOf(MENTION_MARK);
      if (mi >= 0) { ctx = rtext.slice(mi + MENTION_MARK.length).trim(); rtext = rtext.slice(0, mi).trim(); }
      if (!t.request.notice) { var rmd = el('div', 'md'); rmd.innerHTML = md.render(rtext || (t.request.images.length ? '' : '(요청 글 없음)')); rb.appendChild(rmd); }
      requestImages(rb, t.request.images, X.data.session.cwd);
      if (ctx) { var d = el('details', 'req-ctx'); d.appendChild(el('summary', null, '멘션 맥락')); d.appendChild(el('pre', null, ctx)); rb.appendChild(d); }
      req.appendChild(rb);
      // 진행 중에 끼워 넣은 말(바로 넣기) — 요청 아래에 넣은 차례대로
      (t.request.steers || []).forEach(function (st) {
        var sb = el('div', 'req-body req-steer' + (st.pending ? ' pending' : ''));
        if (st.notice) { sb.classList.add('req-notice'); sb.appendChild(el('div', 'steer-label', '진행 중 알림')); renderNotices(sb, st.notice); req.appendChild(sb); return; }
        sb.appendChild(el('div', 'steer-label', st.pending ? '진행 중인 턴에 넣는 중…' : '진행 중에 넣음'));
        var stext = (st.text || '').split(MENTION_MARK)[0].trim();
        var smd = el('div', 'md'); smd.innerHTML = md.render(stext || (st.images && st.images.length ? '' : '(글 없음)')); sb.appendChild(smd);
        requestImages(sb, st.images, X.data.session.cwd);
        req.appendChild(sb);
      });
      sec.appendChild(req);
      // 과정
      sec.appendChild(renderProcess(t));
      // 답변
      var ans = el('div', 'part ans');
      if (!halted || (t.answer && t.answer.text)) ans.appendChild(el('div', 'part-label', '답변'));
      if (t.answer && t.answer.text) {
        var a = el('div', 'md annot answer'); a.dataset.kind = 'session'; a.dataset.turn = t.id; a.dataset.part = 'answer';
        a.dataset.done = t.status === 'inProgress' ? '0' : '1';
        a.innerHTML = md.render(t.answer.text);
        ans.appendChild(a);
        if (t.answer.report) ans.appendChild(renderReport(t));
        var foot = el('div', 'ans-foot');
        foot.appendChild(btn('원문 복사', null, function () { copyText(t.answer.text); }));
        ans.appendChild(foot);
      } else if (t.status !== 'inProgress' && !halted) {
        ans.appendChild(el('div', 'muted', '답변 없음'));
      }
      if (halted) ans.appendChild(haltRow(t));
      else if (t.error) ans.appendChild(el('div', 'turn-err', t.error));
      if ((t.answer && t.answer.text) || t.status !== 'inProgress' || t.error) sec.appendChild(ans);
      decorateTurn(t.id);
      return sec;
    }
    // ------------------------------------------------ 최종 출력 카드(agent-output/1 의 정할 것 · 메인 반영 · 할 일 — 사용자가 손댈 것부터)
    // 고른 답·직접 입력·보낸 기록은 턴마다 localStorage 에 둔다 — 다시 그려도(5초 폴링·복귀) 그대로다.
    // 보내기는 마지막 턴이 끝난 뒤에만 된다. 같은 client_id 로 다시 보내 중복 턴을 막는다.
    function reportBlocked(t) {
      var turns = X.data ? X.data.turns : [], last = turns[turns.length - 1];
      if (!last || last.id !== t.id) return '다음 턴이 이미 있어 여기서는 보낼 수 없습니다';
      if (X.running || t.status === 'inProgress') return '응답 중 — 턴이 끝나면 보낼 수 있습니다';
      return '';
    }
    function renderReport(t) {
      var r = t.answer.report, key = 'out.' + sid + '.' + t.id;
      var st = lsGet(key, {}) || {};
      st.dec = st.dec || {};
      function save() { lsSet(key, st); }
      var box = el('div', 'out-report');
      var controls = [], status = el('div', 'out-status'), busy = false, err = '';
      status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
      function section(kind, title, count) {
        var s = el('section', 'out-sec'); s.dataset.kind = kind;
        var h = el('div', 'out-head'); h.appendChild(el('b', null, title));
        if (count > 1) h.appendChild(el('span', 'muted', String(count)));
        s.appendChild(h); box.appendChild(s); return s;
      }
      function send(kind, text) {
        if (busy || st.sent || reportBlocked(t)) return;
        if (!st.pending || st.pending.text !== text) st.pending = { text: text, client_id: uuid4(), kind: kind };
        save(); busy = true; err = ''; refresh();
        var t0 = performance.now();
        api('POST', '/api/sessions/' + encodeURIComponent(sid) + '/turns', { text: text, client_id: st.pending.client_id, comment_ids: [],
          model: pickState.model || null, effort: pickState.effort || null }).then(function () {
          st.sent = { kind: kind, text: text, at: Date.now() }; delete st.pending; save();
          busy = false; setRunningUi(true); refresh();
          clientLog({ type: 'output_action', stage: kind, ms: Math.round(performance.now() - t0) });
          toast(kind === 'merge' ? '메인 반영을 요청했습니다' : '답을 보냈습니다');
          setTimeout(function () { load(); }, 400);
        }, function (e) {
          busy = false; err = '보내지 못했습니다: ' + e.message + ' — 고른 답은 그대로 있습니다'; refresh();
          clientLog({ type: 'output_action', stage: 'failed', ms: Math.round(performance.now() - t0), error: e.message });
        });
      }
      // 정할 것 — 여러 개 고르기와 직접 입력을 모든 질문에서 받는다. 답한 질문만 모아 한 번에 보낸다.
      var ansBtn = null;
      function decisionText() {
        var lines = [], n = 0;
        r.decisions.forEach(function (d, i) {
          var a = st.dec[d.id] || { sel: [], text: '' };
          var parts = (a.sel || []).filter(function (l) { return d.options.some(function (o) { return o.label === l; }); });
          if ((a.text || '').trim()) parts.push('직접 입력: ' + a.text.trim());
          lines.push((i + 1) + '. ' + d.question + '\n답: ' + (parts.length ? parts.join(' / ') : '(이번에는 정하지 않음)'));
          if (parts.length) n++;
        });
        return n ? '정할 것에 답합니다.\n\n' + lines.join('\n\n') : '';
      }
      if (r.decisions.length) {
        var ds = section('decisions', '정할 것', r.decisions.length);
        r.decisions.forEach(function (d) {
          var a = st.dec[d.id] = st.dec[d.id] || { sel: [], text: '' };
          var q = el('div', 'out-item out-q');
          var qh = el('div', 'out-line');
          if (d.header) qh.appendChild(el('span', 'badge dim', d.header));
          qh.appendChild(el('b', 'grow', d.question));
          q.appendChild(qh);
          d.options.forEach(function (o) {
            var lab = el('label', 'out-opt');
            var cb = el('input'); cb.type = 'checkbox'; cb.checked = a.sel.indexOf(o.label) >= 0;
            cb.addEventListener('change', function () {
              a.sel = a.sel.filter(function (x) { return x !== o.label; });
              if (cb.checked) a.sel.push(o.label);
              lab.classList.toggle('on', cb.checked); save(); refresh();
            });
            lab.classList.toggle('on', cb.checked);
            controls.push(cb); lab.appendChild(cb);
            var txt = el('span', 'out-opt-text');
            var nm = el('span', 'out-opt-label', o.label); txt.appendChild(nm);
            if (o.recommended) txt.appendChild(el('span', 'badge run', '추천'));
            if (o.description) txt.appendChild(el('span', 'out-desc', o.description));
            lab.appendChild(txt); q.appendChild(lab);
          });
          var inp = el('input', 'out-free'); inp.placeholder = '직접 입력(선택과 함께 보냄)'; inp.value = a.text || '';
          inp.setAttribute('aria-label', d.question + ' — 직접 입력');
          inp.addEventListener('input', function () { a.text = inp.value; save(); refresh(); });
          inp.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.isComposing && ansBtn && !ansBtn.disabled) { e.preventDefault(); ansBtn.click(); } });
          controls.push(inp); q.appendChild(inp);
          ds.appendChild(q);
        });
        var row = el('div', 'out-acts');
        ansBtn = btn('답 보내기', 'primary', function () { var x = decisionText(); if (x) send('decision', x); });
        row.appendChild(ansBtn); ds.appendChild(row);
      }
      // 메인 반영 — 누르면 같은 세션에 "메인 반영해 줘"를 보낸다(규칙상 사용자가 그 세션에서 명시해야 하는 말)
      var mergeBtn = null;
      if (r.merge) {
        var m = r.merge, ms = section('merge', '메인 반영 요청', 0);
        var meta = [m.version ? 'v' + String(m.version).replace(/^v/, '') : '', m.bump].filter(Boolean).join(' · ');
        if (meta) ms.querySelector('.out-head').appendChild(el('span', 'badge dim', meta));
        // 요약 앞머리의 버전은 머리글 배지와 겹치니 뗀다
        var summ = m.version ? m.summary.replace(new RegExp('^v?' + String(m.version).replace(/^v/, '').replace(/[.]/g, '\\.') + '\\s*[—–:-]?\\s*'), '') : m.summary;
        if (summ) { var sm = el('div', 'md out-desc'); sm.innerHTML = md.render(summ); ms.appendChild(sm); }
        if (m.checks.length) {
          var ck = el('details', 'out-checks'); ck.appendChild(el('summary', null, '끝낸 확인 ' + m.checks.length));
          var ul = el('ul'); m.checks.forEach(function (c) { ul.appendChild(el('li', null, c)); }); ck.appendChild(ul);
          ms.appendChild(ck);
        }
        var mr = el('div', 'out-acts');
        mergeBtn = btn('메인 반영하기', 'primary', function () {
          send('merge', '메인 반영해 줘' + (meta ? ' (' + meta + ')' : '') + '.');
        }, '이 세션에 "메인 반영해 줘"를 보낸다 — 커밋 → main 병합(태그·CHANGELOG) → 배포');
        mr.appendChild(mergeBtn); ms.appendChild(mr);
      }
      // 할 일 — 에이전트 몫은 입력창에 넣어 이어 맡기고, 사용자 몫은 복사해 둔다
      if (r.todos.length) {
        var ts = section('todo', '남은 할 일', r.todos.length);
        r.todos.forEach(function (d) {
          var row = el('div', 'out-item');
          var top = el('div', 'out-line');
          top.appendChild(el('span', 'badge ' + (d.owner === 'user' ? 'cm' : 'dim'), d.owner === 'user' ? '직접' : '에이전트'));
          top.appendChild(el('b', 'grow', d.title));
          row.appendChild(top);
          if (d.detail) row.appendChild(el('div', 'out-desc', d.detail));
          if (d.project) row.appendChild(el('div', 'out-desc muted', '프로젝트 · ' + d.project));
          var text = [d.title, d.detail, d.project ? '프로젝트: ' + d.project : ''].filter(Boolean).join('\n\n');
          var act = el('div', 'out-acts');
          act.appendChild(btn('복사', 'small', function () { copyText(text); }));
          if (d.owner !== 'user') act.appendChild(btn('입력창에 넣기', 'small', function () { comp.append('이어서 해 줘: ' + text); }));
          row.appendChild(act);
          ts.appendChild(row);
        });
      restoreProcessScroll(ne, pos);
      }
      box.appendChild(status);
      function refresh() {
        var why = reportBlocked(t);
        var lock = busy || !!st.sent || !!why;
        controls.forEach(function (c) { c.disabled = lock; });
        if (ansBtn) { ansBtn.disabled = lock || !decisionText(); ansBtn.textContent = busy && st.pending && st.pending.kind === 'decision' ? '보내는 중…' : '답 보내기'; }
        if (mergeBtn) { mergeBtn.disabled = lock; mergeBtn.textContent = busy && st.pending && st.pending.kind === 'merge' ? '보내는 중…' : '메인 반영하기'; }
        var msg = busy ? '보내는 중…' : err || (st.sent ? (st.sent.kind === 'merge' ? '메인 반영을 요청함' : '답을 보냄') + ' · ' + fmtTime(st.sent.at / 1000) : why);
        status.textContent = msg || '';
        status.className = 'out-status' + (err ? ' err' : '');
        status.hidden = !msg;
      }
      refresh();
      X.reports[t.id] = refresh;
      return box;
    }
    function refreshReports() {
      Object.keys(X.reports).forEach(function (k) {
        var f = X.reports[k];
        if (!X.turnEls[k] || !X.turnEls[k].isConnected) { delete X.reports[k]; return; }
        f();
      });
    }
    // 멈춘·실패한 턴: 하던 중의 말은 과정에 두고, 여기서는 무슨 일이 있었는지와 이어 갈 단추만 보인다.
    // "이어서 하기"는 마지막 턴이고 세션이 쉬고 있을 때만 보인다(CSS — 뒤에 턴이 붙거나 응답 중이면 숨는다).
    var RESUME_TEXT = '중간에 멈춘 곳에서 이어서 진행해 주세요.';
    // 끝나지 않음(끝 기록 없이 끊긴 턴)의 까닭 — 서버가 cut 으로 준다(Codex 는 없음)
    var CUT_TEXT = { process: '이 세션을 돌리던 프로세스가 꺼졌습니다', quiet: '5분 넘게 기록이 없습니다' };
    function haltRow(t) {
      var stopped = t.status === 'interrupted', cut = t.status === 'unfinished';
      var row = el('div', 'turn-halt' + (stopped || cut ? '' : ' failed'));
      var msg = el('div', 'halt-msg');
      msg.appendChild(el('b', null, stopped ? '사용자가 중간에 멈췄습니다' : cut ? '끝나지 않고 멈췄습니다' : '중간에 실패했습니다'));
      if (cut) msg.appendChild(el('span', 'halt-err', CUT_TEXT[t.cut] || '끝 기록 없이 끊겼습니다(프로세스가 꺼졌거나 오래 소식이 없음)'));
      else if (!stopped && t.error) msg.appendChild(el('span', 'halt-err', t.error));
      row.appendChild(msg);
      var go = btn('이어서 하기', 'small primary resume-btn', function () {
        go.disabled = true; go.textContent = '보내는 중…';
        var p = pickState || {};
        api('POST', '/api/sessions/' + encodeURIComponent(sid) + '/turns', { text: RESUME_TEXT, images: [], mentions: [],
          model: p.model || null, effort: p.effort || null, tier: p.tier || null, client_id: uuid4(), comment_ids: [] }).then(function (r) {
          setRunningUi(true);
          if (r.queued) toast('다음 턴으로 보냅니다');
        }, function (e) { go.disabled = false; go.textContent = '이어서 하기'; toast('보내지 못했습니다: ' + e.message); });
      }, '"' + RESUME_TEXT + '"로 다음 턴을 연다 — 입력창 글은 건드리지 않는다');
      row.appendChild(go);
      return row;
    }
    function renderProcess(t) {
      var p = el('div', 'part proc');
      var open = !!ui.open[t.id];
      if (open) p.classList.add('open');
      var head = el('div', 'proc-head');
      var tg = btn('', 'proc-toggle'); tg.setAttribute('aria-expanded', open ? 'true' : 'false');
      tg.appendChild(el('span', 'chev', '›'));
      tg.appendChild(el('b', null, '과정'));
      if (t.status === 'inProgress') {
        var spin = el('span', 'proc-spinner');
        spin.setAttribute('role', 'status'); spin.setAttribute('aria-label', '진행 중');
        tg.appendChild(spin);
      }
      var measured = t.rate && t.rate.measured && Number.isFinite(t.rate.tps) && t.rate.tps > 0;
      if (measured) {
        // Codex: 기록의 실제 출력 토큰 ÷ 응답 시간. 진행 중이면 마지막 응답, 끝났으면 턴 평균.
        var live = t.status === 'inProgress', rt = t.rate;
        var shown = live ? rt.tps : (rt.avg_tps || rt.tps);
        var mr = el('span', 'proc-speed measured', (live ? '' : '평균 ') + Math.round(shown) + ' tok/s');
        mr.title = '마지막 응답: 출력 ' + rt.out + '토큰 ÷ ' + rt.sec.toFixed(1) + '초' +
          (rt.avg_tps ? ' · 이 턴 평균 ' + Math.round(rt.avg_tps) + ' tok/s(응답 ' + rt.responses + '회, 출력 ' + rt.total_out + '토큰)' : '') +
          '\n실제 출력 토큰 수(추론 포함)를 응답 시간으로 나눈 값입니다. 응답 시간은 요청을 보낸 때부터 모델의 마지막 출력까지라 첫 토큰 대기는 들어가고 도구 실행은 빠집니다.';
        tg.appendChild(mr);
      }
      if (t.status === 'inProgress' && !measured) {
        var speed = t.live && t.live.speed;
        if (speed && speed.estimated && Number.isFinite(speed.tps) && speed.tps > 0) {
          var rate = el('span', 'proc-speed', '추정 ' + Math.round(speed.tps) + ' tok/s');
          rate.dataset.speedExpires = speed.expires_at;
          rate.title = '최근 약 3초의 텍스트 수신 속도입니다. UTF-8 4바이트를 1토큰으로 추정하며 언어·모델·네트워크에 따라 실제 생성 속도와 다릅니다. 도구 출력은 제외합니다.';
          paintTokenSpeed(rate); tg.appendChild(rate);
        }
      }
      var c = t.counts || {}, parts = [];
      ['command', 'file_change', 'tool', 'reasoning', 'message', 'web_search'].forEach(function (k) { if (c[k]) parts.push(KIND[k] + ' ' + c[k]); });
      var total = t.process.length;
      tg.appendChild(el('span', 'counts', total ? parts.join(' · ') || ('항목 ' + total) : '도구 사용 없음'));
      head.appendChild(tg);
      var done = t.status !== 'inProgress';
      var sumJob = summaryJob(t.id);
      if (done && total && !sumJob && !X.data.session.readonly) {
        var sb = btn('과정 요약', 'small sum-btn', function () { startSummary(t); }, '이 턴의 과정을 같은 모델로 포크해 쉽게 정리한다(캐시 재사용)');
        var se = btn(sumEffortOf(t.id) + ' ▾', 'small sum-eff', function () {
          effortSheet(sumEffortOf(t.id), function (e) { X.sumEff[t.id] = e; refreshTurn(t.id); });
        }, '과정 요약 effort 고르기 — 기본값은 모델 관리에서');
        se.setAttribute('aria-haspopup', 'dialog');
        head.appendChild(sb); head.appendChild(se);
      }
      p.appendChild(head);
      var slot = el('div', 'summary-slot'); p.appendChild(slot);
      if (t.status === 'inProgress' && (t.live || t.external)) {
        // 이 앱 밖에서 도는 턴은 기록이 마지막으로 바뀐 때를 마지막 신호로 본다(5초마다 다시 읽는다)
        var ls = el('div', 'live-status'); ls.dataset.liveSince = t.live ? t.live.since : (t.started_at || t.external.last_signal);
        ls.dataset.lastSignal = t.live ? t.live.last_signal : t.external.last_signal;
        if (t.quiet_until) ls.dataset.quietUntil = t.quiet_until;
        ls.dataset.stage = liveStage(t);
        ls.title = ls.dataset.stage;   // 좁은 화면에서 잘린 단계를 길게 눌러 본다
        paintLiveStatus(ls); p.appendChild(ls);
      }
      var steps = el('div', 'steps'); steps.hidden = !open; p.appendChild(steps);
      if (open) fillSteps(steps, t);
      tg.addEventListener('click', function () {
        var o = !p.classList.contains('open');
        p.classList.toggle('open', o); tg.setAttribute('aria-expanded', o ? 'true' : 'false');
        steps.hidden = !o;
        if (o && !steps.children.length) fillSteps(steps, t);
        if (o) steps.scrollTop = steps.scrollHeight;
        if (o) ui.open[t.id] = 1; else delete ui.open[t.id];
        saveUi();
      });
      return p;
    }
    function fillSteps(box, t) {
      t.process.forEach(function (s) { box.appendChild(renderStep(s)); });
      if (!t.process.length) box.appendChild(el('div', 'muted', '도구 호출이나 중간 단계가 없습니다'));
    }
    function processScroll(turn) {
      var box = turn && turn.querySelector('.steps:not([hidden])');
      if (!box) return null;
      return { top: box.scrollTop, bottom: box.scrollHeight - box.clientHeight - box.scrollTop <= 8 };
    }
    function restoreProcessScroll(turn, pos) {
      var box = turn.querySelector('.steps:not([hidden])');
      if (box) box.scrollTop = !pos || pos.bottom ? box.scrollHeight : pos.top;
    }
    function renderStep(s) {
      var d = el('details', 'step');
      var sm = el('summary');
      if (s.status === 'inProgress') {
        var sp = el('span', 'proc-spinner step-spinner'); sp.setAttribute('role', 'status'); sp.setAttribute('aria-label', '진행 중');
        sm.appendChild(sp);
      }
      sm.appendChild(el('span', 'skind', KIND[s.kind] || s.kind));
      var title = s.kind === 'reasoning' ? (s.private ? '(비공개 추론)' : (s.detail || '').replace(/\*\*/g, '').split('\n')[0]) :
        s.kind === 'message' ? (s.detail || '').split('\n')[0] : s.title;
      sm.appendChild(el('span', 'st', title || s.title));
      var x = [];
      if (s.kind === 'command' && s.exit_code != null && s.exit_code !== 0) x.push('exit ' + s.exit_code);
      if (s.kind === 'agent' && s.model) x.push(s.model + (s.effort ? ' ' + s.effort : ''));
      if (s.ms) x.push(dur(s.ms));
      if (s.status === 'inProgress') x.push(s.progress && s.progress.at ? '진행 중 · ' + ago(s.progress.at) : '진행 중');
      var sx = el('span', 'sx' + (s.exit_code ? ' bad' : ''), x.join(' · ')); sm.appendChild(sx);
      // 하위 에이전트가 하나면 머리에서 바로 그 세션으로 간다(여럿이면 펼친 본문의 행마다)
      var subs = s.kind === 'agent' ? (s.agents || []).filter(function (a) { return a.thread; }) : [];
      if (subs.length === 1) {
        var go1 = el('a', 'step-open', '세션 ›');
        go1.href = '#/s/' + encodeURIComponent(subs[0].thread);
        go1.title = '하위 세션 열기 — ' + (subs[0].path || subs[0].name || subs[0].thread);
        go1.addEventListener('click', function (e) { e.stopPropagation(); });
        sm.appendChild(go1);
      }
      d.appendChild(sm);
      d.addEventListener('toggle', function () {
        if (!d.open || d.dataset.filled) return;
        d.dataset.filled = '1';
        var b = el('div', 'step-body');
        if (s.kind === 'command') b.appendChild(el('pre', null, '$ ' + s.command + (s.detail ? '\n\n' + s.detail : '')));
        else if (s.kind === 'file_change') (s.files || []).forEach(function (f) { b.appendChild(el('div', 'diff-file', (f.kind || '') + ' ' + f.path)); if (f.diff) b.appendChild(el('pre', null, f.diff)); });
        else if (s.kind === 'reasoning' || s.kind === 'message' || s.kind === 'plan') { var m = el('div', 'md'); m.innerHTML = md.render(s.detail || '(내용 없음)'); b.appendChild(m); }
        else if (s.kind === 'agent') agentBody(b, s);
        else { if (s.arguments) b.appendChild(el('pre', null, s.arguments)); if (s.detail) b.appendChild(el('pre', null, s.detail)); if (!s.arguments && !s.detail) b.appendChild(el('div', 'muted', s.title)); }
        d.appendChild(b);
      });
      return d;
    }

    // 하위 에이전트 단계 — 대상마다 이름·상태·마지막 말, 그리고 그 하위 세션을 읽어 지금 하는 일을 보인다.
    // Codex 가 이 항목에 주는 것은 대상·상태뿐이고(보낸 메시지는 암호화돼 있다) 실제 일은 하위 세션 기록에 있다.
    function agentBody(b, s) {
      if (s.detail) b.appendChild(el('pre', null, s.detail));
      var list = s.agents || [];
      if (!list.length) b.appendChild(el('div', 'muted', '대상 하위 에이전트를 Codex 가 알려 주지 않았습니다'));
      list.forEach(function (a) {
        var row = el('div', 'agent-row');
        var head = el('div', 'agent-head');
        var nm = el('span', 'agent-name', a.name); if (a.path) nm.title = a.path; head.appendChild(nm);
        if (a.status) head.appendChild(el('span', 'badge ' + (a.status === 'errored' ? 'bad' : a.status === 'running' ? 'run' : 'dim'), AGENT_STATE[a.status] || a.status));
        if (a.thread) head.appendChild(btn('하위 세션 열기', 'small link', function () { go('#/s/' + encodeURIComponent(a.thread)); }, a.path || a.thread));
        row.appendChild(head);
        if (a.message) { var m = el('div', 'md'); m.innerHTML = md.render(a.message); row.appendChild(m); }
        if (a.thread) { var peek = el('div', 'agent-peek muted', '하위 세션 읽는 중…'); row.appendChild(peek); agentPeek(peek, a.thread); }
        b.appendChild(row);
      });
    }
    function agentPeek(box, thread) {
      api('GET', '/api/sessions/' + encodeURIComponent(thread), undefined, true).then(function (d) {
        var ts = d.turns || [], t = ts[ts.length - 1];
        box.textContent = '';
        if (!t) { box.textContent = '아직 기록이 없습니다'; return; }
        var st = t.status === 'inProgress' ? '진행 중' : t.status === 'completed' ? '끝남' : t.status === 'interrupted' ? '멈춤' : t.status === 'failed' ? '실패' : (t.status || '');
        var c = t.counts || {}, n = (t.process || []).length;
        var meta = [st, n + '단계' + (c.command ? ' · 명령 ' + c.command : '') + (c.file_change ? ' · 파일 ' + c.file_change : '')];
        if (d.session && d.session.updated_at) meta.push(ago(d.session.updated_at));
        box.appendChild(el('div', 'agent-peek-meta', meta.join(' · ')));
        var said = t.answer ? t.answer.text : '';
        if (!said) { var ms = (t.process || []).filter(function (x) { return x.kind === 'message'; }); if (ms.length) said = ms[ms.length - 1].detail; }
        if (said) { var m = el('div', 'md'); m.innerHTML = md.render(said); box.appendChild(m); }
        var last = (t.process || [])[n - 1];
        if (t.status === 'inProgress' && last && last.kind !== 'message') box.appendChild(el('div', 'agent-peek-meta', '지금 · ' + (KIND[last.kind] || last.kind) + ' · ' + (last.kind === 'reasoning' ? '생각하는 중' : last.title)));
      }, function (e) { box.textContent = '하위 세션을 읽지 못했습니다 — ' + e.message; });
    }

    // ------------------------------------------------ 과정 요약
    function summaryJob(turnId) {
      var js = Object.keys(X.jobs).map(function (k) { return X.jobs[k]; }).filter(function (j) { return j.kind === 'summary' && j.turn_id === turnId; });
      js.sort(function (a, b) { return a.created_at - b.created_at; });
      return js[js.length - 1] || null;
    }
    // 포크 작업의 기본 effort(모델 관리, 세션 에이전트 · 세션 모델 기준). 목록을 아직 못 읽었으면 읽은 뒤 다시 그린다.
    function forkDefault(kind) {
      if (!modelD && !X.mdWait) {
        X.mdWait = true;
        modelData().then(function () { if (S.X === X) redecorateAll(); }, function () {});
      }
      return forkEffort(modelD, kind, X.data.session.agent, X.data.session.model);
    }
    // 과정 요약 effort — 이 화면에서 턴마다 고른 값, 없으면 기본값
    function sumEffortOf(turnId) { return X.sumEff[turnId] || forkDefault('summary'); }
    function startSummary(t) {
      var req = (t.request.text || '').split(MENTION_MARK)[0].trim();
      var eff = X.sumEff[t.id] || '';
      var body = { turn_id: t.id, request: req, effort: eff || null, client_id: uuid4().replace(/-/g, '').slice(0, 16) };
      // 1초 안에 진행 카드로 바꾼다
      X.jobs[body.client_id] = { id: body.client_id, kind: 'summary', turn_id: t.id, status: 'running', stage: 'starting', steps: [], answer: '',
        created_at: Date.now() / 1000, started_at: Date.now() / 1000, last_signal_at: Date.now() / 1000, model: X.data.session.model, effort: eff || forkDefault('summary') };
      refreshTurn(t.id);
      api('POST', '/api/sessions/' + encodeURIComponent(sid) + '/summaries', body).then(function (r) { onJob(r.job); }, function (e) {
        X.jobs[body.client_id] = Object.assign(X.jobs[body.client_id], { status: 'failed', stage: 'failed', error: e.message });
        refreshTurn(t.id);
      });
    }
    function refreshTurn(turnId) {
      var t = X.turns[turnId], old = X.turnEls[turnId];
      if (!t || !old) return;
      var a = document.activeElement, keep = a && old.contains(a) && a.tagName === 'TEXTAREA' ? { card: a.closest('.iq-card'), s: a.selectionStart } : null;
      var free = a && old.contains(a) && a.classList.contains('out-free') ? { i: Array.prototype.indexOf.call(old.querySelectorAll('.out-free'), a), s: a.selectionStart } : null;
      var pos = processScroll(old), ne = renderTurn(t); old.replaceWith(ne);
      restoreProcessScroll(ne, pos);
      if (free) { var fi = ne.querySelectorAll('.out-free')[free.i]; if (fi && !fi.disabled) { fi.focus({ preventScroll: true }); try { fi.setSelectionRange(free.s, free.s); } catch (e) {} } }
      if (keep && keep.card) {
        var sel = keep.card.dataset.draft ? '.iq-card[data-draft="' + keep.card.dataset.draft + '"] textarea' : null;
        var ta = sel && ne.querySelector(sel);
        if (ta) { ta.focus({ preventScroll: true }); try { ta.setSelectionRange(keep.s, keep.s); } catch (e) {} }
      }
    }
    function redecorateAll() { Object.keys(X.turnEls).forEach(refreshTurn); }
    function onJob(j) {
      var prev = X.jobs[j.id];
      X.jobs[j.id] = j;
      var card = document.querySelector('.iq-card[data-jid="' + j.id + '"]');
      // 진행 중 내용만 바뀌었으면 카드만 다시 그린다(입력 중인 초안·스크롤을 건드리지 않게)
      if (card && prev && prev.status === j.status && prev.turn_id === j.turn_id) {
        // 끝난 카드에 사용량 같은 메타만 늦게 오면 본문은 그대로 둔다 — 다시 그리면 사용자가 고르던 글이 풀린다
        if (j.status !== 'running' && prev.answer === j.answer && prev.error === j.error && prev.fork_thread === j.fork_thread) {
          var meta = [].filter.call(card.querySelectorAll('.iq-meta'), function (m) { return m.closest('.iq-card') === card; })[0];
          if (meta) { meta.textContent = metaLine(j); meta.title = metaTitle(j); }
          return;
        }
        fillJobCard(card, j); return;
      }
      refreshTurn(j.turn_id);
    }

    // ------------------------------------------------ 꾸미기: 질문 카드 · 코멘트
    function containerOf(turnEl, part) {
      if (part === 'answer') return turnEl.querySelector('.annot[data-part="answer"]');
      return turnEl.querySelector('.annot[data-part="' + part + '"]');
    }
    function decorateTurn(turnId) {
      var sec = X.turnEls[turnId]; if (!sec) return;
      var t = X.turns[turnId];
      // 요약 카드
      var sj = summaryJob(turnId);
      var slot = sec.querySelector('.summary-slot');
      if (sj && slot) slot.appendChild(jobCard(sj));
      // 질문 카드: 바탕(본 답·요약)에 붙는 것 → 그 아래 이어 묻기
      var qs = Object.keys(X.jobs).map(function (k) { return X.jobs[k]; }).filter(function (j) { return j.kind === 'question' && j.turn_id === turnId; });
      qs.sort(function (a, b) { return a.created_at - b.created_at; });
      var roots = qs.filter(function (j) { return !j.parent_id || !X.jobs[j.parent_id]; });
      roots.forEach(function (j) {
        var c = containerOf(sec, j.part && j.part.indexOf('q:') === 0 ? 'answer' : j.part) || containerOf(sec, 'answer');
        if (!c) return;
        placeCard(jobCard(j), c, j.quote, j.start, j);
      });
      var placed = true, guard = 0;
      while (placed && guard++ < 50) {
        placed = false;
        qs.forEach(function (j) {
          if (!j.parent_id || !X.jobs[j.parent_id] || sec.querySelector('.iq-card[data-jid="' + j.id + '"]')) return;
          var parentCard = sec.querySelector('.iq-card[data-jid="' + j.parent_id + '"]');
          if (!parentCard) return;
          var card = jobCard(j);
          card.style.setProperty('--iq-indent', Math.min(12 * (j.depth || 1), 48) + 'px');
          if (j.part && j.part.indexOf('q:') === 0) {
            var pa = parentCard.querySelector('.iq-answer');
            if (pa) markQuote(pa, j.quote, j.start, 'iq-mark', { jid: j.id });
          }
          var after = parentCard;
          while (after.nextElementSibling && after.nextElementSibling.classList.contains('iq-card') && after.nextElementSibling.dataset.parent === j.parent_id) after = after.nextElementSibling;
          card.dataset.parent = j.parent_id;
          after.after(card);
          placed = true;
        });
      }
      // 초안(작성 카드)
      X.drafts.filter(function (d) { return d.turn === turnId; }).forEach(function (d) {
        var card = draftCard(d);
        if (d.parent_id) {
          var pc = sec.querySelector('.iq-card[data-jid="' + d.parent_id + '"]');
          if (pc) {
            card.style.setProperty('--iq-indent', Math.min(12 * (((X.jobs[d.parent_id] || {}).depth || 0) + 1), 48) + 'px');
            var pa = pc.querySelector('.iq-answer');
            if (pa && d.part.indexOf('q:') === 0) markQuote(pa, d.quote, d.start, 'iq-mark pending', {});
            pc.after(card); return;
          }
        }
        var c = containerOf(sec, d.part) || containerOf(sec, 'answer');
        if (c) placeCard(card, c, d.quote, d.start, null);
      });
      // 코멘트
      sec.querySelectorAll('.annot').forEach(function (c) { decorateComments(c, turnId, c.dataset.part); });
      void t;
    }
    function placeCard(card, container, quote, start, job) {
      var marks = markQuote(container, quote, start, 'iq-mark' + (job && ui.folded[job.id] ? ' iq-folded' : '') + (job ? '' : ' pending'), job ? { jid: job.id } : {});
      var lastMark = marks[marks.length - 1];
      var blk = lastMark ? blockOf(lastMark, container) : null;
        if (o) steps.scrollTop = steps.scrollHeight;
      if (!blk) { container.appendChild(card); return; }
      if (blk.tagName === 'LI') {
        var sub = blk.querySelector(':scope > ul, :scope > ol');
        if (sub) blk.insertBefore(card, sub); else blk.appendChild(card);
        return;
      }
      var next = blk.nextElementSibling;
      while (next && next.classList.contains('iq-card')) next = next.nextElementSibling;
      blk.parentNode.insertBefore(card, next);
    function processScroll(turn) {
      var box = turn && turn.querySelector('.steps:not([hidden])');
      if (!box) return null;
      return { top: box.scrollTop, bottom: box.scrollHeight - box.clientHeight - box.scrollTop <= 8 };
    }
    function restoreProcessScroll(turn, pos) {
      var box = turn.querySelector('.steps:not([hidden])');
      if (box) box.scrollTop = !pos || pos.bottom ? box.scrollHeight : pos.top;
    }
    }
    function blockOf(node, container) {
      var n = node;
      while (n && n !== container) {
        if (n.parentNode === container || /^(LI|PRE|BLOCKQUOTE|TABLE)$/.test(n.tagName) || (n.classList && n.classList.contains('tbl'))) {
          if (n.tagName === 'TABLE' && n.parentNode.classList.contains('tbl')) return n.parentNode;
          return n;
        }
        n = n.parentNode;
      }
      return null;
    }

    // 질문 카드 (끝난·진행 중·실패)
    function jobCard(j) {
      var card = el('div', 'iq-card'); card.dataset.jid = j.id;
      fillJobCard(card, j);
      return card;
    }
    function fillJobCard(card, j) {
      card.innerHTML = '';
      var folded = !!ui.folded[j.id] && j.status !== 'running';
      card.classList.toggle('iq-is-folded', folded);
      card.classList.toggle('iq-cancelled', j.status === 'cancelled');
      if (j.status === 'running') card.dataset.running = '1'; else delete card.dataset.running;
      var isSum = j.kind === 'summary';
      if (folded) {
        var fr = btn('', 'iq-foldrow');
        fr.appendChild(el('span', 'iq-badge' + (isSum ? ' sum' : ''), isSum ? '과정 요약' : '질문'));
        fr.appendChild(el('span', 'iq-qtext', isSum ? firstLine(j.answer) : j.question));
        fr.appendChild(el('span', 'iq-meta', metaLine(j)));
        fr.addEventListener('click', function () { delete ui.folded[j.id]; saveUi(); refreshTurn(j.turn_id); });
        card.appendChild(fr); return;
      }
      var q = el('div', 'iq-q');
      q.appendChild(el('span', 'iq-badge' + (isSum ? ' sum' : ''), isSum ? '과정 요약' : '질문'));
      q.appendChild(el('span', 'iq-qtext', isSum ? '같은 모델로 포크해 정리 · ' + (j.model || '') : j.question));
      if (j.status !== 'running') {
        var fb = btn('▴', 'iq-icon', function () { ui.folded[j.id] = 1; saveUi(); refreshTurn(j.turn_id); }, '접기');
        fb.setAttribute('aria-label', '접기'); q.appendChild(fb);
      }
      card.appendChild(q);
      if (!isSum && j.part && j.part.indexOf('q:') === 0) card.appendChild(el('div', 'iq-selection', j.quote));
      card.appendChild(jobIds(j));
      if (j.status === 'running') {
        var pr = el('div', 'iq-progress'); card.appendChild(pr);
        var steps = el('div', 'iq-steps'); card.appendChild(steps);
        var pm = el('div', 'iq-progmeta'); card.appendChild(pm);
        paintProgress(card, j);
        if (j.answer) { var pa = el('div', 'iq-answer md partial'); pa.innerHTML = md.render(j.answer); card.appendChild(pa); }
        return;
      }
      if (j.status === 'done') {
        var a = el('div', 'iq-answer md annot'); a.dataset.kind = 'session'; a.dataset.turn = j.turn_id;
        a.dataset.part = isSum ? 'summary' : 'q:' + j.id; a.dataset.done = '1'; a.dataset.jid = j.id;
        a.innerHTML = md.render(j.answer || '');
        card.appendChild(a);
        var foot = el('div', 'iq-foot');
        var meta = el('span', 'iq-meta', metaLine(j)); meta.title = metaTitle(j); foot.appendChild(meta);
        var acts = el('span', 'iq-actions');
        acts.appendChild(btn('본 대화에 넣기', 'small', function () { X.comp.append(insertBlock(j)); toast('입력창에 넣었습니다 — 보내야 세션에 갑니다'); }));
        if (!isSum) acts.appendChild(btn('추가 질문', 'small', function () { openDraft({ turn: j.turn_id, part: j.part, quote: j.quote, start: j.start, parent_id: j.id, followup: true }); }));
        acts.appendChild(btn('⋯', 'small', function (e) { jobMenu(card, j); e.stopPropagation(); }, '더 보기'));
        foot.appendChild(acts); card.appendChild(foot);
        return;
      }
      var line = el('div', 'iq-errline');
      line.appendChild(el('span', null, j.status === 'cancelled' ? '멈췄습니다' : '실패 — ' + (j.error || '알 수 없는 오류')));
      line.appendChild(btn(isSum ? '다시 만들기' : '다시 묻기', 'small', function () { retryJob(j); }));
      line.appendChild(btn('삭제', 'small', function () { deleteJob(j); }));
      card.appendChild(line);
    }
    function jobIds(j) {
      var row = el('div', 'iq-ids');
      row.appendChild(idChip('세션', j.thread_id, '원본 세션 ID'));
      if (j.fork_thread) row.appendChild(idChip(j.kind === 'summary' ? '포크(일회용)' : '포크', j.fork_thread,
        '포크한 세션 ID · ' + (j.fork_mode === 'reused' ? '재사용' : '새로 만듦')));
      else row.appendChild(el('span', 'id-pending', '포크 만드는 중'));
      return row;
    }
    function firstLine(s) { return (s || '').replace(/[*#>`]/g, '').split('\n').filter(function (x) { return x.trim(); })[0] || ''; }
    function paintProgress(card, j) {
      var pr = card.querySelector('.iq-progress'), steps = card.querySelector('.iq-steps'), pm = card.querySelector('.iq-progmeta');
      if (!pr) return;
      var now = Date.now() / 1000, idle = now - (j.last_signal_at || now), stalled = idle > STALL_SEC;
      pr.innerHTML = '';
      pr.appendChild(el('span', 'dot'));
      var st = el('span', 'iq-stage' + (stalled ? ' stalled' : ''), (stalled ? '지연 — ' : '') + (STAGE[j.stage] || j.stage) + (j.stage_detail ? ' · ' + j.stage_detail : ''));
      pr.appendChild(st);
      pr.appendChild(btn('취소', 'small', function () { api('POST', '/api/jobs/' + j.id + '/cancel', {}).then(function (r) { onJob(r.job); }, function (e) { toast(e.message); }); }));
      var ss = (j.steps || []).slice(-4).map(function (s, i, arr) {
        var next = arr[i + 1], t = next ? next.t - s.t : (now - j.started_at) - s.t;
        return (STAGE_SHORT[s.stage] || s.stage) + ' ' + Math.max(0, t).toFixed(1) + '초';
      });
      steps.textContent = ss.join(' → ');
      pm.classList.toggle('stalled', stalled);
      pm.textContent = '경과 ' + Math.round(now - j.started_at) + '초 · 마지막 신호 ' + Math.round(idle) + '초 전 · ' + [j.model, j.effort].filter(Boolean).join(' · ');
    }
    function metaLine(j) {
      var u = j.usage || {}, parts = [j.model, j.effort, dur(j.duration_ms)];
      if (j.tools_used) parts.push('도구 ' + j.tools_used + '회');
      if (u.cached) parts.push('캐시 ' + kfmt(u.cached) + ' 읽음');
      return parts.filter(Boolean).join(' · ');
    }
    function metaTitle(j) {
      var u = j.usage || {};
      return '입력 ' + kfmt(u.input) + ' · 캐시 ' + kfmt(u.cached) + ' · 출력 ' + kfmt(u.output) + (j.first_token_ms ? ' · 첫 글자 ' + dur(j.first_token_ms) : '') +
        '\n포크 ' + (j.fork_mode === 'reused' ? '재사용' : '새로') +
        '\n진행: ' + (j.steps || []).map(function (s) { return (STAGE_SHORT[s.stage] || s.stage) + ' ' + s.t + '초'; }).join(' → ');
    }
    function insertBlock(j) {
      if (j.kind === 'summary') return '[과정 요약 · 턴 ' + ((X.turns[j.turn_id] || {}).index || '') + ']\n' + (j.answer || '');
      var quote = (j.quote || '').split('\n').map(function (l) { return '> ' + l; }).join('\n');
      return '[인라인 질문]\n인용:\n' + quote + '\n질문: ' + j.question + '\n답변:\n' + (j.answer || '');
    }
    function jobMenu(card, j) {
      var old = card.querySelector('.iq-menu'); if (old) { old.remove(); return; }
      var m = el('div', 'iq-menu');
      if (j.kind === 'question') m.appendChild(btn('고쳐서 다시 묻기', null, function () {
        m.remove();
        var text = prompt('질문을 고쳐 주세요', j.question); if (text == null) return;
        retryJob(j, text);
      }));
      else m.appendChild(btn('다시 만들기', null, function () { m.remove(); retryJob(j); }));
      m.appendChild(btn('답 복사', null, function () { m.remove(); copyText(j.answer || ''); }));
      m.appendChild(btn('삭제', null, function () { m.remove(); deleteJob(j); }));
      card.appendChild(m);
      setTimeout(function () { document.addEventListener('click', function off(e) { if (!m.contains(e.target)) { m.remove(); document.removeEventListener('click', off); } }); }, 0);
    }
    function retryJob(j, question) {
      api('POST', '/api/jobs/' + j.id + '/retry', question ? { question: question } : {}).then(function (r) {
        delete X.jobs[r.replaced]; onJob(r.job);
      }, function (e) { toast('다시 시작하지 못했습니다: ' + e.message); });
    }
    function deleteJob(j) {
      api('DELETE', '/api/jobs/' + j.id).then(function () { delete X.jobs[j.id]; refreshTurn(j.turn_id); }, function (e) {
        if (e.status === 404) { delete X.jobs[j.id]; refreshTurn(j.turn_id); } else toast(e.message);
      });
    }

    // 요약·질문 포크의 effort — 모델은 세션과 같으니(캐시 재사용) 그 모델이 받는 값만 고른다. 고르지 않으면 모델 관리의 기본값.
    function effortSheet(cur, onPick) {
      models().then(function (list) {
        var m = list.filter(function (x) { return x.id === X.data.session.model; })[0];
        var effs = m ? m.efforts : ['low', 'medium', 'high'];
        var sh = sheet('effort — ' + (X.data.session.model || '세션 모델'));
        sh.body.appendChild(el('div', 'iq-hint', '모델은 세션과 같다(포크가 원본의 캐시를 읽게).'));
        var seg = el('div', 'seg'); sh.body.appendChild(seg);
        effs.forEach(function (e) {
          seg.appendChild(btn(e, cur === e ? 'on' : '', function () { onPick(e); sh.close(); }));
        });
      }, function (e) { toast('모델 목록을 읽지 못했습니다: ' + e.message); });
    }

    // 작성 카드(인라인 질문 초안). 줄을 눌러 열면 질문·코멘트 탭이 있다.
    function saveDrafts() { lsSet('iqDrafts.' + sid, X.drafts.length ? X.drafts : null); }
    function openDraft(d) {
      if (!X.turns[d.turn] || X.turns[d.turn].status === 'inProgress') { toast('이 턴이 끝난 뒤에 물을 수 있습니다'); return; }
      var existing = X.drafts.filter(function (x) { return x.turn === d.turn && x.part === d.part && x.quote === d.quote && (x.parent_id || null) === (d.parent_id || null); })[0];
      if (!existing) {
        existing = Object.assign({ id: uuid4().replace(/-/g, '').slice(0, 16), text: '', tab: 'q', effort: '' }, d);
        X.drafts.push(existing); saveDrafts();
      } else if (d.tab) { existing.tab = d.tab; }
      refreshTurn(d.turn);
      var c = document.querySelector('.iq-card[data-draft="' + existing.id + '"]');
      // 즉시 옮긴다 — 부드러운 스크롤이 도는 동안 누른 자리가 다른 줄로 바뀌지 않게
      if (c) { var ta = c.querySelector('textarea'); c.scrollIntoView({ block: 'nearest' }); if (ta) ta.focus({ preventScroll: true }); }
    }
    function closeDraft(d) {
      X.drafts = X.drafts.filter(function (x) { return x.id !== d.id; }); saveDrafts();
      closeComment(); refreshTurn(d.turn);
    }
    function draftCard(d) {
      var card = el('div', 'iq-card'); card.dataset.draft = d.id;
      var head = el('div', 'iq-q');
      if (d.line && !d.parent_id) {
        var tabs = el('div', 'line-tabs'); tabs.setAttribute('role', 'tablist');
        [['q', '질문'], ['c', '💬 코멘트']].forEach(function (p) {
          var tb = btn(p[1], 'line-tab'); tb.setAttribute('role', 'tab'); tb.setAttribute('aria-selected', d.tab === p[0] ? 'true' : 'false');
          tb.addEventListener('click', function () { d.tab = p[0]; saveDrafts(); if (editing) closeComment(); else refreshTurn(d.turn); });
          tabs.appendChild(tb);
        });
        head.appendChild(tabs);
      } else {
        head.appendChild(el('span', 'iq-badge', d.parent_id ? '추가 질문' : '질문'));
        head.appendChild(el('span', 'iq-qtext iq-dim', '“' + d.quote.replace(/\s+/g, ' ').slice(0, 80) + '”'));
      }
      var x = btn('✕', 'iq-icon', function () { closeDraft(d); }, '닫기'); x.setAttribute('aria-label', '작성 닫기');
      head.appendChild(x); card.appendChild(head);
      if (d.parent_id && d.part.indexOf('q:') === 0) card.appendChild(el('div', 'iq-selection', d.quote));
      if (d.tab === 'c' && d.line) {
        var host = el('div'); card.appendChild(host);
        setTimeout(function () {
          if (!host.isConnected) return;
          startComment({ turn: d.turn, part: d.part, quote: d.quote, start: d.start, text: d.ctext || '',
            onText: function (v) { d.ctext = v; saveDrafts(); } }, host, function () { closeDraft(d); });
        }, 0);
        return card;
      }
      var ta = el('textarea', 'iq-input'); ta.rows = 1; ta.placeholder = d.parent_id ? '이어서 물어보기' : '고른 부분에 대해 묻기 — 비우면 “쉽게 설명해 줘”';
      ta.value = d.text || '';
      ta.addEventListener('input', function () { d.text = ta.value; saveDrafts(); });
      ta.addEventListener('keydown', function (e) {
        if (e.isComposing || e.keyCode === 229) return;
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(); }
        if (e.key === 'Escape') { e.preventDefault(); closeDraft(d); }
      });
      card.appendChild(ta);
      var ctr = el('div', 'iq-controls');
      var chip = btn('', 'chip');
      function paintChip() { chip.innerHTML = ''; chip.appendChild(el('span', 'main', X.data.session.model || '세션 모델')); chip.appendChild(el('span', 'sub', d.effort || forkDefault('question'))); }
      paintChip();
      chip.title = '요약·질문은 세션과 같은 모델로 포크해 캐시를 읽는다 — effort 만 고른다';
      chip.addEventListener('click', function () {
        effortSheet(d.effort || forkDefault('question'), function (e) { d.effort = e; saveDrafts(); paintChip(); });
      });
      ctr.appendChild(chip);
      var err = el('span', 'iq-formerr');
      var send = btn('묻기', 'primary iq-send', ask);
      ctr.appendChild(err); ctr.appendChild(send);
      card.appendChild(ctr);
      function ask() {
        if (send.disabled) return;
        send.disabled = true;
        var body = { turn_id: d.turn, quote: d.quote, start: d.start, part: d.part, question: ta.value.trim(), parent_id: d.parent_id || null,
          effort: d.effort || null, client_id: d.id };
        // 1초 안에 진행 카드로 — 서버 응답 전에 자리 잡기
        X.jobs[d.id] = { id: d.id, kind: 'question', thread_id: sid, turn_id: d.turn, quote: d.quote, start: d.start, part: d.part,
          question: body.question || '선택한 부분을 쉽게 설명해 줘.', parent_id: d.parent_id || null, depth: d.parent_id ? ((X.jobs[d.parent_id] || {}).depth || 0) + 1 : 0,
          status: 'running', stage: 'starting', steps: [], answer: '', created_at: Date.now() / 1000, started_at: Date.now() / 1000,
          last_signal_at: Date.now() / 1000, model: X.data.session.model, effort: d.effort || 'low' };
        X.drafts = X.drafts.filter(function (x) { return x.id !== d.id; }); saveDrafts();
        refreshTurn(d.turn);
        api('POST', '/api/sessions/' + encodeURIComponent(sid) + '/questions', body).then(function (r) { onJob(r.job); }, function (e) {
          delete X.jobs[d.id];
          d.text = ta.value; X.drafts.push(d); saveDrafts(); refreshTurn(d.turn);
          toast('묻지 못했습니다: ' + e.message);
        });
      }
      return card;
    }

    // 코멘트
    function reloadComments() {
      api('GET', '/api/sessions/' + encodeURIComponent(sid) + '/comments').then(function (r) {
        if (seq !== S.seq || JSON.stringify(r.comments) === JSON.stringify(X.comments)) return;
        X.comments = r.comments; redecorateAll();
      }, function () {});
    }
    function decorateComments(container, turnId, part) {
      var mine = X.comments.filter(function (c) { return c.turn_id === turnId && c.part === part; });
      mine.forEach(function (c) { c._found = markQuote(container, c.quote, c.start, 'cm-mark', { cid: c.id }).length > 0; });
      if (editing && editing.turn === turnId && editing.part === part) markQuote(container, editing.quote, editing.start, 'cm-mark pending', {});
      if (!mine.length) return;
      var box = el('div', 'cm-cards');
      mine.forEach(function (c) { box.appendChild(commentCard(c, {
        put: function () { setCommentState(c, 'input').then(function () { X.comp.append(commentBlock(c)); toast('입력창에 넣었습니다 — 보내야 세션에 갑니다'); }); },
        pull: function () { X.comp.remove(commentBlock(c)); setCommentState(c, 'none'); },
        state: setCommentState, remove: removeComment, jump: function () { jumpTo('mark.cm-mark[data-cid="' + c.id + '"]'); }
      })); });
      container.after(box);
    }
    function setCommentState(c, state) {
      return api('PATCH', '/api/comments/' + c.id, { state: state }).then(function (r) {
        Object.assign(c, r.comment); redecorateAll();
      }, function (e) { toast('바꾸지 못했습니다: ' + e.message); throw e; });
    }
    function removeComment(c) {
      api('DELETE', '/api/comments/' + c.id).then(function () { X.comments = X.comments.filter(function (x) { return x.id !== c.id; }); redecorateAll(); },
        function (e) { toast(e.message); });
    }
    X.saveComment = function (p, text, state) {
      var body = { id: p.id, turn_id: p.turn, part: p.part, quote: p.quote, start: p.start, text: text, state: state,
        response: firstLine((X.turns[p.turn] && X.turns[p.turn].answer || {}).text || '') };
      return api('POST', '/api/sessions/' + encodeURIComponent(sid) + '/comments', body).then(function (r) {
        X.comments = X.comments.filter(function (x) { return x.id !== r.comment.id; }).concat([r.comment]);
        if (state === 'input') X.comp.append(commentBlock(r.comment));
        redecorateAll();
        setTimeout(function () { jumpTo('.cm-card[data-cid="' + r.comment.id + '"]'); }, 50);
        toast(state === 'input' ? '저장하고 입력창에 넣었습니다 — 보내야 세션에 갑니다' : '저장했습니다 — 세션에는 가지 않았습니다');
        return r.comment;
      });
    };
    X.canInput = true;
    X.openDraft = openDraft;
    X.refreshTurn = refreshTurn;
    load(true);
  }

  // ---------------------------------------------------------------- 공통: 인용 찾기·형광
  // 인용을 글에서 찾아 <mark> 로 감싼다. 렌더링된 글은 원문과 공백·줄바꿈이 달라 공백을 모두 빼고 찾는다.
  // start(공백 뺀 위치)가 있으면 그 근처의 것을 고른다. 카드(.iq-card, .cm-cards) 안의 글은 원문이 아니다.
  function textMap(root) {
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    var map = [], str = '';
    while (walker.nextNode()) {
      var tn = walker.currentNode, v = tn.nodeValue;
      if (!insideOwn(root, tn)) continue;
      for (var i = 0; i < v.length; i++) { var ch = v.charAt(i); if (!/\s/.test(ch) && ch !== '​') { str += ch; map.push([tn, i]); } }
    }
    return { str: str, map: map };
  }
  // root 안이지만 root 안에 끼운 다른 카드(.iq-card·.cm-cards)의 글은 뺀다
  function insideOwn(root, node) {
    var p = node.parentElement;
    while (p && p !== root) {
      if (p.classList && (p.classList.contains('iq-card') || p.classList.contains('cm-cards'))) return false;
      p = p.parentElement;
    }
    return true;
  }
  function markQuote(root, quote, start, cls, data) {
    var q = squash(quote);
    if (!q || !root) return [];
    var tm = textMap(root), str = tm.str, map = tm.map;
    var at = -1, best = Infinity, i = str.indexOf(q);
    while (i >= 0) {
      var d = start == null ? i : Math.abs(i - start);
      if (d < best) { best = d; at = i; }
      i = str.indexOf(q, i + 1);
    }
    if (at < 0) return [];
    var spans = [], last = null;
    for (var k = at; k < at + q.length; k++) {
      var p = map[k];
      if (last && last.node === p[0]) last.end = p[1] + 1; else { last = { node: p[0], start: p[1], end: p[1] + 1 }; spans.push(last); }
    }
    var marks = [];
    spans.reverse().forEach(function (s) {
      var r = document.createRange(); r.setStart(s.node, s.start); r.setEnd(s.node, s.end);
      var m = document.createElement('mark'); m.className = cls;
      Object.keys(data || {}).forEach(function (k) { m.dataset[k] = data[k]; });
      try { r.surroundContents(m); marks.unshift(m); } catch (e) {}
    });
    return marks;
  }
  function offsetOf(root, node, offset) {
    // 공백을 뺀 글에서 (node, offset) 이 몇 번째 글자인가
    var tm = textMap(root), r = document.createRange();
    try { r.setStart(node, offset); r.collapse(true); } catch (e) { return 0; }
    for (var i = 0; i < tm.map.length; i++) {
      if (r.comparePoint(tm.map[i][0], tm.map[i][1]) >= 0) return i;
    }
    return tm.map.length;
  }
  function jumpTo(selector) {
    var t = document.querySelector(selector);
    if (!t) return;
    t.scrollIntoView({ block: 'center', behavior: 'smooth' });
    t.classList.remove('cm-flash'); void t.offsetWidth; t.classList.add('cm-flash');
    setTimeout(function () { t.classList.remove('cm-flash'); }, 1700);
  }
  document.addEventListener('click', function (e) {
    var m = e.target.closest && e.target.closest('mark.cm-mark[data-cid]');
    if (m) { jumpTo('.cm-card[data-cid="' + m.dataset.cid + '"]'); return; }
    var q = e.target.closest && e.target.closest('mark.iq-mark[data-jid]');
    if (q) {
      var card = document.querySelector('.iq-card[data-jid="' + q.dataset.jid + '"]');
      if (card && S.X && S.X.refreshTurn && card.classList.contains('iq-is-folded')) {
        var ui = lsGet('ui.' + S.X.sid, { open: {}, folded: {} }); delete ui.folded[q.dataset.jid]; lsSet('ui.' + S.X.sid, ui);
      }
      if (card) { card.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); card.classList.add('iq-flash'); setTimeout(function () { card.classList.remove('iq-flash'); }, 1200); }
      return;
    }
    var fp = e.target.closest && e.target.closest('.fpath');
    if (fp) { copyText(fp.dataset.path); return; }
    var doc = e.target.closest && e.target.closest('a.md-doc');
    if (doc) { e.preventDefault(); go('#/md?path=' + encodeURIComponent(doc.dataset.mdPath) + (S.X && S.X.sid ? '&from=' + encodeURIComponent(S.X.sid) : '')); }
  });

  // ---------------------------------------------------------------- 코멘트 작성창(#cm-pop)
  var cmPop = el('div'); cmPop.id = 'cm-pop'; cmPop.hidden = true; cmPop.setAttribute('role', 'dialog'); cmPop.setAttribute('aria-label', '코멘트 쓰기');
  document.body.appendChild(cmPop);
  var editing = null;
  function commentBlock(c) { return (c.quote || '').split('\n').map(function (l) { return '> ' + l; }).join('\n') + (c.text ? '\n\n' + c.text : ''); }
  function closeComment() {
    var was = editing; editing = null; cmPop.hidden = true; cmPop.innerHTML = ''; cmPop.classList.remove('inline');
    if (cmPop.parentNode !== document.body) document.body.appendChild(cmPop);
    if (was && S.X && S.X.refreshTurn && was.turn) S.X.refreshTurn(was.turn);
    if (was && S.D) S.D.redraw();
  }
  // p = {turn, part, quote, start} (세션) 또는 {doc:true, quote, start, line} (문서)
  function startComment(p, host, onDone, rect) {
    if (editing && !host) closeComment();
    editing = { turn: p.turn, part: p.part, quote: (p.quote || '').slice(0, 2000), start: p.start, id: uuid4(), doc: p.doc, line: p.line, busy: false };
    cmPop.innerHTML = '';
    var head = el('div', 'cm-pop-head'); head.appendChild(el('b', null, '💬 코멘트'));
    head.appendChild(btn('✕', 'link', function () { closeComment(); if (onDone) onDone(); }, '취소'));
    cmPop.appendChild(head);
    cmPop.appendChild(el('div', 'cm-pop-quote', '“' + editing.quote + '”'));
    var box = el('textarea'); box.rows = 3; box.placeholder = '내 의견 (선택) — 예: 이 부분은 다른 방법으로 해 줘';
    box.value = p.text || '';
    if (p.onText) box.addEventListener('input', function () { p.onText(box.value); });
    cmPop.appendChild(box);
    var err = el('div', 'cm-pop-err'); err.hidden = true; cmPop.appendChild(err);
    var foot = el('div', 'cm-pop-foot');
    var canInput = p.doc ? true : !!(S.X && S.X.comp);
    var putLabel = p.doc ? (S.D && S.D.from ? '세션 입력창에 넣기' : '새 세션 입력창에 넣기') : '입력창에 넣기';
    var saveOnly = btn('저장만', 'small', function () { save(false); });
    var toInput = btn(putLabel, 'small primary', function () { save(true); });
    saveOnly.appendChild(el('kbd', null, MOD + 'S')); toInput.appendChild(el('kbd', null, MOD + (MAC ? '↵' : 'Enter')));
    saveOnly.title = '저장만 한다 — 세션에는 가지 않는다';
    toInput.title = '저장하고 입력칸에 넣는다 — 보내야 세션에 간다';
    toInput.disabled = !canInput;
    foot.appendChild(btn('취소', 'small', function () { closeComment(); if (onDone) onDone(); }));
    foot.appendChild(el('span', 'grow')); foot.appendChild(saveOnly); foot.appendChild(toInput);
    cmPop.appendChild(foot);
    cmPop.onkeydown = function (e) {
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeComment(); if (onDone) onDone(); return; }
      if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return;
      var b = (e.code === 'KeyS' || e.key.toLowerCase() === 's') ? saveOnly : e.key === 'Enter' ? toInput : null;
      if (!b) return;
      e.preventDefault(); e.stopPropagation();
      if (!e.repeat && !b.disabled) b.click();
    };
    function save(input) {
      if (!editing || editing.busy) return;
      var cur = editing; cur.busy = true; saveOnly.disabled = toInput.disabled = true; err.hidden = true;
      var saver = p.doc ? S.D.saveComment : S.X.saveComment;
      saver(cur, box.value.trim(), input ? 'input' : 'none').then(function () {
        if (editing === cur) { editing = null; closeComment(); if (onDone) onDone(); }
      }, function (e) {
        cur.busy = false; saveOnly.disabled = false; toInput.disabled = !canInput;   // 같은 id 로 다시 보내면 한 번만 저장된다
        err.textContent = '저장하지 못했습니다: ' + e.message; err.hidden = false;
      });
    }
    if (host) { cmPop.classList.add('inline'); host.appendChild(cmPop); }
    else { cmPop.classList.remove('inline'); document.body.appendChild(cmPop); }
    cmPop.hidden = false;
    if (!host) placePop(rect);
    if (S.X && S.X.refreshTurn && p.turn && !host) { S.X.refreshTurn(p.turn); }
    if (S.D && p.doc) S.D.redraw();
    setTimeout(function () { box.focus({ preventScroll: true }); }, 0);
  }
  function placePop(rect) {
    rect = rect || { left: 8, top: 80, bottom: 80 };
    var vv = window.visualViewport, top0 = vv ? vv.offsetTop : 0, vh = vv ? vv.height : window.innerHeight;
    var w = cmPop.offsetWidth, h = cmPop.offsetHeight;
    var x = Math.min(Math.max(8, rect.left), window.innerWidth - w - 8);
    // 의견 위젯(오른쪽 아래 18px + 37px)이 단추를 가리지 않도록 아래 여백을 둔다
    var low = document.documentElement.classList.contains('has-fb') ? 64 : 8;
    var y = rect.bottom + 10;
    if (y + h > top0 + vh - low) y = rect.top - h - 10;
    y = Math.min(Math.max(top0 + 8, y), top0 + vh - h - low);
    cmPop.style.left = Math.max(8, x) + 'px'; cmPop.style.top = Math.max(top0 + 8, y) + 'px';
  }
  function commentCard(c, h) {
    var d = el('div', 'cm-card'); d.dataset.cid = c.id;
    if (c.text) d.appendChild(el('div', 'cm-text', '💬 ' + c.text));
    var q = btn('“' + (c.quote || '').replace(/\s+/g, ' ') + '”', 'cm-q' + (c._found === false ? ' lost' : ''), h.jump,
      c._found === false ? '원문에서 이 글을 찾지 못했습니다(문서가 바뀌었을 수 있음)' : '고른 글로 가기');
    d.appendChild(q);
    var meta = el('div', 'cm-meta');
    var label = c.state === 'input' && c.target_label ? '입력창에 있음 · ' + c.target_label :
      c.state === 'sent' && c.refine_id ? 'AI 다듬기에 보냄' : (STATE_LABEL[c.state] || c.state);
    meta.appendChild(el('span', 'cm-state ' + (c.state || 'none'), label));
    var acts = el('span', 'cm-acts');
    function act(l, cls, fn) { acts.appendChild(btn(l, cls, fn)); }
    if (c.state === 'none') { act(h.putLabel || '입력창에 넣기', 'put', h.put); act('메모로 두기', null, function () { h.state(c, 'memo'); }); }
    else if (c.state === 'input') { if (h.pull) act('입력창에서 빼기', null, h.pull); }
    else act('다시 열기', null, function () { h.state(c, 'none'); });
    if (h.copy) act('복사', null, h.copy);
    if (c.state !== 'input') {
      var del = btn('지우기', 'del');
      del.addEventListener('click', function () {
        if (!del.classList.contains('armed')) {   // 확인 한 번
          del.classList.add('armed'); del.textContent = '정말 지우기';
          setTimeout(function () { if (del.isConnected) { del.classList.remove('armed'); del.textContent = '지우기'; } }, 4000);
          return;
        }
        h.remove(c);
      });
      acts.appendChild(del);
    }
    meta.appendChild(acts); d.appendChild(meta);
    return d;
  }

  // ---------------------------------------------------------------- 고른 글 메뉴 · 줄 누르기
  var pills = el('div', 'sel-pills'); pills.hidden = true; document.body.appendChild(pills);
  var selCtx = null;
  pills.addEventListener('pointerdown', function (e) { e.preventDefault(); });
  pills.addEventListener('mousedown', function (e) { e.preventDefault(); });
  function hidePills() { pills.hidden = true; selCtx = null; }
  function selectionContext() {
    var sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
    var r = sel.getRangeAt(0);
    var a = (r.startContainer.nodeType === 1 ? r.startContainer : r.startContainer.parentElement);
    var b = (r.endContainer.nodeType === 1 ? r.endContainer : r.endContainer.parentElement);
    var ca = a && a.closest('.annot'), cb = b && b.closest('.annot');
    if (!ca || ca !== cb) return null;
    // 카드의 머리·메타·버튼은 고를 대상이 아니다
    if (a.closest('.iq-card') !== b.closest('.iq-card')) return null;
    var inner = a.closest('.iq-card');
    if (inner && !ca.closest('.iq-card')) return null;
    var quote = sel.toString().trim();
    if (!quote) return null;
    var ln = a.closest('.md-line');
    return { container: ca, quote: quote.slice(0, 4000), start: offsetOf(ca, r.startContainer, r.startOffset), rect: r.getBoundingClientRect(),
      line: ln ? +ln.dataset.line : null };
  }
  var selTimer = null;
  function checkSelection() {
    var c = selectionContext();
    if (!c) { hidePills(); return; }
    selCtx = c;
    pills.innerHTML = '';
    var kind = c.container.dataset.kind;
    if (kind === 'session' && c.container.dataset.done === '1' && !(S.X && S.X.data && S.X.data.session.readonly)) pills.appendChild(btn('질문', 'q', function () { var x = selCtx; clearSel(); hidePills(); askFrom(x); }));
    pills.appendChild(btn('💬 코멘트', null, function () { var x = selCtx; clearSel(); hidePills(); commentFrom(x); }));
    pills.appendChild(btn('⧉ 복사', null, function () { var x = selCtx; hidePills(); copyText(x.quote); }));
    pills.hidden = false;
    var w = pills.offsetWidth, hgt = pills.offsetHeight || 34;
    var x = Math.max(8, Math.min(c.rect.left, window.innerWidth - w - 8));
    var touch = lastPointer === 'touch';
    var y = touch ? c.rect.bottom + 12 : c.rect.bottom + 8;
    if (y + hgt > window.innerHeight - 90) y = c.rect.top - hgt - 10;
    y = Math.min(Math.max(8, y), window.innerHeight - hgt - 90);
    pills.style.left = x + 'px'; pills.style.top = y + 'px';
  }
  function clearSel() { var s = window.getSelection(); if (s) s.removeAllRanges(); }
  document.addEventListener('selectionchange', function () { clearTimeout(selTimer); selTimer = setTimeout(checkSelection, lastPointer === 'touch' ? 350 : 120); });
  window.addEventListener('scroll', function () { if (!pills.hidden && selCtx) { clearTimeout(selTimer); selTimer = setTimeout(checkSelection, 100); } }, { passive: true });
  function partOf(container) {
    return { turn: container.dataset.turn, part: container.dataset.part, jid: container.dataset.jid };
  }
  function askFrom(c) {
    if (!S.X) return;
    var p = partOf(c.container);
    var d = { turn: p.turn, part: p.part, quote: c.quote, start: c.start };
    if (p.part && p.part.indexOf('q:') === 0) { d.parent_id = p.jid; d.followup = true; }
    S.X.openDraft(d);
  }
  function commentFrom(c) {
    if (c.container.dataset.kind === 'md') { startComment({ doc: true, quote: c.quote, start: c.start, line: c.line }, null, null, c.rect); return; }
    var p = partOf(c.container);
    startComment({ turn: p.turn, part: p.part, quote: c.quote, start: c.start }, null, null, c.rect);
  }
  // 줄 누르기(마우스): 그 줄을 인용해 작성 카드를 연다 — 세션은 질문·코멘트 탭, 문서는 코멘트
  var lastPointer = 'mouse', downAt = null;
  var LINE_SKIP = 'a, button, input, textarea, summary, label, img, .fpath, mark, .iq-card button, .iq-card textarea, .cm-cards, [contenteditable]';
  document.addEventListener('pointerdown', function (e) { lastPointer = e.pointerType || 'mouse'; downAt = { x: e.clientX, y: e.clientY }; }, true);
  document.addEventListener('mousemove', function () { if (lastPointer === 'mouse' && !document.documentElement.classList.contains('line-on')) document.documentElement.classList.add('line-on'); });
  window.addEventListener('blur', function () { document.documentElement.classList.remove('line-on'); });
  document.addEventListener('touchstart', function () { document.documentElement.classList.remove('line-on'); }, { passive: true });
  document.addEventListener('click', function (e) {
    if (lastPointer !== 'mouse' || e.button !== 0 || e.defaultPrevented) return;
    if (downAt && (Math.abs(e.clientX - downAt.x) > 4 || Math.abs(e.clientY - downAt.y) > 4)) return;
    var sel = window.getSelection(); if (sel && !sel.isCollapsed) return;
    if (e.detail > 1) return;
    var line = e.target.closest && e.target.closest('.annot .md-line');
    if (!line || e.target.closest(LINE_SKIP)) return;
    var container = line.closest('.annot');
    if (line.closest('.iq-card') && !container.closest('.iq-card')) return;
    if (editing) return;
    var quote = (line.innerText || line.textContent || '').trim();
    if (!quote || quote === '​') return;
    var start = offsetOf(container, line, 0);
    if (container.dataset.kind === 'md') {
      startComment({ doc: true, quote: quote, start: start, line: +line.dataset.line }, null, null, line.getBoundingClientRect());
      return;
    }
    if (!S.X) return;
    var p = partOf(container);
    if (container.dataset.done !== '1') { commentFrom({ container: container, quote: quote, start: start, rect: line.getBoundingClientRect() }); return; }
    var d = { turn: p.turn, part: p.part, quote: quote, start: start, line: true, tab: 'q' };
    if (p.part && p.part.indexOf('q:') === 0) { d.parent_id = p.jid; d.followup = true; d.line = false; }
    S.X.openDraft(d);
  });

  // ---------------------------------------------------------------- 프로젝트 탐색기
  function qs(o) {
    var p = new URLSearchParams();
    Object.keys(o).forEach(function (k) { if (o[k]) p.set(k, o[k]); });
    var s = p.toString();
    return s ? '?' + s : '';
  }
  function filesUrl(root, dir, from) { return '#/files' + qs({ root: root, dir: dir, from: from }); }
  function docUrl(path, root, from) { return '#/md' + qs({ path: path, root: root, from: from }); }
  function webUrl(url, from) { return '#/web' + qs({ url: url, from: from }); }
  function isMd(p) { return /\.(md|markdown)$/i.test(p || ''); }
  function tilde(p) { return BOOT.home && (p || '').indexOf(BOOT.home + '/') === 0 ? '~' + p.slice(BOOT.home.length) : p; }
  function size(n) { return n == null ? '' : n < 1024 ? n + 'B' : n < 1048576 ? Math.round(n / 1024) + 'KB' : (n / 1048576).toFixed(1) + 'MB'; }
  function fileRow(e, onClick) {
    var b = btn('', 'frow' + (e.kind === 'dir' ? ' dir' : isMd(e.name) ? ' md' : ''));
    b.appendChild(el('span', 'ficon', e.kind === 'dir' ? '📁' : isMd(e.name) ? '📝' : '📄'));
    b.appendChild(el('span', 'fname', e.name));
    if (e.comments) { var cb = el('span', 'badge cm', '💬 ' + e.comments); cb.title = '열린 코멘트 ' + e.comments + '개'; b.appendChild(cb); }
    var meta = e.kind === 'dir' ? (e.files != null ? e.files + '개' : '') : [size(e.size), ago(e.mtime)].filter(Boolean).join(' · ');
    if (meta) b.appendChild(el('span', 'fmeta', meta));
    b.title = e.path || '';
    b.addEventListener('click', onClick);
    return b;
  }
  function viewProjects(params, pop) {
    setHeader('프로젝트', [], true);
    var seq = S.seq;
    var tools = el('div', 'list-tools');
    var path = el('input'); path.placeholder = '/절대/경로'; path.setAttribute('aria-label', '프로젝트 경로');
    var open = btn('열기', 'small', function () { if (path.value.trim()) go(filesUrl(path.value.trim(), '', params.from)); });
    path.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.isComposing) open.click(); });
    tools.appendChild(path); tools.appendChild(open); view.appendChild(tools);
    var list = el('div', 'tree'); list.appendChild(el('div', 'empty', '읽는 중…')); view.appendChild(list);
    api('GET', '/api/cwds').then(function (r) {
      if (seq !== S.seq) return;
      list.innerHTML = '';
      (r.cwds || []).forEach(function (c) {
        list.appendChild(fileRow({ kind: 'dir', name: base(c), path: c }, function () { go(filesUrl(c, '', params.from)); }))
          .appendChild(el('span', 'fmeta', tilde(c)));
      });
      if (!(r.cwds || []).length) list.appendChild(el('div', 'empty', '세션을 연 폴더가 없습니다 — 경로를 적어 여세요'));
      restoreScroll(pop);
    }, function (e) { if (seq === S.seq) { list.innerHTML = ''; list.appendChild(el('div', 'empty err', e.message)); } });
  }
  function viewFiles(params, pop) {
    var root = params.root || '', dir = params.dir || '', from = params.from || null, seq = S.seq;
    if (!root) return viewProjects(params, pop);
    setHeader(base(root), [tilde(root)], true);
    actionsEl.appendChild(btn('새 세션', 'small', function () { go('#/new' + qs({ cwd: root })); }, '이 프로젝트에서 새 세션'));
    var tools = el('div', 'list-tools');
    var q = el('input'); q.type = 'search'; q.placeholder = '파일 찾기 — 예: .md'; q.value = params.q || '';
    q.setAttribute('aria-label', '파일 찾기');
    tools.appendChild(q); view.appendChild(tools);
    var crumbs = el('nav', 'crumbs'); crumbs.setAttribute('aria-label', '폴더 경로'); view.appendChild(crumbs);
    var list = el('div', 'tree'); view.appendChild(list);
    function paintCrumbs() {
      crumbs.innerHTML = '';
      crumbs.hidden = !!q.value.trim();
      var parts = dir ? dir.split('/') : [];
      var rootBtn = btn('⌂', 'crumb', function () { go(filesUrl(root, '', from)); }, '프로젝트 맨 위');
      rootBtn.disabled = !parts.length; crumbs.appendChild(rootBtn);
      parts.forEach(function (p, i) {
        crumbs.appendChild(el('span', 'sep', '/'));
        var b = btn(p, 'crumb', function () { go(filesUrl(root, parts.slice(0, i + 1).join('/'), from)); });
        b.disabled = i === parts.length - 1; crumbs.appendChild(b);
      });
    }
    function open(e) { if (e.kind === 'dir') go(filesUrl(root, e.rel, from)); else go(docUrl(e.path, root, from)); }
    function load() {
      paintCrumbs();
      var term = q.value.trim();
      if (!list.children.length) list.appendChild(el('div', 'empty', '읽는 중…'));
      var p = term ? api('GET', '/api/files?cwd=' + encodeURIComponent(root) + '&q=' + encodeURIComponent(term) + '&limit=60').then(function (r) {
        return r.files.map(function (f) { return { kind: 'file', name: f.rel, rel: f.rel, path: f.path }; });
      }) : api('GET', '/api/tree?root=' + encodeURIComponent(root) + '&dir=' + encodeURIComponent(dir)).then(function (r) { return r.entries; });
      return p.then(function (entries) {
        if (seq !== S.seq) return;
        list.innerHTML = '';
        entries.forEach(function (e) { list.appendChild(fileRow(e, function () { open(e); })); });
        if (!entries.length) list.appendChild(el('div', 'empty', term ? '맞는 파일이 없습니다' : '빈 폴더입니다'));
      }, function (e) {
        if (seq !== S.seq) return;
        list.innerHTML = ''; list.appendChild(el('div', 'empty err', e.message));
        list.appendChild(btn('다시 읽기', null, load));
      });
    }
    var timer = null;
    q.addEventListener('input', function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        history.replaceState(history.state, '', filesUrl(root, dir, from) + (q.value.trim() ? (dir || from ? '&' : '?') + 'q=' + encodeURIComponent(q.value.trim()) : ''));
        load();
      }, 200);
    });
    function onVis() { if (document.visibilityState === 'visible') load(); }
    document.addEventListener('visibilitychange', onVis);
    S.cleanup.push(function () { clearTimeout(timer); document.removeEventListener('visibilitychange', onVis); });
    load().then(function () { restoreScroll(pop); });
  }

  // ---------------------------------------------------------------- 웹 보기(답변 속 데모 페이지 등)
  // 바깥 링크를 새 탭 대신 앱 안의 iframe 으로 연다. 뒤로 가기·"세션으로"는 떠난 세션 위치로 돌아간다.
  // iframe 을 막는 사이트와 페이지 갱신을 위한 보조 동작은 머리의 페이지 메뉴에 둔다.
  var WEB_SLOW_MS = 30000;
  function isLoopback(h) { return /^(localhost|127\.\d+\.\d+\.\d+|\[::1\]|::1)$/i.test(h || ''); }
  // 주소창에 친 글 → 열 주소. 스킴이 없으면 이 기기 주소는 http, 나머지는 https 로 본다
  function typedUrl(v) {
    v = (v || '').trim();
    if (!v || /^https?:\/\//i.test(v)) return v;
    if (/^[a-z][a-z0-9+.-]*:/i.test(v) && !/^[^/]+:\d+(\/|$)/.test(v)) return '';   // javascript: 같은 다른 스킴
    return (isLoopback(v.split(/[/:]/)[0]) || /^0\.0\.0\.0\b/.test(v) ? 'http://' : 'https://') + v;
  }
  function viewWeb(params, pop) {
    var url = params.url || '', from = params.from || null, seq = S.seq;
    var u = null, target = url, current = url, unreadable = false;
    try { u = /^https?:\/\//i.test(url) ? new URL(url) : null; } catch (e) {}
    // 머리가 곧 주소창이다: [세션으로] [‹ 페이지 뒤로] [주소] [⋯] — 호스트·경로를 따로 또 적지 않는다
    var bar = el('form', 'web-bar'); bar.setAttribute('role', 'search'); bar.setAttribute('aria-label', '페이지 주소');
    var backPage = btn('‹', 'icon-btn web-back', function () {
      if (S.webDepth > 0) { S.webDepth--; clientLog({ type: 'web_nav', action: 'page_back', depth: S.webDepth + 1 }); history.back(); }
      else if (from) webToSession(from, 'page_back');
      else history.back();
    }, '페이지 안에서 뒤로 — 더 갈 곳이 없으면 세션으로');
    backPage.setAttribute('aria-label', '페이지 뒤로');
    var addr = el('input', 'web-addr'); addr.type = 'url'; addr.name = 'url';
    addr.setAttribute('aria-label', '주소'); addr.autocomplete = 'off'; addr.spellcheck = false;
    addr.setAttribute('autocapitalize', 'off'); addr.setAttribute('enterkeyhint', 'go');
    addr.addEventListener('focus', function () { addr.select(); });
    bar.appendChild(backPage); bar.appendChild(addr);
    bar.addEventListener('submit', function (e) {
      e.preventDefault();
      var next = typedUrl(addr.value);
      if (!next) { toast('http·https 주소만 열 수 있습니다'); return; }
      addr.blur();
      clientLog({ type: 'web_nav', action: 'address' });
      if (u && (next === current || next === url)) open(); else go(webUrl(next, from));
    });
    topEl.insertBefore(bar, actionsEl);
    S.cleanup.push(function () { bar.remove(); });
    function showAddr() {
      // 다른 출처 페이지 안에서 옮겨 간 주소는 읽을 수 없다 — 마지막으로 안 주소를 흐리게 둔다
      var stale = S.webDepth > 0 && unreadable;
      if (document.activeElement !== addr) addr.value = current;
      addr.classList.toggle('stale', stale);
      addr.title = stale ? '페이지 안에서 옮겨 갔습니다 — 다른 출처라 지금 주소를 읽을 수 없어 처음 연 주소를 보입니다' : current;
    }
    function head(link) {
      var t = null;
      try { t = new URL(link); } catch (e) {}
      setHeader(t ? t.host : '웹 보기', [], true);   // 탭 제목에만 쓰인다(머리의 제목 칸은 주소창이 대신한다)
      if (more) actionsEl.appendChild(more);
      showAddr();
    }
    S.webDepth = 0;
    window.scrollTo(0, 0);
    var tools = el('div', 'doc-tools web-tools');
    var status = el('span', 'grow');
    tools.appendChild(status);
    var more = null;
    if (u) {
      more = btn('⋯', 'small', function () {
        var sh = sheet('페이지 메뉴');
        var items = el('div', 'web-menu');
        items.appendChild(btn('페이지 새로고침', null, function () { sh.close(); open(); }));
        items.appendChild(btn('브라우저에서 열기', null, function () { sh.close(); window.open(current, '_blank', 'noopener'); }, '브라우저 새 탭으로 열기'));
        sh.body.appendChild(items);
      }, '페이지 메뉴');
      more.setAttribute('aria-label', '페이지 메뉴'); more.setAttribute('aria-haspopup', 'dialog');
    }
    head(url);
    view.appendChild(tools);
    if (!u) { view.appendChild(el('div', 'empty err', 'http·https 주소만 열 수 있습니다')); return; }
    var frame = el('iframe', 'web-frame');
    // 페이지 안에서 옮겨 다니면 앱의 방문 기록 길이가 는다 — 그만큼을 페이지 뒤로(‹)가 먼저 쓴다.
    // 같은 출처 페이지면 지금 주소도 읽어 주소창에 보인다.
    var histLen = history.length;
    function track() {
      if (seq !== S.seq) return;
      if (history.length > histLen) { S.webDepth += history.length - histLen; histLen = history.length; }
      var href = null;
      try { href = frame.contentWindow && frame.contentWindow.location.href; } catch (e) {}
      unreadable = !(href && /^https?:/i.test(href));
      if (!unreadable) current = href; else if (!S.webDepth) current = target;
      showAddr();
    }
    var trackTimer = setInterval(track, 500);
    S.cleanup.push(function () { clearInterval(trackTimer); });
    frame.setAttribute('allow', 'clipboard-read; clipboard-write; fullscreen');
    var t0 = 0, tick = null, via = '';
    function say(text, err) { clearInterval(tick); tick = null; status.textContent = text; status.classList.toggle('err', !!err); tools.hidden = !text; }
    function paint() {
      tools.hidden = false;
      var ms = performance.now() - t0;
      status.classList.toggle('err', ms >= WEB_SLOW_MS);
      status.textContent = (ms >= WEB_SLOW_MS ? '지연 — 응답이 없습니다 · ' : '불러오는 중 · ') + dur(ms);
    }
    // 127.0.0.1 같은 주소는 화면을 보는 기기 자신을 가리킨다 — 서버(Codex 를 돌리는 기기)가 그 포트의 HTTPS 주소로 바꿔 준다
    function open() {
      t0 = performance.now();
      clearInterval(tick); tick = setInterval(paint, 1000); paint();
      api('GET', '/api/web/resolve?url=' + encodeURIComponent(url) + '&via=' + encodeURIComponent(location.origin)).then(function (r) { return r; }, function () { return { url: url }; }).then(function (r) {
        if (seq !== S.seq) return;
        var local = !!r.local, appLocal = isLoopback(location.hostname);
        target = current = r.url || url; via = r.changed ? (r.via || 'https') : r.reason || '';
        S.webDepth = 0; histLen = history.length;
        if (r.changed) head(target); else showAddr();
        if (r.reason === 'down') { frame.remove(); return say(u.host + ' 에서 응답하는 서버가 없습니다 — 서버가 꺼졌거나 포트가 바뀌었습니다', true); }
        if (local && !r.changed && (r.reason === 'no_https' || r.reason === 'no_tailscale') && !appLocal) {
          frame.remove();
          var alt = location.hostname + ':' + (u.port || (u.protocol === 'https:' ? '443' : '80'));
          return say(u.host + ' 는 Codex 를 돌리는 기기 안에서만 열립니다 — ' + alt + ' 로도 닿지 않습니다(서버가 127.0.0.1 에만 묶였거나 방화벽이 막음)'
            + (r.reason === 'no_https' ? ', 그 포트에 붙은 HTTPS 주소(tailscale serve)도 없습니다' : ''), true);
        }
        // HTTPS 로 연 앱 안에서는 http 주소(루프백 제외)를 브라우저가 막는다(혼합 콘텐츠)
        var t = new URL(target);
        if (location.protocol === 'https:' && t.protocol === 'http:' && !isLoopback(t.hostname)) {
          frame.remove(); return say('HTTPS 화면 안에서는 http 주소를 열 수 없습니다 — 새 탭으로 여세요', true);
        }
        frame.title = t.host;
        frame.src = target;   // 같은 주소를 다시 넣어도 다시 불러온다
        if (!frame.parentNode) view.appendChild(frame);   // src 를 넣은 뒤 붙인다 — 빈 about:blank 의 load 를 받지 않게
      });
    }
    frame.addEventListener('load', function () {
      track();
      if (seq !== S.seq || !tick) return;   // 페이지 안에서 옮겨 다닌 load 는 넘긴다
      say('');
      clientLog({ type: 'web_open', host: new URL(target).host, via: via, ms: Math.round(performance.now() - t0) });
    });
    S.cleanup.push(function () { clearInterval(tick); });
    open();
  }

  // ---------------------------------------------------------------- 포트(Codex 가 띄운 데모·개발 서버)
  // 세션이 bin/lens-port 로 띄운 서버를 보고 연다·끈다. 상태는 서버가 1초마다 확인하고, 이 화면은 2초마다 다시 읽는다.
  var PORT_STATE = { starting: '여는 중', listening: '열림', exited: '끝남', stopped: '꺼짐' };
  function portsUrl(p) { return '#/ports' + qs(p || {}); }
  function portRunning(e) { return e.state === 'starting' || e.state === 'listening'; }
  // 앱을 연 주소가 호스트 이름에 포트를 담는 프록시(e2b `8761-<샌드박스>.e2b.app` 등)면 그 자리만 바꾼 origin — weblink.proxied_origin 과 같은 규칙
  function proxiedOrigin(port) {
    var h = location.hostname || '', i = h.indexOf('.'), ap = String(BOOT.port || '');
    if (!ap || i < 0 || isLoopback(h) || /^[\d.]+$|:/.test(h)) return null;
    var re = new RegExp('(^|[^0-9])' + ap + '(?![0-9])', 'g'), first = h.slice(0, i);
    if ((first.match(re) || []).length !== 1) return null;
    return location.protocol + '//' + first.replace(re, '$1' + port) + h.slice(i) + (location.port ? ':' + location.port : '');
  }
  function portHref(e) {
    if (e.url) return e.url;   // 서버가 붙인 바깥 주소(CODEX_LENS_PUBLIC_URL · 내보내기)
    var px = proxiedOrigin(e.port);
    if (px) return px + '/';
    var h = location.hostname || '127.0.0.1';
    return 'http://' + (h.indexOf(':') >= 0 ? '[' + h + ']' : h) + ':' + e.port + '/';
  }
  function isLoopHost(h) { return /^(localhost|127\.\d+\.\d+\.\d+|\[?::1\]?)$/i.test(h || ''); }
  function viewPorts(params, pop) {
    var seq = S.seq, scopeId = params.thread || '', showAll = params.all === '1', from = params.from || null;
    var thread = scopeId && !showAll ? scopeId : '';
    var openLog = params.log || '', info = null, items = null, first = true;
    setHeader('포트', [], true);
    var tools = el('div', 'doc-tools port-tools');
    var scope = null;
    if (scopeId) {
      scope = btn('이 세션만', 'small', function () { showAll = !showAll; thread = showAll ? '' : scopeId; save(); load(); }, '이 세션이 띄운 것만 / 전체');
      tools.appendChild(scope);
    }
    var status = el('span', 'grow'); tools.appendChild(status);
    var clear = btn('끝난 것 지우기', 'small', function () {
      clear.disabled = true;
      api('POST', '/api/ports/clear', {}).then(function (r) { toast(r.removed + '개를 지웠습니다'); load(); },
        function (e) { toast('지우지 못했습니다: ' + e.message); }).then(function () { clear.disabled = false; });
    }, '꺼지거나 끝난 기록과 로그를 지운다');
    clear.hidden = true; tools.appendChild(clear);
    view.appendChild(tools);
    var list = el('div', 'ports'); list.appendChild(el('div', 'empty', '읽는 중…')); view.appendChild(list);
    // 펼친 로그·범위는 주소에 둔다 — 뒤로 가기로 돌아오면 그대로
    function save() { history.replaceState(history.state, '', portsUrl({ thread: scopeId, all: showAll ? '1' : '', from: from, log: openLog })); }
    function paintScope() { if (scope) { scope.textContent = thread ? '이 세션만' : '전체'; scope.classList.toggle('on', !!thread); } }
    function load() {
      var t0 = performance.now();
      return api('GET', '/api/ports' + (thread ? '?thread=' + encodeURIComponent(thread) : '')).then(function (r) {
        if (seq !== S.seq) return;
        info = r; items = r.items || [];
        paint();
        if (first) { first = false; restoreScroll(pop); clientLog({ type: 'ports_render', ms: Math.round(performance.now() - t0), count: items.length }); }
      }, function (e) {
        if (seq !== S.seq) return;
        status.textContent = '읽지 못했습니다 — ' + e.message; status.classList.add('err');
      });
    }
    function paint() {
      var run = items.filter(portRunning).length;
      setSub(run, items.length - run);
      status.textContent = ''; status.classList.remove('err');
      clear.hidden = !items.some(function (e) { return !portRunning(e); });
      paintScope();
      // 펼친 로그의 스크롤·고른 글을 지키려고 행은 ID 로 다시 쓴다
      var keep = {};
      Array.prototype.forEach.call(list.querySelectorAll('.prow'), function (r) { keep[r.dataset.id] = r; });
      var empty = list.querySelector('.empty'); if (empty) empty.remove();
      items.forEach(function (e, i) {
        var row = keep[e.id] || el('div', 'prow');
        delete keep[e.id];
        row.dataset.id = e.id;
        paintRow(row, e);
        if (list.children[i] !== row) list.insertBefore(row, list.children[i] || null);
      });
      Object.keys(keep).forEach(function (k) { keep[k].remove(); });
      if (!items.length) list.appendChild(el('div', 'empty', thread ? '이 세션이 띄운 서버가 없습니다' : '띄운 서버가 없습니다'));
    }
    function setSub(run, done) {
      var sub = [run ? '실행 중 ' + run : '실행 중 없음', done ? '끝남 ' + done : '', info && info.range ? '대역 ' + info.range[0] + '–' + info.range[1] : ''];
      subEl.innerHTML = '';
      sub.filter(Boolean).forEach(function (s, i) { if (i) subEl.appendChild(el('span', 'sep', '·')); subEl.appendChild(document.createTextNode(s)); });
    }
    function paintRow(row, e) {
      var sig = JSON.stringify([e.state, e.exit_code, e.session_title, e.listen_at, e.url, e.expose, openLog === e.id]);
      if (row.dataset.sig === sig) { paintAge(row, e); return; }
      var logBox = row.querySelector('.plog'), logScroll = logBox ? logBox.scrollTop : null;
      row.dataset.sig = sig;
      row.innerHTML = '';
      row.classList.toggle('done', !portRunning(e));
      var head = el('div', 'prow-head');
      var num = el('span', 'pnum', ':' + e.port); head.appendChild(num);
      head.appendChild(el('span', 'pname', e.name || ''));
      var st = PORT_STATE[e.state] || e.state;
      if (e.state === 'exited' && e.exit_code != null) st += ' · 코드 ' + e.exit_code;
      head.appendChild(el('span', 'badge ' + (e.state === 'listening' ? 'run' : e.state === 'starting' ? '' : e.state === 'exited' && e.exit_code ? 'bad' : 'dim'), st));
      head.appendChild(el('span', 'grow'));
      if (portRunning(e)) {
        var a = el('a', 'ext btnlike small' + (e.state === 'listening' ? ' primary' : ''), '열기');   // ext: 앱 안 웹 보기로
        a.href = portHref(e); a.target = '_blank'; a.rel = 'noopener'; a.title = a.href;
        head.appendChild(a);
      }
      head.appendChild(btn(openLog === e.id ? '로그 닫기' : '로그', 'small', function () {
        openLog = openLog === e.id ? '' : e.id; save(); row.dataset.sig = ''; paintRow(row, e);
      }));
      if (portRunning(e)) {
        var stop = btn('끄기', 'small danger', function () {
          if (!stop.classList.contains('armed')) {
            stop.classList.add('armed'); stop.textContent = '정말 끄기';
            setTimeout(function () { if (stop.isConnected && !stop.disabled) { stop.classList.remove('armed'); stop.textContent = '끄기'; } }, 3000);
            return;
          }
          stop.disabled = true; stop.textContent = '끄는 중';
          var t0 = performance.now();
          api('POST', '/api/ports/' + encodeURIComponent(e.id) + '/stop', {}).then(function () {
            toast(':' + e.port + ' 꺼짐');
            clientLog({ type: 'port_stop', ms: Math.round(performance.now() - t0) });
            load();
          }, function (err) { toast('끄지 못했습니다: ' + err.message); stop.disabled = false; stop.textContent = '끄기'; stop.classList.remove('armed'); });
        }, '프로세스 그룹에 종료 신호를 보낸다(5초 뒤에도 살아 있으면 강제 종료)');
        head.appendChild(stop);
      } else {
        head.appendChild(btn('지우기', 'small link', function () {
          api('DELETE', '/api/ports/' + encodeURIComponent(e.id)).then(load, function (err) { toast('지우지 못했습니다: ' + err.message); });
        }, '기록과 로그를 지운다'));
      }
      row.appendChild(head);
      var meta = el('div', 'srow-meta prow-meta');
      if (e.thread_id) {
        var sl = btn(e.session_title || ('세션 ' + e.thread_id.slice(0, 8)), 'link plink', function () { go('#/s/' + encodeURIComponent(e.thread_id)); }, '이 서버를 띄운 세션으로');
        meta.appendChild(sl);
      }
      var where = el('span', 'grow', tilde(e.cwd)); where.title = e.cwd || ''; meta.appendChild(where);
      if (e.expose === 'failed') { var xb = el('span', 'badge bad', '내보내기 실패'); xb.title = e.expose_error || ''; meta.appendChild(xb); }
      else if (e.bind === '127.0.0.1' && !e.url && !isLoopHost(location.hostname)) { var lb = el('span', 'badge dim', '이 기기 전용'); lb.title = '127.0.0.1 에만 묶여 다른 기기에서는 열리지 않을 수 있다'; meta.appendChild(lb); }
      meta.appendChild(el('span', 'page'));
      row.appendChild(meta);
      var cmd = el('div', 'pcmd', e.cmd || ''); cmd.title = (e.cmd || '') + '\nID ' + e.id + ' · PID ' + e.pid; row.appendChild(cmd);
      paintAge(row, e);
      if (openLog === e.id) {
        var pre = el('pre', 'plog', '읽는 중…'); row.appendChild(pre);
        readLog(pre, e, logScroll);
      }
    }
    function paintAge(row, e) {
      var a = row.querySelector('.page'); if (!a) return;
      var end = portRunning(e) ? Date.now() / 1000 : (e.ended_at || Date.now() / 1000);
      a.textContent = (portRunning(e) ? '' : ago(e.ended_at) + ' · ') + dur((end - e.started_at) * 1000);
    }
    function readLog(pre, e, keepScroll) {
      api('GET', '/api/ports/' + encodeURIComponent(e.id) + '/log?bytes=12000').then(function (r) {
        if (!pre.isConnected) return;
        var atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8 || pre.textContent === '읽는 중…';
        var top = pre.scrollTop;
        pre.textContent = r.text || '(출력 없음)';
        // 처음 열면·끝을 보고 있었으면 맨 아래(새 출력), 위로 올려 읽던 중이면 그 자리
        pre.scrollTop = keepScroll != null ? keepScroll : atEnd ? pre.scrollHeight : top;
      }, function (err) { if (pre.isConnected) pre.textContent = '로그를 읽지 못했습니다: ' + err.message; });
    }
    var timer = setInterval(function () {
      if (document.visibilityState !== 'visible') return;
      load().then(function () {
        var pre = list.querySelector('.plog'), row = pre && pre.closest('.prow');
        var e = row && items && items.filter(function (x) { return x.id === row.dataset.id; })[0];
        if (e && portRunning(e)) readLog(pre, e, null);
      });
    }, 2000);
    S.cleanup.push(function () { clearInterval(timer); });
    paintScope();
    load();
  }

  // ---------------------------------------------------------------- 문서(.md) 보기 · 코멘트 · AI 다듬기
  var REFINE_STATE = { running: '다듬는 중', completed: '다듬었습니다', interrupted: '멈췄습니다', failed: '실패했습니다' };
  function viewDoc(params, pop) {
    var seq = S.seq, path = params.path || '', root = params.root || '', from = params.from || null;
    var md_ = isMd(path);
    function where() { return root && path.indexOf(root + '/') === 0 ? base(root) + '/' + path.slice(root.length + 1).replace(/\/?[^/]*$/, '') : tilde(path.replace(/\/[^/]*$/, '')); }
    setHeader(base(path) || '문서', [where().replace(/\/$/, '')], true);
    subEl.title = path;
    var D = { path: path, from: from, comments: [], text: '', refines: [], sending: null };
    S.D = D;
    S.cleanup.push(function () { if (S.D === D) S.D = null; });
    var ui = lsGet('docUi.' + path, { seen: null, changes: true });
    function saveUi() { lsSet('docUi.' + path, ui); }
    var tools = el('div', 'doc-tools');
    var count = el('span', 'grow');
    tools.appendChild(count);
    tools.appendChild(btn('다시 읽기', 'small', function () { load(); }));
    if (from) tools.appendChild(btn('세션으로', 'small', function () { go('#/s/' + encodeURIComponent(from)); }));
    var refineBtn = btn('AI로 다듬기', 'small primary', function () { openRefine(); }, '코멘트대로 에이전트가 이 문서를 고친다');
    if (md_) tools.appendChild(refineBtn);
    view.appendChild(tools);
    var panel = el('div', 'refine-panel'); panel.hidden = true; view.appendChild(panel);
    var doc = el('div', 'doc'); view.appendChild(doc);
    var body = el('div', md_ ? 'md annot' : 'code-view');
    if (md_) body.dataset.kind = 'md';
    doc.appendChild(body);
    body.textContent = '읽는 중…';
    function load() {
      var reqs = [api('GET', '/api/file?path=' + encodeURIComponent(path))];
      if (md_) reqs.push(api('GET', '/api/md-comments?path=' + encodeURIComponent(path)), api('GET', '/api/md-refines?path=' + encodeURIComponent(path)));
      return Promise.all(reqs).then(function (rs) {
        if (seq !== S.seq) return;
        D.text = rs[0].text;
        if (md_) { D.comments = rs[1].comments; D.refines = rs[2].refines; }
        D.redraw(); paintPanel(); watch();
      }, function (e) { if (seq === S.seq) body.innerHTML = '<div class="err">' + md.esc(e.message) + '</div>'; });
    }
    function openComments() { return D.comments.filter(function (c) { return c.state === 'none' || c.state === 'input'; }); }
    function latest() { return D.sending || D.refines[0] || null; }
    D.redraw = function () {
      if (!md_) {
        body.innerHTML = '<pre><code>' + D.text.split('\n').map(function (l, i) { return '<span class="md-line" data-line="' + i + '">' + (md.esc(l) || '&#8203;') + '</span>'; }).join('') + '</code></pre>';
        return;
      }
      body.innerHTML = md.render(D.text);
      var r = latest();
      if (r && r.status === 'completed' && r.current && r.diff && ui.changes) {
        r.diff.lines.forEach(function (n) { body.querySelectorAll('.md-line[data-line="' + n + '"]').forEach(function (x) { x.classList.add('chg'); }); });
      }
      var lastBlock = {}, done = [];
      D.comments.forEach(function (c) {
        var marks = markQuote(body, c.quote, c.start, 'cm-mark', { cid: c.id });
        c._found = marks.length > 0;
        if (!c._found && c.state === 'sent') { done.push(c); return; }   // 다듬어져 원문이 바뀐 코멘트는 아래에 모은다
        var blk = marks.length ? docBlock(marks[marks.length - 1], body) : null;
        var key = blk ? blk : body;
        var box = key === body ? (lastBlock.__end || (lastBlock.__end = el('div', 'cm-cards'))) : (key.__cm || (key.__cm = el('div', 'cm-cards')));
        box.appendChild(card(c));
        if (key !== body && !box.isConnected) key.after(box);
      });
      if (lastBlock.__end) body.appendChild(lastBlock.__end);
      if (done.length) {
        var det = el('details', 'cm-done'); det.appendChild(el('summary', null, '다듬기에 쓴 코멘트 ' + done.length + '개'));
        var box2 = el('div', 'cm-cards'); done.forEach(function (c) { box2.appendChild(card(c)); }); det.appendChild(box2);
        body.appendChild(det);
      }
      if (editing && editing.doc) markQuote(body, editing.quote, editing.start, 'cm-mark pending', {});
      var n = openComments().length;
      count.textContent = n ? '코멘트 ' + n : '코멘트 없음';
      refineBtn.textContent = n ? 'AI로 다듬기 · ' + n : 'AI로 다듬기';
    };
    function card(c) {
      return commentCard(c, {
        putLabel: from ? '세션 입력창에 넣기' : '새 세션 입력창에 넣기',
        put: function () { putDoc(c); }, pull: function () { pullDoc(c); }, state: setState, remove: remove,
        copy: function () { copyText(docBlockText(c)); }, jump: function () { jumpTo('mark.cm-mark[data-cid="' + c.id + '"]'); }
      });
    }
    function docBlock(node, root_) {
      var n = node;
      while (n && n.parentNode !== root_) n = n.parentNode;
      return n;
    }
    function docBlockText(c) { return '[문서 코멘트] ' + path + (c.line != null ? ':' + (c.line + 1) : '') + '\n' + commentBlock(c); }
    var draftKey = from || 'new';
    function putDoc(c) {
      var d = lsGet('draft.' + draftKey, null) || { text: '', mentions: [] };
      var block = docBlockText(c);
      if ((d.text || '').indexOf(block) < 0) d.text = ((d.text || '').replace(/\s+$/, '') + (d.text ? '\n\n' : '') + block + '\n\n');
      d.at = Date.now();
      lsSet('draft.' + draftKey, d);
      var ids = lsGet('mdInputIds', {}); ids[c.id] = block; lsSet('mdInputIds', ids);
      return setState(c, 'input').then(function () {
        toast(from ? '세션 입력창에 넣었습니다' : '새 세션 입력창에 넣었습니다', { label: from ? '세션으로' : '새 세션으로', fn: function () { go(from ? '#/s/' + encodeURIComponent(from) : '#/new' + qs({ cwd: root })); } });
      });
    }
    function dropFromDraft(c) {
      var d = lsGet('draft.' + draftKey, null);
      var block = docBlockText(c);
      if (d && d.text) { d.text = d.text.replace(block, '').replace(/\n{3,}/g, '\n\n').replace(/^\s+/, ''); lsSet('draft.' + draftKey, d.text.trim() ? d : null); }
      var ids = lsGet('mdInputIds', {}); delete ids[c.id]; lsSet('mdInputIds', ids);
    }
    function pullDoc(c) { dropFromDraft(c); return setState(c, 'none'); }
    function setState(c, state) {
      return api('PATCH', '/api/md-comments/' + c.id, { state: state }).then(function (r) { Object.assign(c, r.comment); D.redraw(); },
        function (e) { toast('바꾸지 못했습니다: ' + e.message); throw e; });
    }
    function remove(c) {
      api('DELETE', '/api/md-comments/' + c.id).then(function () { D.comments = D.comments.filter(function (x) { return x.id !== c.id; }); D.redraw(); },
        function (e) { toast(e.message); });
    }
    D.saveComment = function (p, text, state) {
      var body2 = { id: p.id, path: path, quote: p.quote, start: p.start, line: p.line, text: text, state: 'none' };
      return api('POST', '/api/md-comments', body2).then(function (r) {
        D.comments = D.comments.filter(function (x) { return x.id !== r.comment.id; }).concat([r.comment]);
        D.redraw();
        setTimeout(function () { jumpTo('.cm-card[data-cid="' + r.comment.id + '"]'); }, 50);
        if (state === 'input') return putDoc(r.comment);
        toast('저장했습니다');
      });
    };

    // ------------------------------------------------ AI 다듬기 요청
    var refAgent = lsGet('refineAgent', 'codex') === 'claude' ? 'claude' : 'codex';
    var pickRefine = lsGet(pickKey('pick.refine', refAgent), { model: '', effort: '' });
    function openRefine(retry) {
      var sh = sheet('AI로 다듬기 — ' + base(path), false);
      var pickIds = {};
      var cands = D.comments.filter(function (c) { return c.state === 'none' || c.state === 'input' || c.state === 'memo' || (retry && c.refine_id === retry.id); });
      cands.sort(function (a, b) { return (a.start == null ? 1e9 : a.start) - (b.start == null ? 1e9 : b.start); });
      sh.body.appendChild(el('div', 'sheet-sec', '고칠 코멘트'));
      var listBox = el('div', 'ck-list'); sh.body.appendChild(listBox);
      cands.forEach(function (c) {
        var on = retry ? retry.comment_ids.indexOf(c.id) >= 0 : c.state !== 'memo';
        if (on) pickIds[c.id] = 1;
        var row = el('label', 'ck-row');
        var ck = el('input'); ck.type = 'checkbox'; ck.checked = on;
        ck.addEventListener('change', function () { if (ck.checked) pickIds[c.id] = 1; else delete pickIds[c.id]; paintSend(); });
        var tx = el('span', 'ck-text');
        tx.appendChild(el('span', 'ck-main', c.text ? '💬 ' + c.text : '💬 (의견 없음 — 다듬어 줘)'));
        tx.appendChild(el('span', 'ck-quote', '“' + (c.quote || '').replace(/\s+/g, ' ') + '”'));
        row.appendChild(ck); row.appendChild(tx); listBox.appendChild(row);
      });
      if (!cands.length) listBox.appendChild(el('div', 'muted', '코멘트가 없습니다'));
      sh.body.appendChild(el('div', 'sheet-sec', '추가 요청'));
      var ta = el('textarea'); ta.rows = 2; ta.placeholder = '선택 — 예: 문장은 짧게, 표는 유지';
      ta.value = retry ? retry.instruction || '' : lsGet('refineDraft.' + path, '') || '';
      ta.addEventListener('input', function () { lsSet('refineDraft.' + path, ta.value || null); paintSend(); });
      sh.body.appendChild(ta);
      var target = 'new';
      // 새 세션으로 보낼 때만 에이전트를 고른다(연결된 세션은 그 세션의 에이전트)
      var agentSec = el('div', 'sheet-sec', '에이전트');
      var aseg = agentSeg(function () { return refAgent; }, function (a) {
        refAgent = a; lsSet('refineAgent', a);
        pickRefine = lsGet(pickKey('pick.refine', a), { model: '', effort: '' });
        chip.repaint();
      });
      if (from) {
        sh.body.appendChild(el('div', 'sheet-sec', '보낼 곳'));
        var seg = el('div', 'seg'); sh.body.appendChild(seg);
        [['new', '새 세션'], ['session', '연결된 세션']].forEach(function (o) {
          var b = btn(o[1], target === o[0] ? 'on' : '', function () {
            target = o[0]; seg.querySelectorAll('button').forEach(function (x) { x.classList.toggle('on', x === b); });
            agentSec.hidden = aseg.el.hidden = target !== 'new';
            chip.repaint();
          });
          seg.appendChild(b);
        });
      }
      sh.body.appendChild(agentSec); sh.body.appendChild(aseg.el);
      var foot = el('div', 'sheet-foot');
      // 연결된 세션으로 보내면 고르지 않은 모델·effort 는 그 세션의 값이 이어진다 — 칩도 그 값을 보인다
      var fromCur = null;
      var chip = pickChip(function () { return pickRefine; }, function (p) { pickRefine = p; lsSet(pickKey('pick.refine', refAgent), p); },
        { agent: function () { return target === 'new' ? refAgent : (fromCur && fromCur.agent) || null; },
          base: function () { return target === 'session' ? fromCur : null; } });
      if (from) api('GET', '/api/models?thread=' + encodeURIComponent(from)).then(function (r) { fromCur = Object.assign({ agent: r.agent }, r.current); chip.repaint(); }, function () {});
      var send = btn('다듬기 요청', 'primary', function () {
        var ids = cands.filter(function (c) { return pickIds[c.id]; }).map(function (c) { return c.id; });
        sh.close();
        startRefine({ path: path, root: root, comment_ids: ids, instruction: ta.value.trim(), model: pickRefine.model || '',
          effort: pickRefine.effort || '', tier: pickRefine.tier || '', session_id: target === 'session' ? from : null, agent: target === 'new' ? refAgent : null,
          client_id: uuid4() });
      });
      foot.appendChild(chip.el); foot.appendChild(el('span', 'grow')); foot.appendChild(send);
      sh.body.appendChild(foot);
      function paintSend() { send.disabled = !Object.keys(pickIds).length && !ta.value.trim(); }
      paintSend();
    }
    function startRefine(req) {
      if (D.sending && D.sending.status === 'running') return;
      D.sending = { id: req.client_id, status: 'running', sending: true, created_at: Date.now() / 1000, comment_ids: req.comment_ids, instruction: req.instruction, req: req };
      ui.seen = null; saveUi();
      paintPanel();
      api('POST', '/api/md-refines', req).then(function (r) {
        D.sending = null;
        lsSet('refineDraft.' + path, null);
        D.refines = [r.refine].concat(D.refines.filter(function (x) { return x.id !== r.refine.id; }));
        r.refine.comment_ids.forEach(function (id) {   // 다듬기로 보낸 코멘트는 세션 입력창 초안에서 뺀다(두 번 보내지 않게)
          var c = D.comments.filter(function (x) { return x.id === id; })[0];
          if (!c) return;
          if (c.state === 'input') dropFromDraft(c);
          c.state = 'sent'; c.refine_id = r.refine.id;
        });
        D.redraw(); paintPanel(); watch();
      }, function (e) {
        D.sending = Object.assign(D.sending || {}, { status: 'failed', error: '보내지 못했습니다: ' + e.message, sending: false, req: req });
        paintPanel();
      });
    }

    // ------------------------------------------------ 진행 표시(이 문서의 마지막 다듬기)
    var es = null, esFor = null, pollT = null, live = null, reloadT = null;
    function watch() {
      var r = latest();
      var tid = r && r.status === 'running' && r.thread_id;
      if (tid === esFor) return;
      if (es) { es.close(); es = null; }
      clearInterval(pollT); esFor = tid || null; live = null;
      if (!tid) return;
      es = new EventSource('/api/sessions/' + encodeURIComponent(tid) + '/events');
      es.onmessage = function (m) {
        var ev; try { ev = JSON.parse(m.data); } catch (e) { return; }
        if (ev.type === 'live' && ev.turn && (!r.turn_id || ev.turn.id === r.turn_id)) { live = ev.turn; paintPanel(); }
        else if (ev.type === 'turn_done') { clearTimeout(reloadT); reloadT = setTimeout(load, 500); }
      };
      pollT = setInterval(function () {   // 알림을 놓쳐도 끝을 알아챈다
        api('GET', '/api/md-refines?path=' + encodeURIComponent(path)).then(function (x) {
          if (seq !== S.seq) return;
          if (x.refines[0] && x.refines[0].status !== 'running') load();
        }, function () {});
      }, 5000);
    }
    var tick = setInterval(function () { panel.querySelectorAll('[data-live-since]').forEach(paintLiveStatus); }, 1000);
    S.cleanup.push(function () { if (es) es.close(); clearInterval(pollT); clearInterval(tick); clearTimeout(reloadT); });
    function paintPanel() {
      var r = latest();
      panel.innerHTML = '';
      panel.hidden = !r || ui.seen === r.id;
      if (panel.hidden) return;
      panel.className = 'refine-panel ' + r.status;
      var head = el('div', 'rp-head');
      var acts = el('div', 'rp-acts');
      if (r.status === 'running') {
        var ls = el('div', 'live-status');
        ls.dataset.liveSince = live && live.live ? live.live.since : r.created_at;
        ls.dataset.lastSignal = live && live.live ? live.live.last_signal : r.created_at;
        ls.dataset.stage = r.sending ? '보내는 중' : live ? liveStage(live) : '에이전트가 문서를 읽는 중';
        paintLiveStatus(ls);
        head.appendChild(ls);
        if (!r.sending) {
          acts.appendChild(btn('멈춤', 'small', function () {
            api('POST', '/api/sessions/' + encodeURIComponent(r.thread_id) + '/interrupt', {}).then(function () { toast('멈추라고 보냈습니다'); }, function (e) { toast('멈추지 못했습니다: ' + e.message); });
          }));
          acts.appendChild(btn('세션 열기', 'small', function () { go('#/s/' + encodeURIComponent(r.thread_id)); }));
        }
      } else {
        var t = el('div', 'rp-title');
        var label = r.reverted ? '되돌렸습니다' : r.sending === false ? r.error : REFINE_STATE[r.status] || r.status;
        t.appendChild(el('b', null, label));
        var bits = [];
        if (r.status === 'completed' && !r.reverted) bits.push(r.diff ? (r.diff.changed ? '바뀐 줄 +' + r.diff.added + ' −' + r.diff.removed : '문서는 그대로') : '');
        if (r.error && r.sending !== false) bits.push(r.error);
        bits.push((r.comment_ids || []).length + '개 코멘트');
        if (r.finished_at) bits.push(ago(r.finished_at) + ' · ' + dur((r.finished_at - r.created_at) * 1000));
        t.appendChild(el('span', 'rp-meta', bits.filter(Boolean).join(' · ')));
        head.appendChild(t);
        if (r.sending === false) acts.appendChild(btn('다시 시도', 'small primary', function () { startRefine(r.req); }));
        else {
          if (r.status === 'completed' && r.current && r.diff && r.diff.changed && !r.reverted) {
            var tg = btn(ui.changes ? '바뀐 곳 숨기기' : '바뀐 곳 보기', 'small', function () { ui.changes = !ui.changes; saveUi(); D.redraw(); paintPanel(); });
            tg.setAttribute('aria-pressed', ui.changes ? 'true' : 'false'); acts.appendChild(tg);
          }
          if (r.status !== 'completed') acts.appendChild(btn('다시 요청', 'small primary', function () { openRefine(r); }));
          if (r.answer) {
            var ab = btn('답 보기', 'small', function () { var o = panel.querySelector('.rp-answer'); o.hidden = !o.hidden; ab.textContent = o.hidden ? '답 보기' : '답 접기'; });
            acts.appendChild(ab);
          }
          if (r.can_revert) {
            var rv = btn('되돌리기', 'small del', function () {
              if (!rv.classList.contains('armed')) {
                rv.classList.add('armed'); rv.textContent = '정말 되돌리기';
                setTimeout(function () { if (rv.isConnected) { rv.classList.remove('armed'); rv.textContent = '되돌리기'; } }, 4000);
                return;
              }
              rv.disabled = true;
              api('POST', '/api/md-refines/' + encodeURIComponent(r.id) + '/revert', {}).then(function () { toast('다듬기 전 글로 되돌렸습니다'); load(); },
                function (e) { rv.disabled = false; toast(e.message); });
            }, '이 문서를 다듬기 전 글로 되돌리고 코멘트를 다시 연다');
            acts.appendChild(rv);
          }
          acts.appendChild(btn('세션 열기', 'small', function () { go('#/s/' + encodeURIComponent(r.thread_id)); }));
        }
        acts.appendChild(btn('✕', 'small icon', function () { ui.seen = r.id; saveUi(); if (D.sending === r) D.sending = null; paintPanel(); D.redraw(); }, '닫기'));
      }
      panel.appendChild(head);
      panel.appendChild(acts);
      if (r.answer) { var a = el('div', 'md rp-answer'); a.innerHTML = md.render(r.answer); a.hidden = true; panel.appendChild(a); }
    }
    function onVis() { if (document.visibilityState === 'visible' && !editing) load(); }
    document.addEventListener('visibilitychange', onVis);
    S.cleanup.push(function () { document.removeEventListener('visibilitychange', onVis); });
    load().then(function () { restoreScroll(pop); });
  }

  render(!!(history.state && savedScroll() != null));
})();
