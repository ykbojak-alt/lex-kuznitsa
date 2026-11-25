# ИИкузница — модельный слой (Q9)

**Лицензия нашего кода: MIT** (см. `LICENSE`). Внешние компоненты ниже несут свои
лицензии — они не наши и не перелицензируются.

## Маршрутизация: основной путь и fallback

| Профиль | Что это | Когда используется | Статус |
|---|---|---|---|
| **`dev`** | **NexusLink на sage-pl** — внешний LLM-шлюз (Anthropic/OpenAI-совместимый вход). | **Основной путь** разработки в спринте и прогонов качества. | основной |
| **`local`** | **Qwen3-8B-Instruct GGUF Q4_K_M** на нашей машине (llama.cpp server). | **Fallback**: если sage-pl/NexusLink недоступен; демонстрация в изолированном контуре (Q11). | fallback |
| **`mock`** | Детерминированная заглушка без сети. | Тесты, CI. | служебный |

**Правило:** профиль выбирается **конфигом**, не кодом — один и тот же клиент
(`/v1/messages` или `/v1/chat/completions`). Переключение `dev` ↔ `local` не
требует правок в приложении.

### Адреса и переменные

- **NexusLink (sage-pl):** `NEXUS_LINK_BASE_URL` — базовый URL (в репозиторий не
  попадает). `NEXUS_LINK_MODEL` — слот модели; задавать **явно**, т.к. дефолт
  `demo_stand.py` (`claude-sonnet-5`) на sage-pl отдаёт 502 (см. `docs/LIMITS.md`).
  Публичный LLM-вход шлюза — `:4435` за Caddy; локальный listener — `127.0.0.1:5679`.
- **Токен:** `incoming_token` из `~/.nexuslink/config.json`; передаётся клиентом
  как `x-api-key`/Bearer. Только через конфиг/env, в git не попадает (`.env` в
  `.gitignore`).
- **Локальный fallback:** `LOCAL_MODEL_BASE_URL` (по умолчанию
  `http://127.0.0.1:8080/v1`), `LOCAL_MODEL_NAME` (по умолчанию
  `Qwen3-8B-Instruct-Q4_K_M`).

Владелец NexusLink — M.E.R.C.; доступ и ключи выдаются им, не вшиваются в код.

## Локальная модель (fallback, Q9)

**Модель:** Qwen3-8B-Instruct, GGUF Q4_K_M
**Размер:** ~5 GB
**Лицензия (внешняя):** Apache-2.0 — это лицензия веса, не нашего кода
**Источник:** https://huggingface.co/Qwen/Qwen3-8B-Instruct-GGUF

### Требования к железу

- **GPU:** NVIDIA RTX 3050 (8 GiB VRAM).
- **RAM:** 31.9 GiB (наша конфигурация i7-3770).
- **OS:** Linux (предпочтительно) / Windows (llama.cpp).

### Как поднять

```bash
huggingface-cli download Qwen/Qwen3-8B-Instruct-GGUF Qwen3-8B-Instruct-Q4_K_M.gguf --local-dir ./models

./llama-server -m models/Qwen3-8B-Instruct-Q4_K_M.gguf \
  --host 127.0.0.1 --port 8080 \
  --ctx-size 8192 --threads 8 --n-gpu-layers 35 \
  --mlock --no-mmap
```

Проверка: `curl http://127.0.0.1:8080/v1/models`.

### Порог VRAM — почему 7.5 GB, а не 8.0

Раскладка при `--ctx-size 8192`:

| Статья | Оценка |
|---|---|
| Веса Q4_K_M | ~5.0 GB |
| KV-кэш (36 слоёв, 8 KV-heads, head_dim 128, f16) | ~1.2 GB |
| Compute buffer + CUDA context + фрагментация | ~0.5–0.8 GB |
| **Итого** | **~6.7–7.0 GB** |

Порог **7.5 GB** оставлен намеренно: 0.5 GB запаса закрывают фрагментацию
аллокатора и пики при длинном контексте. При `nvidia-smi` > 7.5 GB — снижаем
`--ctx-size` до 4096 (KV падает до ~0.6 GB) или `--n-gpu-layers` до 28 (часть
слоёв на CPU). Это защищает от OOM посреди демо, а не ограничивает модель
искусственно.

**Примечание:** с переходом на NexusLink как основной путь (см. выше) VRAM
перестаёт быть критическим путём — она важна только для fallback-сценария.

## Embedding и reranker

| Роль | Модель | Размер | Лицензия (внешняя) |
|---|---|---|---|
| Embedding | bge-m3 | ~2 GB (FP16) / ~0.5 GB (ONNX int8) | Apache-2.0 |
| Reranker | bge-reranker-base | ~1 GB | Apache-2.0 |

Оба поднимаются локально независимо от профиля LLM (`:8081`, `:8082`).

## Fallback-модель второго уровня

Если Qwen3-8B не помещается и по VRAM, и по времени: **Qwen3-4B-Instruct GGUF
Q5_K_M** (~3 GB, Apache-2.0) — влезает в 6 GB с запасом.

---

**Итог (Q9):** в описании продукта фиксируется основная модель — **NexusLink
(sage-pl)**; локальная **Qwen3-8B-Instruct GGUF Q4_K_M** заявляется как fallback
и как модель для демонстрации в изолированном контуре (Q11).
