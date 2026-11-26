/* console-view.js — Console Lex: шаги агента в виде строк инфобокса.
   Правый столбец по контракту: 3/4 ширины чат, 1/3 консоль.
   Строка = «ключ | значение», цвета шагов приглушены (см. lex.css).

   Виртуализация окном: в DOM живут только строки, попавшие в видимую область
   плюс запас; высота держится двумя распорками. Рассчитано на сотни шагов.

   Классический скрипт, не ES-модуль: UI открывается как статика по file://,
   а модули в Chrome с file:// не грузятся из-за CORS. */

(function (global) {
  'use strict';

  var ROW_H = 42;
  var OVERSCAN = 6;

  /* Порядок и цвета — docs/API-CONTRACT.md §3, §8, §9, §10, §12.5.
     color в событии не используется: цвета мапит UI сам, как требует §3. */
  var STEP_META = {
    accept: { title: 'Запрос принят' },
    decompose: { title: 'Разбор вопроса' },
    clarify: { title: 'Уточнение' },
    inspect: { title: 'Разбор источников' },
    retrieve: { title: 'Поиск' },
    cases: { title: 'Поиск по кейсам' },
    permissions: { title: 'Проверка прав' },
    resolve: { title: 'Действующая редакция' },
    conflict: { title: 'Сопоставление' },
    compose: { title: 'Сборка ответа' },
    verify: { title: 'Самопроверка' }
  };

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function rowNode(step, isLast) {
    var kind = step.kind || 'step';
    var key = kind === 'step' ? (step.key || 'accept') : kind;
    var meta = STEP_META[key] || {};
    var cls = 'step step--' + key;
    if (!isLast) cls += ' step--old';

    var row = el('div', cls);
    row.setAttribute('role', 'listitem');

    var n = kind === 'step' && step.n && step.total
      ? '[' + step.n + '/' + step.total + ']'
      : kind === 'error' ? '[!]' : kind === 'done' ? '[✓]' : '[·]';
    row.appendChild(el('span', 'step__n', n));

    var main = el('div', 'step__main');
    var head = el('div', 'step__key');
    head.appendChild(el('span', null, step.title || meta.title || key));
    if (step.ms != null) head.appendChild(el('span', 'step__ms', step.ms + ' мс'));
    main.appendChild(head);

    if (step.detail) main.appendChild(el('div', 'step__val', step.detail));
    row.appendChild(main);
    return row;
  }

  function ConsoleView(body, metaNode, statusNode) {
    this.body = body;
    this.metaNode = metaNode;
    this.statusNode = statusNode;
    this.steps = [];
    this.paramsNode = null;

    this.vlist = el('div', 'vlist');
    this.padTop = el('div', 'vlist__pad');
    this.windowNode = el('div', 'vlist__window');
    this.padBottom = el('div', 'vlist__pad');
    this.vlist.appendChild(this.padTop);
    this.vlist.appendChild(this.windowNode);
    this.vlist.appendChild(this.padBottom);
    this.body.appendChild(this.vlist);

    var self = this;
    this._onScroll = function () { self.renderWindow(); };
    this.body.addEventListener('scroll', this._onScroll, { passive: true });
    global.addEventListener('resize', this._onScroll);

    this.renderEmpty(true);
  }

  ConsoleView.prototype.clear = function () {
    this.steps = [];
    this.windowNode.textContent = '';
    this.setParams(null);
    this.renderEmpty(true);
    this.updateMeta();
  };

  ConsoleView.prototype.renderEmpty = function (show) {
    var existing = this.body.querySelector('.infobox__empty');
    if (show) {
      if (!existing) {
        var note = el('p', 'infobox__empty',
          'Здесь появятся шаги агента: разбор вопроса, разбор источников, поиск, права, редакция, сборка, самопроверка.');
        this.body.insertBefore(note, this.vlist);
      }
      this.vlist.hidden = true;
    } else {
      if (existing) existing.remove();
      this.vlist.hidden = false;
    }
  };

  ConsoleView.prototype.push = function (step) {
    this.renderEmpty(false);
    this.steps.push(step);
    this.renderWindow();
    this.updateMeta();
    var nearBottom = this.body.scrollTop + this.body.clientHeight >= this.body.scrollHeight - ROW_H * 2;
    if (nearBottom) this.body.scrollTop = this.body.scrollHeight;
  };

  /* Окно: только строки в области просмотра ± OVERSCAN. */
  ConsoleView.prototype.renderWindow = function () {
    var total = this.steps.length;
    if (!total) return;
    var top = this.body.scrollTop;
    var view = this.body.clientHeight || 400;
    var first = Math.max(0, Math.floor(top / ROW_H) - OVERSCAN);
    var last = Math.min(total, Math.ceil((top + view) / ROW_H) + OVERSCAN);

    this.padTop.style.height = (first * ROW_H) + 'px';
    this.padBottom.style.height = ((total - last) * ROW_H) + 'px';

    this.windowNode.textContent = '';
    for (var i = first; i < last; i++) {
      this.windowNode.appendChild(rowNode(this.steps[i], i === total - 1));
    }
  };

  ConsoleView.prototype.updateMeta = function () {
    if (!this.metaNode) return;
    var n = this.steps.filter(function (s) { return (s.kind || 'step') === 'step'; }).length;
    var ms = 0;
    for (var i = 0; i < this.steps.length; i++) {
      if (this.steps[i].ms) ms = Math.max(ms, Number(this.steps[i].ms) || 0);
    }
    this.metaNode.textContent = ms ? n + ' шагов · ' + (ms / 1000).toFixed(1) + ' с' : n + ' шагов';
  };

  /* §10: какие параметры известны, какие запрошены, какие пропущены с умолчанием. */
  ConsoleView.prototype.setParams = function (params) {
    if (this.paramsNode) { this.paramsNode.remove(); this.paramsNode = null; }
    if (!params || !params.length) return;

    var box = el('div', 'params');
    box.appendChild(el('div', 'params__title', 'Параметры диалога'));
    for (var i = 0; i < params.length; i++) {
      var p = params[i];
      var row = el('div', 'params__row');
      row.appendChild(el('span', 'params__k', p.key));
      var v = el('span', 'params__v params__v--' + p.state,
        p.state === 'known' ? 'известно: ' + p.value
          : p.state === 'asked' ? 'запрошено'
            : 'умолчание: ' + p.value);
      row.appendChild(v);
      box.appendChild(row);
    }
    this.body.insertBefore(box, this.vlist);
    this.paramsNode = box;
  };

  ConsoleView.prototype.setStatus = function (text, tone) {
    if (!this.statusNode) return;
    this.statusNode.textContent = text;
    this.statusNode.className = tone ? 'console-status--' + tone : '';
  };

  ConsoleView.prototype.destroy = function () {
    this.body.removeEventListener('scroll', this._onScroll);
    global.removeEventListener('resize', this._onScroll);
  };

  global.ConsoleView = ConsoleView;
  global.LEX_STEP_META = STEP_META;
})(window);
