/* mock-lex.js — мок-режим (?mock=1): эмуляция SSE-потока LEX без бэкенда.
   Нужен для скриншотов и демо, когда FastAPI ещё не поднят.

   Формат ровно как в docs/API-CONTRACT.md (§3 + v1.1–v1.4), чтобы приёмник
   событий в app.js был один и тот же для мока и для живого /api/ask:
   девять step (accept → decompose → inspect → retrieve → cases → permissions →
   resolve → conflict → compose → verify), затем одно из answer/refusal/clarify.

   Демо на sage-pl идёт без сети, поэтому здесь ничего не запрашивается:
   все сценарии — локальные данные с таймерами. */

(function (global) {
  'use strict';

  var ANSWER_STEPS = [
    { key: 'accept', title: 'Запрос принят', detail: 'роль: юрист · направление: закупки · дата отсчёта: 2026-06-15', ms: 11 },
    { key: 'decompose', title: 'Разбор вопроса', detail: 'направление: закупки · 2 подвопроса: срок согласования; кто визирует', ms: 230 },
    { key: 'inspect', title: 'Разбор источников', detail: 'текст 24 · картинки 2 · сканы 1 (1 с плохим распознаванием) · направлений 1', ms: 34 },
    { key: 'retrieve', title: 'Поиск', detail: 'BM25 12 · вектор 10 → топ 5 · документов 5, кейсов 0', ms: 208 },
    { key: 'cases', title: 'Поиск по кейсам', detail: 'найдено 4 кейса, из них по теме 1', ms: 41 },
    { key: 'permissions', title: 'Проверка прав', detail: 'доступно 5 из 12, отброшено 7', ms: 9 },
    { key: 'resolve', title: 'Действующая редакция', detail: '3 редакции, действует Р2 от 2026-06-01', ms: 14 },
    { key: 'conflict', title: 'Сопоставление', detail: 'конфликтов: 0', ms: 11 },
    { key: 'compose', title: 'Сборка ответа', detail: 'из 3 цитат', ms: 960 },
    { key: 'verify', title: 'Самопроверка', detail: 'утверждений 4 · покрыто цитатами 4 · внешних знаний 0', ms: 380 }
  ];

  var ANSWER = {
    text: 'Срок согласования заявки на закупку — пять рабочих дней с момента регистрации заявки в СЭД.[1]\n'
      + 'Заявку визирует руководитель подразделения, затем согласует финансовый контролёр.[1][3]\n'
      + 'Изменения договора согласуются в том же порядке, что и сам договор.[2]',
    citations: [
      {
        doc_id: 'ТЗ-ЭКЗ-СР01-Р2',
        title: 'Правило согласования заявок на закупку',
        revision: 2,
        effective: '2026-06-01',
        quote: 'Срок согласования заявки: пяти рабочих дней с даты регистрации.',
        content_type: 'text',
        business_line: 'закупки',
        section: 'Порядок выполнения процедуры'
      },
      {
        doc_id: 'ТЗ-ЭКЗ-0007-Р2',
        title: 'Регламент согласования договорных изменений',
        revision: 2,
        effective: '2026-04-11',
        quote: 'Порядок согласования договорных изменений совпадает с порядком согласования договора.',
        content_type: 'text',
        business_line: 'закупки'
      },
      {
        doc_id: 'ТЗ-ЭКЗ-0001-Р1',
        title: 'Регламент проверки качества входного контроля',
        revision: 1,
        effective: '2025-11-20',
        quote: 'Финансовый контролёр проверяет соответствие заявки бюджету подразделения.',
        content_type: 'scan',
        page: 2,
        ocr_confidence: 0.52,
        business_line: 'качество'
      },
      {
        doc_id: 'ТЗ-ЭКЗ-0042',
        title: 'Схема процесса согласования заявки',
        revision: 1,
        effective: '2026-01-15',
        quote: 'Блок-схема: заявка → виза руководителя → бюджет → финансовый контролёр → регистрация.',
        content_type: 'image',
        page: 1,
        ocr_confidence: 0.84,
        business_line: 'закупки'
      }
    ],
    refused: false,
    assumptions: []
  };

  /* §12.6 — обязательный демо-кейс: модель знает ответ, базы знаний не хватает. */
  var REFUSAL_NOT_IN_CORPUS = {
    reason: 'В базе знаний организации нет документа, регулирующего этот вопрос. '
      + 'Ставка НДС — общее налоговое знание, а не положение организации; LEX на такие '
      + 'вопросы не отвечает по памяти модели.',
    kind: 'not_in_corpus',
    searched: { documents: 27, cases: 4, business_lines: ['закупки', 'лицензирование'] },
    hint: 'Уточните направление или обратитесь к владельцу регламента',
    sources: []
  };

  var REFUSAL_DATE = {
    reason: 'На 2025-12-01 ни одна редакция правила про срок согласования (ТЗ-ЭКЗ-СР01) '
      + 'не вступила в силу: первая редакция действует с 2026-02-01.',
    kind: 'no_edition_in_force',
    searched: { documents: 27, cases: 4, business_lines: ['закупки'] },
    hint: 'Выберите дату не раньше 2026-02-01',
    sources: ['ТЗ-ЭКЗ-СР01-Р1']
  };

  var REFUSAL_ACCESS = {
    reason: 'Документы по этому вопросу в базе есть, но роли «ИТ-служба» они недоступны: '
      + 'они ведутся Юридическим отделом и относятся к направлению «закупки».',
    kind: 'no_access',
    searched: { documents: 27, cases: 4, business_lines: ['закупки'] },
    hint: 'Смените роль или уточните направление',
    sources: []
  };

  var CLARIFY_QUESTIONS = [
    {
      key: 'as_of',
      text: 'На какую дату определить действующую редакцию?',
      reason: 'в корпусе три редакции правила SR-01, без даты нельзя выбрать',
      kind: 'date'
    },
    {
      key: 'amount',
      text: 'Сумма заявки больше 500 000 рублей?',
      reason: 'от суммы зависит порядок согласования',
      kind: 'choice',
      options: ['до 500 000', 'свыше 500 000', 'не знаю']
    }
  ];

  function clone(v) { return JSON.parse(JSON.stringify(v)); }
  function delay(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  function stepsFor(scenario) {
    var steps = clone(ANSWER_STEPS);
    if (scenario === 'refusal-nocorpus') {
      steps[1].detail = 'направление: определить не удалось · 1 подвопрос';
      steps[3].detail = 'BM25 0 · вектор 2 → топ 0 · документов 0, кейсов 0';
      steps[4].detail = 'найдено 0 кейсов по теме';
      steps[5].detail = 'доступно 5 из 12, отброшено 7';
      steps[6].detail = 'не проверялась: нет найденных документов';
      steps[7].detail = 'сравнивать нечего';
      steps[8].detail = 'ответ не собран: нет ни одной цитаты';
      steps[9].detail = 'утверждений 0 · покрыто 0 · отказ: не из базы знаний';
    } else if (scenario === 'refusal-date') {
      steps[6].detail = '1 редакция, действует с 2026-02-01 — на дату отсчёта не действует';
    } else if (scenario === 'refusal-access') {
      steps[5].detail = 'доступно 0 из 12, отброшено 12 (направление закрыто для роли)';
      steps[6].detail = 'не проверялась: нет доступных документов';
      steps[8].detail = 'ответ не собран: нет доступных источников';
      steps[9].detail = 'утверждений 0 · покрыто 0 · отказ: нет прав';
    } else if (scenario === 'clarify') {
      steps[1] = { key: 'clarify', title: 'Уточнение', detail: 'нужна дата: в корпусе три редакции SR-01 · запрошено 2 параметра', ms: 190 };
    } else if (scenario === 'verify-trimmed') {
      steps[9].detail = 'утверждений 5 · покрыто 3 · 2 отброшено (нет цитаты)';
    }
    return steps;
  }

  function pickScenario(question, roleId, asOf, answers) {
    var forced = (global.LEX_MOCK_SCENARIO || '');
    if (forced) return forced;
    var q = String(question || '');
    if (/ндс|налог на добавленн|ставк[аи] ндс/i.test(q)) return 'refusal-nocorpus';
    if (/друг\w* компан|как обычно|в практике рынка/i.test(q)) return 'refusal-nocorpus';
    if (asOf && asOf < '2026-02-01') return 'refusal-date';
    if (roleId === 'it' && /закупк|согласован|договор/i.test(q)) return 'refusal-access';
    var hasDate = answers && answers.as_of;
    var skippedDate = answers && Object.prototype.hasOwnProperty.call(answers, 'as_of') && !answers.as_of;
    if (!asOf && !hasDate && !skippedDate && /срок/i.test(q)) return 'clarify';
    if (hasDate && String(hasDate) < '2026-02-01') return 'refusal-date';
    if (/покрыт|усеч|отброш/i.test(q)) return 'verify-trimmed';
    return 'answer';
  }

  /**
   * stream(question, roleId, asOf, answers, handlers) — эмуляция POST /api/ask.
   * handlers: onStep, onAnswer, onRefusal, onClarify, onDone, onError
   * Возвращает { abort() }.
   */
  function stream(question, roleId, asOf, answers, handlers) {
    var aborted = false;
    var scenario = pickScenario(question, roleId, asOf, answers);
    var steps = stepsFor(scenario);
    var t0 = (global.performance && performance.now) ? performance.now() : Date.now();

    function elapsed() {
      var now = (global.performance && performance.now) ? performance.now() : Date.now();
      return Math.round(now - t0);
    }
    function guard(fn) {
      return function (payload) { if (!aborted && typeof fn === 'function') fn(payload); };
    }

    (async function run() {
      await delay(160);
      for (var i = 0; i < steps.length; i++) {
        if (aborted) return;
        var s = steps[i];
        if (scenario === 'clarify' && s.key !== 'accept' && s.key !== 'clarify') continue;

        await delay(Math.max(80, Math.min(s.ms, 700)));
        if (aborted) return;
        guard(handlers.onStep)({
          n: i + 1,
          total: steps.length,
          key: s.key,
          title: s.title,
          detail: s.detail,
          ms: s.ms
        });
      }
      if (aborted) return;
      await delay(200);
      if (aborted) return;

      if (scenario === 'clarify') {
        guard(handlers.onClarify)({
          session_id: 's-mock-7f3a1c',
          questions: clone(CLARIFY_QUESTIONS),
          partial: { retrieved: 12, accessible: 5 },
          can_skip: true
        });
        // §10: после clarify поток закрывается, done не приходит
        return;
      }

      if (scenario === 'refusal-nocorpus') guard(handlers.onRefusal)(clone(REFUSAL_NOT_IN_CORPUS));
      else if (scenario === 'refusal-date') guard(handlers.onRefusal)(clone(REFUSAL_DATE));
      else if (scenario === 'refusal-access') guard(handlers.onRefusal)(clone(REFUSAL_ACCESS));
      else guard(handlers.onAnswer)(clone(ANSWER));

      await delay(110);
      if (aborted) return;
      guard(handlers.onDone)({
        elapsed_ms: elapsed(),
        inference_path: 'mock',
        journal_id: 'q-mock-' + String(1000 + (elapsed() % 9000))
      });
    })();

    return { abort: function () { aborted = true; } };
  }

  /* §8: база кейсов для экрана «Кейсы». */
  var CASES = [
    {
      id: 'case-0007',
      question: 'Клиент просит согласовать закупку за 2 дня вместо 5. Что делать?',
      answer_in_practice: 'Согласовали через директора, оформили отступление письмом.',
      outcome: 'resolved',
      author_role: 'director',
      linked_docs: ['ТЗ-ЭКЗ-СР01-Р2'],
      tags: ['срок', 'согласование', 'исключение'],
      rule_key: 'SR-01',
      content_type: 'text',
      business_line: 'закупки'
    },
    {
      id: 'case-0011',
      question: 'Поставщик без лицензии, но с опытом. Можно работать?',
      answer_in_practice: 'Эскалировали в юридический отдел; договор не подписали до получения лицензии.',
      outcome: 'escalated',
      author_role: 'lawyer',
      linked_docs: ['ТЗ-ЭКЗ-0012-Р1'],
      tags: ['лицензия', 'поставщик'],
      rule_key: 'LIC-04',
      content_type: 'text',
      business_line: 'лицензирование'
    },
    {
      id: 'case-0014',
      question: 'Акт приёмки подписан, но партия пришла с браком. Сроки претензии?',
      answer_in_practice: 'Претензия подана в течение 10 дней, партия возвращена.',
      outcome: 'resolved',
      author_role: 'quality',
      linked_docs: ['ТЗ-ЭКЗ-0006-Р2'],
      tags: ['претензия', 'брак', 'срок'],
      rule_key: 'QC-02',
      content_type: 'scan',
      ocr_confidence: 0.71,
      business_line: 'качество'
    },
    {
      id: 'case-0019',
      question: 'Сотрудник просит документ другого отдела — можно показать?',
      answer_in_practice: 'Отказали: права роли не включают этот отдел. Направили к владельцу регламента.',
      outcome: 'rejected',
      author_role: 'it',
      linked_docs: [],
      tags: ['права', 'доступ'],
      rule_key: 'AC-01',
      content_type: 'text',
      business_line: 'персонал'
    }
  ];

  /* §11: направления. */
  var BUSINESS_LINES = [
    { id: 'закупки', title: 'Закупки', docs: 12 },
    { id: 'лицензирование', title: 'Лицензирование', docs: 5 },
    { id: 'качество', title: 'Качество', docs: 6 },
    { id: 'хранение', title: 'Хранение', docs: 3 },
    { id: 'персонал', title: 'Персонал', docs: 4 }
  ];

  /* §2: роли, как отдаёт GET /api/roles. */
  var ROLES = [
    { id: 'lawyer', title: 'Юридический отдел', departments: ['Юридический отдел'], business_lines: ['закупки', 'лицензирование'], systems: [] },
    { id: 'quality', title: 'Отдел качества', departments: ['Отдел качества'], business_lines: ['качество', 'хранение'], systems: [] },
    { id: 'it', title: 'ИТ-служба', departments: ['ИТ-служба'], business_lines: ['персонал'], systems: [] },
    { id: 'director', title: 'Руководитель', departments: ['*'], business_lines: ['*'], systems: ['*'] }
  ];

  global.MockLex = {
    stream: stream,
    roles: function () { return clone(ROLES); },
    businessLines: function () { return clone(BUSINESS_LINES); },
    cases: function () { return clone(CASES); },
    scenarios: ['answer', 'clarify', 'refusal-nocorpus', 'refusal-date', 'refusal-access', 'verify-trimmed']
  };
})(window);
