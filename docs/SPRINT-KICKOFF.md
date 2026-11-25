# ИИкузница — чек-лист открытия спринта (26.11 10:00 МСК)

**Статус:** PREP phase — выполнение ровно в 10:00 при открытии спринта.

---

## 1. Жёсткие требования (GO/NO-GO)

| # | Проверка | GO критерий | Как проверить | Ответственный |
|---|---|---|---|---|
| 1 | **Локальная модель загружена** | `curl http://127.0.0.1:8080/v1/models` → `{"data":[{"id":"Qwen3-8B-Instruct-Q4_K_M",...}]}` | FORGE запускает llama.cpp server заранее (26.11 09:00) | FORGE |
| 2 | **VRAM ≤ 7.5 GB** | `nvidia-smi` показывает занято ≤ 7.5 GB из 8 GB (GPU 0) | `nvidia-smi --query-gpu=memory.used,memory.total --format=csv` | FORGE |
| 3 | **Dev-датасет не пустой** | `sample-corpus/` содержит ≥ 20 синтетических документов с полями: `doc_id`, `title`, `level`, `effective_date`, `revision`, `content`, `cancels[]`, `priority_over[]` | `python -c "import json; print(len(json.load(open('sample-corpus/corpus.json'))))"` | CODEX |
| 4 | **Git remote настроен** | `git remote -v` → `origin` указывает на публичный GitHub репо (org/репо) | `git remote -v` | FORGE |
| 5 | **Репо публично** | GitHub Settings → Pages/Visibility → Public | Открыть URL репо в инкогнито — читается README | FORGE |
| 6 | **LLM-эндпоинты подняты** | embedding (8081) + reranker (8082) отвечают 200 | `curl -s http://127.0.0.1:8081/health` и `curl -s http://127.0.0.1:8082/health` | SPARK |

---

## 2. Первые 2 часа (26.11 10:00–12:00) — распределение

| Время | Агент | Задача | Готово, когда |
|---|---|---|---|
| 10:00–10:15 | **ALL** | Брифинг (Telegram/голос): ТЗ заказчика, формат корпуса, дедлайны, роли | все в курсе |
| 10:15–10:30 | **FORGE** | `git pull` свежий TZ-ветка; запуск dev-стенда `python -m uvicorn src.main:app --port 8874` | `curl /health` → 200 |
| 10:15–11:00 | **CODEX** | Ingest pipeline: парсер формата ТЗ → чанки → BM25 индекс (whoosh/rank-bm25) + векторный (FAISS/pgvector) | `python -m ingest` → `indexes/` созданы |
| 10:15–11:00 | **SPARK** | Retrieval API: BM25 + vector + rerank + фильтры; эндпоинт `/api/retrieve` | `curl /api/retrieve?q=...` → top-K |
| 10:15–11:00 | **ARCH** | Orchestrator скелет: tool-schema (decompose, retrieve, resolve, conflict, compose, verify), журнал (JSONL) | `orchestrator.py` импортируется |
| 11:00–11:30 | **ALL** | Интеграция: ingest → retrieval → orchestrator; прогон 3 вопросов из мини-датасета | 3 вопроса → непустая выдача |
| 11:30–12:00 | **CODEX** | Логика «действующего документа» (§4 ARCHITECTURE): приоритеты, effective_date, revision, cancels, priority_over | `resolve.py` unit-tests pass |
| 11:30–12:00 | **SPARK** | Profile `local` config: base_url, model names для 3-х эндпоинтов; проверка fallback | `config.local.json` валиден |

---

## 3. Ключевые решения, которые нужно принять в 10:00 (если не приняты до)

1. **Название модели в описании продукта** (Q9) — Qwen3-8B-Instruct GGUF Q4_K_M (финальное, после просмотра объёма корпуса).
2. **Профиль для видео и демо жюри** — `local` (подтвердить, что снимаем на локальной модели).
3. **Открывать ли репозиторий в описании до питча** — по итогам брифинга организатора.

---

## 4. Команды для быстрой проверки (скопировать в терминал)

```bash
# 1. Модель
curl -s http://127.0.0.1:8080/v1/models | jq .

# 2. VRAM
nvidia-smi --query-gpu=memory.used,memory.total --format=csv

# 3. Датасет
python -c "import json; d=json.load(open('sample-corpus/corpus.json')); print(f'{len(d)} docs, keys={set().union(*[set(x.keys()) for x in d])}')"

# 4. Git remote
git remote -v

# 5. Репо публично
# Открыть https://github.com/<org>/<repo> в инкогнито

# 6. Embedding + reranker
curl -s http://127.0.0.1:8081/health && curl -s http://127.0.0.1:8082/health
```

---

## 5. Эскалация (если GO не пройден)

| Сбой | Реакция (≤ 15 мин) |
|---|---|
| Модель не поднимается | Переключиться на Qwen3-4B fallback (3 GB), обновить MODEL.md, снять видео на `dev`, честно в описании |
| VRAM > 7.5 GB | `--n-gpu-layers 28` (меньше слоёв на GPU), `--ctx-size 4096` |
| Датасет пустой | CODEX: `python demo/gen-sample-corpus.py --n 30` → синтетика за 2 мин |
| Git remote не настроен | `git remote add origin https://github.com/<org>/iikuznitsa-sprint.git` |
| Репо не публично | GitHub Settings → Danger Zone → Change visibility → Public |

---

**Владелец чек-листа:** FORGE. Подпись: `nx send t1 "SPRINT-KICKOFF GO/NO-GO: <результат>"` в 10:15.