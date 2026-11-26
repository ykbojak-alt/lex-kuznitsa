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

  /* §17: ровно 10 шагов — policy стоит вторым, до всякого поиска.
     Срез модальности (§9) живёт в detail шага retrieve, а не отдельным шагом:
     в UI счётчик должен показывать [10/10], а не [11/11]. */
  var ANSWER_STEPS = [
    { key: 'accept', title: 'Запрос принят', detail: 'роль: юрист · направление: закупки · дата отсчёта: 2026-06-15', ms: 11 },
    { key: 'policy', title: 'Проверка компетенции', detail: 'в компетенции роли: да · направление: закупки · доступных документов 5', ms: 26 },
    { key: 'decompose', title: 'Разбор вопроса', detail: 'направление: закупки · 2 подвопроса: срок согласования; кто визирует', ms: 230 },
    { key: 'retrieve', title: 'Поиск', detail: 'BM25 12 · вектор 10 → топ 5 · текст 24 · картинки 2 · сканы 1 (1 с плохим распознаванием)', ms: 208 },
    { key: 'cases', title: 'Поиск по кейсам', detail: 'найдено 4 кейса, из них по теме 1', ms: 41 },
    { key: 'permissions', title: 'Права доступа', detail: 'чтение: полный корпус (27 документов) · запись: недоступна', ms: 9 },
    { key: 'resolve', title: 'Действующая редакция', detail: '3 редакции, действует Р2 от 2026-06-01', ms: 14 },
    { key: 'conflict', title: 'Сопоставление', detail: 'конфликтов: 0', ms: 11 },
    { key: 'compose', title: 'Сборка ответа', detail: 'краткая выжимка · 3 цитаты · есть форма', ms: 960 },
    { key: 'verify', title: 'Самопроверка', detail: 'утверждений 4 · покрыто цитатами 4 · внешних знаний 0', ms: 380 }
  ];

  var ANSWER = {
    /* §13.1: короткая выжимка по умолчанию — 2-4 предложения, до 400 знаков. */
    short: 'Срок согласования заявки на закупку — пять рабочих дней с момента регистрации в СЭД.[1] '
      + 'Заявку визирует руководитель подразделения, затем согласует финансовый контролёр.[1][3]',
    /* §21.3: тот же смысл в канцелярском тоне. Реквизиты дословно из цитаты. */
    short_formal: 'Согласно редакции 2 правила «Согласование заявок на закупку» (вступила в силу 01.06.2026) '
      + 'срок согласования заявки составляет пять рабочих дней с даты регистрации в СЭД.[1] '
      + 'Заявка визируется руководителем подразделения и согласуется финансовым контролёром.[1][3]',
    tone: 'plain',
    has_details: true,
    detail_id: 'd-q0001',
    /* Полный текст — для старого рендера и для «Подробнее» без бэкенда. */
    text: 'Срок согласования заявки на закупку — пять рабочих дней с момента регистрации заявки в СЭД.[1]\n'
      + 'Заявку визирует руководитель подразделения, затем согласует финансовый контролёр.[1][3]\n'
      + 'Изменения договора согласуются в том же порядке, что и сам договор.[2]',
    text_formal: 'Согласно редакции 2 правила «Согласование заявок на закупку» срок согласования заявки '
      + 'составляет пять рабочих дней с даты регистрации заявки в СЭД.[1]\n'
      + 'Заявка визируется руководителем подразделения, после чего согласуется финансовым контролёром.[1][3]\n'
      + 'Согласование изменений договора осуществляется в порядке, установленном для согласования договора.[2]',
    citations: [
      {
        doc_id: 'ТЗ-ЭКЗ-СР01-Р2',
        title: 'Правило согласования заявок на закупку',
        revision: 2,
        effective: '2026-06-01',
        quote: 'Срок согласования заявки: пяти рабочих дней с даты регистрации.',
        content_type: 'text',
        business_line: 'закупки',
        section: 'Порядок выполнения процедуры',
        view_url: '#doc-ТЗ-ЭКЗ-СР01-Р2',
        download_url: '#doc-ТЗ-ЭКЗ-СР01-Р2/download'
      },
      {
        doc_id: 'ТЗ-ЭКЗ-0007-Р2',
        title: 'Регламент согласования договорных изменений',
        revision: 2,
        effective: '2026-04-11',
        quote: 'Порядок согласования договорных изменений совпадает с порядком согласования договора.',
        content_type: 'text',
        business_line: 'закупки',
        view_url: '#doc-ТЗ-ЭКЗ-0007-Р2',
        download_url: '#doc-ТЗ-ЭКЗ-0007-Р2/download'
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
        business_line: 'качество',
        view_url: '#doc-ТЗ-ЭКЗ-0001-Р1',
        download_url: '#doc-ТЗ-ЭКЗ-0001-Р1/download'
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
        business_line: 'закупки',
        view_url: '#doc-ТЗ-ЭКЗ-0042',
        download_url: '#doc-ТЗ-ЭКЗ-0042/download'
      }
    ],
    refused: false,
    assumptions: [],
    /* §20: ответ с оговоркой — третий исход, включается сценарием caveat. */
    caveat: null,
    /* §13.3: в ответе есть форма — в ленте карточка. */
    form_id: 'form-zayavka'
  };

  /* §20.2: ответ с оговоркой. Уровень medium — конфликт редакций + единственный источник.
     §21.5: тон оговорки всегда formal, смягчать нельзя. */
  var ANSWER_CAVEAT = {
    short: 'Срок согласования заявки на закупку — пять рабочих дней с момента регистрации в СЭД.[1] '
      + 'С 1 января 2027 года срок изменится.[1]',
    has_details: true,
    detail_id: 'd-q0001',
    tone: 'plain',
    caveat: {
      level: 'medium',
      reasons: [
        { code: 'edition_conflict', text: 'Ред. 3 вступает 2027-01-01 — на дату после неё срок изменится' },
        { code: 'single_source', text: 'Подтверждения в других документах нет' }
      ],
      advice: 'Сверьтесь с владельцем регламента перед принятием решения'
    },
    citations: ANSWER.citations,
    refused: false,
    assumptions: [],
    form_id: null
  };

  /* §20.5: аналитика. answer_rate — самая честная метрика продукта. */
  var STATS = {
    total: 46,
    latency_avg_ms: 3800,
    latency_p95_ms: 9100,
    caveats: {
      total: 9,
      by_level: { low: 4, medium: 4, high: 1 },
      by_reason: [
        { code: 'edition_conflict', count: 5 },
        { code: 'single_source', count: 4 }
      ],
      answer_rate: { plain: 31, caveated: 9, refused: 6 }
    }
  };

  /* §13.1: развёрнутая версия — по /api/expand, из тех же фрагментов. */
  var EXPAND = {
    'd-q0001': {
      short: ANSWER.short,
      detailed: 'Согласно редакции 2 регламента согласования заявок, вступившей в силу 1 июня 2026 года, '
        + 'срок согласования заявки на закупку составляет пять рабочих дней. Срок отсчитывается с момента '
        + 'регистрации заявки в СЭД. Редакция 1 (2026-01-15) устанавливала три рабочих дня, редакция 3 '
        + '(вступает 2027-01-01) — семь.',
      sections: [
        { title: 'Порядок выполнения', quote: 'Заявка регистрируется в СЭД, визируется руководителем подразделения, затем согласуется финансовым контролёром.' },
        { title: 'Исключения', quote: 'Закупки свыше 500 000 рублей согласуются в порядке, установленном для крупных закупок (ТЗ-ЭКЗ-СР01-Р3).' },
        { title: 'Роли', quote: 'Руководитель подразделения — виза; финансовый контролёр — согласование бюджета.' }
      ],
      tables: [
        {
          caption: 'Сроки согласования по редакциям',
          rows: [['Ред.', 'Дата вступления', 'Срок'], ['Р1', '2026-01-15', '3 рабочих дня'],
            ['Р2', '2026-06-01', '5 рабочих дней'], ['Р3', '2027-01-01', '7 рабочих дней']]
        }
      ],
      citations: ANSWER.citations
    }
  };

  /* §13.3–13.4: форма и её описание. */
  var FORM = {
    form_id: 'form-zayavka',
    title: 'Заявка на закупку',
    doc_id: 'ТЗ-ЭКЗ-СР01-Р2',
    format: 'docx',
    business_line: 'закупки',
    fields: [
      { key: 'number', label: 'Номер заявки', type: 'text', required: true,
        pattern: '^ЗК-\\d{4}-\\d{4}$', example: 'ЗК-2026-0042', hint: 'формат ЗК-ГГГГ-НННН' },
      { key: 'amount', label: 'Сумма, ₽', type: 'money', required: true, min: 0 },
      { key: 'deadline', label: 'Требуемый срок', type: 'date', required: true },
      { key: 'department', label: 'Подразделение', type: 'choice', required: true,
        options: ['Отдел качества', 'Юридический отдел', 'ИТ-служба', 'Производственный отдел'] }
    ]
  };

  /* §13.4: проверка формы — правила, а не «на глаз модели». */
  function fillForm(formId, values) {
    var errors = [];
    var warnings = [];
    var filled = {};
    var missing = [];
    var fields = FORM.fields;

    for (var i = 0; i < fields.length; i++) {
      var f = fields[i];
      var raw = values[f.key];
      var v = raw == null ? '' : String(raw).trim();

      if (!v) {
        if (f.required) {
          errors.push({ field: f.key, message: 'поле обязательное',
            expected: f.type === 'date' ? 'дата в формате ГГГГ-ММ-ДД' : 'значение' });
          missing.push(f.key);
        }
        filled[f.key] = null;
        continue;
      }

      if (f.key === 'number' && f.pattern && !(new RegExp(f.pattern)).test(v)) {
        errors.push({ field: f.key, value: v,
          message: 'номер не соответствует формату ЗК-ГГГГ-НННН', expected: f.example });
      }
      if (f.type === 'money') {
        var digits = v.replace(/[^\d]/g, '');
        if (!digits) {
          errors.push({ field: f.key, value: v, message: 'сумма должна быть числом', expected: 'например 450000' });
        } else {
          var n = parseInt(digits, 10);
          filled[f.key] = n;
          if (n >= 450000 && n <= 550000) {
            warnings.push({ field: f.key,
              message: 'сумма близка к порогу 500 000 — согласование пойдёт по другому порядку',
              doc_id: 'ТЗ-ЭКЗ-СР01-Р3' });
          }
          if (n > 1000000) {
            warnings.push({ field: f.key, message: 'крупная закупка: срок согласования — семь рабочих дней',
              doc_id: 'ТЗ-ЭКЗ-СР01-Р3' });
          }
          continue;
        }
      }
      if (f.type === 'date') {
        if (!/^\d{4}-\d{2}-\d{2}$/.test(v)) {
          errors.push({ field: f.key, value: v, message: 'дата в формате ГГГГ-ММ-ДД', expected: '2026-07-01' });
        }
      }
      if (f.type === 'choice' && f.options && f.options.indexOf(v) < 0) {
        errors.push({ field: f.key, value: v, message: 'значение не из справочника',
          expected: f.options.join(' | ') });
      }
      if (!(f.type === 'money' && filled[f.key] != null)) filled[f.key] = v;
    }

    var total = fields.length;
    var okCount = fields.filter(function (f2) { return filled[f2.key] != null; }).length;

    return {
      ok: errors.length === 0,
      errors: errors,
      warnings: warnings,
      filled: filled,
      missing: missing,
      progress: okCount + ' из ' + total + ' полей'
    };
  }

  /* «Похожие вопросы» — по журналу; refusal_rate высокий = регламента нет. */
  var SIMILAR = [
    { question: 'Какой срок согласования заявки на закупку?', count: 14, variants: ['сколько дней согласуется закупка', 'срок визы заявки'], refusal_rate: 0 },
    { question: 'Кто согласует изменения в договор?', count: 9, variants: ['порядок согласования изменений договора'], refusal_rate: 0 },
    { question: 'Какая ставка НДС в России?', count: 6, variants: ['размер НДС'], refusal_rate: 1, note: 'регламента нет' },
    { question: 'Как оформить претензию поставщику по браку?', count: 5, variants: ['сроки претензии', 'возврат бракованной партии'], refusal_rate: 0 },
    { question: 'В каких случаях нужна лицензия на продукт?', count: 4, variants: ['лицензирование продукта X'], refusal_rate: 0.75, note: 'регламент не покрывает лицензирование' }
  ];

  /* §12.6 — обязательный демо-кейс: модель знает ответ, базы знаний не хватает. */
  var REFUSAL_OUT_OF_SCOPE = {
    kind: 'out_of_scope',
    reason: 'Вопрос не относится к базе знаний организации.',
    detail: 'База покрывает: закупки, лицензирование, качество, хранение, персонал',
    searched: null,
    hint: 'Переформулируйте или обратитесь в службу качества',
    sources: []
  };

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

  /* §13.2: документ целиком для чтения — оглавление + разделы. */
  var DOCS = {
    'ТЗ-ЭКЗ-СР01-Р2': {
      doc_id: 'ТЗ-ЭКЗ-СР01-Р2',
      title: 'Правило согласования заявок на закупку',
      revision: 2,
      effective: '2026-06-01',
      department: 'Юридический отдел',
      business_line: 'закупки',
      sections: [
        { title: 'Назначение и область применения', text: 'Правило определяет порядок согласования заявок на закупку товаров и услуг для подразделений организации.' },
        { title: 'Порядок выполнения процедуры', text: 'Заявка регистрируется в СЭД. Срок согласования заявки: пяти рабочих дней с даты регистрации. Заявку визирует руководитель подразделения, затем согласует финансовый контролёр на предмет соответствия бюджету.' },
        { title: 'Исключения', text: 'Закупки свыше 500 000 рублей согласуются в порядке для крупных закупок: срок увеличивается до семи рабочих дней.' },
        { title: 'Роли и ответственность', text: 'Руководитель подразделения отвечает за визу, финансовый контролёр — за согласование бюджета, секретариат — за регистрацию.' }
      ]
    },
    'ТЗ-ЭКЗ-0007-Р2': {
      doc_id: 'ТЗ-ЭКЗ-0007-Р2',
      title: 'Регламент согласования договорных изменений',
      revision: 2,
      effective: '2026-04-11',
      department: 'Производственный отдел',
      business_line: 'закупки',
      sections: [
        { title: 'Порядок выполнения процедуры', text: 'Порядок согласования договорных изменений совпадает с порядком согласования договора.' },
        { title: 'Ограничения и исключения', text: 'Изменения, затрагивающие цену более чем на 10%, требуют повторного согласования финансовым контролёром.' }
      ]
    },
    'ТЗ-ЭКЗ-0001-Р1': {
      doc_id: 'ТЗ-ЭКЗ-0001-Р1',
      title: 'Регламент проверки качества входного контроля',
      revision: 1,
      effective: '2025-11-20',
      department: 'Отдел качества',
      business_line: 'качество',
      ocr_degraded: true,
      sections: [
        { title: 'Порядок выполнения процедуры', text: 'Финансовый контролёр проверяет соответствие заявки бюджету подразделения. Партия проходит входной контроль по чек-листу.' }
      ]
    }
  };

  function docFor(docId) { return DOCS[docId] ? clone(DOCS[docId]) : null; }  function delay(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  /* Индексы — по новой таблице §17: 0 accept, 1 policy, 2 decompose, 3 retrieve,
     4 cases, 5 permissions, 6 resolve, 7 conflict, 8 compose, 9 verify. */
  function stepsFor(scenario) {
    var steps = clone(ANSWER_STEPS);
    if (scenario === 'refusal-nocorpus') {
      steps[1].detail = 'в компетенции: да · направление: определить не удалось · доступных документов 5';
      steps[2].detail = 'направление: определить не удалось · 1 подвопрос';
      steps[3].detail = 'BM25 0 · вектор 2 → топ 0 · документов 0, кейсов 0';
      steps[4].detail = 'найдено 0 кейсов по теме';
      steps[5].detail = 'чтение: полный корпус (27 документов) · запись: недоступна';
      steps[6].detail = 'не проверялась: нет найденных документов';
      steps[7].detail = 'сравнивать нечего';
      steps[8].detail = 'ответ не собран: нет ни одной цитаты';
      steps[9].detail = 'утверждений 0 · покрыто 0 · отказ: не из базы знаний';
    } else if (scenario === 'refusal-outofscope') {
      steps[1].detail = 'ВНЕ КОМПЕТЕНЦИИ: база не покрывает «налоги» (база покрывает: закупки, лицензирование, качество, хранение, персонал)';
      steps[2].detail = 'не выполнялся: вопрос вне базы';
      steps[3].detail = 'не выполнялся: вопрос вне базы';
      steps[4].detail = 'не выполнялся';
      steps[5].detail = 'не выполнялся';
      steps[6].detail = 'не выполнялся';
      steps[7].detail = 'не выполнялся';
      steps[8].detail = 'ответ не собирался';
      steps[9].detail = 'проверка не нужна: ответа нет';
    } else if (scenario === 'refusal-date') {
      steps[6].detail = '1 редакция, действует с 2026-02-01 — на дату отсчёта не действует';
    } else if (scenario === 'clarify') {
      /* §10: шаг уточнения встаёт третьим (n=3) — дальше агент ждёт ответа. */
      steps[2] = { key: 'clarify', title: 'Уточнение', detail: 'нужна дата: в корпусе три редакции SR-01 · запрошено 2 параметра', ms: 190 };
    } else if (scenario === 'verify-trimmed') {
      steps[9].detail = 'утверждений 5 · покрыто 3 · 2 отброшено (нет цитаты)';
    } else if (scenario === 'caveat') {
      /* §20: оговорка видна и в шаге verify — уверенность и уровень. */
      steps[9].detail = 'утверждений 3 · покрыто 3 · уверенность 0.68 · ОГОВОРКА (medium)';
      steps[9].caveat_level = 'medium';
    } else if (scenario === 'caveat-high') {
      steps[9].detail = 'утверждений 2 · покрыто 2 · уверенность 0.54 · ОГОВОРКА (high)';
      steps[9].caveat_level = 'high';
    } else if (scenario === 'escalate') {
      steps[8].detail = 'ответ не собран: нет ни одной цитаты';
      steps[9] = { key: 'escalate', title: 'Передано в службу качества', detail: 'заявка esc-0003 · 2 итерации уточнения · срок: до 2 рабочих дней', ms: 140 };
    }
    return steps;
  }

  function pickScenario(question, roleId, asOf, answers) {
    var forced = (global.LEX_MOCK_SCENARIO || '');
    if (forced) return forced;
    var q = String(question || '');
    if (/ндс|налог на добавленн|ставк[аи] ндс|бухгалтер/i.test(q)) return 'refusal-outofscope';
    if (/друг\w* компан|как обычно|в практике рынка|единственн\w* поставщик/i.test(q)) return 'escalate';
    /* §20: демо-триггеры оговорки — конфликт редакций и единственный источник. */
    if (/2027|после нового года|следующ\w* год|редакц\w* 3|оговорк/i.test(q)) return 'caveat';
    if (/срочно|сегодня же|любой ценой/i.test(q)) return 'caveat-high';
    if (asOf && asOf < '2026-02-01') return 'refusal-date';
    var hasDate = answers && answers.as_of;
    var skippedDate = answers && Object.prototype.hasOwnProperty.call(answers, 'as_of') && !answers.as_of;
    /* §10: уточнение — на расплывчатый вопрос, а не на любой вопрос со словом «срок». */
    var vague = String(q).replace(/\s+/g, ' ').trim().length <= 34 && /срок|сколько|когда/i.test(q);
    if (!asOf && !hasDate && !skippedDate && vague) return 'clarify';
    if (hasDate && String(hasDate) < '2026-02-01') return 'refusal-date';
    if (/покрыт|усеч|отброш/i.test(q)) return 'verify-trimmed';
    return 'answer';
  }

  /**
   * stream(question, roleId, asOf, answers, tone, handlers) — эмуляция POST /api/ask.
   * handlers: onStep, onAnswer, onRefusal, onClarify, onEscalate, onDone, onError
   * Возвращает { abort() }.
   *
   * Задержки 150–250 мс на шаг: прогон ~2 с, чтобы прогресс было видно на видео (доп. №8).
   */
  function stream(question, roleId, asOf, answers, tone, handlers) {
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
      await delay(320);
      for (var i = 0; i < steps.length; i++) {
        if (aborted) return;
        var s = steps[i];
        if (scenario === 'clarify' && s.key !== 'accept' && s.key !== 'clarify') continue;
        /* Продолжение диалога после уточнения: первые шаги не повторяем. */
        if (handlers.resumeFrom && i < handlers.resumeFrom - 1) continue;

        await delay(150 + ((i * 37) % 100));
        if (aborted) return;
        guard(handlers.onStep)({
          n: i + 1,
          total: steps.length,
          key: s.key,
          title: s.title,
          detail: s.detail,
          ms: s.ms,
          caveat_level: s.caveat_level || null
        });
      }
      if (aborted) return;
      await delay(260);
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
      else if (scenario === 'refusal-outofscope') guard(handlers.onRefusal)(clone(REFUSAL_OUT_OF_SCOPE));
      else if (scenario === 'refusal-date') guard(handlers.onRefusal)(clone(REFUSAL_DATE));
      else if (scenario === 'escalate') {
        /* §18 + §22: агент сам создаёт тикет и «отправляет письмо» — в демо имитация. */
        var tk = createTicket(question, roleId, 'закупки');
        guard(handlers.onEscalate)({
          ticket: tk.id,
          iterations: tk.clarifications.length,
          reason: tk.why_unresolved,
          searched: { documents: 27, cases: 4 },
          next: 'Заявка передана в службу качества. Ответ появится в базе как кейс.',
          eta: 'до 2 рабочих дней',
          sla_due: tk.sla_due,
          question: question,
          email: tk.email
        });
        return; // поток закрывается на эскалации, done не приходит
      }
      else if (scenario === 'caveat' || scenario === 'caveat-high') {
        var cav = clone(ANSWER_CAVEAT);
        if (scenario === 'caveat-high') {
          cav.caveat.level = 'high';
          cav.caveat.reasons = [
            { code: 'edition_conflict', text: 'Ред. 3 вступает 2027-01-01 — на дату после неё срок изменится' },
            { code: 'edition_conflict', text: 'Ред. 2 и ред. 3 близки по дате вступления, приоритет неявный' }
          ];
          cav.caveat.advice = 'Не принимайте решение без владельца регламента';
        }
        cav.citations = clone(ANSWER.citations).slice(0, 2);
        cav.short = applyTone(cav.short, tone);
        guard(handlers.onAnswer)(cav);
      }
      else {
        var answ = clone(ANSWER);
        answ.short = applyTone(answ.short, tone, answ.short_formal);
        answ.text = applyTone(answ.text, tone, answ.text_formal);
        answ.tone = tone === 'formal' ? 'formal' : 'plain';
        guard(handlers.onAnswer)(answ);
      }

      await delay(140);
      if (aborted) return;
      guard(handlers.onDone)({
        elapsed_ms: elapsed(),
        inference_path: 'mock',
        journal_id: 'q-mock-' + String(1000 + (elapsed() % 9000))
      });
    })();

    return { abort: function () { aborted = true; } };
  }

  /* §21.3: два уровня тона, смысл один, реквизиты дословно. */
  function applyTone(text, tone, formalVariant) {
    if (tone !== 'formal') return text;
    if (formalVariant) return formalVariant;
    return text;
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

  /* §23: роли. Чтение единое у всех; запись — quality/director.
     departments и business_lines остаются для аналитики, на выдачу не влияют. */
  var ROLES = [
    { id: 'lawyer', title: 'Юридический отдел', departments: ['Юридический отдел'], business_lines: ['закупки', 'лицензирование'], systems: [] },
    { id: 'quality', title: 'Отдел качества', departments: ['Отдел качества'], business_lines: ['качество', 'хранение'], systems: [] },
    { id: 'manager', title: 'Менеджер', departments: ['Производственный отдел'], business_lines: ['закупки', 'хранение'], systems: [] },
    { id: 'it', title: 'ИТ-служба', departments: ['ИТ-служба'], business_lines: ['персонал'], systems: [] },
    { id: 'director', title: 'Руководитель', departments: ['*'], business_lines: ['*'], systems: ['*'] }
  ];

  /* §23.1: кто может писать в базу. */
  function canWrite(roleId) { return roleId === 'quality' || roleId === 'director'; }

  /* §22: письмо в службу качества. Адреса — example.invalid по RFC 2606,
     режим simulated обязателен: реальной отправки нет. */
  function makeEmail(roleId, question, sentAt) {
    return {
      to: 'quality@example.invalid',
      from: (roleId || 'employee') + '@example.invalid',
      subject: '[LEX] Вопрос без ответа в базе: ' + String(question || '').slice(0, 70),
      sent_at: sentAt || '2026-11-26T11:20:00Z',
      delivery: 'simulated'
    };
  }

  /* §18.2: тикеты. Создаёт агент (created_by: "agent"), номер человекочитаемый. */
  var TICKETS = [
    {
      id: 'LEX-2026-0042',
      created_at: '2026-11-26T11:20:00Z',
      created_by: 'agent',
      session_id: 's-7f3a1c',
      question: 'Как согласовать закупку, если поставщик единственный?',
      asker: { role_id: 'lawyer', business_line: 'закупки' },
      clarifications: [
        { iteration: 1, asked: ['На какую дату?'], answers: ['2026-07-01'] },
        { iteration: 2, asked: ['Сумма?', 'Направление?'], answers: ['не знаю', 'закупки'] }
      ],
      searched: { documents: 27, cases: 4, accessible: 5, top: ['ТЗ-ЭКЗ-СР01-Р2'] },
      why_unresolved: 'в корпусе нет порядка для единственного поставщика',
      kind: 'not_in_corpus',
      priority: 'normal',
      assignee: null,
      status: 'open',
      sla_due: '2026-11-28T11:20:00Z',
      email: makeEmail('lawyer', 'Как согласовать закупку, если поставщик единственный?', '2026-11-26T11:20:00Z')
    },
    {
      id: 'LEX-2026-0041',
      created_at: '2026-11-26T09:05:00Z',
      created_by: 'agent',
      session_id: 's-1c2d',
      question: 'Можно ли принять партию с отклонением по влажности 2%?',
      asker: { role_id: 'quality', business_line: 'качество' },
      clarifications: [
        { iteration: 1, asked: ['Какая номенклатурная позиция?'], answers: ['комплект уплотнения КУ-120'] }
      ],
      searched: { documents: 27, cases: 4, accessible: 6, top: ['ТЗ-ЭКЗ-0006-Р2'] },
      why_unresolved: 'допуски по влажности описаны в стандарте, но стандарт от другого изделия',
      kind: 'not_in_corpus',
      priority: 'normal',
      assignee: 'И. Петрова',
      status: 'answered',
      sla_due: '2026-11-28T09:05:00Z',
      email: makeEmail('quality', 'Можно ли принять партию с отклонением по влажности 2%?', '2026-11-26T09:05:00Z'),
      quality_answer: 'Для КУ-120 допуск по влажности — не более 1,5%. Партию принимать нельзя, оформить возврат.',
      answered_at: '2026-11-26T10:30:00Z'
    },
    {
      id: 'LEX-2026-0040',
      created_at: '2026-11-26T08:10:00Z',
      created_by: 'agent',
      session_id: 's-44aa',
      question: 'Кто отвечает за хранение документов свыше трёх лет?',
      asker: { role_id: 'lawyer', business_line: 'хранение' },
      clarifications: [],
      searched: { documents: 27, cases: 4, accessible: 4, top: ['ТЗ-ЭКЗ-0004-Р1'] },
      why_unresolved: 'в регламенте срок есть, ответственного нет',
      kind: 'not_in_corpus',
      priority: 'high',
      assignee: 'С. Ковалёв',
      status: 'assigned',
      sla_due: '2026-11-26T16:10:00Z',
      email: makeEmail('lawyer', 'Кто отвечает за хранение документов свыше трёх лет?', '2026-11-26T08:10:00Z')
    },
    {
      id: 'LEX-2026-0038',
      created_at: '2026-11-25T16:40:00Z',
      created_by: 'agent',
      session_id: 's-9a8b',
      question: 'Кому сообщать об утечке учётных данных?',
      asker: { role_id: 'it', business_line: 'персонал' },
      clarifications: [],
      searched: { documents: 27, cases: 4, accessible: 0, top: [] },
      why_unresolved: 'наличие регламента требует службы ИБ, её нет в корпусе',
      kind: 'not_in_corpus',
      priority: 'normal',
      assignee: 'И. Петрова',
      status: 'closed',
      sla_due: '2026-11-27T16:40:00Z',
      email: makeEmail('it', 'Кому сообщать об утечке учётных данных?', '2026-11-25T16:40:00Z'),
      close_reason: 'вопрос вне охвата, передан в ИБ напрямую'
    }
  ];

  /* §16.2: черновик кейса + подсказки: чего не хватает и с чем расходится. */
  var DRAFTS = {
    'LEX-2026-0042': {
      ticket: 'LEX-2026-0042',
      status: 'answered',
      quality_answer: 'Для единственного поставщика — обоснование по 44-ФЗ п.4 ч.1 ст.93, форма ОС-2.',
      resolved_by: 'quality',
      answered_at: '2026-11-26T13:40:00Z',
      draft: {
        question: 'Как согласовать закупку, если поставщик единственный?',
        answer_in_practice: 'Обоснование по 44-ФЗ п.4 ч.1 ст.93, форма ОС-2.',
        outcome: 'resolved',
        business_line: 'закупки',
        linked_docs: [],
        tags: ['единственный поставщик', 'закупки', '44-ФЗ'],
        rule_key: null,
        author_role: 'quality',
        author_name: 'служба качества',
        content_type: 'text',
        source: 'escalation',
        escalation_id: 'LEX-2026-0042'
      },
      suggestions: {
        missing: [
          { field: 'linked_docs', why: 'в ответе упомянут 44-ФЗ, но такого документа нет в корпусе',
            note: 'можно оставить пустым — кейс будет без ссылки на регламент' },
          { field: 'rule_key', why: 'ответ не относится ни к SR-01/02/03' }
        ],
        conflicts: [
          { with: 'ТЗ-ЭКЗ-СР01-Р2', why: 'ответ предлагает обход процедуры; в регламенте такого исключения нет',
            severity: 'warning' }
        ]
      }
    },
    'LEX-2026-0041': {
      ticket: 'LEX-2026-0041',
      status: 'answered',
      quality_answer: 'Для КУ-120 допуск по влажности — не более 1,5%. Партию принимать нельзя, оформить возврат.',
      resolved_by: 'quality',
      answered_at: '2026-11-26T10:30:00Z',
      draft: {
        question: 'Можно ли принять партию с отклонением по влажности 2%?',
        answer_in_practice: 'Для КУ-120 допуск по влажности не более 1,5%; партию не принимать, оформить возврат.',
        outcome: 'rejected',
        business_line: 'качество',
        linked_docs: ['ТЗ-ЭКЗ-0006-Р2'],
        tags: ['влажность', 'приёмка', 'КУ-120'],
        rule_key: 'QC-02',
        author_role: 'quality',
        author_name: 'служба качества',
        content_type: 'text',
        source: 'escalation',
        escalation_id: 'LEX-2026-0041'
      },
      suggestions: { missing: [], conflicts: [] }
    }
  };

  var ticketSeq = 42;

  /* §18.2: тикет создаёт агент — новый номер в очередь. */
  function createTicket(question, roleId, lineId) {
    ticketSeq += 1;
    var id = 'LEX-2026-00' + ticketSeq;
    var t = {
      id: id,
      created_at: '2026-11-26T12:00:00Z',
      created_by: 'agent',
      session_id: 's-mock-' + ticketSeq,
      question: question,
      asker: { role_id: roleId || '', business_line: lineId || 'закупки' },
      clarifications: [
        { iteration: 1, asked: ['На какую дату определить действующую редакцию?'], answers: ['2026-07-01'] },
        { iteration: 2, asked: ['О каком направлении речь?'], answers: ['закупки'] }
      ],
      searched: { documents: 27, cases: 4, accessible: 5, top: ['ТЗ-ЭКЗ-СР01-Р2'] },
      why_unresolved: 'в корпусе нет порядка согласования для этого случая',
      kind: 'not_in_corpus',
      priority: 'normal',
      assignee: null,
      status: 'open',
      sla_due: '2026-11-28T12:00:00Z',
      email: makeEmail(roleId, question, '2026-11-26T12:00:00Z')
    };
    TICKETS.unshift(t);
    DRAFTS[id] = {
      ticket: id,
      status: 'answered',
      quality_answer: 'Ответ службы качества (мок): вопрос решён по действующему регламенту.',
      resolved_by: 'quality',
      answered_at: '2026-11-26T13:00:00Z',
      draft: {
        question: question,
        answer_in_practice: 'Ответ службы качества (мок): вопрос решён по действующему регламенту.',
        outcome: 'resolved',
        business_line: lineId || 'закупки',
        linked_docs: [],
        tags: ['мок', lineId || 'закупки'],
        rule_key: null,
        author_role: 'quality',
        author_name: 'служба качества',
        content_type: 'text',
        source: 'escalation',
        escalation_id: id
      },
      suggestions: { missing: [{ field: 'linked_docs', why: 'к ответу службы качества не приложен документ', note: 'можно оставить пустым' }], conflicts: [] }
    };
    return t;
  }

  function tickets() { return clone(TICKETS); }
  function ticketById(id) {
    for (var i = 0; i < TICKETS.length; i++) if (TICKETS[i].id === id) return clone(TICKETS[i]);
    return null;
  }
  function draftOf(id) { return DRAFTS[id] ? clone(DRAFTS[id]) : null; }

  function assignTicket(id, assignee) {
    for (var i = 0; i < TICKETS.length; i++) {
      if (TICKETS[i].id === id) {
        TICKETS[i].assignee = assignee || 'И. Петрова';
        if (TICKETS[i].status === 'open') TICKETS[i].status = 'assigned';
        return { ok: true, ticket: clone(TICKETS[i]) };
      }
    }
    return { ok: false, error: 'тикет не найден' };
  }

  function answerTicket(id) {
    for (var i = 0; i < TICKETS.length; i++) {
      if (TICKETS[i].id === id) { TICKETS[i].status = 'answered'; return { ok: true }; }
    }
    return { ok: false };
  }

  function closeTicket(id, reason) {
    for (var i = 0; i < TICKETS.length; i++) {
      if (TICKETS[i].id === id) {
        TICKETS[i].status = 'closed';
        TICKETS[i].close_reason = reason || '';
        return { ok: true };
      }
    }
    return { ok: false };
  }

  /* §16.3: подтверждение черновика — те же правила, что на сервере. */
  var caseSeq = 21;
  function confirmDraft(id, caseObj, ackWarnings) {
    var d = DRAFTS[id];
    if (!d) return { ok: false, error: 'черновик не найден' };
    var conflicts = (d.suggestions.conflicts || []);
    var hard = conflicts.filter(function (c) { return c.severity === 'error'; });
    if (hard.length) return { ok: false, error: 'есть конфликты уровня error — добавить нельзя' };
    if (conflicts.length && !ackWarnings) {
      return { ok: false, error: 'есть предупреждения: подтвердите, что понимаете расхождение' };
    }
    if (!caseObj.question || !caseObj.answer_in_practice) {
      return { ok: false, error: 'обязательные поля не заполнены' };
    }
    caseSeq += 1;
    var caseId = 'case-00' + caseSeq;
    for (var i = 0; i < TICKETS.length; i++) if (TICKETS[i].id === id) TICKETS[i].status = 'closed';
    d.status = 'resolved';
    return {
      ok: true,
      case_id: caseId,
      in_index: true,
      ticket_status: 'closed',
      affected: 'кейсов в базе: ' + (CASES.length + 1) + ' · вопрос теперь покрыт'
    };
  }

  global.MockLex = {
    stream: stream,
    roles: function () { return clone(ROLES); },
    canWrite: canWrite,
    businessLines: function () { return clone(BUSINESS_LINES); },
    cases: function () { return clone(CASES); },
    expand: function (detailId) { return clone(EXPAND[detailId] || null); },
    form: function (formId) { return formId === FORM.form_id ? clone(FORM) : null; },
    fillForm: fillForm,
    similar: function () { return clone(SIMILAR); },
    stats: function () { return clone(STATS); },
    doc: function (docId) { return docFor(docId); },
    tickets: tickets,
    ticket: ticketById,
    draft: draftOf,
    confirmDraft: confirmDraft,
    createTicket: createTicket,
    assignTicket: assignTicket,
    answerTicket: answerTicket,
    closeTicket: closeTicket,
    scenarios: ['answer', 'caveat', 'caveat-high', 'clarify', 'refusal-nocorpus',
      'refusal-outofscope', 'refusal-date', 'verify-trimmed', 'escalate']
  };
})(window);
