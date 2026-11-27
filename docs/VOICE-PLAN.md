# LEX: голос и Telegram — план на 28.11.2026

Составлен 28.11.2026 01:15 MSK. Дедлайн сдачи — 28.11.2026 10:00 MSK (8 ч 45 мин).

## Что уже есть (разведка, не предположения)

| Компонент | Факт |
|---|---|
| STT | `/opt/stt-svc/server.py`, faster-whisper **small**, CPU/int8, `:19786`, `/health` + `POST /stt` (multipart `audio`, Ogg/Opus → WAV через ffmpeg). Модель 464 МБ в `.hf-cache`. **Сервис остановлен** 26.11 15:13 UTC |
| Telegram | `/opt/tg-bridge-bot`, **aiogram 3.31.0**, токен `MentalConstuct_bot` (getMe ok), вебхук пуст, pending 0, `tgbot.service` inactive → токен свободен |
| Fish | `/d/tools/video-toolkit/scripts/fish-tts.mjs` + `fish-audio.json` (модель `s2.1-pro-free`, голос `5d8e4d17f1174094aebb5d77bf05de74`), ключ в `secrets/fish-api-key.dpapi` (только roy1). С sage-pl `https://api.fish.audio` отвечает 404 на `/` — хост живой |
| Стенд | `lex-stand.service` :8090 за Caddy :4437, 2 ядра, load 0.06. Эндпоинты: `/api/ask` (SSE), `/api/health`, `/api/analytics` |
| Токены LEX | `NEXUS_LINK_BASE_URL`, `LEX_CORPUS_DIR`, `LEX_EMBED_URL` … Fish-ключ в окружении LEX **отсутствует** |

## Архитектура: три точки входа, один `/api/ask`

```
Telegram (текст)  ──┐
Telegram (голос)  ──┼─→ lex-bot (aiogram polling, sage-pl)
                   │      │
                   │      ├─ голос → POST /api/stt (LEX) → STT :19786
                   │      │
                   │      └─ текст → POST /api/ask (SSE) → ответ
                   │             └─ кнопка «Озвучить» → POST /api/tts → Fish → mp3
                   │
Web UI ────────────┴─→ те же /api/stt и /api/tts, микрофон и кнопка в клиенте
```

Ключевое: **вся логика в LEX, бот — тонкий клиент**. Тогда голос работает и в вебе,
и в боте, и в будущем в чём угодно, а не только в Telegram.

## Задачи

### T1. Поднять STT (ALICE, 5 мин)
`systemctl start alice-stt.service` → `/health` → живой `POST /stt` на тестовом ogg.
Замерить CPU: на 2 ядрах faster-whisper small в int8 держит ~1 с на 5 с речи — приемлемо,
но запускать **только ALICE** (правило 27.11).

### T2. LEX API: `/api/stt` + `/api/tts` (SPARK, ~1.5 ч)
Новый `src/api/voice.py`:
- `POST /api/stt` — multipart `audio` → STT :19786 → `{"text": "..."}`.
- `POST /api/tts` — `{"text": "..."}` → `api.fish.audio/v1/tts` → `audio/mpeg`.
- Конфиг: `LEX_STT_URL` (дефолт `http://127.0.0.1:19786`), `FISH_API_KEY`,
  `FISH_REFERENCE_ID`, `FISH_MODEL` — всё из окружения, значений в коде нет.
- Обе ручки — с таймаутом и понятной ошибкой: стенд без STT обязан отвечать
  `503 {"error":"stt_unavailable"}` и работать дальше, а не падать.

### T3. Telegram-бот (SPARK → FORGE, ~2 ч)
`/opt/lex-bot/` на sage-pl, `lex-bot.service`:
- aiogram 3 polling (вебхук не нужен — Caddy :4437 занят стендом).
- Текст → `/api/ask`, стрим читается как SSE: показываем **шаги каскада**
  (`accept → policy → … → verify`) в одном редактируемом сообщении, затем ответ.
- Голосовое → `POST /api/stt` → тот же путь.
- Inline-кнопки под ответом: «Озвучить» (`/api/tts`), «Источники», «Новый вопрос».
- Роли: `role_id` из Telegram user id по белому списку `LEX_BOT_ROLES` (JSON в env);
  по умолчанию `technical`. Права на чтение единые (§23), но аналитика должна знать, кто спрашивал.

### T4. Fish TTS + кнопка в вебе (SPARK, ~1 ч)
Серверный `/api/tts` (из T2) + в `src/web/app.js` кнопка «Озвучить» у готового ответа:
`fetch('/api/tts')` → `new Audio(URL.createObjectURL(blob))`. Ключ Fish — `/etc/lex-voice.env`,
права 600, в git не попадает, в отчётах только имя переменной.

### T5. Калькулятор: нижняя таблица (ALICE, 30 мин)
Дефект A21:I25: слот шлюза и модель в одной строке без пояснения; `0.225` в F22 против
`0.23` в F9 для той же цены DeepSeek; «средняя по трём слотам 0.2139» подана как стоимость
запроса, хотя запрос идёт в **один** слот. Правка: колонка `A` = слот, `B` = модель,
явный подзаголовок «если запрос уйдёт в этот слот»; цены DeepSeek привести к одному
значению с F9; средняя переименована в «ориентир по трём слотам», рядом — фактическая
0.17 ₽ для работающего слота.

## Порядок и риск

| Шаг | Кто | Время | Риск для сдачи |
|---|---|---|---|
| T1 STT | ALICE | 5 мин | нет, отдельный сервис |
| T2 API | SPARK | 1.5 ч | низкий, аддитивный модуль |
| T4 Fish | SPARK | 1 ч | низкий |
| T3 бот | FORGE | 2 ч | средний: новый сервис на 2 ядрах |
| T5 таблица | ALICE | 30 мин | нет |

Запись видео — на текущем стенде — **не блокируется** этими работами: `/api/ask` никто
не трогает, новые ручки только добавляются. Если к 05:00 MSK бот не готов, видео
записывается без него, а голос показывается в вебе (микрофон + Fish).

## Откат

- STT: `systemctl stop alice-stt` — стенд продолжает работать (деградация до
  `503 stt_unavailable`).
- `/api/voice.py`: `git revert` коммита; одна строка в `app.py`.
- Бот: `systemctl stop lex-bot`, токен возвращается в пул.
