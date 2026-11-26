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

## 5. §15 (v1.7): 2-3 итерации и эскалация

Контракт v1.7 уточняет §10: уточняющих итераций должно быть **2..3**, а после них —
не отказ, а **заявка в службу качества**.

Модуль диалога даёт всё нужное:

```python
dlg = Dialogue()

# 1) после неудачного поиска — решение: ещё итерация или эскалация
decision = dlg.after_search(
    session_id, question, role_id,
    searched={"documents": 27, "cases": 4, "accessible": 5, "top": ["ТЗ-ЭКЗ-СР01-Р2"]},
    kind="not_in_corpus",
)
if decision.questions:
    yield ("clarify", ...)            # ещё одна итерация уточнения
else:
    ticket = decision.escalate        # заявка в службу качества
    yield ("escalate", dlg.escalate_event(ticket))
```

Правила, зашитые в модуль:

* **вопрос вне базы** (про НДС, про практику других компаний) → эскалация сразу,
  без уточнений: уточнять нечего;
* **вопрос неполный** → уточняем; итераций 2..3, не больше;
* на исчерпании итераций → заявка в службу качества, а не отказ;
* заявка обязана содержать: что спрашивали, что ответили, что искали и
  **почему не решилось** — иначе она бесполезна службе качества.

Заявки пишутся в `var/escalations.jsonl` (номера `esc-0001`, `esc-0002`, …),
событие для сотрудника — `dlg.escalate_event(ticket)`:

```json
{"ticket": "esc-0003", "iterations": 3,
 "reason": "в корпусе нет документа по теме «согласовать закупку, если поставщик единственный»",
 "searched": {"documents": 27, "cases": 4, "accessible": 0, "top": []},
 "next": "Заявка передана в службу качества. Ответ появится в базе как кейс.",
 "eta": "до 2 рабочих дней"}
```

## 6. Шаг escalate в потоке

Шаг эскалации — **последний, вместо `verify`**, цвет `orange`:

```python
yield Signal("step", self._escalate_step(len(STEP_ORDER), ticket))
yield Signal("escalate", self.dialogue.escalate_event(ticket))
yield Signal("done", self._done())
```

Отказ policy (`out_of_scope`, `no_access`) при этом **не отдаётся сотруднику как
отказ**: по §15.6 отказом эскалацию подменять нельзя — заявка уходит в службу
качества, а сотрудник видит номер заявки, что уже проверено, что дальше и срок.

## 7. §17: направление неопределимо — спрашиваем его

Policy (`src/lex/policy.py`) умеет вернуть `need_clarify`: тема в компетенции базы,
но направление определить не удалось. Дальше искать нечего — направление нужно
спросить. Модуль диалога это умеет:

```python
questions = dlg.needs_clarification(
    question, role=role_id, need_direction=True, session_id=sid,
)
# -> первый вопрос: business_line, kind="choice", options = направления базы
```

Варианты берутся из `lex.policy.BUSINESS_LINES` (данные), а если policy нет — из
систем документов корпуса. Назвал сотрудник направление — оно пишется в
`params["business_line"]` и повторно не спрашивается.

Тот же вопрос можно построить отдельно:

```python
question = dlg.direction_question()          # или dlg.direction_question(lines)
```

## 8. §21: уточняющие вопросы — простым языком

Уточняющий вопрос тоже должен звучать по-человечески, но:

* **реквизиты не переформулируются**: ключ правила (`SR-01`) и даты редакций
  (`2026-01-15`, `2026-06-01`, `2027-01-01`) переносятся дословно из манифеста —
  они и есть причина, по которой спрашивают;
* **причина остаётся** (`reason`): «без даты нельзя понять, какая действует»;
* **канцелярит убран**: «На какую дату определить действующую редакцию?» →
  «На какую дату считаем?». Список запрещённых оборотов — `FORMAL_MARKERS` в
  `lex.dialogue`; самопроверка `demo/check-dialogue.py` следит, чтобы он не
  вернулся;
* оговорки и отказы (§15, §20) — наоборот, всегда строго: смягчать предупреждение
  нельзя.
