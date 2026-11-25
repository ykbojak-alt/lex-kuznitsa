# ИИкузница — чек-лист открытия спринта (26.11 10:00 МСК)

**Статус:** PREP phase — выполнение ровно в 10:00 при открытии спринта.
**План по часам:** `docs/PLAN-48H.md` (48 часов, гейты 4/12/24/40/48).

---

## 1. Жёсткие требования (GO/NO-GO)

| # | Проверка | GO критерий | Как проверить | Ответственный |
|---|---|---|---|---|
| 1 | **NexusLink (sage-pl) доступен** — основной путь | `GET $NEXUS_LINK_BASE_URL/models` отвечает 200 (401/403 = шлюз жив, нужен токен) и `POST /v1/messages` даёт непустой ответ | `sh demo/run-local.sh --require-inference` (проверяет оба пути) | FORGE |
| 2 | **Слот модели задан явно** | `$NEXUS_LINK_MODEL` непустой и не равен дефолту `claude-sonnet-5` (на sage-pl = 502) | `echo "$NEXUS_LINK_MODEL"` | FORGE |
| 3 | **Fallback: локальная Qwen3-8B** | `curl http://127.0.0.1:8080/v1/models` → `Qwen3-8B-Instruct-Q4_K_M` | `curl -s http://127.0.0.1:8080/v1/models` | FORGE |
| 4 | **VRAM ≤ 7.5 GB** (только для fallback) | `nvidia-smi` занято ≤ 7.5 из 8 GB | `nvidia-smi --query-gpu=memory.used,memory.total --format=csv` | FORGE |
| 5 | **Dev-датасет не пустой** | `sample-corpus/` — корпус собран, вопросов ≥ 20 | `sh demo/run-local.sh` проходит preflight | CODEX |
| 6 | **Git remote настроен** | `origin` → `github.com/ykbojak-alt/iikuznitsa-sprint` | `git remote -v` | FORGE |
| 7 | **Репо публично** (перед сдачей) | README читается в инкогнито | открыть URL репо | FORGE |
| 8 | **Embedding + reranker** | `:8081` и `:8082` отвечают 200 | `curl -s http://127.0.0.1:8081/health && curl -s http://127.0.0.1:8082/health` | SPARK |

**Логика:** пункты 1–2 — основной путь, без них спринт не начинается (но не
блокируют: сразу включаем fallback 3–4). Пункты 5–8 — обязательны к 10:15.

---

## 2. Первые 2 часа (26.11 10:00–12:00)

| Время | Агент | Задача | Готово, когда |
|---|---|---|---|
| 10:00–10:15 | **ALL** | Брифинг: ТЗ заказчика, формат корпуса, ответы на QA-QUESTIONS.md, роли | ответы зафиксированы в `docs/QA-QUESTIONS.md` |
| 10:15–10:30 | **FORGE** | Проверка GO/NO-GO §1, фиксация профиля `dev`, запуск демо-стенда | `nx send` GO-отчёт |
| 10:15–11:00 | **CODEX** | Ingest: парсер формата ТЗ → чанки → BM25 + векторный индекс | индекс строится, поиск возвращает чанки |
| 10:15–11:00 | **SPARK** | Retrieval API: BM25 + vector + rerank + фильтры | `curl /api/retrieve?q=...` → top-K |
| 10:15–11:00 | **ARCH** | Orchestrator скелет: tool-schema, JSONL-журнал | `orchestrator.py` импортируется |
| 11:00–11:30 | **ALL** | Интеграция ingest → retrieval → orchestrator; 3 вопроса из датасета | непустая выдача + 3 шага в журнале |
| 11:30–12:00 | **CODEX** | Логика «действующего документа» (§4 ARCHITECTURE) | unit-тесты resolve проходят |
| 11:30–12:00 | **SPARK** | Конфиг профилей `dev`/`local`, проверка переключения | оба профиля отвечают |

---

## 3. Ключевые решения на 10:00

1. **Модель в описании продукта** (Q9) — основная: **NexusLink (sage-pl)**; локальная Qwen3-8B — fallback и изолированный контур.
2. **Профиль для видео** — `dev` (NexusLink); если организатор требует изоляцию — `local`.
3. **Публичность репозитория** — по итогам брифинга.

---

## 4. Команды проверки

```bash
# 1-2. NexusLink (основной путь) + слот модели
sh demo/run-local.sh --require-inference
echo "$NEXUS_LINK_BASE_URL / $NEXUS_LINK_MODEL"

# 3-4. Fallback: модель и VRAM
curl -s http://127.0.0.1:8080/v1/models
nvidia-smi --query-gpu=memory.used,memory.total --format=csv

# 5. Корпус
sh demo/run-local.sh

# 6-7. Git
git remote -v

# 8. Embedding + reranker
curl -s http://127.0.0.1:8081/health && curl -s http://127.0.0.1:8082/health
```

---

## 5. Эскалация (≤ 15 мин)

| Сбой | Реакция |
|---|---|
| **NexusLink недоступен** | включить профиль `local` (Qwen3-8B, `:8080`), проверить VRAM; работать на fallback, доложить в отчёте |
| Ключа шлюза нет | запросить у M.E.R.C. (владелец NexusLink), до ответа — `local` |
| VRAM > 7.5 GB | `--ctx-size 4096`, затем `--n-gpu-layers 28`; если не помогло — Qwen3-4B |
| Корпус пуст | `python demo/gen-sample-corpus.py --count 20 --seed 20261126 --clean` |
| Git remote нет | `git remote add origin https://github.com/ykbojak-alt/iikuznitsa-sprint.git` |
| Репо приватно | Settings → Change visibility → Public (перед сдачей) |

---

**Владелец:** FORGE. Подпись GO/NO-GO — в 10:15 ответом ALICE.
