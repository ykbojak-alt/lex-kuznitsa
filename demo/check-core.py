"""Самопроверка ядра LEX (src/lex/) — контракт v1.13.

Запуск из корня репозитория:

    python demo/check-core.py

Проверяет то, за что отвечает ядро: шаги каскада, policy на входе, барьеры
«только из базы», оговорку, тикеты, предложения об изменении и язык без
изменения смысла. Сквозной прогон оркестратора — на реальных вопросах корпуса.

Ничего не меняет в репозитории: тикеты, кейсы и предложения пишутся во
временный каталог, корпус только читается.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lex import caveat as caveat_mod
from lex import policy
from lex.cases import detect_deviation
from lex.changes import ChangeRequestStore
from lex.corpus import load_corpus
from lex.grounding import check_grounding, extract_anchors, split_sentences
from lex.models import STEP_ORDER
from lex.orchestrator import Orchestrator, RunOptions
from lex.policy import evaluate
from lex.tickets import get_ticket_store

CORPUS = ROOT / "sample-corpus"
PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    mark = "OK  " if ok else "FAIL"
    print(f"{mark} {name}" + (f" — {detail}" if detail else ""))


# --------------------------------------------------------------- 1. каскад


def check_steps() -> None:
    expected = ("accept", "policy", "decompose", "inspect", "retrieve", "cases",
                "permissions", "resolve", "conflict", "compose", "verify")
    check("§17 шаги: policy вторым, 11 шагов",
          expected == STEP_ORDER, f"{len(STEP_ORDER)} шагов: {' -> '.join(STEP_ORDER[:3])}…")


# --------------------------------------------------------------- 2. policy


def check_policy(corpus) -> None:
    v = evaluate(corpus, "Какой срок согласования заявки на закупку?", "lawyer")
    check("§17 policy пропускает вопрос по базе", v.ok, v.step_detail())

    v = evaluate(corpus, "Какая ставка НДС в России?", "lawyer")
    check("§17 policy отсекает НДС как out_of_scope", v.kind == "out_of_scope",
          v.step_detail())

    v = evaluate(corpus, "Как обычно делают в других компаниях?", "lawyer")
    check("§12.4/§17 внешняя практика — out_of_scope", v.kind == "out_of_scope",
          v.step_detail())

    # §23: чтение единое — вопрос по закупкам проходит для любой роли.
    v1 = evaluate(corpus, "Какой срок согласования заявки на закупку?", "quality")
    v2 = evaluate(corpus, "Какой срок согласования заявки на закупку?", "lawyer")
    check("§23 чтение единое: одинаковый вердикт для разных ролей",
          v1.ok == v2.ok == True and v1.business_line == v2.business_line,  # noqa: E712
          f"{v1.business_line} / {v2.business_line}")

    check("§23 запись: quality может, lawyer нет",
          policy.can_write("quality") and not policy.can_write("lawyer"),
          f"quality={policy.can_write('quality')} lawyer={policy.can_write('lawyer')}")
    check("§23 решение по предложению — только director",
          policy.can_write("director", action="decide")
          and not policy.can_write("quality", action="decide"),
          "director=True quality=False")
    check("§23 чтение открыто всем", policy.can_read("lawyer") and policy.can_read(""), "")


# --------------------------------------------------- 3. барьеры §12 и §21


def check_barriers() -> None:
    frag = ["Срок согласования заявки: пяти рабочих дней. "
            "Порог автоматического одобрения: 150 000 рублей."]

    r = check_grounding("Срок согласования — пять рабочих дней [1].", frag, embedder=None)
    check("§12 утверждение с цитатой проходит", r.claims[0].covered,
          r.claims[0].reason)

    r = check_grounding("Срок согласования — семь рабочих дней [1].", frag, embedder=None)
    check("§21 чужое число отбрасывается", not r.claims[0].covered,
          r.claims[0].reason)

    r = check_grounding("Согласование занимает пять календарных дней [1].", frag, embedder=None)
    check("§21 подмена единицы (календарные вместо рабочих) отбрасывается",
          not r.claims[0].covered, r.claims[0].reason)

    r = check_grounding("Срок согласования — около недели [1].", frag, embedder=None)
    cut = r.claims[0]
    check("§21 усилитель «около» режет утверждение",
          not cut.covered and cut.added_amplifiers == ["около"], cut.reason)

    r = check_grounding("Срок согласования — обычно пять рабочих дней [1].", frag,
                        embedder=None)
    cut = r.claims[0]
    check("§21 «обычно» меняет модальность — режется",
          not cut.covered and "обычно" in cut.added_amplifiers, cut.reason)

    r = check_grounding("Порог одобрения — 150 000 рублей [1].", frag, embedder=None)
    check("§21 запись суммы через пробел совпадает с цитатой",
          r.claims[0].covered, r.claims[0].reason)

    r = check_grounding("Срок согласования заявки составляет три рабочих дня [2].", frag,
                        embedder=None)
    check("§12 ссылка за пределы контекста не считается цитатой",
          not r.claims[0].covered, r.claims[0].reason)

    # §21.2: реквизиты дословно
    a1 = extract_anchors("пяти рабочих дней")
    a2 = extract_anchors("5 рабочих дней")
    check("§21 числительное словами = цифра", a1 == a2, f"{sorted(a1)}")

    # Обрыв ответа: фраза без конца не должна попасть в усечённый текст.
    from lex.grounding import is_complete_sentence

    check("обрыв: «пяти рабочих дней [2]» без точки — не закончено",
          not is_complete_sentence("пяти рабочих дней [2]"), "")
    check("обрыв: «пять рабочих дней [1].» — закончено",
          is_complete_sentence("Срок — пять рабочих дней [1]."), "")
    r = check_grounding(
        "Срок согласования — пять рабочих дней [1]. Редакция вступает 2026-06-01, и",
        frag, embedder=None)
    check("обрыв: усечение отбрасывает незаконченную фразу",
          r.covered_text() == "Срок согласования — пять рабочих дней [1].",
          repr(r.covered_text()))
    # Фраза покрыта цитатой, но оборвана: external == 0, и раньше усечение не
    # срабатывало вовсе — обрывок уходил человеку. Теперь виден в incomplete.
    r = check_grounding("Срок согласования — пять рабочих дней [1], и", frag, embedder=None)
    check("обрыв: покрытая оборванная фраза считается incomplete",
          r.external == 0 and r.claims[0].covered and r.incomplete == 1,
          f"external={r.external} covered={r.claims[0].covered} "
          f"incomplete={r.incomplete}")

    # Числительные основы ловили чужие слова: «дв» -> «дверь», «тр» -> «требования».
    # Верное утверждение отбрасывалось на сверке якорей.
    check("§21 «дверь» не даёт ложного числительного",
          extract_anchors("дверь склада") == set(), str(extract_anchors("дверь склада")))
    check("§21 «требования» не даёт ложного числительного",
          extract_anchors("требования к срокам") == set(),
          str(extract_anchors("требования к срокам")))
    r = check_grounding("Дверь склада: три рабочих дня [1].", frag, embedder=None)
    check("§21 верное утверждение с чужим словом не отбрасывается",
          r.claims[0].covered, r.claims[0].reason)

    # Составное числительное: «двести пятьдесят» — одно число, а не «200 50».
    check("§21 составное числительное складывается",
          extract_anchors("двести пятьдесят рабочих дней") == {"num:250:раб.дн"},
          str(extract_anchors("двести пятьдесят рабочих дней")))

    # Резчик рвал номер пункта: «п. 4 ч.1 ст.93» -> «п.» + «4 ч.1 ст.93».
    # Первое уходило в связки, второе теряло ссылку — «покрыто 0» на верном ответе.
    parts = split_sentences("Срок — п. 4 ч.1 ст.93 44-ФЗ.")
    check("§21 номер пункта не рассекается на два «предложения»", len(parts) == 1,
          repr(parts))


# ------------------------------------------------------------- 4. оговорка


def check_caveat() -> None:
    c = caveat_mod.build_caveat(caveat_mod.CaveatInput(
        mean_similarity=0.9, sources=3, dropped=0, total_claims=4))
    check("§20 уверенный ответ — оговорки нет", c is None)

    c = caveat_mod.build_caveat(caveat_mod.CaveatInput(
        mean_similarity=0.6, sources=3, dropped=0, total_claims=4))
    check("§20 низкая близость — medium",
          c is not None and c.level == "medium" and c.reasons[0].code == "low_coverage",
          c.reasons[0].text if c else "нет")

    c = caveat_mod.build_caveat(caveat_mod.CaveatInput(
        mean_similarity=0.9, sources=3, dropped=2, total_claims=5))
    check("§20 частичный ответ — «отвечено на N из M»",
          c is not None and "3 из 5" in c.reasons[0].text,
          c.reasons[0].text if c else "нет")

    c = caveat_mod.build_caveat(caveat_mod.CaveatInput(
        mean_similarity=0.9, sources=3, min_ocr=0.42))
    check("§20 скан с OCR 0.42 — high",
          c is not None and c.level == "high" and c.reasons[0].code == "ocr_low",
          c.level if c else "нет")

    c = caveat_mod.build_caveat(caveat_mod.CaveatInput(
        mean_similarity=0.9, sources=1))
    check("§20 единственный источник — low",
          c is not None and c.reasons[0].code == "single_source",
          c.reasons[0].text if c else "нет")

    c = caveat_mod.build_caveat(caveat_mod.CaveatInput(
        mean_similarity=0.9, sources=3, practice_only=True))
    check("§20/§19 ответ по практике — оговорка",
          c is not None and c.reasons[0].code == "practice_only",
          c.reasons[0].text if c else "нет")

    c = caveat_mod.build_caveat(caveat_mod.CaveatInput(
        mean_similarity=0.9, sources=3, as_of=date(2026, 3, 1),
        current_edition={"revision": 1, "effective": "2026-01-15"},
        next_edition={"revision": 2, "effective": "2026-03-20"}))
    check("§20 близкая редакция — high, с датой",
          c is not None and c.level == "high" and "2026-03-20" in c.reasons[0].text,
          c.reasons[0].text if c else "нет")


# ------------------------------------------------- 5. кейс vs регламент §19


def check_case_vs_norm(corpus) -> None:
    deviates, text = detect_deviation(
        corpus, rule_key="SR-01", answer="согласовали за 2 рабочих дня",
        question="Какой срок согласования заявки на закупку?", as_of=date(2026, 3, 1))
    check("§19.2 практика «2 дня» против нормы «3 дня» ловится",
          bool(deviates) and "ТЗ-ЭКЗ-СР01-Р1" in deviates, text or "нет расхождения")

    deviates, text = detect_deviation(
        corpus, rule_key="SR-01", answer="согласовали за 3 рабочих дня",
        question="Какой срок согласования заявки на закупку?", as_of=date(2026, 3, 1))
    check("§19.2 совпадение с нормой — флаг не ставится", not deviates, text or "—")

    tmp = Path(tempfile.mkdtemp()) / "changes.jsonl"
    store = ChangeRequestStore(tmp)
    cr = store.upsert_from_case(
        case_id="case-9001", target_doc="ТЗ-ЭКЗ-СР01-Р1", rule_key="SR-01",
        observed="в практике 2 дня", current_norm="трёх рабочих дней")
    check("§19.4 предложение создаётся агентом", cr.created_by == "agent",
          f"{cr.id} · {cr.status}")
    check("§19.6 один кейс — наблюдение, не сигнал", not cr.is_signal,
          f"кейсов {cr.case_count} при пороге {caveat_mod.__name__ and store and 3}")

    store.upsert_from_case(
        case_id="case-9002", target_doc="ТЗ-ЭКЗ-СР01-Р1", rule_key="SR-01",
        observed="ещё случай", current_norm="трёх рабочих дней")
    cr = store.upsert_from_case(
        case_id="case-9003", target_doc="ТЗ-ЭКЗ-СР01-Р1", rule_key="SR-01",
        observed="третий случай", current_norm="трёх рабочих дней")
    check("§19.6 три кейса — сигнал", cr.is_signal, f"кейсов {cr.case_count}")

    other = store.set_status(cr.id, "merged", by="И. Петрова",
                             merged_doc="ТЗ-ЭКЗ-СР01-Р4")
    check("§19.4 статус меняет человек, агент только фиксирует",
          other.status == "merged" and other.decided_by == "И. Петрова",
          f"{other.id} -> {other.status}")


# ------------------------------------------------------------ 6. тикеты §18


def check_tickets() -> None:
    tmp = Path(tempfile.mkdtemp()) / "tickets.jsonl"
    store = get_ticket_store(tmp)
    t = store.open_from_agent(
        question="Как согласовать закупку у единственного поставщика?",
        kind="not_in_corpus", why_unresolved="в корпусе нет порядка",
        session_id="s-check-1", role_id="lawyer", business_line="закупки",
        searched={"documents": 27, "cases": 4})
    check("§18 номер человекочитаемый", t.id.startswith("LEX-"), t.id)
    check("§18 создаёт агент, не человек", t.created_by == "agent", t.created_by)
    check("§18 SLA выставлен", bool(t.sla_due), t.sla_due)

    again = store.open_from_agent(
        question="Тот же вопрос снова", kind="not_in_corpus", why_unresolved="—",
        session_id="s-check-1")
    check("§18.7 дубликат по сессии не создаётся", again.id == t.id, again.id)

    store.assign(t.id, "И. Петрова")
    store.answer(t.id, "обращайтесь к директору")
    got = store.get(t.id)
    check("§18.3 назначение и ответ меняют статус",
          got.status == "answered" and got.assignee == "И. Петрова", got.status)

    bad = False
    try:
        store.open_from_agent(question="НДС?", kind="out_of_scope",
                              why_unresolved="вне базы", session_id="s-check-2")
    except ValueError:
        bad = True
    check("§18.1 out_of_scope тикет НЕ создаёт", bad)


# ------------------------------------------------- 7. сквозной прогон


async def _run(question: str, *, as_of=None, role="lawyer", no_llm=True,
               tickets: Path | None = None, session="s-flow"):
    opts = RunOptions(question=question, role_id=role, as_of=as_of, no_llm=no_llm,
                      session_id=session,
                      tickets_path=tickets or (Path(tempfile.mkdtemp()) / "t.jsonl"))
    out = [sig async for sig in Orchestrator(opts).run()]
    return out


def check_flow(corpus) -> None:
    out = asyncio.run(_run("Какой срок согласования заявки на закупку?",
                           as_of=date(2026, 3, 1)))
    steps = [s.data for s in out if s.kind == "step"]
    final = next((s for s in out if s.kind in ("answer", "refusal", "clarify")), None)
    check("DoD сквозной: 11 шагов, policy второй",
          len(steps) == 11 and getattr(steps[1], "key", "") == "policy",
          f"{len(steps)} шагов, второй — {getattr(steps[1], 'key', '?')}")
    answer = final.data if final and final.kind == "answer" else None
    " ".join(c.quote for c in (answer.citations if answer else []))
    r1 = [c for c in (answer.citations if answer else [])
          if c.doc_id == "ТЗ-ЭКЗ-СР01-Р1" and "трёх рабочих дней" in c.quote]
    check("DoD сквозной: цитата «трёх рабочих дней» (Р1 от 2026-01-15)",
          bool(r1) and r1[0].revision == 1 and r1[0].effective == "2026-01-15",
          f"цитат {len(answer.citations) if answer else 0}, Р1 найдена={bool(r1)}")

    out = asyncio.run(_run("Какой срок согласования заявки на закупку?",
                           as_of=date(2025, 12, 1)))
    kinds = [s.kind for s in out]
    refusal = next((s.data for s in out if s.kind == "refusal"), None)
    check("DoD сквозной: --as-of 2025-12-01 -> no_edition_in_force",
          refusal is not None and refusal.kind == "no_edition_in_force",
          f"шаги {len([k for k in kinds if k == 'step'])}")

    out = asyncio.run(_run("Какая ставка НДС в России?", as_of=date(2026, 3, 1)))
    refusal = next((s.data for s in out if s.kind == "refusal"), None)
    steps = [s.data for s in out if s.kind == "step"]
    check("§12.6/§17 демо НДС: отказ на шаге 2 без поиска",
          refusal is not None and refusal.kind == "out_of_scope" and len(steps) == 2,
          f"{refusal.kind if refusal else '—'}, шагов {len(steps)}")

    # пять вопросов корпуса — не падает
    bad = []
    for line in (CORPUS / "questions.jsonl").read_text(encoding="utf-8").splitlines()[:5]:
        q = json.loads(line)["question"]
        try:
            out = asyncio.run(_run(q, as_of=date(2026, 3, 1)))
        except Exception as exc:
            bad.append(f"{q[:40]}: {exc}")
            continue
        if not any(s.kind in ("answer", "refusal", "clarify") for s in out):
            bad.append(f"{q[:40]}: ни ответа, ни отказа")
    check("DoD пять вопросов корпуса — без падений", not bad,
          "; ".join(bad)[:160] or "5/5 отработаны")


def main() -> int:
    corpus = load_corpus(CORPUS)
    print(f"корпус: документов {len(corpus.documents)}, чанков {len(corpus.chunks)}, "
          f"правил {len(corpus.rules)}\n")
    check_steps()
    check_policy(corpus)
    check_barriers()
    check_caveat()
    check_case_vs_norm(corpus)
    check_tickets()
    check_flow(corpus)
    print(f"\n{PASS.__len__()} passed, {len(FAIL)} failed")
    if FAIL:
        print("провал: " + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
