"""Самопроверка модуля диалога (src/lex/dialogue.py).

Запуск из корня репозитория:

    python demo/check-dialogue.py

Проверяет то, что требует владелец:
  * §10 — агент задаёт уточняющие вопросы там, где без них ответ был бы
    догадкой, и НЕ задаёт их там, где всё уже известно; пропуск даёт явное
    умолчание;
  * §15 (v1.7) — 2-3 уточняющих итерации, потом эскалация в службу качества;
    вопрос «вне базы» эскалируется сразу, без бесполезных уточнений;
  * §17 — направление определить не удалось: спрашиваем его первым;
  * §21 — уточняющие вопросы простым языком, но реквизиты нормы дословно.

Скрипт ничего не меняет в репозитории: сессии и заявки пишутся в var/.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lex.dialogue import (
    FORMAL_MARKERS,
    MAX_CLARIFY_ITERATIONS,
    MIN_CLARIFY_ITERATIONS,
    Dialogue,
    SessionStore,
)

VAR = ROOT / "var"


def _dialogue(tag: str) -> Dialogue:
    """Свой файл сессий на каждый сценарий: состояние прошлого прогона
    делает проверку ложноположительной (счётчик итераций уже исчерпан).

    Корпус — ЯВНО `sample-corpus`: сценарии этого файла построены на его
    правилах (СР-01, «закупки», ревизии-редакции), которых в реальных
    документах нет. После перехода рантайма на `corpus-real` (LEX-SINGLE-
    CORPUS) `Dialogue` по умолчанию читает реальный корпус, и проверки
    молча перестали проверять то, что заявляют: первая же падала на «нужны
    уточнения», потому что вопроса про закупки в реальной базе не бывает.
    """
    return Dialogue(store=SessionStore(path=VAR / f"sessions-{tag}.jsonl"),
                    corpus_dir=ROOT / "sample-corpus")


def main() -> int:
    VAR.mkdir(parents=True, exist_ok=True)
    # Сессии прошлого прогона исчерпывают счётчик кругов и делают проверку
    # ложноположительной. Чистим только свои файлы.
    for stale in VAR.glob("sessions-check*.jsonl"):
        stale.unlink(missing_ok=True)
    dlg = _dialogue("check")

    # --- §10: неполный вопрос -> список уточнений с reason.
    questions = dlg.needs_clarification(
        "Какой срок согласования заявки?", role="lawyer", as_of=None
    )
    assert isinstance(questions, list) and questions, "нужны уточнения"
    assert all(q.reason for q in questions), "у каждого вопроса есть причина"
    assert len(questions) <= 3, "не больше трёх вопросов за раз"
    print("1. неполный вопрос ->", [q.key for q in questions])

    # --- §10: полный вопрос (с датой) -> уточнений нет.
    # Роль — `technical`: она закреплена за одним подразделением фикстуры
    # («Техническая дирекция»), поэтому вопрос «какое подразделение?» не
    # задаётся. `director` и `quality` сквозные — для них такой вопрос
    # законен, и ожидание «уточнять нечего» на них не годится.
    tech = "technical"
    assert dlg.needs_clarification(
        "Какой срок согласования заявки на 2026-07-01?", role=tech
    ) is None, "с датой уточнять нечего"
    print("2. полный вопрос -> None")

    # --- §10: пропуск («не знаю») -> явное умолчание.
    sid = "s-check-skip"
    d2 = _dialogue("check-skip")
    d2.needs_clarification("Какой срок согласования заявки?", role="lawyer",
                           session_id=sid)
    ctx = d2.apply_answers(sid, {"as_of": "не знаю"})
    assumption = next(a for a in ctx["assumptions"] if a["key"] == "as_of")
    assert assumption["value"] and assumption["note"], "умолчание с пояснением"
    print("3. пропуск ->", json.dumps(assumption, ensure_ascii=False))

    # --- §10: сессия переживает перезапуск.
    again = SessionStore(path=VAR / "sessions-check-skip.jsonl").get(sid)
    assert again is not None, "сессия сохранена"
    print("4. персистентность ->", again.session_id)

    # --- §15: вопрос вне базы -> эскалация сразу, без уточнений.
    d3 = _dialogue("check-outside")
    decision = d3.after_search(
        "s-check-outside", "Какая ставка НДС?", role="lawyer",
        searched={"documents": 27, "cases": 0, "accessible": 0, "top": []},
    )
    assert decision.out_of_base, "вопрос вне базы распознан"
    assert decision.escalate and decision.questions is None, "сразу эскалация"
    ticket = decision.escalate
    assert ticket["why_unresolved"], "заявка обязана объяснять, почему не решилось"
    assert ticket["status"] == "open"
    print("5. вне базы -> эскалация", ticket["id"], "|", ticket["why_unresolved"])

    # --- §15: неполный вопрос -> итерации уточнения, потом эскалация.
    d4 = _dialogue("check-iter")
    sid4 = "s-check-iter"
    first = d4.needs_clarification(
        "Как согласовать закупку, если поставщик единственный?",
        role="lawyer", session_id=sid4,
    )
    assert first, "первая итерация задаёт вопрос"
    d4.apply_answers(sid4, {"as_of": "2026-07-01"})
    iterations = 1
    while True:
        decision = d4.after_search(
            sid4, "Как согласовать закупку, если поставщик единственный?",
            role="lawyer",
            searched={"documents": 27, "cases": 0, "accessible": 0, "top": []},
        )
        if decision.escalate:
            break
        assert decision.questions, "решение без вопросов и без эскалации"
        iterations += 1
        assert iterations <= MAX_CLARIFY_ITERATIONS, "итераций больше лимита"
        d4.apply_answers(sid4, {decision.questions[0].key: "ответ сотрудника"})
    assert MIN_CLARIFY_ITERATIONS <= iterations <= MAX_CLARIFY_ITERATIONS, iterations
    ticket4 = decision.escalate
    assert len(ticket4["clarifications"]) == iterations, "в заявке все итерации"
    assert all(c["asked"] and c["answers"] for c in ticket4["clarifications"]), \
        "в заявке есть что спросили и что ответили"
    print(f"6. итераций уточнения -> {iterations}, затем эскалация {ticket4['id']}")

    # --- §15: событие escalate для сотрудника.
    event = d4.escalate_event(ticket4)
    assert event["ticket"] == ticket4["id"]
    assert event["iterations"] == iterations
    assert event["reason"] and event["next"] and event["eta"]
    print("7. событие escalate ->", json.dumps(event, ensure_ascii=False)[:160])

    # --- §21: вопросы простым языком, реквизиты — дословно.
    d5 = _dialogue("check-tone")
    plain = d5.needs_clarification(
        "Какой срок согласования заявки?", role="lawyer", as_of=None
    )
    assert plain, "есть уточняющий вопрос"
    for q in plain:
        for marker in FORMAL_MARKERS:
            assert marker not in q.text.lower(), f"канцелярит в вопросе: {marker}"
            assert marker not in q.reason.lower(), f"канцелярит в причине: {marker}"
    as_of_q = next(q for q in plain if q.key == "as_of")
    rule = d5.knowledge.rules["SR-01"]
    for effective in rule.effective_dates:
        assert effective in as_of_q.reason, f"дата {effective} потеряна в причине"
    assert rule.key in as_of_q.reason, "ключ правила потерян в причине"
    print("8. §21 простой язык ->", as_of_q.text, "| реквизиты целы")

    # --- §17: направление определить не удалось -> спрашиваем первым.
    d6 = _dialogue("check-direction")
    sid6 = "s-check-direction"
    direction = d6.needs_clarification(
        "Какой срок согласования заявки?", role="lawyer", need_direction=True,
        session_id=sid6,
    )
    assert direction and direction[0].key == "business_line", \
        "вопрос о направлении должен быть первым"
    assert direction[0].options, "у вопроса о направлении есть варианты"
    print("9. §17 направление ->", direction[0].text, direction[0].options)

    # направление названо -> про него больше не спрашиваем
    d6.apply_answers(sid6, {"business_line": "Закупки"})
    again = d6.needs_clarification(
        "Какой срок согласования заявки?", role="lawyer", need_direction=True,
        session_id=sid6,
    )
    assert again is None or all(q.key != "business_line" for q in again), \
        "направление спросили повторно"
    print("10. §17 направление названо -> повторно не спрашиваем")

    print("OK: диалог с уточнением и эскалацией работает")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
