# REVIEW-CRITERIA — разбор приёмки LEX

Факт-база: чтение `src/lex/policy.py`, `grounding.py`, `orchestrator.py`, `dialogue.py`, `changes.py`, `cases.py`, `src/api/app.py` (+ связанный `src/api/tickets.py`, `src/api/core.py`, `src/lex/tickets.py`). Код не менялся, тесты не запускались.

## Критерии

| Критерий | Что реализовано (где) | Дыры / риск | Приоритет |
|---|---|---|---|
| **1. Строго по регламентам; отсутствие → запрос в службу качества; недостоверный = не точно по регламенту ИЛИ дополнен внешними знаниями** | `orchestrator.SYSTEM_PROMPT`: только переданные фрагменты; `permissions` до `compose` (§12.4). `grounding.check_grounding`: обязательные `[n]`, similarity-порог, якоря чисел/дат/сумм/кодов, запрет усилителей, отсечка неполных фраз (`grounding.py`). `verify`: непокрытые/обрывки отсекаются; `nothing_covered` или пустой остаток → `refusal.not_in_corpus` + `_open_ticket` + hint «запрос передан в службу качества…» (`orchestrator.py`). API: `/api/ask` SSE, `/api/tickets*`, `/api/inbox*` (`app.py`). | Similarity — эвристика, не доказательство (§12.7). При недоступном embed grounding может пометить цитируемое покрытым (`similarity_checked=False`) — внешние знания могут пройти. | **P0** |
| | | Оркестратор создаёт тикет через `lex.tickets.TicketStore.open_from_agent`, а API/инбокс/confirm работают через `api/tickets.py` (другой JSONL/контур). Демо-цепочка «агент создал → качество видит в инбоксе» может разорваться. | **P1** |
| | | `no_llm`/extractive ответ цитирует фрагменты, но формулировка грубая; пользователь может принять его за «точный» без проверки полноты. | **P2** |
| **2. Уточняющий диалог при неоднозначности** | `Dialogue.needs_clarification`: собирает `as_of/amount/department/rule_key/doc_type/business_line`, max 3 вопроса/круг, 2-3 итерации, TTL сессий, явные `assumptions` при пропуске (`dialogue.py`, §10/§15). `orchestrator`: `policy` → `need_clarify` по направлению; `clarify` до `decompose`; после поиска `after_search` решает уточнить ещё или эскалировать; `clarify` закрывает поток без `done`. | Детекция неоднозначности эвристическая (regex/manifest/правила); нестандартные формулировки могут не вызвать clarify. | **P1** |
| | | `can_skip=true` в clarify payload: пользователь может пропустить, defaults/assumptions могут выбрать не ту редакцию/область. | **P1** |
| | | `Dialogue.after_search` при `out_of_base` создаёт ticket через `Dialogue.escalate` (свой путь), оркестратор — через `lex.tickets`; контуры эскалации не единообразны. | **P2** |
| **3. Дообучение: вопрос → служба качества даёт ответ → база пополняется** | Кейсы: `lex.cases.CaseStore.add` (write-role check, `normative=False`, авто `deviates_from`), кейсы участвуют в retrieval как `kind=case` (`cases.py`, `orchestrator._stage_cases/_build_frames`). Качество: `/api/tickets/{id}/answer`, `/draft`, `/confirm` создаёт кейс; при warnings+`rule_key` создаёт change request (`api/tickets.py`, `api/changes.py`). CR: JSONL, статусы `proposed/accepted/rejected/merged`, агрегация evidence (`api/changes.py`, `lex/changes.py`). Эскалации: `/api/escalate*` тоже есть. | «База пополняется» закрыта на кейсах/CR, но не на нормативах: confirm не создаёт/не обновляет документ регламента, merged CR не применяется к корпусу, retrieval-индекс не перестраивается автоматически. | **P0** |
| | | Два параллельных контура: tickets vs escalations, `api/changes.py` vs `lex/changes.py`. Для демо нужен один канонический сценарий, иначе оператор пойдёт в другой endpoint. | **P1** |
| | | Новый кейс может быть не виден в поиске до переиндексации/перезапуска (зависит от wiring `core.add_case`/index). Требует проверки факта в демо-стенде. | **P1** |

## Минимальный набор проверок для демо

| Критерий | Проверка | Ожидаемый артефакт/сигнал |
|---|---|---|
| 1. Строгость/отсутствие | Задать вопрос с ответом в корпусе | SSE: `step verify`, `answer` с `[n]`, `citations[]` непустые, `grounding.external==0`, `refused=false`. |
| 1. Отсутствие в базе | Задать вопрос вне корпуса | `refusal.kind=not_in_corpus`, `hint` про службу качества, создан тикет; тикет виден в каноническом inbox/tickets endpoint. |
| 1. Недостоверный | Негатив: ответ содержит число/срок/усилитель не из цитаты | Утверждение отсекается или весь ответ → refusal; в отчёте демо зафиксировать `similarity_checked`/ограничение embed. |
| 2. Неоднозначность | Вопрос без даты/направления/суммы/типа | Событие `clarify`, `session_id`, `questions[]` (1-3), поток закрыт без `done`. |
| 2. Продолжение диалога | Повторный `/api/ask` с `session_id` + `answers` | Поток проходит дальше до `answer/refusal`; assumptions видны/учтены. |
| 2. Исчерпание уточнений | 2-3 итерации без результата | `escalate` или тикет с `why_unresolved`, `clarifications`, `searched`; нет бесконечного цикла. |
| 3. Дообучение | Тикет: `answer` → `draft` → `confirm` (роль качества) | Кейс создан (`case_id`), тикет закрыт; при расхождении есть `change_request`. |
| 3. Эффект в ответах | Повторить исходный вопрос после confirm | Ответ может опираться на кейс как практику (`source_kind=case`/пометка), но не как нормативная замена регламента. |
| 3. Граница ответственности | Показать CR после confirm | CR в `/api/change-requests`, статус меняет человек; применение в регламент — ручной шаг, не авто-обновление корпуса. |

## Короткий вердикт

- **Критерий 1:** ядро grounding+refusal+ticket есть, но доказуемость «нет внешних знаний» ограничена эвристикой и состоянием embed-сервиса; контур тикетов нужно унифицировать для демо.
- **Критерий 2:** базовый clarify/after_search реализован; для приёмки важны негативные кейсы на пропуск уточнения и единый путь эскалации.
- **Критерий 3:** петля «вопрос→ответ качества→кейс/CR» частично реализована; полного «дообучения регламента» нет — это управляемый human-in-the-loop процесс, что надо явно проговорить на демо.

## Опорные точки в коде

- Grounding/verify: `src/lex/grounding.py`, `src/lex/orchestrator.py:_stage_verify/_open_ticket`.
- Clarify/escalate: `src/lex/dialogue.py:needs_clarification/after_search/escalate`, `src/lex/orchestrator.py:_stage_clarify/_escalation_decision`.
- Кейсы/CR/тикеты: `src/lex/cases.py`, `src/lex/changes.py`, `src/lex/tickets.py`, `src/api/tickets.py`, `src/api/changes.py`, `src/api/app.py`.
- Ограничение: при падении embed grounding ставит `similarity_checked=False` и не блокирует ответ по similarity — это нужно показывать честно, не как «100% без галлюцинаций».
