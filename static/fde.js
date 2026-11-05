/* Codex Lens — FDE 케이스 화면(#/case?cwd=&tab=). 문제 하나를 이해 → 계획 → 구현 → 검증 → 사용 사이클로 다룬다.
   원본은 서버 fde.py(작업 폴더 fde/case.json). 요지가 먼저, 세부는 접는다. 사람이 정할 것은 선택지 한 번 누르기로 끝난다. */
(function () {
  'use strict';
  var TABS = [['understand', '이해'], ['plan', '계획'], ['build', '구현'], ['verify', '검증'], ['use', '사용']];
  var STEP = { understand: '이해 카드', plan: '계획', ask: '질문' };

  function view(C, params, pop) {
    var el = C.el, btn = C.btn, api = C.api, toast = C.toast, seq = C.S.seq, root = C.view;
    var cwd = params.cwd || '', tab = params.tab || '', data = null, first = true, timer = null, stamp = '', models = null, running = false;
    var opened = (history.state && history.state.fdeOpen) || {};
    C.setHeader('케이스', [cwd ? cwd.split('/').filter(Boolean).pop() : '문제를 이해 → 검증까지'], true);
    C.S.cleanup.push(function () { clearTimeout(timer); });
    function alive() { return seq === C.S.seq; }
    function url(t) { return '#/case?cwd=' + encodeURIComponent(cwd) + (t ? '&tab=' + t : ''); }
    function saveUrl() { history.replaceState(Object.assign({}, history.state, { fdeOpen: opened }), '', url(tab)); }
    function dk(k) { return 'fde.draft.' + cwd + '.' + k; }
    function draft(k, v) { if (v === undefined) return C.lsGet(dk(k), ''); C.lsSet(dk(k), v || null); }
    function last(a) { return a && a.length ? a[a.length - 1] : null; }
    function fmt(t) { var d = new Date(t * 1000); return (d.getMonth() + 1) + '/' + d.getDate() + ' ' + d.getHours() + ':' + ('0' + d.getMinutes()).slice(-2); }
    if (!cwd) return pickFolder();

    // ---------------------------------------------------------- 부품
    function fold(key, summary, build, dflt) {
      var d = el('details', 'fde-fold'), s = el('summary', null, summary), body = el('div', 'fde-fold-body'), built = false;
      d.open = opened[key] == null ? !!dflt : !!opened[key];
      d.appendChild(s); d.appendChild(body);
      function ensure() { if (d.open && !built) { built = true; build(body); } }
      d.addEventListener('toggle', function () { opened[key] = d.open; saveUrl(); ensure(); });
      ensure();
      return d;
    }
    function md(text) { var b = el('div', 'fde-md'); b.innerHTML = C.md.render(text || ''); return b; }
    function bullets(items) { var u = el('ul', 'fde-list'); (items || []).forEach(function (t) { u.appendChild(el('li', null, t)); }); return u; }
    function badge(t, cls) { return el('span', 'fde-badge ' + (cls || ''), t); }
    function card(cls) { return el('section', 'fde-card ' + (cls || '')); }
    function act(action, body) {
      return api('POST', '/api/fde/act/' + action, Object.assign({ cwd: cwd }, body || {})).then(function (r) { accept(r); return r.result; },
        function (e) { toast(e.message); throw e; });
    }
    // 선택지 — 한 번 누르면 정해지고 결정 기록에 남는다. 직접 입력도 된다.
    function choice(c) {
      var box = el('div', 'fde-choice' + (c.decision ? ' done' : ''));
      box.appendChild(el('div', 'fde-choice-q', c.question));
      if (c.why) box.appendChild(el('div', 'fde-dim', c.why));
      if (c.decision) { box.appendChild(el('div', 'fde-chosen', '→ ' + c.answer + ' (' + c.decision + ')')); return box; }
      var opts = el('div', 'fde-opts');
      function pick(label) {
        var all = box.querySelectorAll('button');
        Array.prototype.forEach.call(all, function (b) { b.disabled = true; });
        act('decide', { id: c.id, chosen: label }).then(null, function () { Array.prototype.forEach.call(all, function (b) { b.disabled = false; }); });
      }
      c.options.forEach(function (o) {
        var b = el('button', 'fde-opt'); b.type = 'button';
        var t = el('div', 'fde-opt-label', o.label); if (o.recommended) t.appendChild(badge('추천', 'rec'));
        b.appendChild(t); if (o.effect) b.appendChild(el('div', 'fde-dim', o.effect));
        b.addEventListener('click', function () { pick(o.label); });
        opts.appendChild(b);
      });
      var own = el('div', 'fde-row'), inp = el('input'); inp.placeholder = '직접 입력';
      own.appendChild(inp); own.appendChild(btn('정하기', 'small', function () { if (inp.value.trim()) pick(inp.value.trim()); }));
      box.appendChild(opts); box.appendChild(own);
      return box;
    }
    function choices(list) { var w = el('div'); (list || []).forEach(function (c) { w.appendChild(choice(c)); }); return w; }
    // 피드백 · 질문 — 초안은 대상별로 남는다
    function talk(target) {
      var wrap = el('div', 'fde-talk'), bar = el('div', 'fde-row'), area = el('div'), mode = null;
      var ta = el('textarea'); ta.rows = 3; area.hidden = true;
      var send = btn('보내기', 'primary small', function () {
        var text = ta.value.trim(); if (!text) return;
        send.disabled = true;
        var m = mode, p = m === 'fb' ? act('feedback', { target: target, text: text }) : runStep('ask', { target: target, question: text });
        p.then(function () { draft(m + '.' + target, ''); ta.value = ''; area.hidden = true; }, function () {}).then(function () { send.disabled = false; });
      });
      function open(m) {
        mode = m; ta.placeholder = m === 'fb' ? '내 생각 — "반영해 다시"로 다음 버전에 들어갑니다' : '이 결과에 대해 묻기';
        ta.value = draft(m + '.' + target); area.hidden = false; ta.focus();
      }
      ta.addEventListener('input', function () { draft(mode + '.' + target, ta.value); });
      bar.appendChild(btn('💬 피드백', 'small', function () { open('fb'); }));
      bar.appendChild(btn('질문하기', 'small', function () { open('ask'); }));
      area.appendChild(ta); var r = el('div', 'fde-row'); r.appendChild(send); r.appendChild(btn('닫기', 'small', function () { area.hidden = true; })); area.appendChild(r);
      wrap.appendChild(bar); wrap.appendChild(area);
      var qa = data.case.qa.filter(function (q) { return q.target === target; });
      if (qa.length) wrap.appendChild(fold('qa.' + target, '질문과 답 ' + qa.length, function (b) {
        qa.slice().reverse().forEach(function (q) {
          var it = el('div', 'fde-qa');
          it.appendChild(el('div', 'fde-dim', 'Q. ' + q.question));
          it.appendChild(el('b', null, q.data.headline)); it.appendChild(md(q.data.detail)); it.appendChild(choices(q.data.choices));
          b.appendChild(it);
        });
      }, true));
      return wrap;
    }
    function stepBtn(step, label, target) {
      var fb = data.case.feedback.filter(function (f) { return f.status === 'open' && f.target === target; }).length;
      var has = data.case[target].length, busy = data.case.pending.some(function (p) { return p.step === step; });
      var b = btn(busy ? STEP[step] + ' 만드는 중…' : !has ? label : fb ? '피드백 ' + fb + '개 반영해 다시' : '다시 만들기',
        !has || fb ? 'primary small' : 'small', function () { b.disabled = true; runStep(step).then(null, function () { b.disabled = false; }); });
      b.disabled = busy;
      return b;
    }
    function head(item) {
      var d = item.data, h = el('div', 'fde-head');
      h.appendChild(el('div', 'fde-headline', d.headline));
      h.appendChild(el('div', 'fde-dim', ['v' + item.v, item.model, item.effort, fmt(item.at)].concat((d.sources || []).length ? ['근거 ' + d.sources.join(' ')] : []).filter(Boolean).join(' · ')));
      if (d.summary.length) h.appendChild(bullets(d.summary));
      return h;
    }

    // ---------------------------------------------------------- AI 설정 · 실행
    var ai = C.lsGet('fde.ai', { agent: 'codex', model: '', effort: '' });
    function aiBar() {
      var w = el('div', 'fde-ai'), ag = el('select'), mo = el('select'), ef = el('select');
      ag.setAttribute('aria-label', '에이전트'); mo.setAttribute('aria-label', '모델'); ef.setAttribute('aria-label', 'effort');
      [['codex', 'Codex'], ['claude', 'Claude']].forEach(function (a) { var o = el('option', null, a[1]); o.value = a[0]; ag.appendChild(o); });
      ag.value = ai.agent;
      function fill() {
        mo.innerHTML = ''; ef.innerHTML = '';
        var hidden = (models && models.settings && models.settings.hidden) || [];
        var list = ((models && models.models) || []).filter(function (m) { return m.agent === ai.agent && hidden.indexOf(m.id) < 0; });
        var o0 = el('option', null, '기본 모델'); o0.value = ''; mo.appendChild(o0);
        list.forEach(function (m) { var o = el('option', null, m.label || m.id); o.value = m.id; mo.appendChild(o); });
        if (!list.some(function (m) { return m.id === ai.model; })) ai.model = '';
        mo.value = ai.model;
        var dm = ((models && models.settings && models.settings.defaults) || {})[ai.agent] || {};
        var cur = list.filter(function (m) { return m.id === (ai.model || dm.model); })[0] || list.filter(function (m) { return m.default; })[0];
        var efforts = (cur && cur.efforts) || [];
        var e0 = el('option', null, '기본 effort'); e0.value = ''; ef.appendChild(e0);
        efforts.forEach(function (e) { var o = el('option', null, e); o.value = e; ef.appendChild(o); });
        if (efforts.indexOf(ai.effort) < 0) ai.effort = '';
        ef.value = ai.effort; ef.disabled = !efforts.length;
        C.lsSet('fde.ai', ai);
      }
      ag.addEventListener('change', function () { ai.agent = ag.value; ai.model = ''; fill(); });
      mo.addEventListener('change', function () { ai.model = mo.value; fill(); });
      ef.addEventListener('change', function () { ai.effort = ef.value; C.lsSet('fde.ai', ai); });
      w.appendChild(ag); w.appendChild(mo); w.appendChild(ef);
      if (models) fill(); else api('GET', '/api/models').then(function (r) { models = r; if (alive()) fill(); }, fill);
      return w;
    }
    function runStep(step, extra) {
      return api('POST', '/api/fde/step', Object.assign({ cwd: cwd, step: step, agent: ai.agent, model: ai.model || null, effort: ai.effort || null,
        client_id: C.uuid4().replace(/-/g, '').slice(0, 16) }, extra || {})).then(function (r) { C.clientLog({ type: 'fde_step', detail: step }); accept(r); return r; },
        function (e) { toast(e.message); throw e; });
    }

    // ---------------------------------------------------------- 읽기 · 그리기
    function load() {
      var t0 = performance.now();
      return api('GET', '/api/fde?cwd=' + encodeURIComponent(cwd)).then(function (r) {
        if (!alive()) return;
        accept(r);
        if (first) { first = false; C.restoreScroll(pop); C.clientLog({ type: 'fde_render', ms: Math.round(performance.now() - t0) }); }
      }, function (e) { if (alive()) { root.innerHTML = ''; root.appendChild(el('div', 'empty', '읽지 못했습니다: ' + e.message)); } });
    }
    var jobsEl = null;
    function accept(r) {
      if (!alive()) return;
      data = r;
      var s = JSON.stringify([r.case && r.case.updated_at, r.case && r.case.pending.length]);
      if (s !== stamp) { stamp = s; paint(); } else paintJobs();
      clearTimeout(timer);
      if (r.case && r.case.pending.length) timer = setTimeout(load, 1200);
    }
    function paintJobs() {
      if (!jobsEl) return;
      jobsEl.innerHTML = '';
      (data.jobs || []).forEach(function (j) {
        var row = el('div', 'fde-job' + (j.stalled ? ' stalled' : ''));
        row.appendChild(el('span', 'proc-spinner'));
        row.appendChild(el('span', 'grow', STEP[j.step] + ' 만드는 중 · ' + Math.round(j.elapsed_sec || 0) + '초' + (j.stalled ? ' · 지연 — ' + Math.round(j.idle_sec) + '초째 신호 없음' : '')));
        row.appendChild(btn('취소', 'small', function () { api('POST', '/api/jobs/' + encodeURIComponent(j.id) + '/cancel', {}).then(load, function (e) { toast(e.message); }); }));
        jobsEl.appendChild(row);
      });
    }
    function openChoices(item) { return item ? item.data.choices.filter(function (x) { return !x.decision; }).length : 0; }
    function paint() {
      var y = window.scrollY, c = data.case;
      root.innerHTML = '';
      if (!c) return start();
      if (!tab) tab = !c.understanding.length ? 'understand' : !c.plan.length ? 'plan' : !c.runs.length ? 'verify' : 'use';
      var nav = el('nav', 'fde-steps'); nav.setAttribute('aria-label', '단계');
      TABS.forEach(function (t) {
        var n = t[0] === 'understand' ? openChoices(last(c.understanding)) : t[0] === 'plan' ? openChoices(last(c.plan)) : 0;
        var b = btn('', 'fde-step' + (tab === t[0] ? ' on' : ''), function () { tab = t[0]; saveUrl(); paint(); window.scrollTo(0, 0); });
        b.appendChild(el('span', null, t[1])); if (n) b.appendChild(badge(String(n), 'n'));
        nav.appendChild(b);
      });
      root.appendChild(nav);
      var tools = el('div', 'fde-tools'); tools.appendChild(aiBar());
      var rep = el('a', 'fde-dim', '보고서'); rep.href = '#/md?path=' + encodeURIComponent(data.report) + '&root=' + encodeURIComponent(cwd);
      tools.appendChild(rep); root.appendChild(tools);
      jobsEl = el('div', 'fde-jobs'); root.appendChild(jobsEl); paintJobs();
      if (c.errors.length) {
        var er = el('div', 'fde-error');
        er.appendChild(el('span', 'grow', STEP[last(c.errors).step] + ' 실패: ' + last(c.errors).error));
        er.appendChild(btn('닫기', 'small', function () { act('dismiss_errors'); }));
        root.appendChild(er);
      }
      var p = el('div', 'fde-panel');
      ({ understand: pUnderstand, plan: pPlan, build: pBuild, verify: pVerify, use: pUse })[tab](p, c);
      root.appendChild(p);
      if (!first) window.scrollTo(0, y);
    }

    function pickFolder() {
      var f = card(), input = el('input'), chips = el('div', 'fde-row wrap');
      f.appendChild(el('div', 'fde-headline', '어느 작업 폴더의 문제를 다룰까요?'));
      input.placeholder = '/절대/경로'; input.value = C.lsGet('fde.lastCwd', '');
      function go(v) { if (v) { C.lsSet('fde.lastCwd', v); C.go('#/case?cwd=' + encodeURIComponent(v)); } }
      input.addEventListener('keydown', function (e) { if (e.key === 'Enter') go(input.value.trim()); });
      f.appendChild(input); f.appendChild(chips);
      var r = el('div', 'fde-row'); r.appendChild(btn('열기', 'primary', function () { go(input.value.trim()); })); f.appendChild(r);
      root.appendChild(f);
      api('GET', '/api/cwds').then(function (r) { (r.cwds || []).slice(0, 10).forEach(function (x) { chips.appendChild(btn(x.split('/').filter(Boolean).pop() || x, 'small', function () { go(x); }, x)); }); }, function () {});
      C.restoreScroll(pop);
    }
    function start() {
      var f = card(), ta = el('textarea');
      f.appendChild(el('div', 'fde-headline', '이 폴더에서 케이스를 시작합니다'));
      f.appendChild(el('div', 'fde-dim', cwd + '/fde/ 에 기록이 남습니다'));
      ta.rows = 6; ta.placeholder = '문제를 그대로 적거나 붙여 넣으세요'; ta.value = draft('problem');
      ta.addEventListener('input', function () { draft('problem', ta.value); });
      f.appendChild(ta);
      var b = btn('시작', 'primary', function () {
        if (!ta.value.trim()) return toast('문제를 적어 주세요');
        b.disabled = true;
        api('POST', '/api/fde', { cwd: cwd, problem: ta.value.trim() }).then(function (r) { draft('problem', ''); accept(r); }, function (e) { toast(e.message); b.disabled = false; });
      });
      var r = el('div', 'fde-row'); r.appendChild(b); f.appendChild(r);
      root.appendChild(f);
    }

    // ---------------------------------------------------------- 1. 이해
    function pUnderstand(p, c) {
      p.appendChild(fold('problem', '문제 — ' + c.problem.split('\n')[0].slice(0, 50), function (b) {
        var ta = el('textarea'); ta.rows = 5; ta.value = draft('problem.edit') || c.problem;
        ta.addEventListener('input', function () { draft('problem.edit', ta.value); });
        b.appendChild(ta); b.appendChild(btn('저장', 'small', function () { act('problem', { problem: ta.value }).then(function () { draft('problem.edit', ''); }); }));
      }));
      p.appendChild(fold('ctx', '컨텍스트 ' + c.context.length, function (b) {
        c.context.forEach(function (x) {
          var row = el('div', 'fde-ctx'); row.appendChild(badge(x.id)); row.appendChild(el('span', 'grow', x.path || x.title));
          row.appendChild(btn('빼기', 'small', function () { act('context_remove', { id: x.id }); })); b.appendChild(row);
        });
        var inp = el('input'), dl = el('datalist'), t = null; inp.placeholder = '파일 · 폴더 경로(작업 폴더 기준)'; dl.id = 'fde-files'; inp.setAttribute('list', 'fde-files');
        inp.addEventListener('input', function () {
          clearTimeout(t); t = setTimeout(function () {
            api('GET', '/api/files?cwd=' + encodeURIComponent(cwd) + '&q=' + encodeURIComponent(inp.value) + '&limit=20').then(function (r) {
              dl.innerHTML = ''; (r.files || r.items || []).forEach(function (f) { var o = el('option'); o.value = typeof f === 'string' ? f : (f.path || ''); dl.appendChild(o); });
            }, function () {});
          }, 200);
        });
        var add = btn('추가', 'small', function () { if (inp.value.trim()) act('context', { path: inp.value.trim() }).then(function () { inp.value = ''; }); });
        var r = el('div', 'fde-row'); r.appendChild(inp); r.appendChild(dl); r.appendChild(add); b.appendChild(r);
        var ta = el('textarea'); ta.rows = 2; ta.placeholder = '글 붙여 넣기(담당자 답변 · 메모)'; ta.value = draft('ctx');
        ta.addEventListener('input', function () { draft('ctx', ta.value); });
        b.appendChild(ta);
        b.appendChild(btn('글 추가', 'small', function () { if (ta.value.trim()) act('context', { kind: 'text', text: ta.value.trim() }).then(function () { draft('ctx', ''); }); }));
      }, !c.context.length));
      var bar = el('div', 'fde-row'); bar.appendChild(stepBtn('understand', '이해 카드 만들기', 'understanding')); p.appendChild(bar);
      var it = last(c.understanding);
      if (!it) return p.appendChild(el('div', 'fde-empty', '문제와 컨텍스트를 쉬운 형태(문제 · 입력 · 결과 · 규칙 · 성공의 모습 · 모르는 것)로 바꿔 보여 드립니다.'));
      var k = card('main');
      k.appendChild(head(it)); k.appendChild(choices(it.data.choices));
      if (it.data.detail) k.appendChild(fold('und.detail', '자세히', function (b) { b.appendChild(md(it.data.detail)); }));
      k.appendChild(talk('understanding'));
      p.appendChild(k);
    }

    // ---------------------------------------------------------- 2. 계획(접근 · 메인 알고리즘 · 비교)
    function pPlan(p, c) {
      var bar = el('div', 'fde-row'); bar.appendChild(stepBtn('plan', '계획 만들기', 'plan')); p.appendChild(bar);
      var it = last(c.plan);
      if (!it) return p.appendChild(el('div', 'fde-empty', c.understanding.length ? '핵심 모듈 · 메인 알고리즘 · 해결 방법 비교 · 검증 명령을 한 번에 세웁니다.' : '이해 카드를 먼저 만듭니다.'));
      var d = it.data, k = card('main');
      k.appendChild(head(it));
      if (d.change) k.appendChild(el('div', 'fde-dim', '바꾼 점: ' + d.change));
      k.appendChild(choices(d.choices));
      var mods = el('div', 'fde-mods');
      d.modules.forEach(function (m) {
        var row = el('div', 'fde-mod' + (m.main ? ' main' : ''));
        var h = el('div'); h.appendChild(el('b', null, m.name)); if (m.main) h.appendChild(badge('메인 알고리즘', 'rec'));
        row.appendChild(h); row.appendChild(el('div', 'fde-dim', m.role)); mods.appendChild(row);
      });
      k.appendChild(mods);
      var a = d.algorithm, box = el('div', 'fde-algo');
      box.appendChild(el('div', 'fde-label', '메인 알고리즘 — ' + a.name)); box.appendChild(el('div', null, a.idea));
      box.appendChild(fold('plan.algo', '단계 · 약점', function (b) {
        var ol = el('ol', 'fde-list'); a.steps.forEach(function (s) { ol.appendChild(el('li', null, s)); }); b.appendChild(ol);
        if (a.weaknesses.length) { b.appendChild(el('div', 'fde-label', '약점')); b.appendChild(bullets(a.weaknesses)); }
      }));
      k.appendChild(box);
      if (d.options.length && d.criteria.length) {
        k.appendChild(fold('plan.cmp', '해결 방법 비교 — ' + d.options.map(function (o) { return o.name; }).join(' · '), function (b) {
          var tbl = el('table', 'fde-table'), tr = el('tr');
          tr.appendChild(el('th', null, '기준 (지표)')); d.options.forEach(function (o) { tr.appendChild(el('th', null, o.name)); }); tbl.appendChild(tr);
          d.criteria.forEach(function (cr) {
            var row = el('tr'), th = el('td', 'crit', cr.name + (cr.metric ? ' (' + cr.metric + ')' : '')); th.title = cr.why; row.appendChild(th);
            d.options.forEach(function (o) { row.appendChild(el('td', null, o.eval[cr.name] || '–')); });
            tbl.appendChild(row);
          });
          var tw = el('div', 'fde-table-wrap'); tw.appendChild(tbl); b.appendChild(tw);
        }, true));
      }
      if (d.detail) k.appendChild(fold('plan.detail', '자세히', function (b) { b.appendChild(md(d.detail)); }));
      k.appendChild(talk('plan'));
      p.appendChild(k);
    }

    // ---------------------------------------------------------- 3. 구현
    function brief(c) {
      var plan = last(c.plan), d = plan.data, und = last(c.understanding);
      var chosen = d.choices.filter(function (x) { return x.decision; }).map(function (x) { return '- ' + x.question + ' → ' + x.answer; });
      var L = ['[FDE 케이스 구현 — 계획 v' + plan.v + ']', '', '## 문제', und.data.headline, '', '## 모듈'];
      d.modules.forEach(function (m) { L.push('- ' + m.name + (m.main ? ' (메인 알고리즘)' : '') + ': ' + m.role); });
      L.push('', '## 메인 알고리즘 — ' + d.algorithm.name, d.algorithm.idea);
      d.algorithm.steps.forEach(function (s, i) { L.push((i + 1) + '. ' + s); });
      if (chosen.length) L.push('', '## 정한 것', chosen.join('\n'));
      L.push('', '## 완료 기준');
      c.checks.forEach(function (x) { L.push('- `' + x.command + '` 가 종료 코드 0 (' + x.name + ')'); });
      L.push('', '판단이 필요한 선택은 선택지(추천 포함)로 남기고, 결정 기록은 `fde/CASE.md` 를 따른다. 끝나면 바뀐 파일과 돌린 검증 결과를 적는다.');
      return L.join('\n');
    }
    function pBuild(p, c) {
      if (!c.plan.length) return p.appendChild(el('div', 'fde-empty', '계획이 생기면 그 계획으로 구현 세션을 시작합니다.'));
      var text = brief(c), k = card('main');
      k.appendChild(el('div', 'fde-headline', '계획 v' + last(c.plan).v + '으로 구현 세션을 시작합니다'));
      k.appendChild(el('div', 'fde-dim', '누르면 아래 글이 새 세션 입력창에 들어갑니다. 에이전트 · 모델 · effort · 워크트리는 거기서 고릅니다.'));
      var r = el('div', 'fde-row');
      r.appendChild(btn('구현 세션 시작', 'primary small', function () {
        C.lsSet('draft.new', { text: text, mentions: [] }); C.lsSet('lastCwd', cwd); C.go('#/new?cwd=' + encodeURIComponent(cwd));
      }));
      k.appendChild(r);
      k.appendChild(fold('build.text', '보낼 글', function (b) { b.appendChild(el('pre', 'fde-pre', text)); }));
      p.appendChild(k);
    }

    // ---------------------------------------------------------- 4. 검증 · 메인 알고리즘 버전
    function pVerify(p, c) {
      var lastRun = {}; c.runs.forEach(function (r) { lastRun[r.check] = r; });
      var ok = c.checks.filter(function (x) { return lastRun[x.id] && lastRun[x.id].ok; }).length;
      var bad = c.checks.filter(function (x) { return lastRun[x.id] && !lastRun[x.id].ok; }).length;
      var k = card('main');
      k.appendChild(el('div', 'fde-headline', !c.checks.length ? '검증 명령이 아직 없습니다' : '검증 ' + c.checks.length + '개 · 통과 ' + ok + (bad ? ' · 실패 ' + bad : '')));
      k.appendChild(el('div', 'fde-dim', '통과는 종료 코드 0 으로만 정합니다. 출력의 METRIC 이름=값 줄은 지금 계획 버전의 지표가 됩니다.'));
      var bar = el('div', 'fde-row');
      var all = btn(running ? '실행 중…' : '모두 실행', 'primary small', function () { run([]); }); all.disabled = running || !c.checks.length;
      bar.appendChild(all); k.appendChild(bar);
      c.checks.forEach(function (x) {
        var r = lastRun[x.id], row = el('div', 'fde-check ' + (!r ? '' : r.ok ? 'ok' : 'bad'));
        var h = el('div', 'fde-row');
        h.appendChild(badge(!r ? '미실행' : r.ok ? '✓ 통과' : '✗ 실패', !r ? '' : r.ok ? 'ok' : 'warn'));
        h.appendChild(el('b', 'grow', x.name));
        h.appendChild(btn('실행', 'small', function () { run([x.id]); }));
        h.appendChild(btn('빼기', 'small', function () { act('check_remove', { id: x.id }); }));
        row.appendChild(h); row.appendChild(el('code', 'fde-cmd', x.command));
        if (r) {
          var m = Object.keys(r.metrics).map(function (n) { return n + '=' + r.metrics[n]; }).join(' · ');
          row.appendChild(el('div', 'fde-dim', fmt(r.at) + ' · 계획 v' + r.plan_v + (m ? ' · ' + m : '')));
          row.appendChild(fold('out.' + x.id, '출력', function (b) { b.appendChild(el('pre', 'fde-pre', r.tail || '(출력 없음)')); }, !r.ok));
        }
        k.appendChild(row);
      });
      k.appendChild(fold('check.add', '검증 명령 넣기', function (b) {
        var nm = el('input'), cmd = el('input'); nm.placeholder = '이름'; cmd.placeholder = '명령(작업 폴더에서 실행)';
        b.appendChild(nm); b.appendChild(cmd);
        b.appendChild(btn('넣기', 'small', function () { act('check_add', { name: nm.value, command: cmd.value }); }));
      }, !c.checks.length));
      p.appendChild(k);
      if (!c.plan.length) return;
      // 메인 알고리즘 버전 × 지표 — 계획을 다시 만들면 다음 버전
      var a = card(), names = {}, mt = data.metrics || {};
      Object.keys(mt).forEach(function (v) { Object.keys(mt[v]).forEach(function (n) { names[n] = 1; }); });
      names = Object.keys(names).sort();
      var h2 = el('div', 'fde-row'); h2.appendChild(el('b', 'grow', '메인 알고리즘 버전'));
      h2.appendChild(stepBtn('plan', '계획 만들기', 'plan')); a.appendChild(h2);
      a.appendChild(el('div', 'fde-dim', '"다시 만들기"는 최근 검증 결과 · 피드백 · 사용 기록을 넣어 알고리즘을 개선합니다.'));
      var tbl = el('table', 'fde-table'), tr = el('tr');
      ['계획', '알고리즘'].concat(names).forEach(function (t) { tr.appendChild(el('th', null, t)); }); tbl.appendChild(tr);
      c.plan.slice().reverse().forEach(function (pl) {
        var row = el('tr', pl === last(c.plan) ? 'cur' : null);
        row.appendChild(el('td', null, 'v' + pl.v)); var td = el('td', null, pl.data.algorithm.name); td.title = pl.data.change || ''; row.appendChild(td);
        names.forEach(function (n) { var v = (mt[pl.v] || {})[n]; row.appendChild(el('td', null, v == null ? '–' : String(v))); });
        tbl.appendChild(row);
      });
      var tw = el('div', 'fde-table-wrap'); tw.appendChild(tbl); a.appendChild(tw);
      p.appendChild(a);
    }
    function run(ids) {
      if (running) return;
      running = true; paint();
      var t0 = Date.now(), note = el('div', 'fde-job', '검증 실행 중 · 0초'); if (jobsEl) jobsEl.appendChild(note);
      var tick = setInterval(function () { note.textContent = '검증 실행 중 · ' + Math.round((Date.now() - t0) / 1000) + '초'; }, 1000);
      C.S.cleanup.push(function () { clearInterval(tick); });
      api('POST', '/api/fde/run', { cwd: cwd, ids: ids }).then(function (r) {
        running = false; clearInterval(tick);
        var ok = r.runs.filter(function (x) { return x.ok; }).length;
        toast('검증 ' + r.runs.length + '개 — 통과 ' + ok + (r.runs.length - ok ? ' · 실패 ' + (r.runs.length - ok) : ''));
        stamp = ''; accept(r);
      }, function (e) { running = false; clearInterval(tick); toast(e.message); paint(); });
    }

    // ---------------------------------------------------------- 5. 사용 · 기록
    function pUse(p, c) {
      var k = card('main'), ta = el('textarea');
      k.appendChild(el('div', 'fde-headline', '써 보니 어땠나요?'));
      k.appendChild(el('div', 'fde-dim', '남긴 기록은 다음 사이클의 이해 카드와 알고리즘 개선에 들어갑니다.'));
      ta.rows = 2; ta.placeholder = '기대대로였는지, 어긋난 것, 새로 필요한 것'; ta.value = draft('use');
      ta.addEventListener('input', function () { draft('use', ta.value); });
      k.appendChild(ta);
      var r = el('div', 'fde-row');
      r.appendChild(btn('남기기', 'primary small', function () { act('usage', { text: ta.value }).then(function () { draft('use', ''); }); }));
      r.appendChild(btn('다음 사이클 시작', 'small', function () { runStep('understand').then(function () { tab = 'understand'; saveUrl(); paint(); }); }));
      k.appendChild(r);
      c.usage.slice().reverse().forEach(function (u) { var d = el('div', 'fde-dec'); d.appendChild(el('div', 'fde-dim', fmt(u.at) + (u.plan_v ? ' · 계획 v' + u.plan_v : ''))); d.appendChild(el('div', null, u.text)); k.appendChild(d); });
      p.appendChild(k);
      var log = card();
      log.appendChild(el('b', null, '결정 기록 ' + c.decisions.length));
      c.decisions.slice().reverse().forEach(function (d) {
        var row = el('div', 'fde-dec');
        row.appendChild(el('div', 'fde-dim', d.id + ' · ' + fmt(d.at) + ' — ' + d.question));
        row.appendChild(el('div', 'fde-dec-a', '→ ' + d.chosen + (d.recommended && d.recommended !== d.chosen ? ' (추천은 ' + d.recommended + ')' : '')));
        log.appendChild(row);
      });
      p.appendChild(log);
    }

    root.appendChild(el('div', 'empty', '읽는 중…'));
    load();
  }

  window.CodexFde = { view: view };
})();
