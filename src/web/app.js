/* app.js — LEX: интерфейс. Классический скрипт, без сборки и без CDN.

   Стиль — Википедия Vector 2022: статья по центру, инфобокс справа, узкая
   навигация слева; всё оформление в lex.css.

   Источник событий один на мок и на живой бэкенд: приёмник applyEvent()
   понимает события docs/API-CONTRACT.md §3 (+ v1.1–v1.4). Живой поток —
   POST /api/ask с text/event-stream, разбор свой (EventSource не умеет POST). */

(function (global) {
  'use strict';

  var doc = document;
  var $ = function (id) { return doc.getElementById(id); };

  /* ------------------------------------------------------------------ состояние */

  var state = {
    mock: /(\?|&)mock=1\b/.test(global.location.search),
    live: false,
    roleId: '',
    lineId: '',
    roles: [],
    lines: [],
    sessionId: '',
    clarifyRound: 0,
    answers: {},
    streaming: null,
    turn: null,
    lastQuestion: '',
    lastAsOf: ''
  };

  /* ------------------------------------------------------------------ утилиты */

  function el(tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function fmtDate(iso) {
    if (!iso) return '';
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
    if (!m) return iso;
    var months = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
      'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];
    return parseInt(m[3], 10) + ' ' + months[parseInt(m[2], 10) - 1] + ' ' + m[1];
  }

  function roleTitle(id) {
    for (var i = 0; i < state.roles.length; i++) {
      if (state.roles[i].id === id) return state.roles[i].title;
    }
    return id || '—';
  }

  var CONTENT_TYPE_LABEL = { text: 'текст', image: 'картинка', scan: 'скан', mixed: 'смешанный', case: 'кейс' };
  var REFUSAL_KIND_LABEL = {
    no_edition_in_force: 'нет действующей редакции',
    no_access: 'нет прав доступа',
    not_in_corpus: 'нет в базе знаний',
    conflict_unresolved: 'конфликт не разрешён'
  };

  /* ------------------------------------------------------------------ консоль */

  var consoleView = new global.ConsoleView($('consoleBody'), $('consoleMeta'), $('consoleStatus'));

  $('consoleClear').addEventListener('click', function () {
    consoleView.clear();
    consoleView.setStatus('ожидание запроса', '');
  });

  /* Источник «только из базы знаний» (§12): раскрывается по клику. */
  var srcBadge = $('srcBadge');
  var srcNote = $('srcNote');
  srcBadge.addEventListener('click', function () {
    var open = srcBadge.getAttribute('aria-expanded') === 'true';
    srcBadge.setAttribute('aria-expanded', String(!open));
    srcNote.hidden = open;
  });

  /* ------------------------------------------------------------------ данные */

  function loadReference() {
    if (state.mock) {
      state.roles = global.MockLex.roles();
      state.lines = global.MockLex.businessLines();
      return Promise.resolve();
    }
    return Promise.all([
      fetch('api/roles').then(function (r) { return r.json(); }).catch(function () { return []; }),
      fetch('api/business-lines').then(function (r) { return r.json(); }).catch(function () { return []; })
    ]).then(function (res) {
      state.roles = Array.isArray(res[0]) ? res[0] : [];
      state.lines = Array.isArray(res[1]) ? res[1] : [];
    });
  }

  /* ------------------------------------------------------------------ вкладки и настройки */

  var activeTab = 'role';

  function renderTabs() {
    var tabs = doc.querySelectorAll('.tab');
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].setAttribute('aria-selected', String(tabs[i].getAttribute('data-tab') === activeTab));
    }
    $('badgeRole').textContent = state.roleId ? roleTitle(state.roleId) : 'не выбрана';
    $('badgeRole').className = 'tab__badge' + (state.roleId ? ' tab__badge--set' : '');
    $('badgeLine').textContent = state.lineId || 'авто';
    $('badgeMode').textContent = state.mock ? 'мок' : 'живой';
    renderSettings();
  }

  function renderSettings() {
    var row = $('settingsRow');
    var hint = $('settingsHint');
    clear(row);
    var i, b;

    if (activeTab === 'role') {
      hint.innerHTML = 'Выберите роль — <em>права доступа обязательны</em>: от роли зависит, какие документы агент вправе показать.';
      for (i = 0; i < state.roles.length; i++) {
        b = el('button', 'chip', state.roles[i].title);
        b.type = 'button';
        b.setAttribute('role', 'radio');
        b.setAttribute('aria-checked', String(state.roles[i].id === state.roleId));
        b.addEventListener('click', makeRolePicker(state.roles[i].id));
        row.appendChild(b);
      }
      return;
    }

    if (activeTab === 'line') {
      hint.innerHTML = 'Бизнес-направление уточняет регламенты. «Определи сам» — агент выведет направление из вопроса и роли.';
      var auto = el('button', 'chip', 'определи сам');
      auto.type = 'button';
      auto.setAttribute('role', 'radio');
      auto.setAttribute('aria-checked', String(state.lineId === ''));
      auto.addEventListener('click', function () { state.lineId = ''; renderTabs(); });
      row.appendChild(auto);
      for (i = 0; i < state.lines.length; i++) {
        var L = state.lines[i];
        b = el('button', 'chip', L.title + ' · ' + L.docs);
        b.type = 'button';
        b.setAttribute('role', 'radio');
        b.setAttribute('aria-checked', String(state.lineId === L.id));
        b.addEventListener('click', makeLinePicker(L.id));
        row.appendChild(b);
      }
      return;
    }

    hint.innerHTML = 'Режим работы: мок эмулирует поток событий без бэкенда; живой обращается к /api/ask. Переключатель — <code>?mock=1</code> в адресе.';
    var modes = [{ id: true, t: 'мок (без бэкенда)' }, { id: false, t: 'живой (/api/ask)' }];
    for (i = 0; i < modes.length; i++) {
      b = el('button', 'chip', modes[i].t);
      b.type = 'button';
      b.setAttribute('role', 'radio');
      b.setAttribute('aria-checked', String(modes[i].id === state.mock));
      b.addEventListener('click', (function (m) {
        return function () { state.mock = m; renderTabs(); updateModeChip(); };
      })(modes[i].id));
      row.appendChild(b);
    }
    var scen = el('span', 'chip chip--muted', 'демо: ' + (state.mock ? 'НДС, уточнение, отказ…' : 'живой'));
    row.appendChild(scen);
  }

  function makeRolePicker(id) {
    return function () {
      state.roleId = id;
      renderTabs();
      updateHint();
      renderRolesScreen();
    };
  }
  function makeLinePicker(id) {
    return function () { state.lineId = id; renderTabs(); renderLinesScreen(); };
  }

  var tabEls = doc.querySelectorAll('.tab');
  for (var ti = 0; ti < tabEls.length; ti++) {
    tabEls[ti].addEventListener('click', (function (elTab) {
      return function () { activeTab = elTab.getAttribute('data-tab'); renderTabs(); };
    })(tabEls[ti]));
  }

  function updateModeChip() { renderTabs(); }

  function updateHint() {
    var hint = $('sendHint');
    if (!state.roleId) {
      hint.textContent = 'выберите роль — права доступа обязательны';
      hint.className = 'composer__hint composer__hint--warn';
      return;
    }
    hint.className = 'composer__hint';
    hint.textContent = state.mock
      ? 'мок-режим: поток шагов эмулируется. Демо: «Какая ставка НДС в России?» — отказ не из базы.'
      : 'живой режим: ответ из /api/ask';
  }

  /* ------------------------------------------------------------------ навигация по экранам */

  var SCREEN_TITLE = {
    chat: ['Чат по документам', 'Ответы только из базы знаний организации.'],
    roles: ['Роли', 'Права доступа: роль определяет, какие отделы и направления видит агент.'],
    lines: ['Бизнес-направления', 'Регламенты привязаны к направлению, а не только к отделу.'],
    cases: ['Кейсы', 'Наблюдения из практики. Не отменяют регламент, но видны в поиске с меньшим приоритетом.'],
    stats: ['Аналитика', 'По журналу запросов: отказы, латентность, где не хватает документов.']
  };

  function goScreen(name) {
    var screens = doc.querySelectorAll('.screen');
    for (var i = 0; i < screens.length; i++) {
      screens[i].classList.toggle('is-active', screens[i].id === 'screen-' + name);
    }
    var items = doc.querySelectorAll('.nav__item');
    for (var j = 0; j < items.length; j++) {
      if (items[j].getAttribute('data-screen') === name) items[j].setAttribute('aria-current', 'page');
      else items[j].removeAttribute('aria-current');
    }
    var meta = SCREEN_TITLE[name] || SCREEN_TITLE.chat;
    $('articleTitle').textContent = meta[0];
    $('articleSub').textContent = meta[1];
    if (name === 'roles') renderRolesScreen();
    if (name === 'lines') renderLinesScreen();
    if (name === 'cases') renderCasesScreen();
    if (name === 'stats') renderStatsScreen();
    $('feed').scrollTop = 0;
  }

  var navItems = doc.querySelectorAll('.nav__item');
  for (var ni = 0; ni < navItems.length; ni++) {
    navItems[ni].addEventListener('click', (function (item) {
      return function () { goScreen(item.getAttribute('data-screen')); };
    })(navItems[ni]));
  }
  $('navBrand').addEventListener('click', function (e) { e.preventDefault(); goScreen('chat'); });

  /* ------------------------------------------------------------------ экраны-справочники */

  function renderRolesScreen() {
    var host = $('screen-roles');
    clear(host);
    host.appendChild(el('h2', 'section-title', 'Роли'));
    host.appendChild(el('p', 'section-sub',
      'Аутентификации нет — только выбор роли (контракт §7). Роль несёт отделы и направления.'));
    var table = el('table', 'wikitable');
    var head = el('tr');
    ['Роль', 'Отделы', 'Направления', 'Системы'].forEach(function (h) { head.appendChild(el('th', null, h)); });
    table.appendChild(head);
    for (var i = 0; i < state.roles.length; i++) {
      var r = state.roles[i];
      var tr = el('tr');
      var td0 = el('td');
      var link = el('button', 'linkish', r.title);
      link.type = 'button';
      link.addEventListener('click', makeRolePicker(r.id));
      td0.appendChild(link);
      tr.appendChild(td0);
      tr.appendChild(el('td', null, (r.departments || []).join(', ')));
      tr.appendChild(el('td', null, (r.business_lines || []).join(', ')));
      tr.appendChild(el('td', null, (r.systems || []).join(', ') || '—'));
      table.appendChild(tr);
    }
    host.appendChild(table);
  }

  function renderLinesScreen() {
    var host = $('screen-lines');
    clear(host);
    host.appendChild(el('h2', 'section-title', 'Бизнес-направления'));
    host.appendChild(el('p', 'section-sub',
      'Список направлений приходит из данных, не из кода (контракт §11). Выбор направления сужает поиск регламентов.'));
    var table = el('table', 'wikitable');
    var head = el('tr');
    ['Направление', 'Документов'].forEach(function (h) { head.appendChild(el('th', null, h)); });
    table.appendChild(head);
    for (var i = 0; i < state.lines.length; i++) {
      var L = state.lines[i];
      var tr = el('tr');
      var td0 = el('td');
      var link = el('button', 'linkish', L.title);
      link.type = 'button';
      link.addEventListener('click', makeLinePicker(L.id));
      td0.appendChild(link);
      tr.appendChild(td0);
      tr.appendChild(el('td', 'num', String(L.docs)));
      table.appendChild(tr);
    }
    host.appendChild(table);
  }

  function renderCasesScreen() {
    var host = $('screen-cases');
    clear(host);
    host.appendChild(el('h2', 'section-title', 'Кейсы'));
    host.appendChild(el('p', 'section-sub',
      'Кейсы пополняются ролями руководителей. В поиске они участвуют наравне с документами, но при конфликте регламент сильнее.'));
    var cases = state.mock ? global.MockLex.cases() : [];
    if (!state.mock) {
      host.appendChild(el('p', 'section-sub', 'В живом режиме список приходит из GET /api/cases.'));
    }
    if (!cases.length) return;
    for (var i = 0; i < cases.length; i++) {
      var c = cases[i];
      var box = el('div', 'case');
      box.appendChild(el('p', 'case__q', c.question));
      box.appendChild(el('p', 'case__a', c.answer_in_practice));
      var meta = el('div', 'case__meta');
      var oc = el('span', 'case__tag case__outcome' + (c.outcome !== 'resolved' ? ' case__outcome--' + c.outcome : ''), c.outcome);
      meta.appendChild(oc);
      meta.appendChild(el('span', 'case__tag', c.id));
      if (c.business_line) meta.appendChild(el('span', 'case__tag', c.business_line));
      if (c.rule_key) meta.appendChild(el('span', 'case__tag', c.rule_key));
      for (var t = 0; t < (c.tags || []).length; t++) meta.appendChild(el('span', 'case__tag', '#' + c.tags[t]));
      if (c.content_type) meta.appendChild(el('span', 'ref__type ref__type--' + c.content_type, CONTENT_TYPE_LABEL[c.content_type] || c.content_type));
      box.appendChild(meta);
      host.appendChild(box);
    }
  }

  function renderStatsScreen() {
    var host = $('screen-stats');
    clear(host);
    host.appendChild(el('h2', 'section-title', 'Аналитика'));
    host.appendChild(el('p', 'section-sub',
      'Считается по журналу запросов (контракт §2, /api/analytics). В мок-режиме данные демонстрационные.'));
    var table = el('table', 'wikitable');
    var head = el('tr');
    ['Показатель', 'Значение'].forEach(function (h) { head.appendChild(el('th', null, h)); });
    table.appendChild(head);
    var rows = state.mock
      ? [['Всего запросов', '42'], ['Отказов', '7 (16.7%)'], ['Средняя латентность', '3.8 с'],
         ['p95 латентность', '9.1 с'], ['Чаще всего нет документа', 'лицензирование — 5']]
      : [['Всего запросов', '—'], ['Отказов', '—'], ['Средняя латентность', '—']];
    for (var i = 0; i < rows.length; i++) {
      var tr = el('tr');
      tr.appendChild(el('td', null, rows[i][0]));
      tr.appendChild(el('td', 'num', rows[i][1]));
      table.appendChild(tr);
    }
    host.appendChild(table);
    host.appendChild(el('p', 'section-sub',
      'Отказы по направлениям — критерий «соответствие задаче партнёра»: видно, где регламентов не хватает.'));
  }

  /* ------------------------------------------------------------------ лента: вопрос */

  function addQuestion(question, asOf) {
    var intro = $('intro');
    if (intro) intro.remove();

    var turn = el('article', 'turn');
    var asked = el('div', 'asked');
    asked.appendChild(el('div', 'asked__who', 'Вы · ' + roleTitle(state.roleId)));
    asked.appendChild(el('p', 'asked__text', question));
    if (asOf) {
      var tag = el('span', 'asked__asof', 'на дату: ' + fmtDate(asOf));
      asked.appendChild(tag);
    }
    turn.appendChild(asked);

    var host = el('div', 'answer-host');
    turn.appendChild(host);
    $('screen-chat').appendChild(turn);
    scrollFeed();
    return { el: turn, host: host };
  }

  function scrollFeed() {
    var feed = $('feed');
    feed.scrollTop = feed.scrollHeight;
  }

  /* ------------------------------------------------------------------ «думает» */

  function showThinking(host) {
    var box = el('div', 'thinking');
    var label = el('div', 'thinking__label');
    var pulse = el('span', 'thinking__pulse');
    label.appendChild(pulse);
    label.appendChild(el('span', null, 'Агент работает: идёт по шагам'));
    box.appendChild(label);
    var sk = el('div', 'sk');
    for (var i = 0; i < 3; i++) sk.appendChild(el('div', 'sk__line'));
    box.appendChild(sk);
    host.appendChild(box);
    scrollFeed();
    return box;
  }

  /* ------------------------------------------------------------------ ответ с цитатами и сносками */

  function renderAnswer(host, data) {
    clear(host);
    var body = el('div', 'answer__lead');

    // текст размечен сносками [1] — превращаем их в надстрочные ссылки
    var text = String(data.text || '');
    var parts = text.split(/(\[\d+\])/g);
    var para = el('p');
    for (var i = 0; i < parts.length; i++) {
      var m = /^\[(\d+)\]$/.exec(parts[i]);
      if (m) {
        var n = parseInt(m[1], 10);
        var fn = el('a', 'fn', '[' + n + ']');
        fn.href = '#ref-' + n + '-' + (data._turnId || '0');
        fn.setAttribute('role', 'doc-noteref');
        fn.setAttribute('aria-label', 'сноска ' + n + ': перейти к источнику');
        fn.addEventListener('click', (function (num) {
          return function (e) {
            e.preventDefault();
            var target = doc.getElementById('ref-' + num + '-' + (data._turnId || '0'));
            if (target) {
              target.setAttribute('data-open', 'true');
              target.scrollIntoView({ block: 'center', behavior: 'smooth' });
            }
          };
        })(n));
        para.appendChild(fn);
      } else if (parts[i]) {
        para.appendChild(doc.createTextNode(parts[i]));
      }
    }
    body.appendChild(para);

    if (data.assumptions && data.assumptions.length) {
      var asum = el('div', 'assumptions');
      for (var a = 0; a < data.assumptions.length; a++) {
        var line = el('div');
        line.appendChild(el('dt', null, data.assumptions[a].key + ':'));
        line.appendChild(el('dd', null, data.assumptions[a].note || data.assumptions[a].value));
        asum.appendChild(line);
      }
      body.appendChild(asum);
    }

    host.appendChild(body);
    host.appendChild(renderCitationList(data));
    scrollFeed();
  }

  function renderCitationList(data) {
    var cites = data.citations || [];
    var wrap = el('div', 'refs');
    if (!cites.length) return wrap;
    wrap.appendChild(el('p', 'refs__title', 'Источники'));

    for (var i = 0; i < cites.length; i++) {
      var c = cites[i];
      var n = i + 1;
      var ref = el('div', 'ref');
      ref.id = 'ref-' + n + '-' + (data._turnId || '0');
      ref.appendChild(el('span', 'ref__num', '[' + n + ']'));

      var content = el('div');
      var head = el('button', 'ref__head');
      head.type = 'button';
      head.setAttribute('aria-expanded', 'false');
      head.appendChild(el('span', null, c.title || c.doc_id || 'источник'));
      head.appendChild(el('span', 'ref__type ref__type--' + (c.content_type || 'text'),
        CONTENT_TYPE_LABEL[c.content_type || 'text'] || 'текст'));
      if (c.business_line) head.appendChild(el('span', 'ref__line', c.business_line));
      content.appendChild(head);

      if (c.quote) content.appendChild(el('p', 'ref__quote', c.quote));

      var more = el('dl', 'ref__more');
      var pairs = [
        ['Документ', c.doc_id || '—'],
        ['Редакция', c.revision != null ? 'Р' + c.revision : '—'],
        ['Вступила в силу', c.effective ? fmtDate(c.effective) : '—']
      ];
      if (c.page != null) pairs.push(['Страница', String(c.page)]);
      if (c.section) pairs.push(['Раздел', c.section]);
      if (c.business_line) pairs.push(['Направление', c.business_line]);
      for (var p = 0; p < pairs.length; p++) {
        var cell = el('div');
        cell.appendChild(el('dt', null, pairs[p][0]));
        cell.appendChild(el('dd', null, pairs[p][1]));
        more.appendChild(cell);
      }
      content.appendChild(more);

      if (c.content_type === 'scan' && c.ocr_confidence != null && c.ocr_confidence < 0.6) {
        content.appendChild(el('span', 'warn-ocr',
          'источник распознан неуверенно (' + Math.round(c.ocr_confidence * 100) + '%) — сверьте с оригиналом'));
      }

      if ((c.content_type === 'image' || c.content_type === 'scan' || c.content_type === 'mixed') && c.preview_url) {
        var fig = el('figure', 'ref__preview');
        var img = doc.createElement('img');
        img.src = c.preview_url;
        img.alt = 'превью: ' + (c.title || c.doc_id || 'источник');
        img.loading = 'lazy';
        fig.appendChild(img);
        var cap = el('figcaption', null, 'Страница ' + (c.page || 1) + ' — ' + (c.doc_id || ''));
        fig.appendChild(cap);
        content.appendChild(fig);
      } else if (c.content_type === 'image' || c.content_type === 'scan') {
        // В мок-режиме картинки нет: вместо битой картинки honest-заглушка.
        var ph = el('div', 'ref__preview');
        var phBox = el('div', null, 'превью страницы ' + (c.page || 1) + ' · ' + (c.doc_id || '') + ' (мок-режим)');
        phBox.style.cssText = 'padding:26px 12px;text-align:center;color:var(--text-3);font-size:12px;';
        ph.appendChild(phBox);
        content.appendChild(ph);
      }

      head.addEventListener('click', (function (refEl, headEl) {
        return function () {
          var open = refEl.getAttribute('data-open') === 'true';
          refEl.setAttribute('data-open', String(!open));
          headEl.setAttribute('aria-expanded', String(!open));
        };
      })(ref, head));

      ref.appendChild(content);
      wrap.appendChild(ref);
    }
    return wrap;
  }

  /* ------------------------------------------------------------------ отказ (§12.3) */

  function renderRefusal(host, data) {
    clear(host);
    var box = el('div', 'refusal');
    var head = el('div', 'refusal__head');
    head.appendChild(el('span', 'refusal__title', 'В базе знаний ответа нет'));
    head.appendChild(el('span', 'refusal__kind', REFUSAL_KIND_LABEL[data.kind] || data.kind || ''));
    box.appendChild(head);
    box.appendChild(el('p', 'refusal__reason', data.reason || ''));

    if (data.searched) {
      var s = data.searched;
      var searched = el('p', 'refusal__reason');
      var bits = [];
      if (s.documents != null) bits.push('документов: ' + s.documents);
      if (s.cases != null) bits.push('кейсов: ' + s.cases);
      if (s.business_lines && s.business_lines.length) bits.push('направлений: ' + s.business_lines.join(', '));
      searched.textContent = 'Что искали — ' + bits.join(' · ');
      box.appendChild(searched);
    }

    if (data.hint) {
      var hint = el('p', 'refusal__reason');
      hint.appendChild(el('span', null, 'Подсказка: ' + data.hint));
      box.appendChild(hint);
    }

    if (data.sources && data.sources.length) {
      var srcs = el('div', 'refusal__sources');
      srcs.appendChild(el('span', null, 'связанные документы'));
      for (var i = 0; i < data.sources.length; i++) srcs.appendChild(el('span', null, data.sources[i]));
      box.appendChild(srcs);
    }
    host.appendChild(box);
    scrollFeed();
  }

  /* ------------------------------------------------------------------ уточнение (§10) */

  function renderClarify(host, data, ctx) {
    clear(host);
    var box = el('div', 'clarify');
    var head = el('div', 'clarify__head');
    head.appendChild(el('span', 'clarify__badge', 'уточнение'));
    head.appendChild(el('span', 'clarify__lead', 'Вопрос неполный — агент спрашивает, а не догадывается.'));
    box.appendChild(head);

    var answered = {};
    var inputs = [];

    for (var i = 0; i < data.questions.length; i++) {
      var q = data.questions[i];
      var qBox = el('div', 'q');
      qBox.appendChild(el('p', 'q__text', q.text));
      if (q.reason) qBox.appendChild(el('p', 'q__reason', 'Почему: ' + q.reason));
      var control = el('div', 'q__control');

      if (q.kind === 'date') {
        var di = doc.createElement('input');
        di.type = 'date';
        di.id = 'q-' + q.key;
        di.setAttribute('aria-label', q.text);
        control.appendChild(di);
        inputs.push({ key: q.key, el: di, read: function (node) { return node.value; } });
      } else if (q.kind === 'choice') {
        var sel = doc.createElement('select');
        sel.id = 'q-' + q.key;
        sel.setAttribute('aria-label', q.text);
        var opt0 = doc.createElement('option');
        opt0.value = '';
        opt0.textContent = 'выберите…';
        sel.appendChild(opt0);
        for (var o = 0; o < (q.options || []).length; o++) {
          var opt = doc.createElement('option');
          opt.value = q.options[o];
          opt.textContent = q.options[o];
          sel.appendChild(opt);
        }
        control.appendChild(sel);
        inputs.push({ key: q.key, el: sel, read: function (node) { return node.value; } });
      } else {
        var ti = doc.createElement('input');
        ti.type = 'text';
        ti.id = 'q-' + q.key;
        ti.setAttribute('aria-label', q.text);
        ti.placeholder = q.kind === 'doc_id' ? 'идентификатор документа' : 'значение';
        control.appendChild(ti);
        inputs.push({ key: q.key, el: ti, read: function (node) { return node.value; } });
      }

      qBox.appendChild(control);
      box.appendChild(qBox);
    }

    var actions = el('div', 'clarify__actions');
    var submit = el('button', 'btn btn--primary', 'Ответить');
    submit.type = 'button';
    var skip = null;
    if (data.can_skip) {
      skip = el('button', 'btn', 'Пропустить, выбери сам');
      skip.type = 'button';
    }

    function collect() {
      for (var j = 0; j < inputs.length; j++) {
        var v = String(inputs[j].read(inputs[j].el) || '').trim();
        answered[inputs[j].key] = (v === 'не знаю') ? '' : v;
      }
      return answered;
    }

    submit.addEventListener('click', (function (answers, round) {
      return function () {
        collect();
        box.querySelectorAll('input, select, button').forEach(function (n) { n.disabled = true; });
        ctx.onClarifyAnswer(answers, false, round);
      };
    })(answered, ctx.round));

    if (skip) {
      skip.addEventListener('click', (function (answers, round, questions) {
        return function () {
          for (var j = 0; j < questions.length; j++) answers[questions[j].key] = '';
          box.querySelectorAll('input, select, button').forEach(function (n) { n.disabled = true; });
          ctx.onClarifyAnswer(answers, true, round);
        };
      })(answered, ctx.round, data.questions));
    }

    actions.appendChild(submit);
    if (skip) actions.appendChild(skip);
    actions.appendChild(el('span', 'clarify__note', 'можно пропустить: агент применит умолчание и скажет какое'));
    box.appendChild(actions);

    host.appendChild(box);

    // §10: в консоли видно, какие параметры известны / запрошены
    var params = [];
    if (state.roleId) params.push({ key: 'role_id', state: 'known', value: state.roleId });
    if (state.lineId) params.push({ key: 'business_line', state: 'known', value: state.lineId });
    for (var k = 0; k < data.questions.length; k++) {
      params.push({ key: data.questions[k].key, state: 'asked', value: '' });
    }
    consoleView.setParams(params);

    scrollFeed();
    var first = box.querySelector('input, select');
    if (first) first.focus();
  }

  /* ------------------------------------------------------------------ приём событий */

  function applyEvent(ev, ctx) {
    var d = ev.data || {};

    if (ev.type === 'step') {
      consoleView.push({ kind: 'step', key: d.key, title: d.title, detail: d.detail, n: d.n, total: d.total, ms: d.ms });
      if (d.key === 'verify') {
        var trimmed = /отброшено/.test(String(d.detail || ''));
        consoleView.setStatus(trimmed ? 'ответ усечён: часть утверждений без цитаты' : 'проверка пройдена: каждое утверждение с цитатой', trimmed ? 'warn' : 'ok');
      } else {
        consoleView.setStatus('шаг ' + (d.n || '?') + ' из ' + (d.total || '?') + ': ' + (d.title || d.key), 'live');
      }
      return;
    }

    if (ev.type === 'clarify') {
      state.sessionId = d.session_id || state.sessionId;
      state.clarifyRound += 1;
      if (ctx.thinkingBox) { ctx.thinkingBox.remove(); ctx.thinkingBox = null; }
      consoleView.setStatus('нужно уточнение: агент спросил ' + (d.questions || []).length + ' параметр(а)', 'live');
      renderClarify(ctx.host, d, {
        round: state.clarifyRound,
        onClarifyAnswer: function (answers, skipped, round) {
          submitAsk(state.lastQuestion, state.lastAsOf, answers, skipped);
        }
      });
      return;
    }

    if (ev.type === 'answer') {
      if (ctx.thinkingBox) { ctx.thinkingBox.remove(); ctx.thinkingBox = null; }
      d._turnId = ctx.turnId;
      renderAnswer(ctx.host, d);
      consoleView.setStatus('ответ готов', 'ok');
      return;
    }

    if (ev.type === 'refusal') {
      if (ctx.thinkingBox) { ctx.thinkingBox.remove(); ctx.thinkingBox = null; }
      renderRefusal(ctx.host, d);
      consoleView.setStatus('отказ: ' + (REFUSAL_KIND_LABEL[d.kind] || d.kind || 'нет ответа'), 'idle');
      return;
    }

    if (ev.type === 'done') {
      consoleView.push({ kind: 'done', key: 'done', title: 'Готово', detail: 'путь: ' + (d.inference_path || '—') + ' · журнал: ' + (d.journal_id || '—'), ms: d.elapsed_ms });
      consoleView.setStatus('готово за ' + ((d.elapsed_ms || 0) / 1000).toFixed(1) + ' с', 'ok');
      return;
    }

    if (ev.type === 'error') {
      if (ctx.thinkingBox) { ctx.thinkingBox.remove(); ctx.thinkingBox = null; }
      consoleView.push({ kind: 'error', key: 'error', title: 'Ошибка', detail: (d.message || '') + (d.stage ? ' · стадия ' + d.stage : '') });
      consoleView.setStatus('ошибка: ' + (d.stage || 'сервер'), 'bad');
      var note = el('p', 'refusal__reason', 'Запрос не обработан: ' + (d.message || 'ошибка сервера') + '.');
      ctx.host.appendChild(note);
    }
  }

  /* ------------------------------------------------------------------ отправка */

  function submitAsk(question, asOf, answers, skipped) {
    var ctxId = String(Date.now()) + '-' + String(Math.random()).slice(2, 7);
    var turn = addQuestion(question, skipped ? '' : asOf);
    turn.host.dataset.turnId = ctxId;
    var thinkingBox = showThinking(turn.host);

    var ctx = {
      host: turn.host,
      turnId: ctxId,
      thinkingBox: thinkingBox
    };

    consoleView.clear();
    consoleView.setStatus('запрос отправлен', 'live');
    $('send').disabled = true;

    var payload = {
      question: question,
      role_id: state.roleId
    };
    if (asOf) payload.as_of = asOf;
    if (state.lineId) payload.business_line = state.lineId;
    if (state.sessionId) payload.session_id = state.sessionId;
    if (answers) payload.answers = answers;

    function finish() {
      $('send').disabled = false;
      state.streaming = null;
    }

    if (state.mock) {
      state.streaming = global.MockLex.stream(question, state.roleId, asOf, answers, {
        onStep: function (d) { applyEvent({ type: 'step', data: d }, ctx); },
        onClarify: function (d) { applyEvent({ type: 'clarify', data: d }, ctx); finish(); },
        onAnswer: function (d) { applyEvent({ type: 'answer', data: d }, ctx); },
        onRefusal: function (d) { applyEvent({ type: 'refusal', data: d }, ctx); },
        onDone: function (d) { applyEvent({ type: 'done', data: d }, ctx); finish(); }
      });
      return;
    }

    // живой режим: POST + text/event-stream, разбор свой (EventSource не умеет POST)
    fetch('api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
      body: JSON.stringify(payload)
    }).then(function (resp) {
      if (!resp.ok || !resp.body) throw new Error('HTTP ' + resp.status);
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buf = '';

      function pump() {
        return reader.read().then(function (res) {
          if (res.done) { finish(); return; }
          buf += decoder.decode(res.value, { stream: true });
          var frames = buf.split('\n\n');
          buf = frames.pop();
          for (var i = 0; i < frames.length; i++) {
            var type = '';
            var dataLines = [];
            var lines = frames[i].split('\n');
            for (var l = 0; l < lines.length; l++) {
              if (lines[l].indexOf('event:') === 0) type = lines[l].slice(6).trim();
              else if (lines[l].indexOf('data:') === 0) dataLines.push(lines[l].slice(5).trim());
            }
            if (!type) continue;
            var parsed = {};
            try { parsed = dataLines.length ? JSON.parse(dataLines.join('\n')) : {}; }
            catch (e) { parsed = { message: 'не разобран JSON события' }; }
            applyEvent({ type: type, data: parsed }, ctx);
            if (type === 'done' || type === 'clarify' || type === 'error') finish();
          }
          return pump();
        });
      }
      return pump();
    }).catch(function (e) {
      finish();
      applyEvent({ type: 'error', data: { message: String(e && e.message || e), stage: 'network' } }, ctx);
    });
  }

  /* ------------------------------------------------------------------ форма */

  $('composer').addEventListener('submit', function (e) {
    e.preventDefault();
    var q = $('question').value.trim();
    if (!q) { $('question').focus(); return; }
    if (!state.roleId) {
      activeTab = 'role';
      renderTabs();
      var firstRole = $('settingsRow').querySelector('.chip');
      if (firstRole) firstRole.focus();
      return;
    }
    state.lastQuestion = q;
    state.lastAsOf = $('asOf').value || '';
    state.sessionId = '';
    state.clarifyRound = 0;
    $('question').value = '';
    goScreen('chat');
    submitAsk(q, state.lastAsOf, null, false);
  });

  // Enter отправляет, Shift+Enter — новая строка
  $('question').addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      $('composer').dispatchEvent(new Event('submit', { cancelable: true }));
    }
  });

  /* ------------------------------------------------------------------ старт */

  loadReference().then(function () {
    renderTabs();
    updateHint();
    renderRolesScreen();
    $('navCount').textContent = state.mock ? 'демо-режим · без бэкенда' : 'живой режим';
    document.documentElement.setAttribute('data-mode', state.mock ? 'mock' : 'live');
  });
})(window);
