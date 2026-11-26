"""Самопроверка модуля диалога (src/lex/dialogue.py).

Запуск из корня репозитория:

    python demo/check-dialogue.py

Проверяет то, что требует владелец:
  * §10 — агент задаёт уточняющие вопросы там, где без них ответ был бы
    догадкой, и НЕ задаёт их там, где всё уже известно; пропуск даёт явное
    умолчание;
  * §15 (v1.7) — 2-3 уточняющих итерации, потом эскалация в службу качества;
    вопрос «вне базы» эскалируется сразу, без бесполезных уточнений.

Скрипт ничего не меняет в репозитории: сессии и заявки пишутся в var/.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lex.dialogue import (  # noqa: E402
    MAX_CLARIFY_ITERATIONS,
    MIN_CLARIFY_ITERATIONS,
    Dialogue,
    SessionStore,
)

VAR = ROOT / "var"


def _dialogue(tag: str) -> Dialogue:
    """Свой файл сессий на каждый сценарий: состояние прошлого прогона
    делает проверку ложноположительной (счётчик итераций уже исчерпан)."""
    return Dialogue(store=SessionStore(path=VAR / f"sessions-{tag}.jsonl"))


def main() -> int:
    VAR.mkdir(parents=True, exist_ok=True)
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
    assert dlg.needs_clarification(
        "Какой срок согласования заявки на 2026-07-01?", role="lawyer"
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

    print("OK: диалог с уточнением и эскалацией работает")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
