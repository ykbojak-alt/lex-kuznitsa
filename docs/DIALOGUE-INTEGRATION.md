# Подключение диалога с уточнением параметров (контракт §10)

Модуль: `src/lex/dialogue.py` (MIMIC). Он самодостаточен: не знает про HTTP,
не трогает шаги ядра и читает только манифест корпуса.

## 1. Точка входа в оркестраторе

`src/lex/orchestrator.py` в репозитории ещё нет (пишет FORGE), поэтому
подключение описано словами и кодом — вставлять нужно три строки
**перед `decompose`**:

```python
from .dialogue import Dialogue

dlg = Dialogue()          # один экземпляр на процесс

async def stream(question, role_id, as_of=None, session_id=None, answers=None):
    questions = dlg.needs_clarification(
        question, role=role_id, as_of=as_of,
        session_id=session_id, answers=answers,
    )
    if questions is not None:
        # шаг clarify вместо decompose, цвет sky, total = 9
        yield ("step", dlg.clarify_step(questions))
        yield ("clarify", dlg.clarify_payload(session_id, questions, partial=partial))
        return                      # поток закрывается БЕЗ done (контракт §10)
    ctx = dlg.apply_answers(session_id, answers) if session_id else {}
    as_of = ctx.get("params", {}).get("as_of") or as_of
    ...
```

Порядок шагов при уточнении: `accept -> clarify` и конец потока. Шаг
`clarify` идёт **вместо** `decompose` (§10).

## 2. Что нужно в HTTP-слое (SPARK)

`src/api/app.py` и `src/api/core.py` — зона SPARK, я их не трогал. Нужно:

1. `AskRequest`: добавить `session_id: str | None = None` и
   `answers: dict[str, str] | None = None`.
2. `core.stream_answer(...)`: принимать `session_id` и `answers`,
   прокидывать в `lex.orchestrator.stream`.
3. Обработать событие `clarify`: отдать как `event: clarify`, **без**
   `event: done` (контракт §10: «После clarify поток закрывается»).
4. В `answer` прокинуть `assumptions` (список `{key, value, note}`) из
   `ctx["assumptions"]` — это обязательное поле ответа при пропуске.

## 3. Границы модуля

* максимум 3 вопроса за круг, максимум 2 круга на сессию, дальше — умолчания;
* сессия живёт 30 минут без активности (`var/sessions.jsonl`, TTL и путь —
  в `SessionStore`);
* пропуск («не знаю» или отсутствие ключа) всегда превращается в явное
  умолчание с пояснением — в `answer.assumptions`;
* параметры уточняются только те, без которых ответ был бы догадкой:
  `as_of`, `department`, `amount`, `doc_type`, `rule_key`.

## 4. Проверка

```
python demo/check-dialogue.py
```
