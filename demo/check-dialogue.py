"""Самопроверка модуля диалога (src/lex/dialogue.py).

Запуск из корня репозитория:

    python demo/check-dialogue.py

Проверяет то, что требует владелец: агент задаёт уточняющие вопросы там, где
без них ответ был бы догадкой, и НЕ задаёт их там, где всё уже известно.
Скрипт ничего не меняет в репозитории и пишет сессии в var/ (как и сам агент).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lex.dialogue import Dialogue, SessionStore  # noqa: E402


def _dialogue(tmp: Path) -> Dialogue:
    return Dialogue(store=SessionStore(path=tmp / "sessions.jsonl"))


def main() -> int:
    tmp = ROOT / "var"
    tmp.mkdir(parents=True, exist_ok=True)
    # Сессии из прошлого прогона делают проверку ложноположительной: счётчик
    # кругов уже исчерпан, и тест 3 падает. Чистим только свои файлы.
    for stale in ("sessions-check.jsonl", "sessions-check-skip.jsonl"):
        (tmp / stale).unlink(missing_ok=True)
    dlg = _dialogue(tmp)
    ok = True

    # 1. Неполный вопрос -> список уточнений с reason.
    questions = dlg.needs_clarification(
        "Какой срок согласования заявки?", role="lawyer", as_of=None
    )
    assert isinstance(questions, list) and questions, "нужны уточнения"
    assert all(q.reason for q in questions), "у каждого вопроса есть причина"
    assert len(questions) <= 3, "не больше трёх вопросов за раз"
    print("1. неполный вопрос ->", [q.key for q in questions])

    # 2. Полный вопрос (с датой) -> уточнений нет.
    assert dlg.needs_clarification(
        "Какой срок согласования заявки на 2026-07-01?", role="lawyer"
    ) is None, "с датой уточнять нечего"
    print("2. полный вопрос -> None")

    # 3. Два круга уточнений, третий не спрашивает.
    sid = "s-check-dialogue"
    store = SessionStore(path=tmp / "sessions-check.jsonl")
    d2 = Dialogue(store=store)
    r1 = d2.needs_clarification("Какой срок согласования заявки?",
                                role="director", session_id=sid)
    r2 = d2.needs_clarification("Какой срок согласования заявки?",
                                role="director", session_id=sid)
    r3 = d2.needs_clarification("Какой срок согласования заявки?",
                                role="director", session_id=sid)
    assert r1 and r2, "первые два круга задают вопросы"
    assert r3 is None, "третий круг не спрашивает"
    ctx = d2.apply_answers(sid, {})
    assert ctx["rounds"] == 2 and ctx["exhausted"], "круги посчитаны"
    assert any(a["key"] == "as_of" for a in ctx["assumptions"]), "as_of закрыт умолчанием"
    print("3. круги ->", ctx["rounds"], "exhausted:", ctx["exhausted"])

    # 4. Пропуск («не знаю») -> явное умолчание.
    sid2 = "s-check-skip"
    d3 = Dialogue(store=SessionStore(path=tmp / "sessions-check-skip.jsonl"))
    d3.needs_clarification("Какой срок согласования заявки?", role="lawyer",
                           session_id=sid2)
    ctx2 = d3.apply_answers(sid2, {"as_of": "не знаю"})
    assumption = next(a for a in ctx2["assumptions"] if a["key"] == "as_of")
    assert assumption["value"] and assumption["note"], "умолчание с пояснением"
    print("4. пропуск ->", json.dumps(assumption, ensure_ascii=False))

    # 5. Сессия переживает перезапуск.
    again = SessionStore(path=tmp / "sessions-check.jsonl").get(sid)
    assert again is not None and again.rounds == 2, "сессия сохранена"
    print("5. персистентность ->", again.session_id, "rounds:", again.rounds)

    print("OK: диалог с уточнением параметров работает")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
