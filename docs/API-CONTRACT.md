# LEX — контракт API и событий (v1, 26.11.2026 10:25)

**Статус:** обязательный. Бэкенд, UI и аналитика строятся против этого файла.
Изменения — только через ALICE, с пометкой `v1.1` в конце.

Стек: Python 3.11+, FastAPI + uvicorn. Порт разработки — **8874**, на sage-pl — за Caddy.

---

## 1. Структура репозитория

```
src/
  lex/                 # ядро: ingest → retrieve → resolve → compose   [FORGE]
    __init__.py
    corpus.py          # чтение manifest + документов, чанкинг
    index.py           # BM25 + эмбеддинги (:8081), поиск
    rerank.py          # реранкер (:8082)
    access.py          # роли и фильтр прав                        [ACE]
    editions.py        # логика действующей редакции
    orchestrator.py    # 8 шагов, эмиссия событий
    llm.py             # клиент NexusLink (sage-pl), профили
    journal.py         # журнал JSONL + агрегаты                   [ACE]
    models.py          # pydantic-схемы (общие для API и ядра)
  api/                 # HTTP-слой                                    [SPARK]
    app.py             # FastAPI, роуты, SSE
    static/            # собранный UI (копия из web/dist)
  web/                 # фронтенд                                      [NEO]
    index.html
    app.js
    lex.css
    assets/
```

Правило: `src/lex` **не знает** про HTTP, `src/api` **не содержит** логики RAG.

---

## 2. HTTP-эндпоинты

### `GET /api/health`
```json
{"status":"ok","version":"1.0.0",
 "llm":{"slot":"claude-sonnet-5","base":"…","reachable":true},
 "embed":{"model":"bge-m3","dim":1024,"reachable":true},
 "rerank":{"model":"bge-reranker-base","reachable":true},
 "corpus":{"documents":27,"chunks":412}}
```
`reachable` — результат живой проверки, не константа.

### `GET /api/roles`
```json
[{"id":"lawyer","title":"Юридический отдел","departments":["Юридический отдел"],"systems":[]},
 {"id":"quality","title":"Отдел качества","departments":["Отдел качества"],"systems":[]},
 {"id":"it","title":"ИТ-служба","departments":["ИТ-служба"],"systems":[]},
 {"id":"director","title":"Руководитель","departments":["*"],"systems":["*"]}]
```
`*` = все. Отделы берутся из `manifest.json → documents[].department`.

### `POST /api/ask` → `text/event-stream`
Запрос:
```json
{"question":"Какой срок согласования заявки на закупку?",
 "role_id":"lawyer",
 "as_of":"2026-03-01"}
```
- `as_of` — необязательная дата (YYYY-MM-DD). Если задана — действует редакция,
  вступившая в силу не позже неё. Если раньше первой редакции — обоснованный отказ.
- `role_id` — обязателен (права доступа обязательны по брифу).

### `GET /api/journal?limit=50&role_id=`
```json
[{"id":"q-001","ts":"2026-11-26T10:31:02Z","role_id":"lawyer",
  "question":"…","refused":false,"elapsed_ms":3410,"steps":[{"key":"retrieve","ms":210}],
  "doc_ids":["ТЗ-ЭКЗ-СР01-Р2"],"inference_path":"nexus-link"}]
```

### `GET /api/analytics`
```json
{"total":42,"refusals":7,"refusal_rate":0.167,"avg_latency_ms":3800,"p95_latency_ms":9100,
 "top_missing":[{"topic":"лицензирование продукта X","count":4}],
 "top_sources":[{"doc_id":"ТЗ-ЭКЗ-СР01-Р2","count":12}],
 "by_role":[{"role_id":"lawyer","count":20}],
 "unanswered":[{"question":"…","ts":"…"}]}
```
`top_missing` — из отказов и пустых выдач. Это критерий «Соответствие задаче партнёра».

### `POST /api/ingest`
```json
{"ok":true,"documents":28,"chunks":430,"added":["ТЗ-ЭКЗ-0031"]}
```
Перечитывает `sample-corpus/` и перестраивает индекс. Для живого показа
«положили документ — ответ изменился».

---

## 3. События SSE

Порядок фиксирован. Каждый `step` — отдельная запись; UI рисует строку в Console Lex.

| # | `key` | Заголовок в UI | Что в `detail` |
|---|---|---|---|
| 1 | `accept` | Запрос принят | роль, дата отсчёта |
| 2 | `decompose` | Разбор вопроса | «N подвопросов» и сами подвопросы |
| 3 | `retrieve` | Поиск | «BM25 N · вектор M → топ K» |
| 4 | `permissions` | Проверка прав | «доступно X из Y, отброшено Z» |
| 5 | `resolve` | Действующая редакция | «N редакций, действует Р2 от 2026-06-01» |
| 6 | `conflict` | Сопоставление | «конфликтов: N» / «приказ сильнее регламента» |
| 7 | `compose` | Сборка ответа | «из K цитат» |
| 8 | `verify` | Самопроверка | «покрыто N из N утверждений» |

```json
event: step
data: {"n":3,"total":8,"key":"retrieve","title":"Поиск",
       "detail":"BM25 12 · вектор 10 → топ 5","ms":210,"color":"cyan"}
```

Цвета (UI мапит сам): `accept`—`slate`, `decompose`—`violet`, `retrieve`—`cyan`,
`permissions`—`amber`, `resolve`—`blue`, `conflict`—`rose`, `compose`—`emerald`,
`verify`—`green`.

### Завершение — ровно одно из двух

```json
event: answer
data: {"text":"Пять рабочих дней (Правило SR-01).",
       "citations":[{"doc_id":"ТЗ-ЭКЗ-СР01-Р2","title":"…","revision":2,
                     "effective":"2026-06-01","quote":"Срок согласования заявки: пяти рабочих дней."}],
       "refused":false}
```
```json
event: refusal
data: {"reason":"На 2025-12-01 ни одна редакция правила SR-01 не действует",
       "sources":["ТЗ-ЭКЗ-СР01-Р1"],"kind":"no_edition_in_force"}
```
`kind`: `no_edition_in_force` | `no_access` | `not_in_corpus` | `conflict_unresolved`.

Затем всегда:
```json
event: done
data: {"elapsed_ms":3410,"inference_path":"nexus-link","journal_id":"q-001"}
```

Ошибка:
```json
event: error
data: {"message":"…","stage":"retrieve"}
```

---

## 4. Роли и права (обязательны по брифу)

`src/lex/access.py`:
- роль = список отделов + список систем; `*` — все;
- документ доступен, если `doc.department ∈ role.departments` **или** `role.departments` содержит `*`;
- если после фильтра по вопросу не осталось доступных документов, **но** они есть
  без фильтра — это `refusal.kind = "no_access"`, а не «не найдено». Разница
  видна в Console и в аналитике.
- `permissions` считает и отдаёт в `detail`: сколько отброшено.

---

## 5. Модель

NexusLink на sage-pl, слот `NEXUS_LINK_MODEL` (сейчас `claude-sonnet-5`).
Переменные — из `.env` (не в коде!): `NEXUSLINK_KEY`, `NEXUS_LINK_BASE_URL`,
`NEXUS_LINK_MODEL`. Эндпоинт `POST /v1/chat/completions`, `max_tokens ≥ 1024`
(иначе ответ приходит только блоком `thinking`).

Эмбеддинги — `POST http://127.0.0.1:8081/v1/embeddings` (bge-m3, dim 1024).
Реранк — `POST http://127.0.0.1:8082/rerank` (`{query, documents}`).

Оба локальны — это аргумент к критерию «Экономика эксплуатации».

---

## 6. Журнал

`var/journal.jsonl`, одна строка = один запрос. Поля как в §2 `GET /api/journal`.
Пишется **после** ответа, атомарно (append). Агрегаты считаются по файлу,
отдельной БД нет. Путь — из конфига, не из кода.

---

## 7. Границы (что НЕ делаем)

- Аутентификация пользователей (логин/пароль) — только выбор роли.
- Мультиязычность интерфейса — русский.
- Векторная БД (Qdrant/Chroma) — индекс в памяти, корпус маленький.
- Токен-стриминг ответа модели — отдаём готовый ответ; шаги стримятся.
