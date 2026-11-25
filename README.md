# IIKUZNITSA — проект спринта

**Название:** IIKUZNITSA (ИИкузница) — платформа вопросов/ответов на базе RAG.

**1 предложение:** Один агент принимает сложный вопрос команды и собирает из
нескольких экспертов и документов рабочий «пакет решения» — маршрутизация,
проверка, документирование.

**Стек:** Python/FastAPI backend + LLM + RAG + опрос голосов команд (swarm).

**Статус:** PREP phase — проект структурирован, код продукта не пишется до старта
спринта 26.11 10:00.

## 5-командный старт

```bash
# 1. Структура и скелет
mkdir -p src docs demo sample-corpus && touch src/__init__.py docs/.gitkeep demo/.gitkeep sample-corpus/.gitkeep

# 2. Конфиг окружения
cp -r /opt/iikuznitsa/env.example .env

# 3. Установка зависимостей
pip install -r requirements.txt

# 4. Запуск стенда
python -m uvicorn src.main:app --port 8874

# 5. Smoke
curl http://localhost:8874/health && curl http://localhost:8874/docs
```

## Скриншот

![placeholder](docs/assets/screenshot-placeholder.png)

> Placeholder: скриншот стенда будет положен в `docs/assets/` после старта спринта.

## Лицензия

MIT — см. `LICENSE`.
