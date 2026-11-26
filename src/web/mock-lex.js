/* mock-lex.js — мок-режим (?mock=1): эмуляция SSE-потока LEX без бэкенда.
   Нужен для скриншотов и демо, когда FastAPI ещё не поднят.

   Формат ровно как в docs/API-CONTRACT.md §3, чтобы приёмник событий в app.js
   был один и тот же для мока и для живого /api/ask: восемь step, затем одно
   из answer/refusal, затем done.

   Демо на sage-pl идёт без сети, поэтому здесь ничего не запрашивается:
   все сценарии — локальные данные с таймерами. */

(function (global) {
  'use strict';

  var STEPS_FULL = [
    { key: 'accept', title: 'Запрос принят', detail: 'роль: юрист · дата отсчёта: 2026-06-15', ms: 12 },
    { key: 'decompose', title: 'Разбор вопроса', detail: '2 подвопроса: срок согласования; кто подписывает', ms: 240 },
    { key: 'retrieve', title: 'Поиск', detail: 'BM25 12 · вектор 10 → топ 5', ms: 210 },
    { key: 'permissions', title: 'Проверка прав', detail: 'доступно 5 из 12, отброшено 7', ms: 9 },
    { key: 'resolve', title: 'Действующая редакция', detail: '3 редакции, действует Р2 от 2026-06-01', ms: 15 },
    { key: 'conflict', title: 'Сопоставление', detail: 'конфликтов: 0', ms: 12 },
    { key: 'compose', title: 'Сборка ответа', detail: 'из 3 цитат', ms: 1180 },
    { key: 'verify', title: 'Самопроверка', detail: 'покрыто 4 из 4 утверждений', ms: 420 }
  ];

  var ANSWER = {
    text: 'Срок согласования заявки на закупку — пять рабочих дней с момента регистрации заявки в СЭД.\n\n'
      + 'Заявку визирует руководитель подразделения, затем согласует финансовый контролёр. Изменения договора '
      + 'согласуются в том же порядке, что и сам договор.',
    citations: [
      {
        doc_id: 'ТЗ-ЭКЗ-СР01-Р2',
        title: 'Правило согласования заявок на закупку',
        revision: 2,
        effective: '2026-06-01',
        quote: 'Срок согласования заявки: пяти рабочих дней с даты регистрации.'
      },
      {
        doc_id: 'ТЗ-ЭКЗ-0007-Р2',
        title: 'Регламент согласования договорных изменений',
        revision: 2,
        effective: '2026-04-11',
        quote: 'Порядок согласования договорных изменений совпадает с порядком согласования договора.'
      },
      {
        doc_id: 'ТЗ-ЭКЗ-0001-Р1',
        title: 'Регламент проверки качества входного контроля',
        revision: 1,
        effective: '2025-11-20',
        quote: 'Финансовый контролёр проверяет соответствие заявки бюджету подразделения.'
      }
    ],
    refused: false
  };

  var REFUSAL = {
    reason: 'На 2025-12-01 ни одна редакция правила про срок согласования (ТЗ-ЭКЗ-СР01) не вступила в силу:'
      + ' первая редакция действует с 2026-02-01. Ответить на эту дату нельзя — источника ещё не было.',
    sources: ['ТЗ-ЭКЗ-СР01-Р1'],
    kind: 'no_edition_in_force'
  };

  var REFUSAL_ACCESS = {
    reason: 'Документы по этому вопросу есть, но роли «ИТ-служба» они недоступны: они ведутся '
      + 'Юридическим отделом и Отделом качества. Права доступа не позволяют показать цитаты.',
    sources: [],
    kind: 'no_access'
  };

  function delay(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  /* Сколько шагов прошло до момента: детали шагов «догоняют» реальный отказ. */
  function stepsFor(scenario) {
    var steps = STEPS_FULL.map(function (s) { return Object.assign({}, s); });
    if (scenario === 'refusal-date') {
      steps[4].detail = '1 редакция, действует с 2026-02-01 — на дату отсчёта не действует';
      steps[5].detail = 'сравнивать нечего: действующей редакции нет';
      steps[6].detail = 'ответ не собран: нет действующего источника';
      steps[7].detail = 'проверка остановлена на отказе';
    } else if (scenario === 'refusal-access') {
      steps[3].detail = 'доступно 0 из 12, отброшено 12';
      steps[4].detail = 'редакция не проверялась: нет доступных документов';
      steps[6].detail = 'ответ не собран: нет доступных источников';
      steps[7].detail = 'проверка остановлена на отказе прав';
    }
    return steps;
  }

  function pickScenario(question, roleId, asOf) {
    if (asOf && asOf < '2026-02-01') return 'refusal-date';
    if (roleId === 'it' && /закупк|согласован|договор/i.test(question || '')) return 'refusal-access';
    return 'answer';
  }

  /**
   * stream(question, roleId, asOf, handlers) — эмуляция POST /api/ask.
   * handlers: onStep(step), onAnswer(data), onRefusal(data), onDone(data), onError(data)
   * Возвращает объект с abort(): прерывает поток.
   */
  function stream(question, roleId, asOf, handlers) {
    var aborted = false;
    var scenario = pickScenario(question, roleId, asOf);
    var steps = stepsFor(scenario);
    var t0 = (global.performance && performance.now) ? performance.now() : Date.now();

    function elapsed() {
      var now = (global.performance && performance.now) ? performance.now() : Date.now();
      return Math.round(now - t0);
    }

    function guard(fn) {
      return function () { if (!aborted) fn(); };
    }

    (async function run() {
      await delay(180);
      for (var i = 0; i < steps.length; i++) {
        if (aborted) return;
        var s = steps[i];
        // задержка шага: как в контракте, плюс небольшая дрожь
        await delay(Math.max(90, Math.min(s.ms, 900)));
        if (aborted) return;
        guard(handlers.onStep)({
          n: i + 1,
          total: steps.length,
          key: s.key,
          title: s.title,
          detail: s.detail,
          ms: s.ms,
          color: s.key
        });
      }
      if (aborted) return;
      await delay(220);
      if (aborted) return;

      if (scenario === 'refusal-date') {
        guard(handlers.onRefusal)(REFUSAL);
      } else if (scenario === 'refusal-access') {
        guard(handlers.onRefusal)(REFUSAL_ACCESS);
      } else {
        guard(handlers.onAnswer)(ANSWER);
      }

      await delay(120);
      if (aborted) return;
      guard(handlers.onDone)({
        elapsed_ms: elapsed(),
        inference_path: 'mock',
        journal_id: 'q-mock-' + String(1000 + Math.floor(elapsed() % 9000))
      });
    })();

    return {
      abort: function () { aborted = true; }
    };
  }

  global.MockLex = {
    stream: stream,
    scenarios: ['answer', 'refusal-date', 'refusal-access']
  };
})(window);
