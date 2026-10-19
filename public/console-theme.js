/*
 * AICC Portal 관리 콘솔 — 디자인 한 벌(2026-10-19 개편) · 화면 동작은 건드리지 않는 표시 층
 *
 * 하는 일(전부 겉모습):
 *  1) 사이드바·하단 탭의 이모지 아이콘을 선 아이콘(SVG)으로 바꾼다 — 메뉴는 data-v 로 찾는다(문구·순서 그대로).
 *  2) 상단 바 도구(검색·비용·즐겨찾기·다크·알림·메뉴)의 이모지를 선 아이콘으로 바꾼다.
 *  3) 본문 제목·버튼·지표 이름 앞에 붙은 장식 이모지를 걷어낸다(글자는 그대로, 이모지만 아이콘 자리에 있던 것).
 *  4) 사이드바 묶음을 접고 펼 수 있게 한다(45개 화면 — 지금 화면이 있는 묶음은 항상 펼친다). 상태는 이 브라우저에만 남는다.
 *  5) Chart.js 그래프 색을 브랜드 팔레트로 맞춘다(의미 색 — 빨강·초록·주황 — 은 같은 계열로 유지).
 * 화면이 다시 그려져도(보기 전환·자동 갱신) MutationObserver 가 같은 처리를 다시 한다.
 */
(function () {
  'use strict';
  var S = 'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"';
  var P = {
    mic: '<path d="M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3Z"/><path d="M19 11a7 7 0 0 1-14 0M12 18v3"/>',
    chat: '<path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 9 9 0 0 1-3.8-.8L3 20l1.3-4.1A8.4 8.4 0 1 1 21 11.5Z"/>',
    heart: '<path d="M19 14c1.5-1.5 3-3.2 3-5.5A5.5 5.5 0 0 0 12 5a5.5 5.5 0 0 0-10 3.5c0 2.3 1.5 4 3 5.5l7 7Z"/>',
    home: '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/><path d="M10 21v-6h4v6"/>',
    bot: '<rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 8V4M8 14h.01M16 14h.01M9.5 17.5h5"/>',
    flow: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/><path d="M10 6.5h4a3 3 0 0 1 3 3V14"/>',
    target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
    split: '<path d="M6 3v12M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM18 9a9 9 0 0 1-9 9"/>',
    sliders: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
    pulse: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
    chart: '<path d="M3 3v18h18"/><path d="M7 15l4-4 3 3 5-6"/>',
    bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.9 1.9 0 0 0 3.4 0"/>',
    book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5Z"/><path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
    users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8"/>',
    lock: '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5M12 15V3"/>',
    monitor: '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
    bar: '<path d="M12 20V10M18 20V4M6 20v-4"/>',
    out: '<path d="M7 17 17 7M8 7h9v9"/>',
    layers: '<path d="m12 2 10 5-10 5L2 7Z"/><path d="m2 17 10 5 10-5M2 12l10 5 10-5"/>',
    signal: '<path d="M2 20h.01M7 20v-4M12 20v-8M17 20V8M22 4v16"/>',
    gauge: '<path d="M12 14l4-4"/><path d="M3.3 19a10 10 0 1 1 17.4 0"/>',
    phone: '<path d="M6.5 4h3l1.4 4-2 1.3a11 11 0 0 0 5.8 5.8l1.3-2 4 1.4v3a2 2 0 0 1-2.2 2A16 16 0 0 1 4.5 6.2 2 2 0 0 1 6.5 4Z"/>',
    gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 0 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 0 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 0 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 0 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z"/>',
    megaphone: '<path d="M3 11v2a1 1 0 0 0 1 1h3l6 4V6L7 10H4a1 1 0 0 0-1 1Z"/><path d="M17 8a5 5 0 0 1 0 8"/>',
    idcard: '<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="11" r="2"/><path d="M6 16c.6-1.3 1.7-2 3-2s2.4.7 3 2M15 10h3M15 13h3"/>',
    receipt: '<path d="M5 3h14v18l-3-2-2 2-2-2-2 2-2-2-3 2Z"/><path d="M9 8h6M9 12h6"/>',
    won: '<path d="M4 6l3 12 3-9 2 6 2-6 3 9 3-12M3 10h18M3 14h18"/>',
    calendar: '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
    terminal: '<path d="m4 17 6-6-6-6M12 19h8"/>',
    code: '<path d="m16 18 6-6-6-6M8 6l-6 6 6 6"/>',
    tag: '<path d="M20.6 13.4 13.4 20.6a2 2 0 0 1-2.8 0L3 13V3h10l7.6 7.6a2 2 0 0 1 0 2.8Z"/><path d="M7.5 7.5h.01"/>',
    smartphone: '<rect x="6" y="2" width="12" height="20" rx="2.5"/><path d="M11 18h2"/>',
    file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6M8 13h8M8 17h5"/>',
    headset: '<path d="M4 13a8 8 0 0 1 16 0"/><rect x="3" y="13" width="4" height="6" rx="1.5"/><rect x="17" y="13" width="4" height="6" rx="1.5"/>',
    text: '<path d="M4 7V4h16v3M9 20h6M12 4v16"/>',
    award: '<circle cx="12" cy="8" r="6"/><path d="M8.2 13.4 7 22l5-3 5 3-1.2-8.6"/>',
    brain: '<path d="M9.5 2A2.5 2.5 0 0 0 7 4.5 2.5 2.5 0 0 0 4.5 7 3 3 0 0 0 3 12a3 3 0 0 0 1.5 5A2.5 2.5 0 0 0 7 19.5 2.5 2.5 0 0 0 9.5 22 2.5 2.5 0 0 0 12 19.5v-15A2.5 2.5 0 0 0 9.5 2ZM14.5 2A2.5 2.5 0 0 1 17 4.5 2.5 2.5 0 0 1 19.5 7 3 3 0 0 1 21 12a3 3 0 0 1-1.5 5 2.5 2.5 0 0 1-2.5 2.5 2.5 2.5 0 0 1-2.5 2.5 2.5 2.5 0 0 1-2.5-2.5"/>',
    sparkle: '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8Z"/><path d="M19 17v4M17 19h4"/>',
    wave: '<path d="M2 12h2M6 8v8M10 5v14M14 8v8M18 10v4M22 12h-2"/>',
    star: '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1 6.2L12 17.3 6.5 20.2l1-6.2L3 9.6l6.2-.9Z"/>',
    moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.3-1.4M17.7 6.3l1.4-1.4"/>',
    menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
    coin: '<circle cx="12" cy="12" r="9"/><path d="M8 9l2 6 2-4 2 4 2-6M7 12h10"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 8h.01M11 12h1v4h1"/>',
    up: '<path d="M7 10v11M15 5.9 14 10h5.8a2 2 0 0 1 2 2.3l-1.4 7A2 2 0 0 1 18.4 21H7V10l4-8a3 3 0 0 1 3 3.9Z"/>',
    down: '<path d="M17 14V3M9 18.1 10 14H4.2a2 2 0 0 1-2-2.3l1.4-7A2 2 0 0 1 5.6 3H17v11l-4 8a3 3 0 0 1-3-3.9Z"/>',
    pause: '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
    play: '<path d="M6 4l14 8-14 8Z"/>'
  };
  // 버튼 글자가 이모지 하나뿐인 자리 — 같은 뜻의 선 아이콘으로(누르는 기능은 그대로)
  var SOLO = { '👍': 'up', '👎': 'down', '⏸': 'pause', '▶': 'play', '🎙': 'mic', '🎙️': 'mic' };
  var NAV = {
    voice: 'mic', chat: 'chat', wellbeing: 'heart', home: 'home', agentmgmt: 'bot', builder: 'flow', qa: 'target', abtest: 'split',
    envvar: 'sliders', monitor: 'pulse', hist: 'list', report: 'chart', alerts: 'bell', kb: 'book', rag: 'search', agents: 'users',
    accounts: 'lock', exportc: 'download', board: 'monitor', statin: 'bar', statout: 'out', statsvc: 'layers', chan: 'signal',
    sysresp: 'gauge', calls: 'phone', svccfg: 'gear', disclosure: 'megaphone', callerid: 'idcard', settle: 'receipt', settlelines: 'won',
    basis: 'calendar', syslog: 'terminal', code: 'code', reason: 'tag', cpaas: 'phone', mm: 'smartphone', mmhist: 'list', docs: 'file',
    assistdesk: 'headset', ta: 'text', qamgmt: 'award', kms: 'book', nlu: 'brain', sllm: 'sparkle', vstudio: 'wave'
  };
  function svg(name, cls) { return '<svg class="' + (cls || 'ct-ic') + '" ' + S + '>' + (P[name] || P.info) + '</svg>'; }

  // 이모지(그림 문자) — 장식으로 앞에 붙은 것만 걷어낸다
  var EMO;
  try { EMO = new RegExp('^\\s*(?:\\p{Extended_Pictographic}|[\\u2600-\\u27BF\\u2B50\\u2B06\\u2934\\u2935\\u20A9])(?:\\uFE0F|\\u200D(?:\\p{Extended_Pictographic}))*\\s*', 'u'); }
  catch (e) { EMO = /^\s*(?:[☀-➿]|[\uD83C-\uDBFF][\uDC00-\uDFFF])+️?\s*/; }
  var KEEP = /^[✕✖×+\-–]$/; // 닫기(✕)·덧셈 같은 기능 기호는 남긴다

  function stripLead(el) {
    if (!el || el.dataset.ctClean === el.textContent) return;
    var w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null);
    var n = w.nextNode();
    while (n && !n.nodeValue.trim()) n = w.nextNode();
    if (n) {
      var t = n.nodeValue, m = t.match(EMO);
      if (m && m[0].trim() && !KEEP.test(m[0].trim()) && t.slice(m[0].length).trim()) n.nodeValue = t.slice(m[0].length);
    }
    el.dataset.ctClean = el.textContent;
  }

  function navIcons() {
    document.querySelectorAll('#sideNav [data-v], #favNav [data-v], #botnav a').forEach(function (a) {
      // 하단 탭은 data-bn 을 쓰고, 「메뉴」 칸은 키가 없다(사이드 메뉴 열기)
      var key = a.getAttribute('data-v') || a.getAttribute('data-bn');
      var name = key ? NAV[key] : (/toggleSide/.test(a.getAttribute('onclick') || '') ? 'menu' : null);
      if (!name) return;
      var ic = a.querySelector('.ic') || a.querySelector('span');
      if (!ic || ic.dataset.ctIc === name) return;
      ic.innerHTML = svg(name); ic.dataset.ctIc = name; ic.classList.add('ic');
    });
    document.querySelectorAll('#favSec, .navsec').forEach(stripLead);
  }

  function setIcon(el, name) {
    if (!el) return;
    // 아이콘 자리의 첫 글자 노드만 바꾼다 — 배지·팝업 같은 자식 요소는 그대로 둔다
    var key = name + '|' + (el.getAttribute('aria-pressed') || '');
    if (el.dataset.ctTool === key) return;
    var first = el.firstChild;
    if (first && first.nodeType === 3) el.removeChild(first);
    var old = el.querySelector(':scope > svg.ct-ic'); if (old) old.remove();
    el.insertAdjacentHTML('afterbegin', svg(name));
    el.dataset.ctTool = key;
  }

  function topbar() {
    var sr = document.querySelector('.topbar .search');
    if (sr && !sr.dataset.ct) {
      if (sr.firstChild && sr.firstChild.nodeType === 3) sr.removeChild(sr.firstChild);
      sr.insertAdjacentHTML('afterbegin', svg('search'));
      var inp = sr.querySelector('input'); if (inp) inp.insertAdjacentHTML('afterend', '<span class="ct-kbd" aria-hidden="true">Ctrl K</span>');
      sr.dataset.ct = '1';
    }
    document.querySelectorAll('.topbar .stat').forEach(function (st) {
      if (st.dataset.ct) return;
      var t = st.firstChild;
      if (t && t.nodeType === 3 && /\S/.test(t.nodeValue) && EMO.test(t.nodeValue)) { st.removeChild(t); st.insertAdjacentHTML('afterbegin', svg('coin')); }
      st.dataset.ct = '1';
    });
    var hb = document.querySelector('.topbar .hamb'); if (hb) setIcon(hb, 'menu');
    var dk = document.getElementById('darkBtn'); if (dk) setIcon(dk, document.body.classList.contains('dark') ? 'sun' : 'moon');
    var fv = document.getElementById('favBtn'); if (fv) { setIcon(fv, 'star'); fv.classList.toggle('ct-faved', fv.getAttribute('aria-pressed') === 'true'); }
    var nb = document.querySelector('.topbar .iconbtn[aria-label="알림"]'); if (nb) setIcon(nb, 'bell');
  }

  function decor() {
    var root = document.getElementById('app'); if (!root) return;
    root.querySelectorAll('.kpi .l, .card > h3, .wrap button, .chip, .hpill b, #noticeBar > span, .popup .noti, .popup .hd, .wrap h3, .wrap h4, .bcard .t, .tmgroup > button')
      .forEach(stripLead);
    root.querySelectorAll('.wrap button').forEach(function (b) {
      if (b.children.length) return;
      var t = (b.textContent || '').trim(), name = SOLO[t];
      if (name) { if (!b.getAttribute('aria-label')) b.setAttribute('aria-label', name === 'up' ? '좋아요' : name === 'down' ? '아쉬워요' : t); b.innerHTML = svg(name); }
    });
    // 둘러보기 카드 — 큰 이모지 → 선 아이콘, 옛 이름 「CallBot AICC 포털」 → 「AICC Portal」
    var ti = document.getElementById('tourIcon');
    if (ti && !ti.querySelector('svg')) ti.innerHTML = svg('sparkle');
    var tt = document.getElementById('tourTitle');
    if (tt && /CallBot AICC 포털/.test(tt.textContent)) tt.textContent = tt.textContent.replace(/CallBot AICC 포털/g, 'AICC Portal');
    root.querySelectorAll('.spark polyline').forEach(function (p) { p.setAttribute('stroke', '#2457C5'); });
    root.querySelectorAll('.spark circle').forEach(function (c) { c.setAttribute('fill', '#2457C5'); });
  }

  // ── 사이드바 묶음 접기 ──
  var FOLD_KEY = 'ct-nav-closed';
  function readClosed() { try { return JSON.parse(localStorage.getItem(FOLD_KEY) || 'null'); } catch (e) { return null; } }
  function writeClosed(v) { try { localStorage.setItem(FOLD_KEY, JSON.stringify(v)); } catch (e) { /* 저장 못 해도 동작은 같다 */ } }
  function folds() {
    var secs = Array.prototype.slice.call(document.querySelectorAll('#sideNav .navsec')).filter(function (s) { return s.id !== 'favSec'; });
    var closed = readClosed();
    if (!closed) { closed = {}; secs.forEach(function (s, i) { if (i > 2) closed[s.textContent.trim()] = 1; }); writeClosed(closed); }
    secs.forEach(function (sec) {
      var nav = sec.nextElementSibling; if (!nav || !nav.classList.contains('nav')) return;
      var key = sec.textContent.trim();
      var hasOn = !!nav.querySelector('a.on');
      if (!sec.dataset.ctFold) {
        sec.dataset.ctFold = '1'; sec.classList.add('ct-fold');
        sec.setAttribute('role', 'button'); sec.setAttribute('tabindex', '0');
        var toggle = function () {
          var c = readClosed() || {}; var now = !nav.classList.contains('ct-closed');
          if (now) c[key] = 1; else delete c[key]; writeClosed(c); apply();
        };
        sec.addEventListener('click', toggle);
        sec.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } });
      }
      function apply() {
        var c = readClosed() || {};
        var shut = !!c[key] && !nav.querySelector('a.on');
        nav.classList.toggle('ct-closed', shut); sec.classList.toggle('ct-closed', shut);
        sec.setAttribute('aria-expanded', shut ? 'false' : 'true');
      }
      apply();
      if (hasOn) { nav.classList.remove('ct-closed'); sec.classList.remove('ct-closed'); }
    });
  }

  // ── 그래프 색 ──
  var PAL = ['#2457C5', '#5B8DEF', '#9DB9F5', '#2E7D4F', '#A6620A', '#C9372C', '#6B7684', '#1A3F94'];
  function rgbOf(c) {
    if (typeof c !== 'string') return null;
    var m = c.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
    if (m) { var h = m[1].length === 3 ? m[1].replace(/./g, '$&$&') : m[1]; return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16), 1]; }
    m = c.match(/^rgba?\(([^)]+)\)$/i);
    if (m) { var p = m[1].split(',').map(function (x) { return parseFloat(x); }); return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; }
    return null;
  }
  function remap(c) {
    var v = rgbOf(c); if (!v) return c;
    var r = v[0] / 255, g = v[1] / 255, b = v[2] / 255, mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn, h = 0, l = (mx + mn) / 2;
    if (d < 0.08) return c; // 회색은 그대로
    if (mx === r) h = ((g - b) / d) % 6; else if (mx === g) h = (b - r) / d + 2; else h = (r - g) / d + 4;
    h = (h * 60 + 360) % 360;
    var out;
    if (h >= 340 || h < 15) out = '#C9372C';
    else if (h < 55) out = '#D9922B';            // 그래프 면은 글자색(#A6620A)보다 밝은 주황 — 갈색으로 탁해지지 않게
    else if (h < 170) out = '#2E7D4F';
    else if (h < 250) out = l > 0.7 ? '#9DB9F5' : (l > 0.55 ? '#5B8DEF' : '#2457C5');
    else out = '#5B8DEF'; // 보라 계열은 밝은 파랑으로
    var o = rgbOf(out);
    return v[3] < 1 ? 'rgba(' + o[0] + ',' + o[1] + ',' + o[2] + ',' + v[3] + ')' : out;
  }
  function mapAny(v) { return Array.isArray(v) ? v.map(remap) : remap(v); }
  function themeChart(ch) {
    if (!ch || !ch.data || ch.$ctThemed) return;
    (ch.data.datasets || []).forEach(function (ds) {
      ['backgroundColor', 'borderColor', 'pointBackgroundColor', 'pointBorderColor'].forEach(function (k) { if (ds[k] && typeof ds[k] !== 'function' && typeof ds[k] !== 'object' || Array.isArray(ds[k])) ds[k] = mapAny(ds[k]); });
      if (ch.config.type === 'bar' && ds.borderRadius == null) ds.borderRadius = 6;
    });
    ch.$ctThemed = true;
  }
  function charts() {
    var C = window.Chart; if (!C) return;
    if (!C.$ctReady) {
      C.$ctReady = true;
      try {
        C.defaults.font.family = '"Pretendard Variable", Pretendard, -apple-system, sans-serif';
        C.defaults.color = '#6B7684';
        C.defaults.borderColor = '#EEF0F3';
        C.register({ id: 'ctTheme', beforeUpdate: function (ch) { themeChart(ch); } });
      } catch (e) { /* Chart 판본이 달라도 화면은 그대로 돈다 */ }
    }
    var inst = C.instances ? Object.keys(C.instances).map(function (k) { return C.instances[k]; }) : [];
    inst.forEach(function (ch) { if (!ch.$ctThemed) { themeChart(ch); try { ch.update('none'); } catch (e) { /* 무시 */ } } });
  }

  // ══ 모션(2차) ═══════════════════════════════════════════════════════════
  var REDUCE = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var easeOut = function (t) { return 1 - Math.pow(1 - t, 3); };

  // 숫자 카운트업 — 화면에 들어올 때 한 번. 표기(소수 자리·천 단위 쉼표·단위)는 그대로 둔다.
  var NUM = /^(\D*?)(-?\d[\d,]*(?:\.\d+)?)(.*)$/;
  function countUp(el) {
    var txt = (el.textContent || '').trim();
    var m = txt.match(NUM); if (!m || el.children.length) return;
    var raw = m[2], target = parseFloat(raw.replace(/,/g, '')); if (!isFinite(target) || target === 0) return;
    var dec = (raw.split('.')[1] || '').length, comma = raw.indexOf(',') >= 0;
    var fmt = function (v) { var s = v.toFixed(dec); if (comma) { var p = s.split('.'); p[0] = p[0].replace(/\B(?=(\d{3})+(?!\d))/g, ','); s = p.join('.'); } return m[1] + s + m[3]; };
    var t0 = performance.now(), dur = 760;
    el.dataset.ctCount = txt;
    (function step(now) {
      if (el.dataset.ctCount !== txt) return;                 // 그사이 화면이 값을 바꿨으면 손을 뗀다
      var k = Math.min(1, (now - t0) / dur);
      el.textContent = k < 1 ? fmt(target * easeOut(k)) : txt;
      el.dataset.ctCount = k < 1 ? el.textContent : txt;
      if (k < 1) requestAnimationFrame(step);
    })(t0);
  }
  // 스파크라인 — 선을 왼쪽에서부터 그린다
  function drawSpark(svgEl) {
    var p = svgEl.querySelector('polyline'); if (!p || !p.getTotalLength) return;
    var len = p.getTotalLength(); if (!len) return;
    var c = svgEl.querySelector('circle');
    p.style.transition = 'none'; p.style.strokeDasharray = len; p.style.strokeDashoffset = len;
    if (c) { c.style.transition = 'none'; c.style.opacity = '0'; }
    p.getBoundingClientRect();
    p.style.transition = ''; p.style.strokeDashoffset = '0';
    if (c) { c.style.transition = ''; c.style.opacity = '1'; }
  }
  // 화면 전환 — 보이는 화면(section)이 바뀌면 들어오는 효과 + 카드 차례 등장 + 숫자·선·그래프 다시 그리기
  var lastView = null;
  function enterView() {
    var sec = document.querySelector('#mainwrap > section:not(.hidden)');
    if (!sec || sec === lastView) return;
    lastView = sec;
    if (REDUCE) return;
    sec.classList.remove('ct-enter'); void sec.offsetWidth; sec.classList.add('ct-enter');
    var items = sec.querySelectorAll('.kpi, .hpill, .card, .bcard, .agent');
    var n = 0;
    Array.prototype.forEach.call(items, function (el) {
      if (el.offsetParent === null) return;
      if (el.parentElement && el.parentElement.closest('.card')) return; // 카드 안의 카드는 부모와 같이 움직인다
      el.classList.remove('ct-st'); void el.offsetWidth;
      el.style.setProperty('--i', Math.min(n++, 8)); el.classList.add('ct-st');
    });
    sec.querySelectorAll('.kpi .v, .hpill .st, .bcard .n').forEach(countUp);
    sec.querySelectorAll('.spark svg').forEach(drawSpark);
    var C = window.Chart;
    if (C && C.instances) Object.keys(C.instances).forEach(function (k) {
      var ch = C.instances[k];
      if (ch && ch.canvas && sec.contains(ch.canvas) && ch.canvas.offsetParent !== null) { try { ch.reset(); ch.update(); } catch (e) { /* 무시 */ } }
    });
    setTimeout(function () { sec.classList.remove('ct-enter'); }, 900);
  }
  // 사이드바 선택 표시 — 배경 한 장이 선택 칸으로 미끄러진다
  var ind = null;
  function indicator() {
    var side = document.getElementById('sideNav'); if (!side) return;
    if (!ind) { ind = document.createElement('div'); ind.className = 'ct-ind'; ind.setAttribute('aria-hidden', 'true'); side.insertBefore(ind, side.firstChild); side.classList.add('ct-has-ind'); }
    var on = side.querySelector('.nav:not(.ct-closed) a.on');
    if (!on || on.offsetParent === null) { ind.classList.remove('on'); return; }
    var top = on.getBoundingClientRect().top - side.getBoundingClientRect().top + side.scrollTop;
    var key = Math.round(top) + ':' + on.offsetHeight;
    if (ind.dataset.k === key) return;
    ind.dataset.k = key;
    var first = !ind.classList.contains('on');
    if (first) ind.style.transition = 'none';                 // 처음 자리 잡을 때는 미끄러지지 않는다
    ind.style.height = on.offsetHeight + 'px';
    ind.style.transform = 'translateY(' + Math.round(top) + 'px)';
    ind.classList.add('on');
    if (first) { ind.getBoundingClientRect(); ind.style.transition = ''; }
  }
  // 불러오는 중 — 짧은 안내 글자를 잔잔한 빛줄기로
  var LOADING = /^(불러오는 중|모으는 중|확인 중|.{0,20}(불러오는|모으는|만드는) 중)…?\.{0,3}$/;
  function skeletons() {
    document.querySelectorAll('#mainwrap [role="status"], #mainwrap .hint, #mainwrap .empty').forEach(function (el) {
      if (el.children.length > 1) return;
      var t = (el.textContent || '').trim();
      el.classList.toggle('ct-skel', LOADING.test(t) && t.length < 40);
    });
  }
  var scrollBound = false;
  function scrolled() {
    if (scrollBound) return;
    var main = document.querySelector('.main'); if (!main) return;
    scrollBound = true;
    main.addEventListener('scroll', function () { main.classList.toggle('ct-scrolled', main.scrollTop > 4); }, { passive: true });
  }
  function chartMotion() {
    var C = window.Chart; if (!C || C.$ctMotion) return;
    C.$ctMotion = true;
    try {
      C.defaults.animation = C.defaults.animation || {};
      C.defaults.animation.duration = REDUCE ? 0 : 720;
      C.defaults.animation.easing = 'easeOutQuart';
      // 막대·점이 왼쪽부터 차례로 — 처음 그릴 때만(값이 갱신될 때는 지연 없이)
      C.defaults.animation.delay = function (ctx) { return (!REDUCE && ctx.type === 'data' && ctx.mode === 'default') ? ctx.dataIndex * 32 + ctx.datasetIndex * 90 : 0; };
      var tt = C.defaults.plugins && C.defaults.plugins.tooltip;
      if (tt) {
        tt.backgroundColor = 'rgba(25,31,40,.94)'; tt.padding = 10; tt.cornerRadius = 8; tt.boxPadding = 4;
        tt.titleFont = { weight: '700', size: 12 }; tt.bodyFont = { size: 12 }; tt.caretSize = 5; tt.displayColors = true; tt.usePointStyle = true;
      }
      if (C.defaults.elements && C.defaults.elements.line) { C.defaults.elements.line.tension = 0.35; C.defaults.elements.line.borderWidth = 2; }
      if (C.defaults.elements && C.defaults.elements.point) { C.defaults.elements.point.radius = 0; C.defaults.elements.point.hoverRadius = 4; }
    } catch (e) { /* Chart 판본이 달라도 화면은 그대로 돈다 */ }
  }

  var queued = false;
  function run() {
    queued = false;
    try { navIcons(); topbar(); decor(); folds(); chartMotion(); charts(); skeletons(); scrolled(); enterView(); indicator(); } catch (e) { /* 표시 층 오류가 콘솔 동작을 막지 않는다 */ }
  }
  window.addEventListener('resize', function () { if (ind) ind.dataset.k = ''; schedule(); });
  function schedule() { if (!queued) { queued = true; (window.requestAnimationFrame || setTimeout)(run); } }

  function start() {
    run();
    var mo = new MutationObserver(schedule);
    mo.observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['class', 'aria-pressed'] });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
