/*
 * AICC Portal 관리 콘솔 — 4차 일괄 개편(2026-10-19) · 공통 부품 + 화면별 보정(표시 층)
 *
 * console-theme.js 와 같은 원칙: admin.html 의 화면 구조·문구·동작은 바꾸지 않는다.
 * 노드를 옮기거나(보기 설정), 클래스를 붙이거나(배지·빈 상태), 보조 요소를 덧붙일 뿐이다(탭·상세 패널·복사 버튼).
 * 데이터 값은 고치지 않는다 — 같은 알림을 묶을 때도 숨기기만 하고 건수를 옆에 적는다.
 * 화면이 다시 그려지면 MutationObserver 가 같은 처리를 다시 한다(모든 처리는 여러 번 돌아도 결과가 같다).
 */
(function () {
  'use strict';
  var S = 'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"';
  var IC = {
    sliders: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
    x: '<path d="M18 6 6 18M6 6l12 12"/>',
    copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    max: '<path d="M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3"/>',
    cl: '<path d="m15 18-6-6 6-6"/>',
    cr: '<path d="m9 18 6-6-6-6"/>'
  };
  function svg(n) { return '<svg class="ct-ic" ' + S + '>' + IC[n] + '</svg>'; }
  function $(s, r) { return (r || document).querySelector(s); }
  function $$(s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); }
  function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } return null; }

  // ── 1. 상단 바 「보기 설정」 — 언어·큰 글씨·봇 음성·비용·표 밀도를 한 메뉴로(노드를 옮길 뿐 동작은 그대로) ──
  function viewMenu() {
    var top = $('.topbar'); if (!top || $('.ct-wrapview', top)) return;
    var wrap = document.createElement('span'); wrap.className = 'ct-wrapview';
    wrap.innerHTML = '<button type="button" class="ct-viewbtn" aria-haspopup="true" aria-expanded="false">' + svg('sliders') + '보기</button>' +
      '<div class="ct-viewpop" role="dialog" aria-label="보기 설정"><div class="hd">보기 설정</div></div>';
    var anchor = document.getElementById('darkBtn') || $('.iconbtn', top);
    if (anchor) top.insertBefore(wrap, anchor); else top.appendChild(wrap);
    var pop = $('.ct-viewpop', wrap), btn = $('.ct-viewbtn', wrap);
    function row(label, el) { if (!el) return; var r = document.createElement('div'); r.className = 'row'; r.innerHTML = '<span>' + label + '</span>'; r.appendChild(el); pop.appendChild(r); }
    row('언어', document.getElementById('langBtn'));
    row('큰 글씨', document.getElementById('bigBtn'));
    var tog = $('.topbar .tog'); if (tog) row('봇 음성 읽기', tog);
    var cost = $$('.topbar .stat').filter(function (s) { return !s.getAttribute('title'); })[0];
    if (cost) { pop.insertAdjacentHTML('beforeend', '<div class="sep"></div>'); row('이번 세션 비용', cost); }
    pop.insertAdjacentHTML('beforeend', '<div class="sep"></div>');
    var dense = document.createElement('button'); dense.type = 'button'; dense.className = 'ct-switch'; dense.setAttribute('aria-label', '표 촘촘하게');
    var on = store('ct-dense') === '1'; document.body.classList.toggle('ct-dense', on); dense.setAttribute('aria-pressed', on ? 'true' : 'false');
    dense.addEventListener('click', function () { var v = !document.body.classList.contains('ct-dense'); document.body.classList.toggle('ct-dense', v); dense.setAttribute('aria-pressed', v ? 'true' : 'false'); store('ct-dense', v ? '1' : '0'); });
    row('표 촘촘하게', dense);
    function close() { pop.classList.remove('on'); btn.setAttribute('aria-expanded', 'false'); }
    btn.addEventListener('click', function (e) { e.stopPropagation(); var o = !pop.classList.contains('on'); pop.classList.toggle('on', o); btn.setAttribute('aria-expanded', o ? 'true' : 'false'); });
    pop.addEventListener('click', function (e) { e.stopPropagation(); });
    document.addEventListener('click', close);
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });
  }

  // ── 3. 빈 상태·오류 / 24·28번 버그: 다른 행이 보이는데 「없습니다」 행이 함께 보이던 것 ──
  var EMPTY = /^(아직 .{0,40}(없습니다|없어요)|.{0,40}(없습니다|없어요|결과가 없습니다))\.?$/;
  var ERR = /^(.{0,30}(불러오지 못했|실패했|오류가 발생))/;
  function states() {
    $$('#mainwrap tbody').forEach(function (tb) {
      var rows = $$(':scope > tr', tb);
      rows.forEach(function (tr) {
        var tds = tr.children;
        if (tds.length !== 1 || !tds[0].hasAttribute('colspan')) return;
        var t = (tds[0].textContent || '').trim();
        if (!EMPTY.test(t) && !/등록된|없습니다/.test(t)) return;
        var others = rows.some(function (o) { return o !== tr && o.children.length > 1 && o.offsetParent !== null && !o.classList.contains('ct-hide'); });
        tr.classList.toggle('ct-hide', others);
        tds[0].classList.toggle('ct-empty', !others);
      });
    });
    $$('#mainwrap .card div, #mainwrap .card p, #mainwrap .hint').forEach(function (el) {
      if (el.children.length > 1 || el.classList.contains('ct-empty')) return;
      if (el.closest('table, .tb, #chat, #tr, .ct-drawer')) return;
      var t = (el.textContent || '').trim();
      if (t.length < 6 || t.length > 90) return;
      if (EMPTY.test(t) && !el.querySelector('button, input')) el.classList.toggle('ct-empty', true);
      else if (ERR.test(t) && el.getAttribute('role') === 'alert') el.classList.toggle('ct-err', true);
    });
  }

  // ── 4. 표: 숫자 칸 오른쪽 정렬, 상태 배지, 행 상세 패널 ──
  var NUMV = /^[-+]?(₩\s?)?[\d,]+(\.\d+)?\s*(%|%p|건|콜|명|원|분|초|s|ms|점|회|개|회선)?$|^\d{1,2}:\d{2}(:\d{2})?$/;
  var TONE = {
    '운영': 'ok', '활성': 'ok', '정상': 'ok', '준수': 'ok', '완료': 'ok', '성공': 'ok', '연결': 'ok', '해결': 'ok', '긍정': 'ok', '통과': 'ok', '배포': 'ok',
    '검수': 'warn', '대기': 'warn', '점검': 'warn', '제한': 'warn', '임시': 'warn', '경고': 'warn', '지연': 'warn', '주의': 'warn', '보류': 'warn', '검토': 'warn',
    '위반': 'bad', '장애': 'bad', '실패': 'bad', '오류': 'bad', '중지': 'bad', '불만': 'bad', '부정': 'bad', '이탈': 'bad', '차단': 'bad',
    '정보': 'info', '통화중': 'info', '진행': 'info', '진행중': 'info', '상담중': 'info', '세션': 'info', '신규': 'info',
    '오프': 'neu', '전역': 'neu', '중립': 'neu', '휴식': 'neu'
  };
  function tables() {
    $$('#mainwrap table').forEach(function (t) {
      var body = $$('tbody tr', t).filter(function (tr) { return tr.children.length > 1; });
      if (!body.length) return;
      var cols = Math.max.apply(null, body.map(function (tr) { return tr.children.length; }));
      for (var c = 0; c < cols; c++) {
        var cells = body.map(function (tr) { return tr.children[c]; }).filter(Boolean);
        var nums = cells.filter(function (td) { return !td.children.length && NUMV.test((td.textContent || '').trim()); }).length;
        var isNum = cells.length && nums / cells.length >= 0.6;
        cells.forEach(function (td) { if (!td.children.length && NUMV.test((td.textContent || '').trim())) td.classList.toggle('ct-num', !!isNum); });
        var th = $$('thead th', t)[c]; if (th) th.classList.toggle('ct-num', !!isNum);
      }
      body.forEach(function (tr) {
        Array.prototype.forEach.call(tr.children, function (td) {
          if (td.children.length) return;
          var v = (td.textContent || '').trim(), tone = TONE[v];
          if (tone && v.length <= 6) td.innerHTML = '<span class="ct-badge ' + tone + '">' + v + '</span>';
        });
      });
    });
    // 이력 화면은 행을 누르면 오른쪽에서 상세가 열린다(행에 원래 동작이 있으면 건드리지 않는다)
    ['hist', 'calls', 'syslog', 'mmhist', 'agentmgmt', 'accounts'].forEach(function (v) {
      $$('#view-' + v + ' tbody tr').forEach(function (tr) {
        if (tr.dataset.ctRow || tr.children.length < 2 || tr.getAttribute('onclick')) return;
        tr.dataset.ctRow = '1'; tr.classList.add('ct-clickable'); tr.tabIndex = 0;
        tr.addEventListener('click', function (e) { if (e.target.closest('a,button,input,select,label,.tag[onclick]')) return; openRow(tr); });
        tr.addEventListener('keydown', function (e) { if (e.key === 'Enter') openRow(tr); });
      });
    });
  }
  var drawer, drawerBg, lastRow;
  function openRow(tr) {
    if (!drawer) {
      drawerBg = document.createElement('div'); drawerBg.className = 'ct-drawer-bg';
      drawer = document.createElement('aside'); drawer.className = 'ct-drawer'; drawer.setAttribute('role', 'dialog'); drawer.setAttribute('aria-modal', 'true');
      drawer.innerHTML = '<div class="hd"><b>상세</b><button type="button" aria-label="닫기">' + svg('x') + '</button></div><div class="bd"></div>';
      document.body.appendChild(drawerBg); document.body.appendChild(drawer);
      drawerBg.addEventListener('click', closeRow); $('.hd button', drawer).addEventListener('click', closeRow);
      document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && drawer.classList.contains('on')) closeRow(); });
    }
    var table = tr.closest('table'), heads = $$('thead th', table).map(function (th) { return (th.textContent || '').trim(); });
    var title = (document.getElementById('title') || {}).textContent || '상세';
    var dl = '<dl>' + Array.prototype.map.call(tr.children, function (td, i) {
      return '<dt>' + esc(heads[i] || ('항목 ' + (i + 1))) + '</dt><dd>' + td.innerHTML + '</dd>';
    }).join('') + '</dl>';
    $('.hd b', drawer).textContent = title + ' 상세';
    $('.bd', drawer).innerHTML = dl;
    if (lastRow) lastRow.classList.remove('ct-open');
    lastRow = tr; tr.classList.add('ct-open');
    drawerBg.classList.add('on'); drawer.classList.add('on');
    $('.hd button', drawer).focus();
  }
  function closeRow() { if (!drawer) return; drawer.classList.remove('on'); drawerBg.classList.remove('on'); if (lastRow) { lastRow.classList.remove('ct-open'); lastRow.focus(); } }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }

  // ── 6. 위험 동작 버튼 ──
  var DANGER = /^(삭제|비우기|지우기|초기화|전체 삭제|계정 중지|중지)$/;
  function danger() { $$('#mainwrap button').forEach(function (b) { if (DANGER.test((b.textContent || '').trim())) b.classList.toggle('ct-danger', true); }); }

  // ── 7. 그래프 공통 ──
  function chartDefaults() {
    var C = window.Chart; if (!C || C.$ctPages) return; C.$ctPages = true;
    try {
      var lg = C.defaults.plugins.legend;
      lg.position = 'top'; lg.align = 'end';
      lg.labels = Object.assign(lg.labels || {}, { usePointStyle: true, pointStyle: 'circle', boxWidth: 7, boxHeight: 7, padding: 14, font: { size: 11.5, weight: '600' } });
      C.defaults.font.size = 11.5;
      if (C.defaults.scale) { C.defaults.scale.grid = Object.assign(C.defaults.scale.grid || {}, { color: 'rgba(25,31,40,.06)', drawTicks: false }); C.defaults.scale.border = Object.assign(C.defaults.scale.border || {}, { display: false }); C.defaults.scale.ticks = Object.assign(C.defaults.scale.ticks || {}, { padding: 8 }); }
      if (C.overrides && C.overrides.doughnut) C.overrides.doughnut.cutout = '72%';
    } catch (e) { /* 판본이 달라도 화면은 그대로 */ }
  }

  // ── 8. 본문 이모지 전수: 순위 메달 → 순위 배지, 색 동그라미 → 색 점, 나머지 장식은 걷어낸다 ──
  var MEDAL = { '🥇': 1, '🥈': 2, '🥉': 3 }, DOTS = { '🟢': 'ok', '⚪': 'neu', '🔴': 'bad' };
  var PICT; try { PICT = /\p{Extended_Pictographic}️?(‍\p{Extended_Pictographic}️?)*/gu; } catch (e) { PICT = null; }
  function emojiPass() {
    if (!PICT) return;
    var root = document.getElementById('mainwrap'); if (!root) return;
    var w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) {
        var p = n.parentElement; if (!p) return NodeFilter.FILTER_REJECT;
        if (p.closest('script,style,textarea,input,[contenteditable],#chat,#tr,.tb,.msg,svg')) return NodeFilter.FILTER_REJECT;
        PICT.lastIndex = 0; return PICT.test(n.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      }
    });
    var list = []; var n; while ((n = w.nextNode())) list.push(n);
    list.forEach(function (node) {
      var t = node.nodeValue, frag = document.createDocumentFragment(), last = 0, changed = false;
      PICT.lastIndex = 0; var m;
      while ((m = PICT.exec(t))) {
        var ch = m[0].replace(/️/g, '');
        if (t.slice(0, m.index).trim() === '' && t.slice(m.index + m[0].length).trim() === '' && node.parentElement.tagName === 'BUTTON') continue; // 버튼 하나뿐인 아이콘은 theme 층이 바꾼다
        frag.appendChild(document.createTextNode(t.slice(last, m.index)));
        if (MEDAL[ch]) { var r = document.createElement('span'); r.className = 'ct-rank r' + MEDAL[ch]; r.textContent = MEDAL[ch]; frag.appendChild(r); }
        else if (DOTS[ch]) { var d = document.createElement('span'); d.className = 'ct-dot ' + DOTS[ch]; d.setAttribute('aria-hidden', 'true'); frag.appendChild(d); }
        last = m.index + m[0].length;
        if (!MEDAL[ch] && !DOTS[ch] && t.charAt(last) === ' ') last++;
        changed = true;
      }
      if (!changed) return;
      frag.appendChild(document.createTextNode(t.slice(last)));
      node.parentNode.replaceChild(frag, node);
    });
  }

  // ── 9·42. 인라인 색 정리: 옛 테라코타·제각각 색 → 한 벌 팔레트, 다크에서 흰 면 보정 ──
  function rgbOf(c) {
    var m = String(c).match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
    if (m) { var h = m[1].length === 3 ? m[1].replace(/./g, '$&$&') : m[1]; return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16), 1]; }
    m = String(c).match(/^rgba?\(([^)]+)\)$/i);
    if (m) { var p = m[1].split(',').map(parseFloat); return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; }
    return null;
  }
  function hsl(v) {
    var r = v[0] / 255, g = v[1] / 255, b = v[2] / 255, mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn, h = 0, l = (mx + mn) / 2;
    if (d) { if (mx === r) h = ((g - b) / d) % 6; else if (mx === g) h = (b - r) / d + 2; else h = (r - g) / d + 4; h = (h * 60 + 360) % 360; }
    return { h: h, l: l, d: d };
  }
  function uiColor(c, kind) {
    var v = rgbOf(c); if (!v) return null;
    var x = hsl(v), tint = x.l > 0.86, out;
    if (x.d < (tint ? 0.035 : 0.1)) return null;                     // 회색·검정·흰색은 그대로
    if (x.h >= 345 || x.h < 8) out = tint ? '#FCE8E6' : '#C9372C';
    else if (tint && x.h < 28) out = '#EAF0FC';                        // 옛 브랜드 연한 면(살구색) → 블루 연한 면
    else if (x.h < 20 && x.l < 0.6) out = '#2457C5';                   // 옛 브랜드(테라코타) → 블루. 주황 경고색(20°↑)은 아래 주황으로
    else if (x.h < 55) out = tint ? '#FDF1DC' : (kind === 'text' ? '#A6620A' : '#D9922B');
    else if (x.h < 170) out = tint ? '#E6F4EA' : '#2E7D4F';
    else if (x.h < 250) out = tint ? '#EAF0FC' : (x.l > 0.62 ? '#5B8DEF' : '#2457C5');
    else out = tint ? '#EEF0FB' : '#4F6BD8';                         // 보라 → 남청
    if (v[3] < 1) { var o = rgbOf(out); out = 'rgba(' + o[0] + ',' + o[1] + ',' + o[2] + ',' + v[3] + ')'; }
    return out;
  }
  var COLOR_IN = /#[0-9a-f]{6}\b|#[0-9a-f]{3}\b|rgba?\([^)]+\)/gi;
  function inlineColors() {
    var dark = document.body.classList.contains('dark');
    $$('#app [style]').forEach(function (el) {
      if (el.closest('svg, .ct-drawer, canvas')) return;
      var st = el.getAttribute('style') || '', key = st + '|' + dark;
      if (el.dataset.ctSt === key) return;
      var s = el.style;
      if (s.color) { var c = uiColor(s.color, 'text'); if (c) s.color = c; }
      if (s.backgroundColor) { var bg = uiColor(s.backgroundColor, 'bg'); if (bg) s.backgroundColor = bg; }
      if (s.backgroundImage && /gradient/.test(s.backgroundImage)) s.backgroundImage = s.backgroundImage.replace(COLOR_IN, function (m) { return uiColor(m, 'bg') || m; });
      ['borderColor', 'borderLeftColor', 'borderTopColor'].forEach(function (k) { if (s[k]) { var b = uiColor(s[k], 'line'); if (b) s[k] = b; } });
      // 다크에서 흰 면으로 고정된 칸
      var white = /^(#fff|#ffffff|white|rgb\(255, 255, 255\))$/i.test((s.backgroundColor || s.background || '').trim());
      el.classList.toggle('ct-darkfix', dark && white);
      el.dataset.ctSt = (el.getAttribute('style') || '') + '|' + dark;
    });
  }

  // ── 10. 데모 표시 통일 ──
  function demoBadges() {
    $$('#mainwrap span[style], #mainwrap small').forEach(function (el) {
      if (el.children.length) return;
      var t = (el.textContent || '').trim();
      if (/^\(?데모\)?$/.test(t)) { el.classList.toggle('ct-demo', true); if (el.textContent !== '데모') el.textContent = '데모'; }
    });
  }

  // ── 긴 화면을 탭으로(실시간 모니터링·TA·QA·상담사 현황) — 아이 순서대로 묶는다 ──
  var TABS = {
    monitor: [['실시간', [0, 1, 2, 6, 7]], ['엔진·연결', [3, 4]], ['실측 지표', [5]]],
    ta: [['요약', [0, 1, 2, 3]], ['추이·키워드', [4, 5]], ['감정·이슈', [6, 7]], ['기능 안내', [8]]],
    qamgmt: [['자동 평가', [1, 2, 3, 4]], ['평가 기준', [5, 7]], ['코칭', [0, 6]]],
    agents: [['실시간', [0, 1]], ['성과', [2]], ['근태', [3]]]
  };
  function tabs() {
    Object.keys(TABS).forEach(function (v) {
      var sec = document.getElementById('view-' + v); if (!sec) return;
      var kids = Array.prototype.filter.call(sec.children, function (c) { return !c.classList.contains('ct-tabs'); });
      var groups = TABS[v];
      var max = Math.max.apply(null, groups.map(function (g) { return Math.max.apply(null, g[1]); }));
      if (kids.length <= max) return;                                  // 구조가 바뀌었으면 손대지 않는다
      var bar = $('.ct-tabs', sec);
      var cur = parseInt(sessionStorage.getItem('ct-tab-' + v) || '0', 10) || 0;
      if (!bar) {
        bar = document.createElement('div'); bar.className = 'ct-tabs'; bar.setAttribute('role', 'tablist');
        bar.innerHTML = groups.map(function (g, i) { return '<button type="button" role="tab" data-i="' + i + '">' + g[0] + '</button>'; }).join('');
        sec.insertBefore(bar, sec.firstChild);
        bar.addEventListener('click', function (e) {
          var b = e.target.closest('button'); if (!b) return;
          try { sessionStorage.setItem('ct-tab-' + v, b.dataset.i); } catch (er) { /* 무시 */ }
          apply(parseInt(b.dataset.i, 10));
          if (window.Chart && window.Chart.instances) Object.keys(window.Chart.instances).forEach(function (k) { var ch = window.Chart.instances[k]; if (ch && ch.canvas && sec.contains(ch.canvas)) { try { ch.resize(); ch.reset(); ch.update(); } catch (er) { /* 무시 */ } } });
        });
      }
      function apply(i) {
        var show = groups[i] ? groups[i][1] : groups[0][1];
        kids.forEach(function (k, idx) { k.classList.toggle('ct-tabhide', show.indexOf(idx) < 0); });
        $$('button', bar).forEach(function (b) { b.setAttribute('aria-selected', String(parseInt(b.dataset.i, 10) === i)); });
      }
      apply(cur < groups.length ? cur : 0);
    });
  }

  // ── 접는 보조 카드(상담 Assist 오른쪽, 텍스트 상담 오른쪽) ──
  function folds() {
    $$('#view-assistdesk .adesk > div:nth-child(2) .card, #view-chat .grid > div:last-child > .card').forEach(function (card, i) {
      var h = card.querySelector(':scope > h3'); if (!h || card.dataset.ctFold) return;
      card.dataset.ctFold = '1'; card.classList.add('ct-fold-card');
      var key = 'ct-fold-' + (h.textContent || '').trim();
      if (store(key) === '1') card.classList.add('ct-folded');
      h.setAttribute('role', 'button'); h.tabIndex = 0;
      function t() { var f = !card.classList.contains('ct-folded'); card.classList.toggle('ct-folded', f); store(key, f ? '1' : '0'); h.setAttribute('aria-expanded', f ? 'false' : 'true'); }
      h.addEventListener('click', function (e) { if (e.target.closest('button,a,input')) return; t(); });
      h.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); t(); } });
    });
  }

  // ── 화면별 작은 보정 ──
  function perView() {
    // 2. 버튼만 있는 첫 줄 → 도구 줄
    $$('#mainwrap > section > div[style*="justify-content:flex-end"]').forEach(function (d) { d.classList.toggle('ct-toolbar', true); });
    // 20. 운영 통계 기간 선택 고정
    var rp = $('#view-report > .card:first-child'); if (rp) rp.classList.toggle('ct-sticky', true);
    // 15. 시나리오 빌더 캔버스
    $$('#view-builder .flow').forEach(function (f) { f.classList.toggle('ct-canvas', true); });
    // 노드 앞뒤 이동 버튼 ◀ ▶ → 꺾쇠 아이콘(동작·이름표는 그대로)
    $$('#view-builder button.nmv').forEach(function (b) { if (b.querySelector('svg')) return; b.innerHTML = svg(b.classList.contains('l') ? 'cl' : 'cr'); });
    // 17. 환경 변수 — 변수명·값은 코드 글꼴
    $$('#view-envvar tbody tr').forEach(function (tr) { [0, 1].forEach(function (i) { var td = tr.children[i]; if (td && !td.children.length) td.classList.toggle('ct-code', true); }); });
    // 13. 코드 블록 복사
    $$('#mainwrap pre').forEach(function (pre) {
      if (pre.querySelector('.ct-copy')) return;
      var b = document.createElement('button'); b.type = 'button'; b.className = 'ct-copy'; b.innerHTML = '복사';
      b.addEventListener('click', function () {
        var txt = Array.prototype.filter.call(pre.childNodes, function (n) { return n !== b; }).map(function (n) { return n.textContent; }).join('');
        if (navigator.clipboard) navigator.clipboard.writeText(txt).then(function () { b.textContent = '복사됨'; setTimeout(function () { b.textContent = '복사'; }, 1400); });
      });
      pre.appendChild(b);
    });
    // 26. 전광판 전체 화면
    var board = document.getElementById('view-board');
    if (board && !$('.ct-toolbar', board) && board.requestFullscreen) {
      var tb = document.createElement('div'); tb.className = 'ct-toolbar';
      tb.innerHTML = '<button type="button">' + svg('max') + ' 전체 화면</button>';
      $('button', tb).addEventListener('click', function () { board.requestFullscreen().catch(function () { /* 거부되면 그대로 */ }); });
      board.insertBefore(tb, board.firstChild);
    }
    // 21. 알림 센터 — 연달아 같은 알림은 한 줄로 묶고 「×N」
    $$('#view-alerts tbody').forEach(function (tb) {
      var rows = $$(':scope > tr', tb).filter(function (r) { return r.children.length >= 3; });
      var heads = [], counts = new Map();
      rows.forEach(function (r) {
        var key = (r.children[1].textContent || '').trim() + '|' + (r.children[2].textContent || '').replace(/×\d+\s*$/, '').trim();
        var last = heads[heads.length - 1];
        if (last && last.key === key) { r.classList.toggle('ct-hide', true); counts.set(last.row, counts.get(last.row) + 1); }
        else { r.classList.toggle('ct-hide', false); heads.push({ key: key, row: r }); counts.set(r, 1); }
      });
      // 바뀐 칸만 고친다 — 같은 값을 다시 쓰면 관찰자가 또 깨어난다
      heads.forEach(function (h) { setTimes(h.row, counts.get(h.row)); });
    });
    // 39. 알림 팝업 — 모두 보기
    var np = document.getElementById('notiPop');
    if (np && !$('.ct-notimore', np)) {
      var more = document.createElement('div'); more.className = 'ct-notimore'; more.setAttribute('role', 'button'); more.tabIndex = 0; more.textContent = '알림 센터에서 모두 보기';
      more.addEventListener('click', function (e) { e.stopPropagation(); if (window.show) window.show('alerts'); np.classList.remove('show'); });
      np.appendChild(more);
    }
  }
  function setTimes(row, n) {
    var cell = row.children[2]; var b = cell.querySelector('.ct-times');
    if (n <= 1) { if (b) b.remove(); return; }
    if (!b) { b = document.createElement('span'); b.className = 'ct-times'; cell.appendChild(b); }
    if (b.textContent !== '×' + n) b.textContent = '×' + n;
  }

  var queued = false, busy = false;
  function run() {
    queued = false; if (busy) return; busy = true;
    try { viewMenu(); chartDefaults(); emojiPass(); inlineColors(); demoBadges(); tables(); states(); danger(); tabs(); folds(); perView(); }
    catch (e) { /* 표시 층 오류가 콘솔 동작을 막지 않는다 */ }
    busy = false;
  }
  function schedule() { if (!queued) { queued = true; setTimeout(run, 60); } }
  function start() {
    run();
    new MutationObserver(function (list) {
      // 이 층이 만든 변화(배지·묶음 표시)만으로는 다시 돌지 않는다
      for (var i = 0; i < list.length; i++) { var t = list[i].target; if (!(t.nodeType === 1 && t.closest && t.closest('.ct-drawer, .ct-viewpop'))) { schedule(); return; } }
    }).observe(document.getElementById('app') || document.body, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['class'] });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
