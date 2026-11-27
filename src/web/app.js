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
    tone: 'plain',
    roles: [],
    lines: [],
    sessionId: '',
    clarifyRound: 0,
    answers: {},
    streaming: null,
    turn: null,
    lastQuestion: '',
    lastAsOf: '',
    lastTicket: null,
    ticketCards: {},
    ticketDetailCache: {},
    ticketDraftCache: {},
    caseCache: null,
    caseMeta: null,
    stats: null,
    juryStats: null,
    health: null,
    similar: [],
    paused: false
  };

  /* ------------------------------------------------------------------ утилиты */

  /* Роль запоминается в localStorage: сотрудник качества возвращается по
     ссылке из письма и не должен каждый раз выбирать роль заново. Чужой
     cookie-контекст тут ни при чём — это локальная настройка браузера, а
     доступ всё равно проверяет сервер. */
  var ROLE_KEY = 'lex.role';

  function rememberRole(id) {
    try { global.localStorage.setItem(ROLE_KEY, id || ''); } catch (e) { /* private mode */ }
  }

  function storedRole() {
    try { return global.localStorage.getItem(ROLE_KEY) || ''; } catch (e) { return ''; }
  }

  /* Иконка из assets/ (CODEX): узкая обёртка, чтобы не тянуть разметку в каждый рендер. */
  function icon(name, cls) {
    var img = doc.createElement('img');
    img.src = 'assets/' + name + '.svg';
    img.alt = '';
    img.className = 'ico' + (cls ? ' ' + cls : '');
    img.width = 14;
    img.height = 14;
    return img;
  }

  function el(tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  /* Число с разделителем разрядов: 4821 -> «4 821». Токены читают глазами,
     и «4821» без пробела путается с четырёхзначным кодом. */
  function fmtNum(n) {
    var v = Math.round(Number(n) || 0);
    return String(v).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
  }

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

  function currentRole() {
    for (var i = 0; i < state.roles.length; i++) {
      if (state.roles[i].id === state.roleId) return state.roles[i];
    }
    return null;
  }

  /* Подпись роли с объёмом чтения: «Дирекция ИБ · 2 из 4».
     Без числа человек не видит границы доступа — выбирает роль и не понимает,
     почему агент не находит документ. Числа приходят с /api/roles из того же
     фильтра, что и чтение (§23 v1.16), поэтому обещать больше ядра UI не может. */
  function roleDocsLabel(role) {
    var title = role.title || role.id;
    if (role.documents_total == null) return title;
    return title + ' · ' + role.documents_available + ' из ' + role.documents_total;
  }

  var CONTENT_TYPE_LABEL = { text: 'текст', image: 'картинка', scan: 'скан', mixed: 'смешанный', case: 'кейс' };
  var REFUSAL_KIND_LABEL = {
    no_edition_in_force: 'нет действующей редакции',
    not_in_corpus: 'нет в базе знаний',
    out_of_scope: 'вне базы знаний',
    conflict_unresolved: 'конфликт не разрешён'
  };

  /* Промпт велит модели отвечать «ОТКАЗ: <причина>», но бэкенд этот маркер не
     разбирает и присылает его обычным текстом ответа — с цитатой, как будто это
     ответ. UI не имеет права выдавать отказ за ответ: распознаём маркер и
     рисуем отказ. Найдено на живом прогоне (вопрос про утечку учётных данных:
     пришло «ОТКАЗ: В фрагментах нет ответа…» в зелёной карточке ответа). */
  var REFUSAL_MARKER = /^\s*ОТКАЗ\s*(?:[::]|—|–|-)\s*/i;

  function refusalFromAnswer(d) {
    var text = String(d.short || d.text || '');
    var m = REFUSAL_MARKER.exec(text);
    if (!m) {
      /* Пустой ответ — не ответ. Барьер §12 усекает текст до покрытых фраз, и
         при неудаче приходит answer с пустым текстом; зелёная карточка без
         содержимого врёт сильнее, чем честный отказ. */
      if (!text.trim()) {
        return {
          kind: 'not_in_corpus',
          reason: 'Модель не дала ни одного утверждения, подтверждённого базой знаний',
          hint: 'Агент может передать вопрос в службу качества — это следующий шаг.'
        };
      }
      return null;
    }
    var reason = text.slice(m[0].length).replace(/\s*\[\d+\]/g, ' ').replace(/\s+/g, ' ').trim();
    return {
      kind: 'not_in_corpus',
      reason: reason || 'Модель не нашла ответа во фрагментах базы знаний',
      hint: 'Агент может передать вопрос в службу качества — это следующий шаг.'
    };
  }

  var TICKET_STATUS_LABEL = { open: 'открыт', assigned: 'в работе', answered: 'отвечен', closed: 'закрыт' };
  var TICKET_STATUS_TONE = { open: 'danger', assigned: 'amber', answered: 'ok', closed: 'muted' };

  /* §23.1: чтение — всем, запись — только эти две роли. */
  function canWrite() {
    return state.roleId === 'quality' || state.roleId === 'director';
  }

  /* §23: «доступно N из M» — те же числа, что у шага permissions. */
  function caseCountLabel(meta) {
    if (!meta || meta.total === undefined) return '';
    if (!meta.dropped) return 'Кейсы: доступны все ' + meta.total;
    var reason = '';
    for (var k in meta.by_reason) {
      if (Object.prototype.hasOwnProperty.call(meta.by_reason, k)) {
        reason = k + ': ' + meta.by_reason[k];
        break;
      }
    }
    return 'Кейсы: доступно ' + meta.available + ' из ' + meta.total
      + (reason ? ' · ' + reason : '');
  }

  /* ------------------------------------------------------------------ консоль */

  var consoleView = new global.ConsoleView($('consoleBody'), $('consoleMeta'), $('consoleStatus'));

  var progressView = new global.ProgressView({
    wrap: $('progress'),
    track: $('progressBar'),
    fill: $('progressFill'),
    count: $('progressCount'),
    now: $('progressNow'),
    detail: $('progressDetail'),
    dots: $('progressDots'),
    ibBar: $('ibProgress'),
    ibFill: $('ibProgressFill')
  });

  $('consoleClear').addEventListener('click', function () {
    consoleView.clear();
    consoleView.setStatus('ожидание запроса', '');
  });

  /* доп. №9: на узком экране консоль сворачивается в нижнюю панель. */
  var consoleToggle = $('consoleToggle');
  if (consoleToggle) {
    consoleToggle.addEventListener('click', function () {
      var aside = $('console');
      var collapsed = aside.classList.toggle('is-collapsed');
      consoleToggle.setAttribute('aria-expanded', String(!collapsed));
    });
  }

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
    $('badgeTone').textContent = state.tone === 'formal' ? 'формальный' : 'простой';
    $('badgeMode').textContent = state.mock ? 'мок' : 'живой';
    renderSettings();
  }

  function renderSettings() {
    var row = $('settingsRow');
    var hint = $('settingsHint');
    clear(row);
    var i, b;

    if (activeTab === 'role') {
      hint.innerHTML = 'Выберите роль — от неё зависит, <em>какие документы</em> агент вправе показать. Регламенты читаются только по отделам роли; запись — у службы качества и руководства.';
      for (i = 0; i < state.roles.length; i++) {
        var roleItem = state.roles[i];
        b = el('button', 'chip', roleDocsLabel(roleItem));
        b.type = 'button';
        b.setAttribute('role', 'radio');
        b.setAttribute('aria-checked', String(roleItem.id === state.roleId));
        if (roleItem.documents_total != null) {
          b.title = 'Доступно ' + roleItem.documents_available + ' из '
            + roleItem.documents_total + ' документов корпуса';
        }
        b.addEventListener('click', makeRolePicker(roleItem.id));
        row.appendChild(b);
      }
      /* §23.2: маленькая строка о модели прав — без неё новый выбор читается неверно. */
      var own = currentRole();
      var permText;
      if (own && own.documents_total != null) {
        permText = 'Чтение: ' + own.documents_available + ' из ' + own.documents_total
          + ' документов';
      } else {
        permText = 'Чтение: по отделам роли';
      }
      permText += canWrite() ? ' · Запись: разрешена' : ' · Запись: только служба качества и руководство';
      var perm = el('p', 'panelbar__perm', permText);
      perm.id = 'permLine';
      row.appendChild(perm);
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

    if (activeTab === 'tone') {
      hint.innerHTML = 'Тон меняет формулировку, но не смысл (§21): реквизиты — числа, сроки, суммы, коды — берутся из цитаты дословно. В оговорке и отказе тон всегда формальный.';
      var tones = [{ id: 'plain', t: 'простой (по умолчанию)' }, { id: 'formal', t: 'формальный' }];
      for (i = 0; i < tones.length; i++) {
        b = el('button', 'chip', tones[i].t);
        b.type = 'button';
        b.setAttribute('role', 'radio');
        b.setAttribute('aria-checked', String(state.tone === tones[i].id));
        b.addEventListener('click', (function (toneId) {
          return function () { state.tone = toneId; renderTabs(); };
        })(tones[i].id));
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
      rememberRole(id);
      // §23: кейсы отбираются по роли — кеш прошлой роли показывал бы чужие.
      state.caseCache = null;
      state.caseMeta = null;
      renderTabs();
      updateHint();
      renderRolesScreen();
      refreshTicketNav();
      openPendingTicket();
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
      hint.textContent = pendingTicket
        ? 'открыта ссылка на заявку из письма — выберите роль «Отдел качества», чтобы её увидеть'
        : 'выберите роль — права записи зависят от роли, чтение документов одинаково у всех';
      hint.className = 'composer__hint composer__hint--warn';
      return;
    }
    hint.className = 'composer__hint';
    hint.textContent = state.mock
      ? 'мок-режим: поток шагов эмулируется. Демо: «Какая ставка НДС в России?» — отказ вне базы.'
      : 'живой режим: ответ из /api/ask';
  }

  /* ------------------------------------------------------------------ навигация по экранам */

  var SCREEN_TITLE = {
    chat: ['Чат по документам', 'Ответы только из базы знаний организации.'],
    roles: ['Роли', 'Права доступа: роль определяет, какие отделы и направления видит агент.'],
    lines: ['Бизнес-направления', 'Регламенты привязаны к направлению, а не только к отделу.'],
    cases: ['Кейсы', 'Наблюдения из практики. Не отменяют регламент, но видны в поиске с меньшим приоритетом.'],
    tickets: ['Заявки в службу качества', 'Агент создаёт тикет сам, когда ответа в базе нет. Доступно службе качества и руководителю.'],
    stats: ['Аналитика', 'По журналу запросов: отказы, латентность, где не хватает документов.'],
    mail: ['Почта качества', 'Синтетический ящик отдела качества: сюда приходят письма агента, отсюда эксперт отвечает.']
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
    if (name === 'mail') renderMailScreen();
    $('feed').scrollTop = 0;
  }

  /* Ящик отдела качества. Адрес Mailpit приходит с сервера (/api/health ->
     mail_ui), а не зашит в клиент: на локальном стенде ящика может не быть,
     и тогда вместо пустого iframe показываем честное объяснение. */
  var mailLoaded = false;
  function renderMailScreen() {
    var frame = $('mailFrameSrc');
    var open = $('mailOpen');
    if (!frame) return;
    if (mailLoaded) return;
    mailLoaded = true;
    // Адрес спрашиваем у сервера: на локальном стенде почта симулирована,
    // и mail_ui пустой — тогда вкладка объясняет это, а не показывает пустоту.
    fetch('api/health').then(function (r) { return r.json(); }).then(function (h) {
      showMail((h && h.email && h.email.mail_ui) || '');
    }).catch(function () { showMail(''); });

    function showMail(url) {
      if (!url) {
        var box = $('mailFrame');
        if (box) {
          box.innerHTML = '<div class="mailframe__empty">'
            + '<p class="mailframe__empty-t">Почта качества не подключена на этом стенде</p>'
            + '<p class="mailframe__empty-x">Письма формируются, но не отправляются '
            + '(EMAIL_MODE=simulated). На демо-стенде ящик подключён — там видно весь цикл: '
            + 'вопрос → письмо → ответ эксперта → кейс в базе.</p></div>';
        }
        return;
      }
      frame.src = url;
      if (open) { open.href = url; open.removeAttribute('hidden'); }
    }
  }

  var navItems = doc.querySelectorAll('.nav__item');
  for (var ni = 0; ni < navItems.length; ni++) {
    navItems[ni].addEventListener('click', (function (item) {
      return function () { goScreen(item.getAttribute('data-screen')); };
    })(navItems[ni]));
  }
  $('navBrand').addEventListener('click', function (e) { e.preventDefault(); goScreen('chat'); });

  /* Ссылка из письма в отдел качества: #ticket=LEX-2026-0007.
     Сотрудник качества открывает письмо в почте, жмёт ссылку и должен
     попасть сразу в эту заявку. Роль к этому моменту может быть ещё не
     выбрана — тогда запоминаем тикет и открываем его сразу после выбора
     роли, иначе ссылка из письма ведёт в чат и заявку не найти. */
  var pendingTicket = '';

  function openFromHash() {
    var m = /(?:^|[#&])ticket=([^&]+)/.exec(global.location.hash || '');
    if (!m) return;
    var tid = decodeURIComponent(m[1]);
    if (!ticketsAllowed()) {
      pendingTicket = tid;
      goScreen('chat');
      updateHint();
      return;
    }
    pendingTicket = '';
    goScreen('tickets');
    renderTicketDetail(tid);
  }
  global.addEventListener('hashchange', openFromHash);

  function openPendingTicket() {
    if (!pendingTicket || !ticketsAllowed()) return;
    var tid = pendingTicket;
    pendingTicket = '';
    goScreen('tickets');
    renderTicketDetail(tid);
  }

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
    var hrow = el('div', 'section-head');
    hrow.appendChild(el('h2', 'section-title', 'Кейсы'));
    var addCase = el('button', 'btn', 'Добавить кейс');
    addCase.type = 'button';
    addCase.id = 'addCaseBtn';
    addCase.disabled = !canWrite();
    addCase.title = canWrite() ? 'Открыть форму нового кейса' : 'Доступно службе качества и руководству';
    addCase.setAttribute('aria-disabled', String(!canWrite()));
    hrow.appendChild(addCase);
    host.appendChild(hrow);
    host.appendChild(el('p', 'section-sub',
      'Кейсы пополняются ролями руководителей. В поиске они участвуют наравне с документами, но при конфликте регламент сильнее.'));
    if (!canWrite()) {
      host.appendChild(el('p', 'permnote',
        'Кейсы отбираются по роли: свои отделы и общая практика. '
        + 'Добавление — службе качества и руководству (§23). '
        + 'Кнопка неактивна намеренно: видно, что функция есть, но не для этой роли.'));
    }
    var cases = state.mock ? global.MockLex.cases() : (state.caseCache || []);
    if (state.mock) {
      host.appendChild(el('p', 'permnote', 'Кейсы: ' + cases.length + ' (мок-режим, без фильтра ролей)'));
    } else if (state.caseMeta) {
      host.appendChild(el('p', 'permnote', caseCountLabel(state.caseMeta)));
    }
    if (!state.mock && state.caseCache === null) {
      // §23: роль обязательна. Без неё сервер отвечает 400 — и правильно:
      // «покажи все» больше не дефолт. Пока роль не выбрана, кейсы не грузим.
      if (!state.roleId) {
        host.appendChild(el('p', 'permnote', 'Выберите роль — от неё зависит, какие кейсы агент вправе показать.'));
        return;
      }
      fetch('api/cases?role_id=' + encodeURIComponent(state.roleId))
        .then(function (r) { return r.json(); }).then(function (j) {
          state.caseCache = Array.isArray(j) ? j : (j.cases || []);
          state.caseMeta = (j && j.total !== undefined) ? j : null;
          renderCasesScreen();
        }).catch(function () {
          state.caseCache = [];
          state.caseMeta = null;
          renderCasesScreen();
        });
      return;
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
    // §22.3: входящие — это GET /api/inbox, а не общий список тикетов.
    return fetch('api/inbox').then(function (r) { return r.json(); })
      .then(function (j) { return Array.isArray(j.letters) ? j.letters : []; })
      .catch(function () { return []; });
  }

  function sortTickets(list) {
    var order = { open: 0, assigned: 1, answered: 2, closed: 3 };
    return list.slice().sort(function (a, b) {
      var ra = Object.prototype.hasOwnProperty.call(order, a.status) ? order[a.status] : 9;
      var rb = Object.prototype.hasOwnProperty.call(order, b.status) ? order[b.status] : 9;
      if (ra !== rb) return ra - rb;
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

  var MAIL_STATUS = {
    open: { label: 'новое', tone: 'danger' },
    assigned: { label: 'в работе', tone: 'amber' },
    answered: { label: 'отвечено', tone: 'ok' },
    closed: { label: 'закрыто', tone: 'muted' }
  };

  function renderTicketsScreen() {
    var host = $('screen-tickets');
    clear(host);
    host.appendChild(el('h2', 'section-title', 'Входящие службы качества'));
    host.appendChild(el('p', 'section-sub',
      'Когда ответа в базе нет, агент отправляет письмо в службу качества и создаёт заявку. '
      + 'Здесь эти письма лежат во входящих: откройте письмо, ответьте — агент предложит черновик кейса.'));
    host.appendChild(el('p', 'mail__demo',
      'демонстрационный режим — письма не отправляются по SMTP, адреса из RFC 2606 (example.invalid)'));

    if (!ticketsAllowed()) {
      host.appendChild(el('p', 'section-sub', 'Экран доступен ролям «Отдел качества» и «Руководитель».'));
      return;
    }

    var wrap = el('div', 'mail');
    host.appendChild(wrap);

    loadTickets().then(function (list) {
      clear(wrap);
      if (!list.length) {
        var empty = el('div', 'mail__empty');
        empty.appendChild(icon('illustration-empty', 'mail__emptyart'));
        empty.appendChild(el('p', 'section-sub', 'Входящие пусты: пока ни один вопрос не остался без ответа.'));
        wrap.appendChild(empty);
        return;
      }

      var bar = el('div', 'mail__bar');
      bar.appendChild(el('span', 'mail__count', 'Писем: ' + list.length));
      wrap.appendChild(bar);

      var table = el('table', 'wikitable mail__table');
      var head = el('tr');
      ['', 'Тема', 'От кого', 'Дата', 'Статус', 'Срок'].forEach(function (h) {
        head.appendChild(el('th', null, h));
      });
      table.appendChild(head);

      sortTickets(list).forEach(function (t) {
        var em = ticketEmail(t);
        var st = MAIL_STATUS[t.status] || { label: t.status, tone: 'muted' };
        var tr = el('tr', 'mail__row' + (t.status === 'open' ? ' mail__row--unread' : ''));
        tr.tabIndex = 0;
        tr.setAttribute('role', 'button');
        tr.setAttribute('aria-label', 'Открыть письмо ' + t.id + ': ' + em.subject);

        tr.appendChild(el('td', 'mail__env', '✉'));

        var tdSub = el('td');
        tdSub.appendChild(el('span', 'mail__subject', em.subject.replace('[LEX] ', '')));
        tdSub.appendChild(el('span', 'mail__id', t.id + (t.clarifications && t.clarifications.length
          ? ' · уточнений: ' + t.clarifications.length : '')));
        tr.appendChild(tdSub);

        tr.appendChild(el('td', 'mail__from', em.from));
        tr.appendChild(el('td', null, fmtDate(String(t.created_at).slice(0, 10))));

        var tdSt = el('td');
        tdSt.appendChild(el('span', 'tstatus tstatus--' + st.tone, st.label));
        tr.appendChild(tdSt);

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

    var back = el('button', 'linkish', '← Входящие');
    back.type = 'button';
    back.addEventListener('click', renderTicketsScreen);
    host.appendChild(back);

    if (!state.mock && !state.ticketDetailCache[id]) {
      host.appendChild(el('p', 'section-sub', 'Загружаем письмо…'));
      fetch('api/inbox/' + encodeURIComponent(id)).then(function (r) { return r.json(); })
        .then(function (letter) { state.ticketDetailCache[id] = letter; renderTicketDetail(id); })
        .catch(function () { host.appendChild(el('p', 'section-sub', 'Письмо не найдено.')); });
      return;
    }
    var t = state.mock ? global.MockLex.ticket(id) : state.ticketDetailCache[id];
    if (!t) { host.appendChild(el('p', 'section-sub', 'Письмо не найдено.')); return; }

    var em = ticketEmail(t);
    var st = MAIL_STATUS[t.status] || { label: t.status, tone: 'muted' };

    /* Тело письма — как в почтовом клиенте. */
    var sheet = el('section', 'mail__sheet');
    var headRow = el('div', 'mail__sheethead');
    var env = el('span', 'mail__envelope', '✉');
    env.setAttribute('aria-hidden', 'true');
    headRow.appendChild(env);
    headRow.appendChild(el('h2', 'mail__subjectbig', em.subject));
    sheet.appendChild(headRow);

    var dl = el('dl', 'lettercard__meta');
    [['От кого', em.from], ['Кому', em.to], ['Дата', fmtDate(String(t.created_at).slice(0, 10))],
      ['Номер', t.id]].forEach(function (pair) {
      dl.appendChild(el('dt', null, pair[0]));
      dl.appendChild(el('dd', null, pair[1]));
    });
    sheet.appendChild(dl);

    var stRow = el('div', 'mail__sheetstatus');
    stRow.appendChild(el('span', 'tstatus tstatus--' + st.tone, st.label));
    /* Строку про доставку рисуем по факту, а не всегда: письмо на стенде
       уходит по SMTP, и «демонстрационный режим» рядом с реально
       отправленным письмом — прямая ложь сотруднику. */
    var dlv = em.delivery || 'simulated';
    if (dlv === 'sent') {
      stRow.appendChild(el('span', 'mail__demo mail__demo--ok', 'письмо отправлено по SMTP'));
    } else if (dlv === 'failed') {
      stRow.appendChild(el('span', 'mail__demo mail__demo--fail', 'письмо не ушло — SMTP отказал'));
    } else {
      stRow.appendChild(el('span', 'mail__demo', 'демонстрационный режим — реальная отправка не выполнялась'));
    }
    sheet.appendChild(stRow);
    host.appendChild(sheet);

    host.appendChild(el('h3', 'detail-h', 'Вопрос сотрудника'));
    host.appendChild(el('p', null, t.question));
    if (t.asker) {
      host.appendChild(el('p', 'ticketcard__reason',
        'Роль: ' + roleTitle(t.asker.role_id) + ' · направление: ' + (t.asker.business_line || '—')));
    }

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

    if (t.searched) {
      host.appendChild(el('h3', 'detail-h', 'Что проверил агент'));
      var bits = [];
      if (t.searched.documents != null) bits.push('документов: ' + t.searched.documents);
      if (t.searched.cases != null) bits.push('кейсов: ' + t.searched.cases);
      if (t.searched.accessible != null) bits.push('доступно: ' + t.searched.accessible);
      if (t.searched.top && t.searched.top.length) bits.push('топ: ' + t.searched.top.join(', '));
      host.appendChild(el('p', null, bits.join(' · ')));
    }

    host.appendChild(el('h3', 'detail-h', 'Почему ответа не нашлось'));
    host.appendChild(el('p', 'ticketcard__reason', t.why_unresolved || '—'));

    var actions = el('div', 'ticket-actions');

    if (t.status === 'open') {
      var take = el('button', 'btn btn--primary', 'Взять в работу');
      take.type = 'button';
      take.addEventListener('click', function () {
        if (state.mock) { global.MockLex.assignTicket(id, 'И. Петрова'); renderTicketDetail(id); return; }
        fetch('api/tickets/' + encodeURIComponent(id) + '/assign', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ assignee: 'И. Петрова' })
        }).then(function () { delete state.ticketDetailCache[id]; renderTicketDetail(id); });
      });
      actions.appendChild(take);
    }
    host.appendChild(actions);

    if (t.status !== 'closed') {
      host.appendChild(el('h3', 'detail-h', 'Ответить'));
      var ta = el('textarea', 'ticket-answer');
      ta.rows = 4;
      ta.setAttribute('aria-label', 'Ответ службы качества');
      ta.placeholder = 'Ответ сотруднику: что делать по существу вопроса';
      if (t.quality_answer) ta.value = t.quality_answer;
      host.appendChild(ta);

      var row = el('div', 'ticket-actions');
      var submit = el('button', 'btn btn--primary', 'Отправить');
      submit.type = 'button';
      submit.addEventListener('click', function () {
        var text = ta.value.trim();
        if (!text) { ta.focus(); return; }
        if (state.mock) { global.MockLex.answerTicket(id); updateTicketCards(id); renderTicketDetail(id); return; }
        // §22.3: ответ через /api/inbox/{id}/reply; сервер сразу отдаёт черновик кейса.
        fetch('api/inbox/' + encodeURIComponent(id) + '/reply', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ body: text, resolved_by: 'quality' })
        }).then(function (r) { return r.json(); })
          .then(function (res) {
            state.ticketDraftCache[id] = res;
            delete state.ticketDetailCache[id];
            updateTicketCards(id);
            renderTicketDetail(id);
          });
      });
      row.appendChild(submit);

      var close = el('button', 'btn', 'Закрыть без ответа');
      close.type = 'button';
      close.addEventListener('click', function () {
        if (state.mock) { global.MockLex.closeTicket(id, ta.value.trim()); updateTicketCards(id); renderTicketDetail(id); return; }
        fetch('api/tickets/' + encodeURIComponent(id) + '/close', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ reason: ta.value.trim() })
        }).then(function () { delete state.ticketDetailCache[id]; updateTicketCards(id); renderTicketDetail(id); });
      });
      row.appendChild(close);
      host.appendChild(row);
    }

    if (t.status === 'closed' && t.close_reason) {
      host.appendChild(el('p', 'ticketcard__reason', 'Закрыто: ' + t.close_reason));
    }
    if (t.quality_answer) {
      host.appendChild(el('h3', 'detail-h', 'Отвечено'));
      host.appendChild(el('p', 'detail-quote', t.quality_answer));
    }

    var draft = state.mock ? global.MockLex.draft(id) : state.ticketDraftCache[id];
    if (!state.mock && !draft && (t.status === 'answered' || t.status === 'closed')) {
      var draftHost = host;
      fetch('api/tickets/' + encodeURIComponent(id) + '/draft').then(function (r) { return r.json(); })
        .then(function (j) { state.ticketDraftCache[id] = j; renderTicketDetail(id); })
        .catch(function () { draftHost.appendChild(el('p', 'section-sub', 'Черновик кейса пока недоступен.')); });
    }
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

  /* Экономика: всё считает /api/stats, UI только показывает его числа. */
  function renderEconomyBlock(host, tu) {
    host.appendChild(el('h3', 'detail-h', 'Экономика · расход токенов'));
    if (!tu || tu.empty) {
      host.appendChild(el('p', 'section-sub', (tu && tu.hint)
        ? tu.hint : 'Недостаточно записей с данными о токенах — пустое состояние, а не нули.'));
      return;
    }
    var t = el('table', 'wikitable');
    function row(label, value) {
      var tr = el('tr');
      tr.appendChild(el('td', null, label));
      tr.appendChild(el('td', 'num', value === null || value === undefined ? '— (пока не измерялось)' : String(value)));
      t.appendChild(tr);
    }
    var w = tu.window20 || {};
    row('Prompt-токенов · за всё время', tu.prompt_tokens);
    row('Completion-токенов · за всё время', tu.completion_tokens);
    row('Всего токенов · за всё время', tu.total_tokens);
    row('Среднее на запрос · за всё время', tu.avg_total_per_request);
    row('p95 на запрос · за всё время', tu.p95_total_per_request);
    row('Среднее на запрос · последние 20', w.avg_total_per_request);
    row('p95 на запрос · последние 20', w.p95_total_per_request);
    row('LLM-вызовов · за всё время', tu.llm_calls);
    row('Оценочная стоимость', tu.cost_estimate);
    host.appendChild(t);
  }

  /* «Здоровье системы» — что работает прямо сейчас, из GET /api/health. */
  function renderHealthBlock(host, h) {
    host.appendChild(el('h3', 'detail-h', 'Здоровье системы'));
    var llm = h.llm || {}, emb = h.embed || {}, rr = h.rerank || {}, corp = h.corpus || {};
    var indexed = corp.indexed_at || corp.rebuilt_at || corp.last_indexed || corp.index_built_at;
    var rows = [
      ['Слот модели', llm.slot || '—'],
      ['LLM', llm.reachable ? 'доступен' : 'недоступен'],
      ['Embed', (emb.model || '—') + ' · ' + (emb.reachable ? 'доступен' : 'недоступен')],
      ['Rerank', (rr.model || '—') + ' · ' + (rr.reachable ? 'доступен' : 'недоступен')],
      ['Корпус', (corp.documents != null ? corp.documents : '—') + ' документов / '
        + (corp.chunks != null ? corp.chunks : '—') + ' чанков'],
      ['Размер индекса', corp.index_bytes || corp.index_size || '— (пока не измерялось)'],
      ['Индекс пересчитан', indexed || '— (пока не измерялось)'],
      ['Последний запрос', (state.juryStats && state.juryStats.last_request_ts) || '— (пока не измерялось)']
    ];
    var t = el('table', 'wikitable');
    for (var i = 0; i < rows.length; i++) {
      var tr = el('tr');
      tr.appendChild(el('td', null, rows[i][0]));
      tr.appendChild(el('td', null, rows[i][1]));
      t.appendChild(tr);
    }
    host.appendChild(t);
  }

  /* Экран для жюри: три критерия + таймлайн — всё из GET /api/stats (реальные журналы). */
  function renderJuryStats(host, s) {
    if (!s || s.empty) {
      host.appendChild(el('p', 'section-sub', s && s.hint ? s.hint : 'Журнал пуст — данных для аналитики нет.'));
      return;
    }
    /* ноль и «не измерялось» — разные состояния */
    function na(v) { return v === null || v === undefined ? '— (пока не измерялось)' : String(v); }
    if (s.fallback) {
      var a = s.analytics || {};
      var ar = a.caveats && a.caveats.answer_rate;
      host.appendChild(el('p', 'section-sub',
        'Удалённый стенд: /api/stats недоступен — критерии 1–3 пока не измерялись. Реальные данные /api/analytics: '
        + 'всего ' + (a.total != null ? a.total : '—') + ' · отказов ' + (a.refusals != null ? a.refusals : '—')
        + (ar ? ' · ответы ' + ar.plain + '/' + ar.caveated + '/' + ar.refused : '')));
      return;
    }
    var c1 = s.criterion1 || {}, c2 = s.criterion2 || {}, c3 = s.criterion3 || {};

    host.appendChild(el('h3', 'detail-h', 'Критерий 1 · ответы, уточнения, эскалации'));
    var o = c1.outcomes || { counts: {}, shares: {} };
    var t1 = el('table', 'wikitable');
    var h1 = el('tr');
    ['Исход', 'Количество', 'Доля'].forEach(function (x) { h1.appendChild(el('th', null, x)); });
    t1.appendChild(h1);
    [['answer', 'ответ'], ['clarify', 'уточнение'], ['escalate', 'эскалация'], ['refusal', 'отказ']].forEach(function (pair) {
      var tr = el('tr');
      tr.appendChild(el('td', null, pair[1]));
      tr.appendChild(el('td', 'num', String((o.counts || {})[pair[0]] || 0)));
      tr.appendChild(el('td', 'num', Math.round(((o.shares || {})[pair[0]] || 0) * 100) + '%'));
      t1.appendChild(tr);
    });
    host.appendChild(t1);

    var rg = c1.relevance_gate || {};
    host.appendChild(el('p', 'section-sub',
      'Гейт релевантности остановил ответ: ' + na(rg.stops) + ' раз из ' + (rg.retrieve_steps || 0)
      + ' шагов retrieve (порог покрытия темы < ' + rg.subject_coverage_min + ' или косинус < ' + rg.top_cosine_min + ').'));
    var reasons = c1.top_escalation_reasons || [];
    if (reasons.length) {
      var t2 = el('table', 'wikitable');
      var h2 = el('tr');
      ['Причина эскалации (топ-5)', 'Раз'].forEach(function (x) { h2.appendChild(el('th', null, x)); });
      t2.appendChild(h2);
      reasons.forEach(function (r) {
        var tr = el('tr');
        tr.appendChild(el('td', null, r.reason));
        tr.appendChild(el('td', 'num', String(r.count)));
        t2.appendChild(tr);
      });
      host.appendChild(t2);
    }

    host.appendChild(el('h3', 'detail-h', 'Критерий 2 · диалог с уточнениями'));
    host.appendChild(el('p', 'section-sub',
      'Диалогов с уточнениями: ' + (c2.dialogues_with_clarify || 0)
      + ' · среднее число раундов: ' + (c2.avg_rounds || 0)));
    var params = c2.top_params || [];
    if (params.length) {
      host.appendChild(el('p', 'section-sub', 'Чаще спрашивают: '
        + params.slice(0, 5).map(function (p) { return p.key + ' — ' + p.count; }).join(' · ')));
    }

    host.appendChild(el('h3', 'detail-h', 'Критерий 3 · кейсы из заявок службы качества'));
    host.appendChild(el('p', 'section-sub',
      'Кейсов из заявок: ' + (c3.cases_from_tickets || 0)
      + ' · среднее время заявка→кейс: ' + na(c3.avg_ticket_to_case_hours) + ' ч'
      + ' · вопросов отвечено из кейсов: ' + na(c3.answered_from_cases)));

    var lat = s.latency || {};
    if (!lat.empty && lat.all_time && lat.recent20) {
      host.appendChild(el('p', 'section-sub', 'Скорость ответа · за всё время: avg '
        + lat.all_time.avg_ms + ' мс · p95 ' + lat.all_time.p95_ms + ' мс (' + lat.all_time.records
        + ' запросов); последние 20: avg ' + lat.recent20.avg_ms + ' мс · p95 '
        + lat.recent20.p95_ms + ' мс.'));
    }
    var days = s.timeline || [];
    host.appendChild(el('h3', 'detail-h', 'Ответы по дням'));
    if (!days.length) {
      host.appendChild(el('p', 'section-sub', 'Данных по дням нет.'));
      return;
    }
    var NS = 'http://www.w3.org/2000/svg';
    var svg = doc.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 300 60');
    svg.setAttribute('class', 'sparkline');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', 'Ответы по дням: ' + days.map(function (d) { return d.day + ' — ' + d.count; }).join('; '));
    var max = Math.max.apply(null, days.map(function (d) { return d.count; }).concat([1]));
    var points = days.map(function (d, i) {
      var x = days.length > 1 ? (i * 300 / (days.length - 1)) : 150;
      var y = 58 - (d.count / max) * 50;
      return x.toFixed(1) + ',' + y.toFixed(1);
    }).join(' ');
    var line = doc.createElementNS(NS, 'polyline');
    line.setAttribute('points', points);
    line.setAttribute('fill', 'none');
    line.setAttribute('stroke', 'var(--link)');
    line.setAttribute('stroke-width', '2');
    svg.appendChild(line);
    host.appendChild(svg);
  }

  function renderStatsScreen() {
    if (!state.mock && !state.stats) {
      fetch('api/analytics').then(function (r) { return r.json(); }).then(function (j) {
        state.stats = j;
        state.similar = Array.isArray(j.similar_questions) ? j.similar_questions : [];
        renderStatsScreen();
      }).catch(function () { /* экран останется с честными прочерками */ });
    }
    var host = $('screen-stats');
    clear(host);
    host.appendChild(el('h2', 'section-title', 'Аналитика'));
    host.appendChild(el('p', 'section-sub',
      'Считается по журналу запросов (контракт §2, /api/analytics). В мок-режиме данные демонстрационные.'));
    var refresh = el('button', 'btn btn--small', 'Обновить');
    refresh.type = 'button';
    refresh.addEventListener('click', function () {
      state.juryStats = null; state.health = null; state.stats = null;
      renderStatsScreen();
    });
    host.appendChild(refresh);
    if (!state.mock && !state.juryStats) {
      fetch('api/stats').then(function (r) {
        if (!r.ok) throw new Error('stats ' + r.status);
        return r.json();
      }).then(function (j) {
        state.juryStats = j;
        renderStatsScreen();
      }).catch(function () {
        /* старый стенд без /api/stats: честно показываем, что есть в /api/analytics */
        fetch('api/analytics').then(function (r) { return r.json(); }).then(function (a) {
          state.juryStats = { empty: false, fallback: true, analytics: a };
          renderStatsScreen();
        }).catch(function () {});
      });
    }
    if (!state.mock && !state.health) {
      fetch('api/health').then(function (r) { return r.json(); }).then(function (h) {
        state.health = h;
        renderStatsScreen();
      }).catch(function () {});
    }
    if (!state.mock && state.juryStats) {
      renderJuryStats(host, state.juryStats);
      renderEconomyBlock(host, state.juryStats.token_usage);
    }
    if (!state.mock && state.health) renderHealthBlock(host, state.health);
    var table = el('table', 'wikitable');
    var head = el('tr');
    ['Показатель', 'Значение'].forEach(function (h) { head.appendChild(el('th', null, h)); });
    table.appendChild(head);

    /* §20.5: answer_rate — самый честный показатель продукта, поэтому отдельно и крупно. */
    var stats = state.mock ? global.MockLex.stats() : (state.stats || null);
    if (stats && stats.caveats && stats.caveats.answer_rate) {
      var ar = stats.caveats.answer_rate;
      host.appendChild(el('h2', 'section-title', 'На сколько вопросов LEX отвечает'));
      host.appendChild(el('p', 'section-sub',
        'Полный ответ и ответ с оговоркой — это ответ. Отказ — единственный случай, когда сотрудник уходит искать сам.'));
      var rate = el('div', 'rate');
      var cells = [
        { n: ar.plain, lbl: 'отвечено полностью', cls: 'answered' },
        { n: ar.caveated, lbl: 'с оговоркой', cls: 'caveated' },
        { n: ar.refused, lbl: 'отказ', cls: 'refused' }
      ];
      for (var rc = 0; rc < cells.length; rc++) {
        var cell = el('div', 'rate__cell rate--' + cells[rc].cls);
        cell.appendChild(el('span', 'rate__num', String(cells[rc].n)));
        cell.appendChild(el('span', 'rate__lbl', cells[rc].lbl));
        rate.appendChild(cell);
      }
      host.appendChild(rate);
      host.appendChild(el('p', 'section-sub',
        'Оговорок: ' + stats.caveats.total + ' · по уровню: low ' + stats.caveats.by_level.low
        + ', medium ' + stats.caveats.by_level.medium + ', high ' + stats.caveats.by_level.high
        + ' · причины: ' + stats.caveats.by_reason.map(function (r) { return r.code + ' ' + r.count; }).join(', ')));
    }

    var rows = state.mock
      ? [['Всего запросов', '42'], ['Отказов', '7 (16.7%)'], ['Средняя латентность', '3.8 с'],
         ['p95 латентность', '9.1 с'], ['Чаще всего нет документа', 'лицензирование — 5']]
      : (function () {
        var s = state.stats || {};
        var miss = (s.missing_by_line || [])[0];
        var topic = ((s.tickets || {}).top_topics || [])[0];
        return [
          ['Всего запросов', s.total != null ? String(s.total) : '—'],
          ['Отказов', s.refusals != null
            ? String(s.refusals) + (s.refusal_rate != null ? ' (' + Math.round(s.refusal_rate * 100) + '%)' : '')
            : '—'],
          ['Средняя латентность', s.avg_latency_ms != null ? (s.avg_latency_ms / 1000).toFixed(1) + ' с' : '—'],
          ['p95 латентность', s.p95_latency_ms != null ? (s.p95_latency_ms / 1000).toFixed(1) + ' с' : '—'],
          ['Чаще всего нет документа', miss ? miss.line + ' — ' + miss.count : '—'],
          ['Тикетов', (s.tickets || {}).total != null ? String(s.tickets.total) : '—'],
          ['Тема тикета', topic ? topic.topic + ' — ' + topic.count : '—']
        ];
      })();
    for (var i = 0; i < rows.length; i++) {
      var tr = el('tr');
      tr.appendChild(el('td', null, rows[i][0]));
      tr.appendChild(el('td', 'num', rows[i][1]));
      table.appendChild(tr);
    }
    host.appendChild(table);
    host.appendChild(el('p', 'section-sub',
      'Отказы по направлениям — критерий «соответствие задаче партнёра»: видно, где регламентов не хватает.'));

    /* Доп. №7/№8: три исхода подряд — для скриншота нужно видеть разницу. */
    if (state.mock) {
      host.appendChild(el('h2', 'section-title', 'Три исхода на одном экране'));
      host.appendChild(el('p', 'section-sub',
        'Один и тот же интерфейс отвечает тремя способами. Отказ — не ошибка, а честный результат: '
        + 'сотрудник понимает, что база вопроса не покрывает, вместо правдоподобной выдумки.'));
      var demo = el('div', 'outcomes');
      var items = [
        { cls: 'ok', badge: 'ответ', q: 'Какой срок согласования заявки на закупку?',
          a: 'Пять рабочих дней с регистрации в СЭД — редакция 2 (в силе с 1 июня 2026).', meta: 'цитат 4 · без оговорки' },
        { cls: 'caveat', badge: 'ответ с оговоркой', q: 'Какой срок согласования заявки после 2027 года?',
          a: 'Сейчас пять рабочих дней. Ред. 3 вступает 2027-01-01 — срок изменится.', meta: 'цитат 2 · оговорка medium · сверьтесь с владельцем' },
        { cls: 'refusal', badge: 'отказ (нет в базе)', q: 'Какая ставка НДС в России?',
          a: 'В базе знаний ответа нет: проверено 27 документов, 4 кейса.', meta: 'цитат 0 · письмо в службу качества при нехватке' },
        { cls: 'scope', badge: 'вопрос вне базы', q: 'Что будет с курсом акций в 2027 году?',
          a: 'Вопрос вне предметной области: база покрывает закупки, лицензирование, качество, хранение, персонал.', meta: 'цитат 0 · тикет не создаётся' }
      ];
      items.forEach(function (it) {
        var card = el('div', 'outcome outcome--' + it.cls);
        card.appendChild(el('span', 'outcome__badge', it.badge));
        card.appendChild(el('p', 'outcome__q', it.q));
        card.appendChild(el('p', 'outcome__a', it.a));
        card.appendChild(el('p', 'outcome__meta', it.meta));
        demo.appendChild(card);
      });
      host.appendChild(demo);
    }

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
      // Сервер отдаёт каноническую формулировку в поле canonical, мок — в
      // question. Читаем оба: иначе колонка «Вопрос» пустая, и таблица
      // показывает одни числа — владелец это и увидел.
      var text = String(item.canonical || item.question || '');
      if (!text) continue;
      // Видимый текст короткий, полный вопрос — в title/tooltip; счётчик справа.
      var shortText = text.length > 72 ? text.slice(0, 72).replace(/\s+\S*$/, '') + '…' : text;
      var tr2 = el('tr');
      var tdq = el('td');
      var qlink = el('button', 'linkish', shortText);
      qlink.type = 'button';
      qlink.title = text;
      qlink.addEventListener('click', (function (qtext) {
        return function () {
          goScreen('chat');
          var box = $('question');
          box.value = qtext;
          box.focus();
          box.setSelectionRange(qtext.length, qtext.length);
        };
      })(text));
      tdq.appendChild(qlink);
      if ((item.refusal_rate || 0) >= 0.5) {
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
    var nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 200;
    if (!nearBottom && state.streaming) return; /* пользователь сам прокрутил вверх — не дёргаем */
    feed.scrollTop = feed.scrollHeight;
  }

  /* §10: ответ сотрудника на уточнение — в ленту, чтобы диалог читался. */
  function addClarifyAnswer(turnEl, text) {
    if (!turnEl) return;
    var line = el('p', 'asked__clarify', 'Уточнение · ' + text);
    turnEl.appendChild(line);
    scrollFeed();
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

  /* §20.4: оговорка под ответом. Спокойная, никогда не красная; ответ остаётся видимым. */
  function renderCaveat(host, cav) {
    var level = cav.level || 'low';
    var box = el('div', 'caveat caveat--' + level);
    box.setAttribute('role', 'note');
    var head = el('div', 'caveat__head');
    var icon = el('span', 'caveat__icon', '⚠');
    icon.setAttribute('aria-hidden', 'true');
    head.appendChild(icon);
    head.appendChild(el('span', null, level === 'high' ? 'Не полная уверенность' : 'Не полная уверенность'));
    if (level === 'low') head.appendChild(el('span', 'caveat__lvl', 'информационно'));
    else if (level === 'high') head.appendChild(el('span', 'caveat__lvl', 'лучше решать с человеком'));
    box.appendChild(head);

    if (cav.reasons && cav.reasons.length) {
      var ul = el('ul', 'caveat__reasons');
      for (var i = 0; i < cav.reasons.length; i++) ul.appendChild(el('li', null, cav.reasons[i].text));
      box.appendChild(ul);
    }
    if (cav.advice) box.appendChild(el('p', 'caveat__advice', cav.advice));
    host.appendChild(box);
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
      more.setAttribute('aria-controls', 'detail-' + turnId);
      var detailHost = el('div', 'answer-detail');
      detailHost.id = 'detail-' + turnId;
      detailHost.hidden = true;
      more.addEventListener('click', (function (btn, hostD) {
        return function () {
          var isOpen = btn.getAttribute('aria-expanded') === 'true';
          if (!isOpen) {
            if (!hostD.dataset.loaded) {
              loadDetails(hostD, data, turnId);
              hostD.dataset.loaded = '1';
            }
            hostD.hidden = false;
            /* разворот через кадр, чтобы transition отработал от нуля */
            global.requestAnimationFrame(function () { hostD.classList.add('is-open'); });
            btn.textContent = 'Свернуть';
            btn.setAttribute('aria-expanded', 'true');
          } else {
            hostD.classList.remove('is-open');
            btn.textContent = 'Подробнее';
            btn.setAttribute('aria-expanded', 'false');
            global.setTimeout(function () { if (!hostD.classList.contains('is-open')) hostD.hidden = true; }, 300);
          }
        };
      })(more, detailHost));
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

    /* §20.4: сначала ответ, потом оговорка — с задержкой 150 мс (доп. №9). */
    if (data.caveat) {
      var cavHost = el('div', 'caveat-host');
      host.appendChild(cavHost);
      var cav = data.caveat;
      global.setTimeout(function () { renderCaveat(cavHost, cav); }, 150);
    }

    /* §13.3: карточка формы, если ответ её несёт. */
    if (data.form_id) host.appendChild(renderFormCard(data.form_id));

    scrollFeed();
  }

  /* §13.1: развёрнутый ответ — разделы, таблицы, цитаты — прямо в ленте.
     Деталь принадлежит роли и сессии, которые получили ответ: сервер
     отказывает чужому запросу, поэтому роль и сессию передаём явно. */
  function loadDetails(hostD, data, turnId) {
    if (state.mock) { renderDetail(hostD, global.MockLex.expand(data.detail_id), data, turnId); return; }
    if (!data.detail_id || !state.roleId) { renderDetailFallback(hostD, data, turnId); return; }
    hostD.appendChild(el('p', 'detail-note', 'Загружаем развёрнутую версию…'));
    fetch('api/expand', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        detail_id: data.detail_id,
        section: 'all',
        role_id: state.roleId,
        session_id: state.sessionId || ''
      })
    }).then(function (r) {
      if (!r.ok) throw new Error('expand ' + r.status);
      return r.json();
    }).then(function (detail) {
      hostD.textContent = '';
      renderDetail(hostD, detail, data, turnId);
    }).catch(function () {
      hostD.textContent = '';
      renderDetailFallback(hostD, data, turnId);
    });
  }

  function renderDetailFallback(hostD, data, turnId) {
    // без /api/expand показываем полный текст, если он есть
    if (data.text && data.text !== data.short) {
      var lede = el('p');
      fillWithNotes(lede, data.text, turnId);
      hostD.appendChild(lede);
    }
  }

  function renderDetail(hostD, detail, data, turnId) {
    if (!detail) { renderDetailFallback(hostD, data, turnId); return; }

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
    h.appendChild(icon('icon-form'));
    h.appendChild(el('span', 'formcard__title', 'Есть форма: ' + (form ? form.title : formId)));
    card.appendChild(h);
    card.appendChild(el('p', 'formcard__hint', 'Бланк можно скачать и заполнить офлайн или заполнить здесь — агент сверит данные с регламентом.'));

    var actions = el('div', 'formcard__actions');
    var dl = el('button', 'btn');
    dl.appendChild(icon('icon-download'));
    dl.appendChild(doc.createTextNode('Скачать бланк'));
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
      ref.style.setProperty('--i', String(i));
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

      /* §13.2: у цитаты две ссылки — «Читать» и «Скачать». */
      if (c.view_url || c.download_url) {
        var links = el('div', 'ref__links');
        if (c.view_url) {
          var read = el('button', 'linkish');
          read.appendChild(icon('icon-expand'));
          read.appendChild(doc.createTextNode('Читать'));
          read.type = 'button';
          read.addEventListener('click', (function (cite) {
            return function () { openDocument(cite.doc_id, cite.view_url); };
          })(c));
          links.appendChild(read);
        }
        if (c.download_url) {
          var dlLink = el('a', 'linkish ref__dl');
        dlLink.appendChild(icon('icon-download'));
        dlLink.appendChild(doc.createTextNode('Скачать'));
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

    /* OUTCOME: refusal — человеку нужно знать, куда идти дальше, и номер заявки. */
    if (!outOfScope) {
      var next = el('p', 'refusal__next', 'Обратитесь в службу качества — она ответит по существу вопроса.');
      var tid = d.ticket || state.lastTicket;
      if (tid) next.appendChild(el('span', 'refusal__ticket', ' · заявка № ' + tid));
      box.appendChild(next);
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

  /* §22.2: карточка отправленного письма. Имитация честная: пометка про демо
     видна всегда и не мелким шрифтом — это защита от обвинения в обмане. */
  function ticketEmail(t) {
    if (t && t.email) return t.email;
    /* Живой escalate (§15.3) отдаёт только ticket/iterations/reason/searched/
       next/eta — ни вопроса, ни письма. Тема «Вопрос без ответа в базе: » без
       текста вопроса выглядит как потерянное письмо, поэтому берём последний
       заданный вопрос. */
    var q = String((t && (t.question || t.topic)) || state.lastQuestion || '');
    var subject = '[LEX] Вопрос без ответа в базе:' + (q ? ' ' + q.slice(0, 60) : ' без формулировки');
    return {
      to: 'quality@example.invalid',
      from: (t && t.asker && t.asker.role_id ? t.asker.role_id : (state.roleId || 'employee')) + '@example.invalid',
      subject: subject,
      sent_at: (t && t.created_at) || '2026-11-26T11:20:00Z',
      delivery: 'simulated'
    };
  }

  function renderTicketCard(host, data, ctx) {
    var status = data.status || 'open';
    var em = ticketEmail(data);

    var box = el('div', 'lettercard');
    box.setAttribute('role', 'article');

    var head = el('div', 'lettercard__head');
    var env = el('span', 'lettercard__env', '✉');
    env.setAttribute('aria-hidden', 'true');
    head.appendChild(env);
    head.appendChild(el('span', 'lettercard__title', 'Письмо отправлено в службу качества'));
    head.appendChild(el('span', 'ticketcard__badge ticketcard__badge--' + (TICKET_STATUS_TONE[status] || 'muted'),
      TICKET_STATUS_LABEL[status] || status));
    box.appendChild(head);

    /* OUTCOME: escalate — явно говорим, что заявка передана, и называем срок. */
    if (!data.status || status === 'open' || status === 'assigned') {
      box.appendChild(el('p', 'lettercard__lead',
        'Заявка передана в службу качества. Срок ответа — до 2 рабочих дней.'));
    }

    var meta = el('dl', 'lettercard__meta');
    /* Живой escalate не присылает ни question, ни email: поля собираются из
       того, что есть. Пустая строка «Тема» в карточке — не «нет данных», а
       потерянный вопрос, поэтому подставляем последний заданный. */
    var eta = data.eta || (data.sla_due ? 'до ' + fmtDate(String(data.sla_due).slice(0, 10)) : '');
    var rows = [
      ['Кому', em.to],
      ['Тема', em.subject],
      ['Номер', (data.ticket || '') + (data.sla_due ? ' · ответ до ' + fmtDate(String(data.sla_due).slice(0, 10)) : '')]
    ];
    if (eta) rows.push(['Срок ответа', eta]);
    for (var r = 0; r < rows.length; r++) {
      meta.appendChild(el('dt', null, rows[r][0]));
      var dd = el('dd', null, rows[r][1]);
      if (rows[r][0] === 'Номер' && data.ticket) {
        var copy = el('button', 'linkish lettercard__copy', 'Скопировать номер заявки');
        copy.type = 'button';
        copy.setAttribute('aria-label', 'Скопировать номер заявки ' + data.ticket);
        copy.addEventListener('click', (function (id, btn) {
          return function () {
            try {
              if (global.navigator && global.navigator.clipboard) global.navigator.clipboard.writeText(id);
            } catch (e) { /* буфер недоступен — не критично */ }
            btn.textContent = 'скопировано';
            global.setTimeout(function () { btn.textContent = 'скопировать'; }, 1400);
          };
        })(data.ticket, copy));
        dd.appendChild(copy);
      }
      meta.appendChild(dd);
    }
    box.appendChild(meta);

    box.appendChild(el('p', 'lettercard__demo',
      'демонстрационный режим — реальная отправка не выполнялась'));

    /* ESCALATE: что именно искал агент — по документу/топу видно, почему ответа нет. */
    if (data.searched) {
      var s = data.searched;
      var bits = [];
      if (s.documents != null) bits.push('документов: ' + s.documents);
      if (s.cases != null) bits.push('кейсов: ' + s.cases);
      if (s.business_lines && s.business_lines.length) bits.push('направлений: ' + s.business_lines.join(', '));
      if (s.top && s.top.length) bits.push('топ: ' + s.top.join(', '));
      if (bits.length) box.appendChild(el('p', 'ticketcard__reason', 'Что искали — ' + bits.join(' · ')));
    }

    if (status === 'open' || status === 'assigned') {
      if (data.reason) box.appendChild(el('p', 'ticketcard__reason', 'Почему: ' + data.reason));
      if (data.assignee) box.appendChild(el('p', 'ticketcard__line', 'Взял в работу: ' + data.assignee));
      /* §15.3: «что дальше» приходит в поле next — это обещание сотруднику,
         без него карточка оставляет человека в неведении. */
      if (data.next) box.appendChild(el('p', 'ticketcard__line', 'Что дальше: ' + data.next));
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

  /* §18.4/§22: когда служба качества ответила, карточка в чате сотрудника меняет состояние. */
  function updateTicketCards(id) {
    var host = state.ticketCards[id];
    if (!host) return;
    var tk = state.mock ? global.MockLex.ticket(id) : null;
    if (!tk || (tk.status !== 'answered' && tk.status !== 'closed')) return;
    clear(host);
    renderTicketCard(host, {
      ticket: tk.id, status: tk.status, sla_due: tk.sla_due, question: tk.question, email: tk.email
    }, {
      onGetAnswer: function () {
        clear(host);
        var ahost = el('div', 'answer-host');
        host.appendChild(ahost);
        renderAnswer(ahost, {
          short: tk.quality_answer || 'Служба качества ответила по существу вопроса.',
          has_details: false, citations: [], _turnId: 'ticket-' + tk.id
        });
      }
    });
    delete state.ticketCards[id];
  }

  function renderClarify(host, data, ctx) {    clear(host);
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
      consoleView.push({ kind: 'step', key: d.key, title: d.title, detail: d.detail, n: d.n, total: d.total, ms: d.ms, caveat_level: d.caveat_level });
      progressView.step(d.n, d.total, d.key, d.title, d.detail);
      if (d.key === 'verify') {
        var trimmed = /отброшено/.test(String(d.detail || ''));
        var cavLevel = d.caveat_level || (/ОГОВОРКА/.test(String(d.detail || '')) ? 'medium' : null);
        if (cavLevel) {
          consoleView.setStatus('самопроверка пройдена · ответ с оговоркой (' + cavLevel + ')', 'warn');
        } else {
          consoleView.setStatus(trimmed ? 'ответ усечён: часть утверждений без цитаты' : 'проверка пройдена: каждое утверждение с цитатой', trimmed ? 'warn' : 'ok');
        }
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
      /* §10 + доп. №8: полоса встаёт на месте, после ответа продолжится оттуда же. */
      progressView.pause('вопрос уже в ленте — ответьте, и агент продолжит с этого шага');
      state.paused = true;
      renderClarify(ctx.host, d, {
        round: state.clarifyRound,
        onClarifyAnswer: function (answers, skipped, round) {
          progressView.resume();
          state.paused = false;
          submitAsk(state.lastQuestion, state.lastAsOf, answers, skipped, true);
        }
      });
      return;
    }

    if (ev.type === 'answer') {
      if (ctx.thinkingBox) { ctx.thinkingBox.remove(); ctx.thinkingBox = null; }
      /* Отказ, пришедший текстом «ОТКАЗ: …», — не ответ. Рисуем его отказом:
         иначе человек читает «нет ответа» в оформлении ответа и верит ему. */
      var asRefusal = refusalFromAnswer(d);
      if (asRefusal) {
        renderRefusal(ctx.host, asRefusal);
        consoleView.setStatus('в базе знаний ответа нет: агент сам отказал', 'idle');
        ctx.outcome = 'refusal';
        progressView.finish('refusal', 'Ответа в базе нет');
        return;
      }
      d._turnId = ctx.turnId;
      renderAnswer(ctx.host, d);
      if (d.caveat) {
        consoleView.setStatus('ответ с оговоркой (' + (d.caveat.level || 'low') + ')', 'warn');
        ctx.caveated = true;
        progressView.finish('caveat', 'Готово, с оговоркой');
      } else {
        consoleView.setStatus('ответ готов', 'ok');
        ctx.outcome = 'ok';
        progressView.finish('ok', 'Готово');
      }
      return;
    }

    if (ev.type === 'refusal') {
      if (ctx.thinkingBox) { ctx.thinkingBox.remove(); ctx.thinkingBox = null; }
      renderRefusal(ctx.host, d);
      consoleView.setStatus(d.kind === 'out_of_scope' ? 'вопрос вне компетенции базы' : 'отказ: ' + (REFUSAL_KIND_LABEL[d.kind] || d.kind || 'нет ответа'), 'idle');
      if (d.kind === 'out_of_scope') { ctx.outcome = 'scope'; progressView.finish('scope', 'Вопрос вне базы знаний'); }
      else { ctx.outcome = 'refusal'; progressView.finish('refusal', 'Ответа в базе нет'); }
      return;
    }

    if (ev.type === 'escalate') {
      if (ctx.thinkingBox) { ctx.thinkingBox.remove(); ctx.thinkingBox = null; }
      renderTicketCard(ctx.host, {
        ticket: d.ticket, status: 'open', searched: d.searched, eta: d.eta, reason: d.reason,
        sla_due: d.sla_due, question: d.question, email: d.email, next: d.next
      }, {});
      state.lastTicket = d.ticket;
      consoleView.setStatus('заявка передана в службу качества: ' + d.ticket + ' · ответ до 2 рабочих дней', 'warn');
      ctx.outcome = 'refusal';
      progressView.finish('refusal', 'Передано в службу качества');
      /* §18.4/§22: карточка в чате меняет состояние, когда служба качества ответила. */
      state.ticketCards[d.ticket] = ctx.host;
      return;
    }

    if (ev.type === 'done') {
      /* Токены вх/вых — из usage провайдера, за весь запрос (все попытки LLM).
         Нет данных (мок, локальный путь) — строку не рисуем вовсе, чтобы не
         показывать «0» там, где счётчик просто не снимался. */
      var tok = d.tokens;
      var tokenText = '';
      if (tok && (tok.total || tok.prompt || tok.completion)) {
        tokenText = ' · токены ' + fmtNum(tok.prompt || 0) + ' вх + '
          + fmtNum(tok.completion || 0) + ' вых = ' + fmtNum(tok.total || 0);
        if (tok.calls > 1) tokenText += ' (' + tok.calls + ' выз.)';
      }
      consoleView.push({ kind: 'done', key: 'done', title: 'Готово', detail: 'путь: ' + (d.inference_path || '—') + ' · журнал: ' + (d.journal_id || '—') + tokenText, ms: d.elapsed_ms });
      /* После отказа «готово» вводит в заблуждение: отвечать было нечем. */
      if (ctx.outcome === 'error') {
        consoleView.setStatus('запрос прерван: сбой на стороне агента', 'bad');
      } else if (ctx.outcome === 'refusal' || ctx.outcome === 'scope') {
        consoleView.setStatus(ctx.outcome === 'scope'
          ? 'ответа нет: вопрос вне базы знаний'
          : 'ответа нет: в базе не нашлось', 'idle');
      } else {
        consoleView.setStatus('готово за ' + ((d.elapsed_ms || 0) / 1000).toFixed(1) + ' с', 'ok');
      }
      if (ctx.caveated) progressView.relabel('Готово, с оговоркой · ' + ((d.elapsed_ms || 0) / 1000).toFixed(1) + ' с');
      else if (ctx.outcome === 'ok') progressView.relabel('Готово · ' + ((d.elapsed_ms || 0) / 1000).toFixed(1) + ' с');
      return;
    }

    if (ev.type === 'error') {
      if (ctx.thinkingBox) { ctx.thinkingBox.remove(); ctx.thinkingBox = null; }
      consoleView.push({ kind: 'error', key: 'error', title: 'Ошибка', detail: (d.message || '') + (d.stage ? ' · стадия ' + d.stage : '') });
      consoleView.setStatus('ошибка: ' + (d.stage || 'сервер'), 'bad');
      /* Иначе полоса навсегда остаётся «живой» и врёт, будто агент ещё думает. */
      ctx.outcome = 'error';
      progressView.finish('error', 'Сбой на стадии ' + (d.stage || 'сервер'));
      var note = el('p', 'refusal__reason', 'Запрос не обработан: ' + (d.message || 'ошибка сервера') + '.');
      ctx.host.appendChild(note);
    }
  }

  /* ------------------------------------------------------------------ отправка */

  function submitAsk(question, asOf, answers, skipped, resume) {
    var ctxId = String(Date.now()) + '-' + String(Math.random()).slice(2, 7);
    if (!resume) {
      var turn = addQuestion(question, skipped ? '' : asOf);
      turn.host.dataset.turnId = ctxId;
      state.turn = turn;
      consoleView.clear();
      progressView.start();
    } else {
      /* §10: продолжение диалога — тот же ход ленты, полоса не начинается заново. */
      ctxId = state.turn ? state.turn.host.dataset.turnId : ctxId;
      if (answers) {
        var said = [];
        for (var k in answers) if (Object.prototype.hasOwnProperty.call(answers, k)) {
          said.push(k + ': ' + (answers[k] || 'пропущено'));
        }
        if (said.length) addClarifyAnswer(state.turn.el, said.join(' · '));
      }
    }
    var host = state.turn.host;

    var thinkingBox = showThinking(host);

    var ctx = {
      host: host,
      turnId: ctxId,
      thinkingBox: thinkingBox
    };

    consoleView.setStatus(resume ? 'продолжаю диалог: ' + question : 'запрос отправлен', 'live');
    $('send').disabled = true;

    var payload = {
      question: question,
      role_id: state.roleId,
      tone: state.tone
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
      var streamHandlers = {
        onStep: function (d) { applyEvent({ type: 'step', data: d }, ctx); },
        onClarify: function (d) { applyEvent({ type: 'clarify', data: d }, ctx); finish(); },
        onAnswer: function (d) { applyEvent({ type: 'answer', data: d }, ctx); },
        onRefusal: function (d) { applyEvent({ type: 'refusal', data: d }, ctx); },
        onEscalate: function (d) { applyEvent({ type: 'escalate', data: d }, ctx); finish(); },
        onDone: function (d) { applyEvent({ type: 'done', data: d }, ctx); finish(); }
      };
      if (resume) streamHandlers.resumeFrom = progressView.done;
      state.streaming = global.MockLex.stream(question, state.roleId, asOf, answers, state.tone, streamHandlers);
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

  /* доп. №9: поле растёт по содержимому до 6 строк — не прокручивать вслепую. */
  function autoGrow() {
    var box = $('question');
    box.style.height = 'auto';
    var line = 21;
    var max = line * 6 + 18;
    box.style.height = Math.min(box.scrollHeight, max) + 'px';
    box.style.overflowY = box.scrollHeight > max ? 'auto' : 'hidden';
  }
  $('question').addEventListener('input', autoGrow);

  /* доп. №9: Ctrl+K — фокус в поле вопроса; Esc — закрыть раскрытое или снять фокус. */
  doc.addEventListener('keydown', function (e) {
    if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault();
      goScreen('chat');
      $('question').focus();
      return;
    }
    if (e.key === 'Escape') {
      var modal = doc.querySelector('.modal');
      if (modal) { var closer = modal.querySelector('.modal__head .btn'); if (closer) closer.click(); return; }
      var openDetail = doc.querySelector('.answer-detail.is-open');
      if (openDetail && openDetail.parentNode) {
        var toggle = openDetail.parentNode.querySelector('.answer-actions .btn');
        if (toggle) { toggle.click(); return; }
      }
      if (doc.activeElement && doc.activeElement.blur) doc.activeElement.blur();
    }
  });

  /* доп. №9: кнопка «вниз», если пользователь сам прокрутил ленту вверх. */
  (function () {
    var feed = $('feed');
    var btn = el('button', 'feed__todown', '↓ к последнему');
    btn.type = 'button';
    btn.hidden = true;
    btn.setAttribute('aria-label', 'Прокрутить к последнему сообщению');
    btn.addEventListener('click', function () { scrollFeed(); });
    feed.parentNode.insertBefore(btn, feed.nextSibling);

    feed.addEventListener('scroll', function () {
      var gap = feed.scrollHeight - feed.scrollTop - feed.clientHeight;
      btn.hidden = gap < 120;
    }, { passive: true });
  })();

  /* ------------------------------------------------------------------ старт */

  loadReference().then(function () {
    // Роль из прошлой сессии — чтобы ссылка из письма открывала заявку сразу,
    // без повторного «выберите роль».
    var saved = storedRole();
    if (saved && state.roles.some(function (r) { return r.id === saved; })) {
      state.roleId = saved;
    }
    renderTabs();
    updateHint();
    renderRolesScreen();
    refreshTicketNav();
    $('navCount').textContent = state.mock ? 'демо-режим · без бэкенда' : 'живой режим';
    document.documentElement.setAttribute('data-mode', state.mock ? 'mock' : 'live');
    openFromHash();
  });
})(window);
