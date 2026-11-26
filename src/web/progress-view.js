/* progress-view.js — наглядный прогресс размышлений агента (дополнение №8).

   Не спиннер: полоса растёт по мере шагов SSE, под ней — точки шагов и живая
   подпись «что агент делает прямо сейчас». На уточнении останавливается на
   текущем месте и продолжается оттуда же после ответа сотрудника — это
   доказывает, что идёт диалог, а не перезапуск.

   Классический скрипт: UI открывается по file://, ES-модули там не грузятся. */

(function (global) {
  'use strict';

  /* Шагов 11 (§3: accept → policy → decompose → inspect → retrieve → cases →
     permissions → resolve → conflict → compose → verify). Значение по умолчанию,
     реальное берётся из total каждого шага SSE — mock и живой бэкенд совпадают. */
  var TOTAL = 11;
  var MIN_STEP_MS = 90;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function ProgressView(nodes) {
    this.wrap = nodes.wrap;
    this.track = nodes.track;
    this.fill = nodes.fill;
    this.count = nodes.count;
    this.now = nodes.now;
    this.detail = nodes.detail;
    this.dots = nodes.dots;
    this.ibFill = nodes.ibFill;
    this.ibBar = nodes.ibBar;

    this.done = 0;
    this.total = TOTAL;
    this.stepKeys = [];
    this.lastAt = 0;
    this.state = 'idle';
    this._hideTimer = null;
    this._stillTimer = null;
    this._buildDots(TOTAL);
  }

  ProgressView.prototype._buildDots = function (total) {
    this.dots.textContent = '';
    this.total = total;
    this.dots.style.setProperty('--dots', String(total));
    for (var i = 0; i < total; i++) {
      var li = el('li', 'pdot');
      this.dots.appendChild(li);
    }
  };

  /* Начало прогона: полоса на нуле, снова видна. */
  ProgressView.prototype.start = function (total) {
    this._clearHide();
    if (total && total !== this.total) this._buildDots(total);
    this.done = 0;
    this.lastAt = 0;
    this.state = 'live';
    this.wrap.hidden = false;
    this.ibBar.hidden = false;
    this.wrap.className = 'progress progress--live';
    if (this.ibBar) this.ibBar.className = 'ibprog ibprog--live';
    this.now.textContent = 'Принимаю запрос…';
    this.detail.textContent = '';
    this.count.textContent = '0 из ' + this.total;
    this._paint(0);
    this._startStill();
  };

  /* §3/§17: шаг пришёл. Прогресс растёт; шаг не моложе 90 мс, чтобы видео читалось. */
  ProgressView.prototype.step = function (n, total, key, title, detail) {
    if (this.state === 'idle' || this.state === 'finished') this.start(total);

    if (total && total !== this.total) this._buildDots(total);
    this.done = Math.min(this.total, Math.max(this.done, n || 0));
    this.stepKeys = this.stepKeys.slice(0, Math.max(0, this.done - 1));
    this.stepKeys[this.done - 1] = key || '';

    this.now.textContent = (title || '') + (title ? '…' : '');
    this.detail.textContent = detail || '';
    this.count.textContent = this.done + ' из ' + this.total;
    this._paint(this.done);

    this.lastAt = nowMs();
    this._idlePulse(key);
    if (key === 'verify' || key === 'escalate') this._stopStill();
  };

  /* §10: диалог на паузе. Полоса стоит на месте, подпись — «Нужно уточнение». */
  ProgressView.prototype.pause = function (text) {
    if (this.state === 'idle') return;
    this.state = 'paused';
    this.wrap.className = 'progress progress--paused';
    this._stopStill();
    this.now.textContent = 'Нужно уточнение';
    this.detail.textContent = text || '';
    this.count.textContent = this.done + ' из ' + this.total;
  };

  ProgressView.prototype.resume = function () {
    if (this.state !== 'paused') return;
    this.state = 'live';
    this.wrap.className = 'progress progress--live';
    this.now.textContent = 'Продолжаю с того же места…';
    this.lastAt = nowMs();
    this._startStill();
  };

  /* Исход: ok (зелёная) | caveat (янтарная) | refusal (серая) | scope (серая). */
  ProgressView.prototype.finish = function (kind, label) {
    if (this.state === 'idle') return;
    this._clearHide();
    this._stopStill();
    this.state = 'finished';

    var tone = kind === 'ok' ? 'ok'
      : kind === 'caveat' ? 'caveat'
        : kind === 'error' ? 'error'
          : kind === 'refusal' ? 'refusal' : 'scope';
    this.wrap.className = 'progress progress--done progress--' + tone;
    if (this.ibBar) this.ibBar.className = 'ibprog ibprog--done ibprog--' + tone;

    /* Отказ и «вне базы» прогресс не догоняют до конца: это честное «дошли и встали». */
    if (kind === 'ok' || kind === 'caveat') {
      this.done = this.total;
      this._paint(this.total);
      this.count.textContent = this.total + ' из ' + this.total;
    } else {
      this._paint(this.done);
    }

    this.now.textContent = label || '';
    this.detail.textContent = '';
    var self = this;
    this._hideTimer = global.setTimeout(function () { self.wrap.hidden = true; }, 800);
  };

  /* Уточнить подпись после done: «Готово» -> «Готово · 3.4 с», не перерисовывая полосу. */
  ProgressView.prototype.relabel = function (text) {
    if (this.state !== 'finished' || this.wrap.hidden) return;
    this._clearHide();
    this.now.textContent = text;
    var self = this;
    this._hideTimer = global.setTimeout(function () { self.wrap.hidden = true; }, 800);
  };

  ProgressView.prototype.reset = function () {    this._clearHide();
    this._stopStill();
    this.done = 0;
    this.lastAt = 0;
    this.state = 'idle';
    this.wrap.hidden = true;
    if (this.ibBar) this.ibBar.hidden = true;
    this.wrap.className = 'progress';
  };

  ProgressView.prototype._paint = function (done) {
    var pct = this.total ? Math.round((done / this.total) * 100) : 0;
    this.fill.style.width = pct + '%';
    if (this.ibFill) this.ibFill.style.width = pct + '%';
    this.track.setAttribute('aria-valuenow', String(pct));

    var dots = this.dots.children;
    for (var i = 0; i < dots.length; i++) {
      var c = 'pdot';
      if (i < done - 1) c += ' pdot--past';
      else if (i === done - 1) c += ' pdot--now';
      dots[i].className = c;
    }
  };

  /* Пульсирующий сегмент текущего шага: класс «свежий» гаснет через 800 мс. */
  ProgressView.prototype._idlePulse = function (key) {
    var self = this;
    this.wrap.setAttribute('data-step', key || '');
    this._stopStill();
    this._stillTimer = global.setTimeout(function () {
      if (self.state === 'live' && self.wrap.getAttribute('data-step') === (key || '')) {
        self.wrap.setAttribute('data-step', '');
      }
    }, MIN_STEP_MS + 400);
  };

  ProgressView.prototype._startStill = function () {
    this.wrap.classList.add('progress--live');
  };

  ProgressView.prototype._stopStill = function () {};

  ProgressView.prototype._clearHide = function () {
    if (this._hideTimer) { global.clearTimeout(this._hideTimer); this._hideTimer = null; }
  };

  function nowMs() {
    return (global.performance && global.performance.now) ? global.performance.now() : Date.now();
  }

  global.ProgressView = ProgressView;
})(window);
