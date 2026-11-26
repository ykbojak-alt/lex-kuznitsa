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
    out_of_scope: 'вне базы знаний',
    conflict_unresolved: 'конфликт не разрешён'
  };

  var TICKET_STATUS_LABEL = { open: 'открыт', assigned: 'в работе', answered: 'отвечен', closed: 'закрыт' };
  var TICKET_STATUS_TONE = { open: 'danger', assigned: 'amber', answered: 'ok', closed: 'muted' };

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
      refreshTicketNav();
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
    tickets: ['Заявки в службу качества', 'Агент создаёт тикет сам, когда ответа в базе нет. Доступно службе качества и руководителю.'],
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
    if (name === 'tickets') renderTicketsScreen();
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

  /* ------------------------------------------------------------------ экран «Заявки» (§18) */

  function ticketsAllowed() {
    return state.roleId === 'quality' || state.roleId === 'director';
  }

  function refreshTicketNav() {
    var nav = $('navTickets');
    if (nav) nav.hidden = !ticketsAllowed();
    // если открыт экран заявок, а роль больше не даёт доступа — вернуть в чат
    if (!ticketsAllowed() && doc.getElementById('screen-tickets').classList.contains('is-active')) {
      goScreen('chat');
    }
  }

  function loadTickets() {
    if (state.mock) return Promise.resolve(global.MockLex.tickets());
    return fetch('api/tickets').then(function (r) { return r.json(); }).catch(function () { return []; });
  }

  function sortTickets(list) {
    var order = { open: 0, assigned: 1, answered: 2, closed: 3 };
    return list.slice().sort(function (a, b) {
      var d = (order[a.status] || 9) - (order[b.status] || 9);
      if (d !== 0) return d;
      return String(b.created_at || '').localeCompare(String(a.created_at || ''));
    });
  }

  function slaBadge(t) {
    if (!t.sla_due) return null;
    var now = Date.now();
    var due = Date.parse(t.sla_due);
    var breached = t.status !== 'closed' && due < now;
    var span = el('span', 'sla' + (breached ? ' sla--breached' : ''),
      (breached ? 'просрочен: ' : 'срок до ') + fmtDate(String(t.sla_due).slice(0, 10)));
    return span;
  }

  function renderTicketsScreen() {
    var host = $('screen-tickets');
    clear(host);
    host.appendChild(el('h2', 'section-title', 'Заявки в службу качества'));
    host.appendChild(el('p', 'section-sub',
      'Тикеты создаёт агент сам, когда ответа в базе нет. Служба качества берёт в работу и отвечает; после ответа агент предлагает черновик кейса.'));

    if (!ticketsAllowed()) {
      host.appendChild(el('p', 'section-sub', 'Экран доступен ролям «Отдел качества» и «Руководитель».'));
      return;
    }

    var wrap = el('div', 'tickets');
    host.appendChild(wrap);

    loadTickets().then(function (list) {
      clear(wrap);
      if (!list.length) { wrap.appendChild(el('p', 'section-sub', 'Заявок нет.')); return; }

      var table = el('table', 'wikitable tickets__table');
      var head = el('tr');
      ['Заявка', 'Вопрос', 'Направление', 'Статус', 'Кто взял', 'Срок'].forEach(function (h) {
        head.appendChild(el('th', null, h));
      });
      table.appendChild(head);

      sortTickets(list).forEach(function (t) {
        var tr = el('tr', 'tickets__row');
        tr.tabIndex = 0;
        tr.setAttribute('role', 'button');
        tr.setAttribute('aria-label', 'Открыть заявку ' + t.id);

        var tdId = el('td', null, t.id);
        tdId.className = 'tickets__id';
        tr.appendChild(tdId);
        tr.appendChild(el('td', null, t.question));

        var line = t.asker && t.asker.business_line ? t.asker.business_line : '—';
        tr.appendChild(el('td', null, line));

        var tdSt = el('td');
        tdSt.appendChild(el('span', 'tstatus tstatus--' + (TICKET_STATUS_TONE[t.status] || 'muted'),
          TICKET_STATUS_LABEL[t.status] || t.status));
        if (t.clarifications && t.clarifications.length) {
          tdSt.appendChild(el('span', 'tickets__iters', 'уточнений: ' + t.clarifications.length));
        }
        tr.appendChild(tdSt);

        tr.appendChild(el('td', null, t.assignee || '—'));

        var tdSla = el('td');
        var sb = slaBadge(t);
        if (sb) tdSla.appendChild(sb);
        tr.appendChild(tdSla);

        function openIt() { renderTicketDetail(t.id); }
        tr.addEventListener('click', openIt);
        tr.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openIt(); } });
        table.appendChild(tr);
      });
      wrap.appendChild(table);
    });
  }

  function renderTicketDetail(id) {
    var host = $('screen-tickets');
    clear(host);

    var back = el('button', 'linkish', '← к списку заявок');
    back.type = 'button';
    back.addEventListener('click', renderTicketsScreen);
    host.appendChild(back);

    var t = state.mock ? global.MockLex.ticket(id) : null;
    if (!t) { host.appendChild(el('p', 'section-sub', 'Заявка не найдена.')); return; }

    host.appendChild(el('h2', 'section-title', 'Заявка ' + t.id));

    var status = el('p', 'tikcket-detail__status');
    status.appendChild(el('span', 'tstatus tstatus--' + (TICKET_STATUS_TONE[t.status] || 'muted'),
      TICKET_STATUS_LABEL[t.status] || t.status));
    status.appendChild(el('span', 'tickets__iters', t.created_by === 'agent'
      ? 'создан агентом автоматически' : 'создан ' + t.created_by));
    if (t.assignee) status.appendChild(el('span', 'tickets__iters', 'в работе: ' + t.assignee));
    var sb = slaBadge(t);
    if (sb) status.appendChild(sb);
    host.appendChild(status);

    /* Вопрос сотрудника */
    host.appendChild(el('h3', 'detail-h', 'Вопрос сотрудника'));
    host.appendChild(el('p', null, t.question));
    if (t.asker) {
      host.appendChild(el('p', 'ticketcard__reason',
        'Роль: ' + roleTitle(t.asker.role_id) + ' · направление: ' + (t.asker.business_line || '—')));
    }

    /* Что спросили (итерации) */
    if (t.clarifications && t.clarifications.length) {
      host.appendChild(el('h3', 'detail-h', 'Что спросили у сотрудника'));
      var cl = el('ol', 'ticket-clar');
      t.clarifications.forEach(function (it) {
        var li = el('li');
        li.appendChild(el('span', 'ticket-clar__asked', 'Итерация ' + it.iteration + ': ' + it.asked.join('; ')));
        li.appendChild(el('span', 'ticket-clar__answered', '→ ' + (it.answers || []).join('; ')));
        cl.appendChild(li);
      });
      host.appendChild(cl);
    }

    /* Что искали */
    if (t.searched) {
      host.appendChild(el('h3', 'detail-h', 'Что искали'));
      var bits = [];
      if (t.searched.documents != null) bits.push('документов: ' + t.searched.documents);
      if (t.searched.cases != null) bits.push('кейсов: ' + t.searched.cases);
      if (t.searched.accessible != null) bits.push('доступно: ' + t.searched.accessible);
      if (t.searched.top && t.searched.top.length) bits.push('топ: ' + t.searched.top.join(', '));
      host.appendChild(el('p', null, bits.join(' · ')));
    }

    /* Почему не решилось — обязательное поле */
    host.appendChild(el('h3', 'detail-h', 'Почему не решилось'));
    host.appendChild(el('p', 'ticketcard__reason', t.why_unresolved || '—'));

    /* Действия службы качества */
    var actions = el('div', 'ticket-actions');

    if (t.status === 'open') {
      var take = el('button', 'btn btn--primary', 'Взять в работу');
      take.type = 'button';
      take.addEventListener('click', function () {
        if (state.mock) global.MockLex.assignTicket(id, 'И. Петрова');
        renderTicketDetail(id);
      });
      actions.appendChild(take);
    }
    host.appendChild(actions);

    if (t.status !== 'closed') {
      host.appendChild(el('h3', 'detail-h', 'Ответ службы качества'));
      var ta = el('textarea', 'ticket-answer');
      ta.rows = 4;
      ta.setAttribute('aria-label', 'Ответ службы качества');
      ta.placeholder = 'Ответ сотруднику: что делать по существу вопроса';
      if (t.quality_answer) ta.value = t.quality_answer;
      host.appendChild(ta);

      var row = el('div', 'ticket-actions');
      var submit = el('button', 'btn btn--primary', 'Ответить');
      submit.type = 'button';
      submit.addEventListener('click', function () {
        var text = ta.value.trim();
        if (!text) { ta.focus(); return; }
        if (state.mock) { global.MockLex.answerTicket(id); }
        renderTicketDetail(id);
      });
      row.appendChild(submit);

      var close = el('button', 'btn', 'Закрыть без ответа');
      close.type = 'button';
      close.addEventListener('click', function () {
        if (state.mock) global.MockLex.closeTicket(id, ta.value.trim());
        renderTicketDetail(id);
      });
      row.appendChild(close);
      host.appendChild(row);
    }

    if (t.status === 'closed' && t.close_reason) {
      host.appendChild(el('p', 'ticketcard__reason', 'Закрыт: ' + t.close_reason));
    }
    if (t.quality_answer) {
      host.appendChild(el('h3', 'detail-h', 'Дан ответ'));
      host.appendChild(el('p', 'detail-quote', t.quality_answer));
    }

    /* §16: после ответа — карточка черновика кейса */
    var draft = state.mock ? global.MockLex.draft(id) : null;
    if (draft && (t.status === 'answered' || t.status === 'closed') && draft.status !== 'resolved') {
      host.appendChild(renderDraftCard(id, draft));
    }
    if (draft && draft.status === 'resolved') {
      host.appendChild(el('p', 'form__ok', 'Кейс добавлен в базу. На этот вопрос теперь отвечают.'));
    }
  }

  /* §16.2–16.3: карточка черновика кейса с suggestions и подтверждением. */
  function renderDraftCard(ticketId, draftData) {
    var d = draftData.draft || {};
    var sug = draftData.suggestions || { missing: [], conflicts: [] };
    var card = el('section', 'draftcard');
    card.appendChild(el('h3', 'detail-h', 'Черновик кейса — предлагает агент'));
    card.appendChild(el('p', 'formcard__hint',
      'Агент не пишет в базу сам: проверьте и подтвердите. Правки сохраняются в кейс.'));

    var values = {
      question: d.question || '',
      answer_in_practice: d.answer_in_practice || '',
      business_line: d.business_line || '',
      tags: (d.tags || []).join(', '),
      linked_docs: (d.linked_docs || []).join(', '),
      outcome: d.outcome || 'resolved'
    };

    var inputs = {};

    function field(key, label, opts) {
      var row = el('div', 'form__row');
      var lab = el('label', 'form__label', label);
      lab.setAttribute('for', 'dr-' + key);
      row.appendChild(lab);
      var input;
      if (opts && opts.select) {
        input = doc.createElement('select');
        opts.select.forEach(function (o) {
          var op = doc.createElement('option');
          op.value = o;
          op.textContent = o;
          input.appendChild(op);
        });
        input.value = values[key];
      } else if (opts && opts.area) {
        input = doc.createElement('textarea');
        input.rows = 3;
        input.value = values[key];
      } else {
        input = doc.createElement('input');
        input.type = 'text';
        input.value = values[key];
      }
      input.id = 'dr-' + key;
      input.className = 'form__input';
      input.addEventListener('input', function () { values[key] = input.value; updateState(); });
      input.addEventListener('change', function () { values[key] = input.value; updateState(); });
      row.appendChild(input);
      inputs[key] = input;
      card.appendChild(row);
    }

    field('question', 'Вопрос кейса');
    field('answer_in_practice', 'Ответ (практика)', { area: true });
    field('business_line', 'Направление');
    field('tags', 'Теги (через запятую)');
    field('linked_docs', 'Ссылки на документы (через запятую)');
    field('outcome', 'Итог', { select: ['resolved', 'escalated', 'rejected', 'open'] });

    /* SUGGESTIONS */
    if (sug.missing && sug.missing.length) {
      var mBox = el('div', 'sug sug--missing');
      mBox.appendChild(el('p', 'sug__title', 'Чего не хватает'));
      sug.missing.forEach(function (m) {
        var li = el('p', 'sug__item');
        li.appendChild(el('strong', null, m.field + ': '));
        li.appendChild(doc.createTextNode(m.why || ''));
        if (m.note) li.appendChild(el('span', 'sug__note', ' — ' + m.note));
        mBox.appendChild(li);
      });
      card.appendChild(mBox);
    }

    if (sug.conflicts && sug.conflicts.length) {
      var cBox = el('div', 'sug sug--conflict');
      cBox.appendChild(el('p', 'sug__title', 'С чем расходится'));
      sug.conflicts.forEach(function (c) {
        var li = el('p', 'sug__item');
        li.appendChild(el('strong', null, c.with + ': '));
        li.appendChild(doc.createTextNode(c.why || ''));
        var link = el('button', 'linkish sug__link', 'открыть документ');
        link.type = 'button';
        link.addEventListener('click', function () { openDocument(c.with, c.with); });
        li.appendChild(link);
        cBox.appendChild(li);
      });
      card.appendChild(cBox);
    }

    var hasWarnings = (sug.conflicts || []).length > 0;
    var ack = null;
    if (hasWarnings) {
      var ackRow = el('label', 'sug__ack');
      ack = doc.createElement('input');
      ack.type = 'checkbox';
      ack.id = 'dr-ack';
      ack.addEventListener('change', updateState);
      ackRow.appendChild(ack);
      ackRow.appendChild(el('span', null, 'Понимаю расхождение, всё равно добавить'));
      card.appendChild(ackRow);
    }

    var statusRow = el('div', 'draftcard__status');
    var addBtn = el('button', 'btn btn--primary', 'Добавить в базу');
    addBtn.type = 'button';
    addBtn.disabled = true;
    statusRow.appendChild(addBtn);
    var msg = el('span', 'form__err');
    statusRow.appendChild(msg);
    card.appendChild(statusRow);

    var result = el('div', 'draftcard__result');
    card.appendChild(result);

    function updateState() {
      var filled = values.question.trim() && values.answer_in_practice.trim();
      var ackOk = !hasWarnings || (ack && ack.checked);
      addBtn.disabled = !(filled && ackOk);
    }
    updateState();

    addBtn.addEventListener('click', function () {
      var caseObj = {
        question: values.question.trim(),
        answer_in_practice: values.answer_in_practice.trim(),
        business_line: values.business_line.trim(),
        tags: values.tags.split(',').map(function (s) { return s.trim(); }).filter(Boolean),
        linked_docs: values.linked_docs.split(',').map(function (s) { return s.trim(); }).filter(Boolean),
        outcome: values.outcome,
        author_role: 'quality',
        author_name: 'служба качества',
        content_type: 'text',
        source: 'escalation',
        escalation_id: ticketId
      };
      var res = state.mock
        ? global.MockLex.confirmDraft(ticketId, caseObj, ack ? ack.checked : false)
        : { ok: true, case_id: 'case-????', affected: 'отправлено' };

      clear(result);
      if (res.ok) {
        var okRow = el('div', 'draftcard__done');
        okRow.appendChild(el('p', 'form__ok',
          'Кейс ' + res.case_id + ' в базе. На этот вопрос теперь отвечают.'));
        if (res.affected) okRow.appendChild(el('p', 'formcard__hint', res.affected));
        var backBtn = el('button', 'btn', 'Вернуться к списку');
        backBtn.type = 'button';
        backBtn.addEventListener('click', function () { renderTicketsScreen(); refreshTicketNav(); });
        okRow.appendChild(backBtn);
        result.appendChild(okRow);
        addBtn.disabled = true;
      } else {
        msg.textContent = res.error || 'не удалось добавить';
        msg.className = 'form__err form__err--show';
      }
    });

    return card;
  }

  function renderStatsScreen() {    var host = $('screen-stats');
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

    /* «Часто спрашивают»: канонический вопрос, частота, варианты, сигнал «регламента нет». */
    host.appendChild(el('h2', 'section-title', 'Часто спрашивают'));
    host.appendChild(el('p', 'section-sub',
      'Похожие формулировки сводятся к каноническому вопросу. Высокая доля отказов — сигнал, что регламента нет. Нажмите на вопрос, чтобы подставить его в поле ввода.'));

    var sims;
    if (state.mock) {
      sims = global.MockLex.similar();
    } else {
      sims = (state.similar || []);
    }
    if (!sims.length) {
      host.appendChild(el('p', 'section-sub', 'В живом режиме список приходит из аналитики журнала.'));
      return;
    }

    var stable = el('table', 'wikitable');
    var sh = el('tr');
    ['Вопрос', 'Спросили', 'Доля отказов', 'Формулировки'].forEach(function (h) { sh.appendChild(el('th', null, h)); });
    stable.appendChild(sh);

    for (var s = 0; s < sims.length; s++) {
      var item = sims[s];
      var tr2 = el('tr');
      var tdq = el('td');
      var qlink = el('button', 'linkish', item.question);
      qlink.type = 'button';
      qlink.addEventListener('click', (function (text) {
        return function () {
          goScreen('chat');
          var box = $('question');
          box.value = text;
          box.focus();
          box.setSelectionRange(text.length, text.length);
        };
      })(item.question));
      tdq.appendChild(qlink);
      if (item.refusal_rate >= 0.5) {
        tdq.appendChild(el('span', 'sig-nodoc', item.note ? item.note : 'регламента нет'));
      }
      tr2.appendChild(tdq);
      tr2.appendChild(el('td', 'num', '×' + item.count));
      tr2.appendChild(el('td', 'num', Math.round((item.refusal_rate || 0) * 100) + '%'));
      tr2.appendChild(el('td', null, (item.variants || []).join(' · ')));
      stable.appendChild(tr2);
    }
    host.appendChild(stable);
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

  /* ------------------------------------------------------------------ ответ: §13.1 кратко, §13.5 кнопки */

  /* Текст с сносками [n] -> надстрочные кликабельные ссылки. */
  function fillWithNotes(target, text, turnId) {
    var parts = String(text || '').split(/(\[\d+\])/g);
    for (var i = 0; i < parts.length; i++) {
      var m = /^\[(\d+)\]$/.exec(parts[i]);
      if (m) {
        var n = parseInt(m[1], 10);
        var fn = el('a', 'fn', '[' + n + ']');
        fn.href = '#ref-' + n + '-' + turnId;
        fn.setAttribute('role', 'doc-noteref');
        fn.setAttribute('aria-label', 'сноска ' + n + ': перейти к источнику');
        fn.addEventListener('click', (function (num) {
          return function (e) {
            e.preventDefault();
            var t = doc.getElementById('ref-' + num + '-' + turnId);
            if (t) { t.setAttribute('data-open', 'true'); t.scrollIntoView({ block: 'center', behavior: 'smooth' }); }
          };
        })(n));
        target.appendChild(fn);
      } else if (parts[i]) {
        target.appendChild(doc.createTextNode(parts[i]));
      }
    }
  }

  function renderAnswer(host, data) {
    clear(host);
    var turnId = data._turnId || '0';

    /* §13.1: по умолчанию — короткая выжимка, крупно. Полный text — запасной путь. */
    var shortText = data.short || data.text || '';
    var box = el('div', 'answer-short');
    var p = el('p');
    fillWithNotes(p, shortText, turnId);
    box.appendChild(p);
    host.appendChild(box);

    var hasDetails = data.has_details === true || (data.text && data.short && data.text !== data.short);
    var actions = el('div', 'answer-actions');

    if (hasDetails) {
      var more = el('button', 'btn', 'Подробнее');
      more.type = 'button';
      more.setAttribute('aria-expanded', 'false');
      var detailHost = el('div', 'answer-detail');
      detailHost.hidden = true;
      more.addEventListener('click', (function (btn, hostD, open) {
        return function () {
          var isOpen = btn.getAttribute('aria-expanded') === 'true';
          if (!isOpen) {
            if (!hostD.dataset.loaded) {
              loadDetails(hostD, data, turnId);
              hostD.dataset.loaded = '1';
            }
            hostD.hidden = false;
            btn.textContent = 'Свернуть';
            btn.setAttribute('aria-expanded', 'true');
          } else {
            hostD.hidden = true;
            btn.textContent = 'Подробнее';
            btn.setAttribute('aria-expanded', 'false');
          }
        };
      })(more, detailHost, false));
      actions.appendChild(more);

      var firstCite = (data.citations && data.citations[0]) || null;
      if (firstCite && firstCite.view_url) {
        var openDoc = el('button', 'btn', 'Открыть документ');
        openDoc.type = 'button';
        openDoc.addEventListener('click', (function (c) {
          return function () { openDocument(c.doc_id, c.view_url); };
        })(firstCite));
        actions.appendChild(openDoc);
      }

      host.appendChild(actions);
      host.appendChild(detailHost);
    } else if (data.text && !data.short) {
      // Ответ без короткой формы: показываем текст как есть (обратная совместимость).
      var full = el('div', 'answer__lead');
      var fp = el('p');
      fillWithNotes(fp, data.text, turnId);
      full.appendChild(fp);
      host.appendChild(full);
    }

    if (data.assumptions && data.assumptions.length) {
      var asum = el('div', 'assumptions');
      for (var a = 0; a < data.assumptions.length; a++) {
        var line = el('div');
        line.appendChild(el('dt', null, data.assumptions[a].key + ':'));
        line.appendChild(el('dd', null, data.assumptions[a].note || data.assumptions[a].value));
        asum.appendChild(line);
      }
      host.appendChild(asum);
    }

    host.appendChild(renderCitationList(data));

    /* §13.3: карточка формы, если ответ её несёт. */
    if (data.form_id) host.appendChild(renderFormCard(data.form_id));

    scrollFeed();
  }

  /* §13.1: развёрнутый ответ — разделы, таблицы, цитаты — прямо в ленте. */
  function loadDetails(hostD, data, turnId) {
    var detail = null;
    if (state.mock) detail = global.MockLex.expand(data.detail_id);
    if (!detail) {
      // без /api/expand показываем полный текст, если он есть
      if (data.text && data.text !== data.short) {
        var lede = el('p');
        fillWithNotes(lede, data.text, turnId);
        hostD.appendChild(lede);
      }
      return;
    }

    if (detail.detailed) {
      var d = el('p');
      fillWithNotes(d, detail.detailed, turnId);
      hostD.appendChild(d);
    }

    if (detail.sections && detail.sections.length) {
      for (var i = 0; i < detail.sections.length; i++) {
        var sec = detail.sections[i];
        var h = el('h4', 'detail-h', sec.title);
        hostD.appendChild(h);
        if (sec.quote) hostD.appendChild(el('p', 'detail-quote', sec.quote));
      }
    }

    if (detail.tables && detail.tables.length) {
      for (var t = 0; t < detail.tables.length; t++) {
        var tb = detail.tables[t];
        var table = el('table', 'wikitable detail-table');
        var cap = el('caption', null, tb.caption);
        table.appendChild(cap);
        for (var r = 0; r < tb.rows.length; r++) {
          var tr = el('tr');
          for (var c = 0; c < tb.rows[r].length; c++) {
            var cell = el(r === 0 ? 'th' : 'td', null, tb.rows[r][c]);
            tr.appendChild(cell);
          }
          table.appendChild(tr);
        }
        hostD.appendChild(table);
      }
    }
    hostD.appendChild(el('p', 'detail-note',
      'Развёрнутая версия построена из тех же фрагментов, что и выжимка: новых источников она не добавляет.'));
  }

  /* §13.2: читать документ — модальное окно с оглавлением слева и текстом справа. */
  function openDocument(docId, viewUrl) {
    var docData = null;
    if (state.mock) docData = global.MockLex.doc(docId);
    if (!docData) {
      if (viewUrl && viewUrl.charAt(0) !== '#') { global.open(viewUrl, '_blank', 'noopener'); return; }
      alert('Документ ' + docId + ' не найден в мок-режиме.');
      return;
    }

    var backdrop = el('div', 'modal');
    backdrop.setAttribute('role', 'dialog');
    backdrop.setAttribute('aria-modal', 'true');
    backdrop.setAttribute('aria-label', 'Документ: ' + docData.title);

    var win = el('div', 'modal__win');
    var head = el('div', 'modal__head');
    head.appendChild(el('span', 'modal__title', docData.title + ' · Р' + docData.revision));
    var close = el('button', 'btn', 'Закрыть');
    close.type = 'button';
    head.appendChild(close);
    win.appendChild(head);

    var body = el('div', 'modal__body');
    var toc = el('nav', 'modal__toc');
    toc.appendChild(el('p', 'modal__tocTitle', 'Оглавление'));
    var text = el('div', 'modal__text');
    text.appendChild(el('p', 'modal__meta',
      'Документ ' + docData.doc_id + ' · редакция ' + docData.revision + ' от ' + fmtDate(docData.effective)
      + ' · ' + docData.department + ' · направление: ' + docData.business_line));

    for (var i = 0; i < docData.sections.length; i++) {
      var s = docData.sections[i];
      var anchor = 'dsec-' + i;
      var li = el('button', 'linkish modal__tocItem', s.title);
      li.type = 'button';
      li.addEventListener('click', (function (id) {
        return function () { var t = doc.getElementById(id); if (t) t.scrollIntoView({ block: 'start', behavior: 'smooth' }); };
      })(anchor));
      toc.appendChild(li);

      var h = el('h3', 'modal__h', s.title);
      h.id = anchor;
      text.appendChild(h);
      text.appendChild(el('p', null, s.text));
    }
    if (docData.ocr_degraded) {
      text.appendChild(el('span', 'warn-ocr', 'скан распознан неуверенно — сверьте с оригиналом'));
    }

    body.appendChild(toc);
    body.appendChild(text);
    win.appendChild(body);

    var foot = el('div', 'modal__foot');
    var dl = el('a', 'btn', 'Скачать');
    dl.href = '#';
    dl.setAttribute('role', 'button');
    dl.addEventListener('click', function (e) {
      e.preventDefault();
      alert('Скачивание в мок-режиме недоступно: файл отдаёт GET /api/doc/{id}/download.');
    });
    foot.appendChild(dl);
    win.appendChild(foot);

    backdrop.appendChild(win);
    doc.body.appendChild(backdrop);

    function closeModal() { backdrop.remove(); doc.removeEventListener('keydown', onKey); }
    function onKey(e) { if (e.key === 'Escape') closeModal(); }
    close.addEventListener('click', closeModal);
    backdrop.addEventListener('click', function (e) { if (e.target === backdrop) closeModal(); });
    doc.addEventListener('keydown', onKey);
    close.focus();
  }

  /* §13.3–13.4: карточка формы и мини-форма с проверкой. */
  function renderFormCard(formId) {
    var form = state.mock ? global.MockLex.form(formId) : null;
    var card = el('div', 'formcard');
    var h = el('div', 'formcard__head');
    h.appendChild(el('span', 'formcard__title', 'Есть форма: ' + (form ? form.title : formId)));
    card.appendChild(h);
    card.appendChild(el('p', 'formcard__hint', 'Бланк можно скачать и заполнить офлайн или заполнить здесь — агент сверит данные с регламентом.'));

    var actions = el('div', 'formcard__actions');
    var dl = el('button', 'btn', 'Скачать бланк');
    dl.type = 'button';
    dl.addEventListener('click', function () { alert('Скачивание: GET /api/forms/' + formId + '/download (в мок-режиме недоступно).'); });

    var fill = el('button', 'btn btn--primary', 'Заполнить здесь');
    fill.type = 'button';
    fill.setAttribute('aria-expanded', 'false');

    var host = el('div', 'formfield-host');
    host.hidden = true;

    fill.addEventListener('click', (function (btn, hostF) {
      return function () {
        var isOpen = btn.getAttribute('aria-expanded') === 'true';
        if (!isOpen) {
          if (!hostF.dataset.loaded) { buildForm(hostF, formId); hostF.dataset.loaded = '1'; }
          hostF.hidden = false;
          btn.textContent = 'Свернуть форму';
          btn.setAttribute('aria-expanded', 'true');
        } else {
          hostF.hidden = true;
          btn.textContent = 'Заполнить здесь';
          btn.setAttribute('aria-expanded', 'false');
        }
      };
    })(fill, host));

    actions.appendChild(dl);
    actions.appendChild(fill);
    card.appendChild(actions);
    card.appendChild(host);
    return card;
  }

  function buildForm(host, formId) {
    var form = state.mock ? global.MockLex.form(formId) : null;
    if (!form) { host.appendChild(el('p', 'formcard__hint', 'Описание формы приходит из /api/forms/' + formId)); return; }

    var inputs = {};
    var wrap = el('div', 'form');
    var progress = el('div', 'form__progress');

    for (var i = 0; i < form.fields.length; i++) {
      var f = form.fields[i];
      var row = el('div', 'form__row');
      var label = el('label', 'form__label', f.label);
      label.setAttribute('for', 'ff-' + f.key);
      if (f.required) label.appendChild(el('span', 'form__req', '*'));
      row.appendChild(label);

      var input;
      if (f.type === 'choice') {
        input = doc.createElement('select');
        var o0 = doc.createElement('option');
        o0.value = '';
        o0.textContent = 'выберите…';
        input.appendChild(o0);
        for (var o = 0; o < (f.options || []).length; o++) {
          var op = doc.createElement('option');
          op.value = f.options[o];
          op.textContent = f.options[o];
          input.appendChild(op);
        }
      } else {
        input = doc.createElement('input');
        input.type = f.type === 'date' ? 'date' : 'text';
        if (f.type === 'money') input.inputMode = 'numeric';
        if (f.example) input.placeholder = f.example;
      }
      input.id = 'ff-' + f.key;
      input.className = 'form__input';
      input.setAttribute('aria-describedby', 'fh-' + f.key + ' fe-' + f.key);
      row.appendChild(input);

      var hint = el('div', 'form__hint', f.hint || '');
      hint.id = 'fh-' + f.key;
      row.appendChild(hint);

      var errBox = el('div', 'form__err');
      errBox.id = 'fe-' + f.key;
      row.appendChild(errBox);

      inputs[f.key] = { field: f, el: input, err: errBox, hint: hint };

      input.addEventListener('input', (function (ref, prog) {
        return function () { liveValidate(ref, prog, inputs); };
      })(inputs[f.key], progress));

      wrap.appendChild(row);
    }

    function updateProgress() {
      var filledCount = 0;
      for (var k in inputs) {
        if (inputs[k].el.value.trim()) filledCount++;
      }
      progress.textContent = filledCount + ' из ' + form.fields.length + ' полей';
    }

    wrap.appendChild(progress);

    var check = el('button', 'btn btn--primary', 'Проверить');
    check.type = 'button';

    var resultBox = el('div', 'form__result');

    check.addEventListener('click', function () {
      var values = {};
      for (var k in inputs) values[k] = inputs[k].el.value;
      var res;
      if (state.mock) res = global.MockLex.fillForm(formId, values);
      else res = { ok: true, errors: [], warnings: [], progress: '' };

      clear(resultBox);
      // ошибки по полям
      for (var e = 0; e < (res.errors || []).length; e++) {
        var er = res.errors[e];
        if (inputs[er.field]) {
          inputs[er.field].err.textContent = er.message + (er.expected ? ' · ожидается: ' + er.expected : '');
          inputs[er.field].err.className = 'form__err form__err--show';
          inputs[er.field].el.classList.add('form__input--bad');
        }
      }
      for (var w = 0; w < (res.warnings || []).length; w++) {
        var wr = res.warnings[w];
        var wn = el('p', 'form__warn', 'Внимание: ' + wr.message + (wr.doc_id ? ' (' + wr.doc_id + ')' : ''));
        resultBox.appendChild(wn);
      }
      var summary = el('p', res.ok ? 'form__ok' : 'form__bad',
        res.ok ? 'Проверки пройдены · ' + (res.progress || '') : 'Есть ошибки · ' + (res.progress || ''));
      resultBox.insertBefore(summary, resultBox.firstChild);
    });

    var foot = el('div', 'form__foot');
    foot.appendChild(check);
    foot.appendChild(resultBox);
    wrap.appendChild(foot);

    host.appendChild(wrap);
    updateProgress();
  }

  /* живая проверка одного поля — зелёная галочка или красный текст */
  function liveValidate(ref, progress, allInputs) {
    var f = ref.field;
    var v = ref.el.value.trim();
    var ok = true;
    var msg = '';

    if (!v) {
      ok = !f.required;
    } else if (f.key === 'number' && f.pattern && !(new RegExp(f.pattern)).test(v)) {
      ok = false; msg = 'формат: ' + (f.hint || f.example || '');
    } else if (f.type === 'money' && !v.replace(/[^\d]/g, '')) {
      ok = false; msg = 'нужно число';
    } else if (f.type === 'date' && !/^\d{4}-\d{2}-\d{2}$/.test(v)) {
      ok = false; msg = 'дата в формате ГГГГ-ММ-ДД';
    }

    ref.el.classList.toggle('form__input--good', ok && !!v);
    ref.el.classList.toggle('form__input--bad', !ok);
    ref.err.textContent = ok ? '' : msg;
    ref.err.className = 'form__err' + (ok ? '' : ' form__err--show');

    var filledCount = 0;
    for (var k in allInputs) if (allInputs[k].el.value.trim()) filledCount++;
    progress.textContent = filledCount + ' из ' + Object.keys(allInputs).length + ' полей';
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

      if ((c.content_type === 'image' || c.content_type === 'scan' || c.content_type === 'mixed') && c.preview_url) {        var fig = el('figure', 'ref__preview');
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

      /* §13.2: у цитаты две ссылки — «Читать» и «Скачать». */
      if (c.view_url || c.download_url) {
        var links = el('div', 'ref__links');
        if (c.view_url) {
          var read = el('button', 'linkish', 'Читать');
          read.type = 'button';
          read.addEventListener('click', (function (cite) {
            return function () { openDocument(cite.doc_id, cite.view_url); };
          })(c));
          links.appendChild(read);
        }
        if (c.download_url) {
          var dlLink = el('a', 'linkish ref__dl', 'Скачать');
          dlLink.href = c.download_url;
          dlLink.setAttribute('download', '');
          dlLink.addEventListener('click', function (e) {
            if (String(this.getAttribute('href')).charAt(0) === '#') {
              e.preventDefault();
              alert('Скачивание в мок-режиме: файл отдаёт GET /api/doc/{id}/download.');
            }
          });
          links.appendChild(dlLink);
        }
        content.appendChild(links);
      }

      ref.appendChild(content);
      wrap.appendChild(ref);
    }
    return wrap;
  }

  /* §12.3 / §17.4: отказ — нормальный результат, не ошибка.
     Два разных текста: вне компетенции (§17) и нет в базе (§12.3). */
  function renderRefusal(host, data) {
    clear(host);
    var outOfScope = data.kind === 'out_of_scope';
    var box = el('div', outOfScope ? 'refusal refusal--scope' : 'refusal');
    var head = el('div', 'refusal__head');
    head.appendChild(el('span', 'refusal__title',
      outOfScope ? 'Вопрос вне базы знаний организации' : 'В базе знаний ответа нет'));
    head.appendChild(el('span', 'refusal__kind', REFUSAL_KIND_LABEL[data.kind] || data.kind || ''));
    box.appendChild(head);
    box.appendChild(el('p', 'refusal__reason', data.reason || ''));

    if (data.detail) box.appendChild(el('p', 'refusal__reason', data.detail));

    /* Что искали — только для not_in_corpus: при out_of_scope поиска не было (§17.4). */
    if (data.searched) {
      var s = data.searched;
      var bits = [];
      if (s.documents != null) bits.push('документов: ' + s.documents);
      if (s.cases != null) bits.push('кейсов: ' + s.cases);
      if (s.business_lines && s.business_lines.length) bits.push('направлений: ' + s.business_lines.join(', '));
      var searched = el('p', 'refusal__reason', 'Что искали — ' + bits.join(' · '));
      box.appendChild(searched);
    } else if (outOfScope) {
      box.appendChild(el('p', 'refusal__reason',
        'Поиск не выполнялся: вопрос не относится к предметной области базы знаний, поэтому тикет в службу качества не создаётся.'));
    }

    if (data.hint) box.appendChild(el('p', 'refusal__reason', 'Подсказка: ' + data.hint));

    if (data.sources && data.sources.length) {
      var srcs = el('div', 'refusal__sources');
      srcs.appendChild(el('span', null, 'связанные документы'));
      for (var i = 0; i < data.sources.length; i++) srcs.appendChild(el('span', null, data.sources[i]));
      box.appendChild(srcs);
    }
    host.appendChild(box);
    scrollFeed();
  }

  /* §18.4: карточка тикета в чате — меняет состояние. */
  function renderTicketCard(host, data, ctx) {
    var box = el('div', 'ticketcard');
    var status = data.status || 'open';
    var head = el('div', 'ticketcard__head');
    head.appendChild(el('span', 'ticketcard__badge ticketcard__badge--' + (TICKET_STATUS_TONE[status] || 'muted'),
      TICKET_STATUS_LABEL[status] || status));
    head.appendChild(el('span', 'ticketcard__id', data.ticket || ''));
    head.appendChild(el('span', 'ticketcard__by', 'создан агентом автоматически'));
    box.appendChild(head);

    if (status === 'open' || status === 'assigned') {
      box.appendChild(el('p', 'ticketcard__line', 'Вопрос передан в службу качества.'));
      if (data.searched) {
        var bits = [];
        if (data.searched.documents != null) bits.push(data.searched.documents + ' документов');
        if (data.searched.cases != null) bits.push(data.searched.cases + ' кейсов');
        box.appendChild(el('p', 'ticketcard__line', 'Что проверили: ' + bits.join(', ')
          + (data.eta ? ' · срок: ' + data.eta : '')));
      }
      if (data.assignee) box.appendChild(el('p', 'ticketcard__line', 'Взял в работу: ' + data.assignee));
      if (data.reason) box.appendChild(el('p', 'ticketcard__reason', 'Почему: ' + data.reason));
    } else if (status === 'closed' || status === 'answered') {
      box.appendChild(el('p', 'ticketcard__line', 'Ваш вопрос решён'
        + (data.case_id ? ' · добавлено в базу как кейс ' + data.case_id : '')));
      if (ctx && ctx.onGetAnswer) {
        var btn = el('button', 'btn btn--primary', 'Получить ответ');
        btn.type = 'button';
        btn.addEventListener('click', ctx.onGetAnswer);
        box.appendChild(btn);
      }
    }
    host.appendChild(box);
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
      consoleView.setStatus(d.kind === 'out_of_scope' ? 'вопрос вне компетенции базы' : 'отказ: ' + (REFUSAL_KIND_LABEL[d.kind] || d.kind || 'нет ответа'), 'idle');
      return;
    }

    if (ev.type === 'escalate') {
      if (ctx.thinkingBox) { ctx.thinkingBox.remove(); ctx.thinkingBox = null; }
      renderTicketCard(ctx.host, {
        ticket: d.ticket, status: 'open', searched: d.searched, eta: d.eta, reason: d.reason
      }, {});
      state.lastTicket = d.ticket;
      consoleView.setStatus('тикет создан: ' + d.ticket + ' · ждёт службу качества', 'warn');
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
