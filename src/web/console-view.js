/* console-view.js — Console Lex: виртуализированный список шагов агента.
   Шаги приходят из SSE (event: step) и рисуются человеческим языком, не JSON.
   Виртуализация окном: в DOM живут только строки, попавшие в видимую область
   плюс запас; высота держится двумя распорками. Это рассчитано на сотни шагов
   без просадки прокрутки.

   Классический скрипт (не ES-модуль): UI открывается как статика по file://,
   а модули в Chrome с file:// не грузятся из-за CORS. */

(function (global) {
  'use strict';

  var ROW_H = 44;        // базовая высота строки, px
  var OVERSCAN = 6;      // строк сверху и снизу за пределами окна
  var STEP_COLORS = {
    accept: 'slate',
    decompose: 'violet',
    retrieve: 'cyan',
    permissions: 'amber',
    resolve: 'blue',
    conflict: 'rose',
    compose: 'emerald',
    verify: 'green'
  };

  function colorOf(key) {
    return STEP_COLORS[key] || 'slate';
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  /* Строка шага. kind: 'step' | 'error' | 'done'. */
  function rowNode(step, index, total, isLast) {
    var kind = step.kind || 'step';
    var cls = 'step step--' + (kind === 'error' ? 'error' : kind === 'done' ? 'done' : colorOf(step.key));
    if (!isLast) cls += ' step--old';

    var row = el('div', cls);
    row.dataset.key = step.key || kind;
    row.setAttribute('role', 'listitem');

    var num = kind === 'step' && step.n && step.total
      ? '[' + step.n + '/' + step.total + ']'
      : kind === 'error' ? '[!]' : '[✓]';
    row.appendChild(el('span', 'step__n', num));

    var content = el('div', 'step__content');
    content.appendChild(el('div', 'step__title', step.title || step.key || ''));
    row.appendChild(content);

    row.appendChild(el('span', 'step__ms', step.ms != null ? step.ms + ' мс' : ''));

    if (step.detail) {
      var d = el('div', 'step__detail', step.detail);
      d.style.gridColumn = '2 / -1';
      row.appendChild(d);
    }
    return row;
  }

  function ConsoleView(body, metaNode, statusNode) {
    this.body = body;
    this.metaNode = metaNode;
    this.statusNode = statusNode;
    this.steps = [];
    this.vlist = el('div', 'vlist');
    this.padTop = el('div', 'vlist__pad');
    this.padBottom = el('div', 'vlist__pad');
    this.windowNode = el('div', 'vlist__window');
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
    this.renderEmpty(true);
    this.updateMeta();
  };

  ConsoleView.prototype.renderEmpty = function (show) {
    var existing = this.body.querySelector('.console-empty');
    if (show) {
      if (!existing) {
        var note = el('div', 'console-empty', 'Здесь появятся шаги агента: разбор, поиск, права, редакция, сборка, самопроверка.');
        this.body.insertBefore(note, this.vlist);
      }
      this.vlist.style.display = 'none';
    } else {
      if (existing) existing.remove();
      this.vlist.style.display = '';
    }
  };

  ConsoleView.prototype.push = function (step) {
    this.renderEmpty(false);
    this.steps.push(step);
    this.renderWindow();
    this.updateMeta();
    // автоскролл к новому шагу, если пользователь не отлистал вверх вручную
    var nearBottom = this.body.scrollTop + this.body.clientHeight >= this.body.scrollHeight - ROW_H * 2;
    if (nearBottom) {
      this.body.scrollTop = this.body.scrollHeight;
    }
  };

  /* Перерисовка видимого окна: только строки в области просмотра ± OVERSCAN.
     Окно намеренно пересобирается целиком — при 8-30 строках это дешевле
     учёта отдельных узлов и не даёт рассинхрона индексов. */
  ConsoleView.prototype.renderWindow = function () {
    var total = this.steps.length;
    if (!total) return;

    var top = this.body.scrollTop;
    var view = this.body.clientHeight || 400;
    var first = Math.max(0, Math.floor(top / ROW_H) - OVERSCAN);
    var last = Math.min(total, Math.ceil((top + view) / ROW_H) + OVERSCAN);

    // Высота списка держится распорками, чтобы скроллбар был честным.
    this.padTop.style.height = (first * ROW_H) + 'px';
    this.padBottom.style.height = ((total - last) * ROW_H) + 'px';

    this.windowNode.textContent = '';
    for (var i = first; i < last; i++) {
      this.windowNode.appendChild(rowNode(this.steps[i], i, total, i === total - 1));
    }
  };

  ConsoleView.prototype.updateMeta = function () {
    var steps = this.steps.filter(function (s) { return (s.kind || 'step') === 'step'; });
    var ms = 0;
    for (var i = 0; i < this.steps.length; i++) {
      if (this.steps[i].ms) ms = Math.max(ms, Number(this.steps[i].ms) || 0);
    }
    if (this.metaNode) {
      this.metaNode.textContent = steps.length + ' шагов · ' + (ms / 1000).toFixed(1) + ' с';
    }
  };

  ConsoleView.prototype.setStatus = function (text, tone) {
    if (!this.statusNode) return;
    this.statusNode.textContent = text;
    this.statusNode.className = 'console-status' + (tone ? ' console-status--' + tone : '');
  };

  ConsoleView.prototype.destroy = function () {
    this.body.removeEventListener('scroll', this._onScroll);
    global.removeEventListener('resize', this._onScroll);
  };

  global.ConsoleView = ConsoleView;
  global.LEX_STEP_COLORS = STEP_COLORS;
})(window);
