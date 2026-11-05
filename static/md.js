/* 작은 마크다운 렌더러 — 원문 한 줄마다 span.md-line 을 둔다(줄 클릭 질문·코멘트의 단위).
   모든 글은 이스케이프한다. 지원: 제목, 문단(줄바꿈은 <br>), 목록(중첩), 코드 블록, 인용, 표, 구분선, 인라인 서식·링크,
   그리고 답변에 자주 섞이는 HTML 중 접기(<details>·<summary>, 한 줄에 두어야 한다)와 줄바꿈(<br>)만 — 나머지 태그는 글자 그대로. */
(function () {
  'use strict';
  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  // 로컬 경로 링크: .md 는 앱 안의 문서 보기로, 나머지는 경로 표시(누르면 복사)
  function linkHtml(label, href) {
    var h = href.trim();
    var local = /^(\/|~\/|file:\/\/|\.{1,2}\/)/.test(h) || (!/^[a-z][a-z0-9+.-]*:/i.test(h) && !h.startsWith('#'));
    if (local) {
      var p = h.replace(/^file:\/\//, '');
      try { p = decodeURIComponent(p); } catch (e) {}
      var clean = p.replace(/#.*$/, '').replace(/:\d+(:\d+)?$/, '');
      if (/\.(md|markdown)$/i.test(clean)) {
        return '<a href="#" class="md-doc" data-md-path="' + esc(clean) + '" title="' + esc(clean) + '">' + label + '</a>';
      }
      return '<span class="fpath" data-path="' + esc(p) + '" title="' + esc(p) + ' — 누르면 경로 복사">' + label + '</span>';
    }
    if (/^javascript:/i.test(h)) return label;
    return extLink(h, label);
  }
  // 바깥(http·https) 링크 — class ext 는 앱 안의 웹 보기(#/web)로 연다. 수정키를 누르고 누르면 새 탭.
  function extLink(href, label) { return '<a class="ext" href="' + esc(href) + '" target="_blank" rel="noopener">' + label + '</a>'; }
  // 맨 URL 끝의 문장 부호·닫는 따옴표는 링크에서 뺀다(이스케이프된 뒤라 &quot; 도 본다)
  var BARE_URL = /(^|[\s(])(https?:\/\/[^\s<)\u0080-\uffff]+)/g;
  function bare(_, pre, url) {
    var tail = /(?:[.,;:!?'\]]|&quot;|&#39;)+$/.exec(url), rest = '';
    if (tail) { rest = tail[0]; url = url.slice(0, -rest.length); }
    return pre + extLink(url.replace(/&amp;/g, '&'), url) + rest;
  }
  function inline(s) {
    var codes = [];
    s = s.replace(/`([^`]+)`/g, function (_, c) { codes.push(c); return '\u0000' + (codes.length - 1) + '\u0000'; });
    s = esc(s);
    s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, function (_, alt) { return '[' + alt + ']'; });
    s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, function (_, label, href) { return linkHtml(label, href.replace(/&amp;/g, '&')); });
    s = s.replace(BARE_URL, bare);
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/__([^_]+)__/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>');
    s = s.replace(/~~([^~]+)~~/g, '<del>$1</del>');
    s = s.replace(/&lt;br\s*\/?&gt;/gi, '<br>');
    s = s.replace(/\u0000(\d+)\u0000/g, function (_, i) {
      var c = codes[+i];
      if (/^(\/|~\/)[^\s]+\.(md|markdown)$/i.test(c)) return '<a href="#" class="md-doc" data-md-path="' + esc(c) + '"><code>' + esc(c) + '</code></a>';
      if (/^https?:\/\/[^\s]+$/i.test(c)) return extLink(c, '<code>' + esc(c) + '</code>');
      return '<code>' + esc(c) + '</code>';
    });
    return s;
  }
  function line(n, html) { return '<span class="md-line" data-line="' + n + '">' + (html || '&#8203;') + '</span>'; }

  function render(src) {
    var lines = String(src || '').replace(/\r\n?/g, '\n').split('\n');
    var out = [], i = 0;
    function isTableSep(s) { return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(s); }
    function cells(s) { return s.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(function (c) { return c.trim(); }); }
    function listRe(s) { return /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(s); }
    // 접기: <details[ open]>[<summary>제목</summary>] … </details>. 제목은 다음 줄의 <summary> 여도 된다.
    var DETAILS = /^\s*<details(\s+open)?\s*>\s*(?:<summary>(.*?)<\/summary>)?\s*$/i;
    var SUMMARY = /^\s*<summary>(.*?)<\/summary>\s*$/i;
    var DETAILS_END = /^\s*<\/details>\s*$/i;
    function isDetails(s) { return DETAILS.test(s) || DETAILS_END.test(s); }
    var depth = 0;
    while (i < lines.length) {
      var l = lines[i];
      var fence = /^(\s*)(```|~~~)\s*([\w+-]*)/.exec(l);
      if (fence) {
        var body = [], j = i + 1;
        while (j < lines.length && !new RegExp('^\\s*' + fence[2]).test(lines[j])) { body.push(line(j, esc(lines[j]))); j++; }
        out.push('<pre class="md-pre"' + (fence[3] ? ' data-lang="' + esc(fence[3]) + '"' : '') + '><code>' + body.join('\n') + '</code></pre>');
        i = j + 1; continue;
      }
      if (!l.trim()) { i++; continue; }
      var dm = DETAILS.exec(l);
      if (dm) {
        var title = dm[2], n = i + 1;
        if (title == null) {
          while (n < lines.length && !lines[n].trim()) n++;
          var sm = n < lines.length && SUMMARY.exec(lines[n]);
          if (sm) { title = sm[1]; i = n; }
        }
        out.push('<details class="md-details"' + (dm[1] ? ' open' : '') + '><summary>' + (title && title.trim() ? inline(title.trim()) : '자세히') + '</summary>');
        depth++; i++; continue;
      }
      if (DETAILS_END.test(l) && depth) { out.push('</details>'); depth--; i++; continue; }
      var h = /^(#{1,6})\s+(.*)$/.exec(l);
      if (h) { out.push('<h' + h[1].length + '>' + line(i, inline(h[2])) + '</h' + h[1].length + '>'); i++; continue; }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(l)) { out.push('<hr>'); i++; continue; }
      if (/^\s*>/.test(l)) {
        var q = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) { q.push(line(i, inline(lines[i].replace(/^\s*>\s?/, '')))); i++; }
        out.push('<blockquote><p>' + q.join('<br>') + '</p></blockquote>');
        continue;
      }
      if (l.indexOf('|') >= 0 && i + 1 < lines.length && isTableSep(lines[i + 1])) {
        var head = cells(l), rows = [], k = i + 2;
        while (k < lines.length && lines[k].indexOf('|') >= 0 && lines[k].trim()) { rows.push([k, cells(lines[k])]); k++; }
        var t = '<div class="tbl"><table><thead><tr class="md-line" data-line="' + i + '">' + head.map(function (c) { return '<th>' + inline(c) + '</th>'; }).join('') + '</tr></thead><tbody>';
        rows.forEach(function (r) { t += '<tr class="md-line" data-line="' + r[0] + '">' + r[1].map(function (c) { return '<td>' + inline(c) + '</td>'; }).join('') + '</tr>'; });
        out.push(t + '</tbody></table></div>');
        i = k; continue;
      }
      if (listRe(l)) {
        // 들여쓰기로 중첩을 만든다
        var stack = [], html = '';
        while (i < lines.length) {
          var m = listRe(lines[i]);
          if (!m) {
            if (lines[i].trim() && stack.length && /^\s+/.test(lines[i])) {   // 항목의 이어지는 줄
              html += '<br>' + line(i, inline(lines[i].trim())); i++; continue;
            }
            break;
          }
          var ind = m[1].replace(/\t/g, '    ').length, ordered = /\d/.test(m[2]);
          while (stack.length && ind < stack[stack.length - 1].ind) { html += '</li></' + stack.pop().tag + '>'; }
          var top = stack[stack.length - 1];
          if (!top || ind > top.ind) {
            var tag = ordered ? 'ol' : 'ul';
            var startAttr = ordered && parseInt(m[2], 10) !== 1 ? ' start="' + parseInt(m[2], 10) + '"' : '';
            html += '<' + tag + startAttr + '>';
            stack.push({ ind: ind, tag: tag });
          } else {
            html += '</li>';
          }
          var task = /^\[([ xX])\]\s+(.*)$/.exec(m[3]);
          html += '<li>' + (task ? '<span class="task">' + (task[1] === ' ' ? '☐' : '☑') + '</span>' + line(i, inline(task[2])) : line(i, inline(m[3])));
          i++;
          if (i < lines.length && !lines[i].trim() && i + 1 < lines.length && listRe(lines[i + 1])) i++;   // 항목 사이 빈 줄
        }
        while (stack.length) html += '</li></' + stack.pop().tag + '>';
        out.push(html);
        continue;
      }
      var para = [];
      while (i < lines.length && lines[i].trim() && !/^(#{1,6})\s/.test(lines[i]) && !/^\s*(```|~~~)/.test(lines[i]) &&
             !(para.length && isDetails(lines[i])) &&
             !(para.length && (listRe(lines[i]) || /^\s*>/.test(lines[i]))) &&
             !(lines[i].indexOf('|') >= 0 && i + 1 < lines.length && isTableSep(lines[i + 1]))) {
        para.push(line(i, inline(lines[i]))); i++;
      }
      out.push('<p>' + para.join('<br>') + '</p>');
    }
    while (depth-- > 0) out.push('</details>');
    return out.join('\n');
  }
  window.CodexMd = { render: render, esc: esc };
})();
