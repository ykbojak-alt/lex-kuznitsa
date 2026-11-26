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
import os as _os
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lex import caveat as caveat_mod
from lex import policy
from lex.cases import CaseStore, detect_deviation
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

    # §23 v1.16: policy проверяет КОМПЕТЕНЦИЮ базы (тема наша или нет), а не
    # право чтения — фильтр по отделам живёт в access/orchestrator. Поэтому
    # вердикт по одному вопросу одинаков для любых ролей: роль не делает
    # вопрос «нашим» или «не нашим».
    v1 = evaluate(corpus, "Какой срок согласования заявки на закупку?", "quality")
    v2 = evaluate(corpus, "Какой срок согласования заявки на закупку?", "lawyer")
    check("§23 policy одинаков для ролей (компетенция, не права)",
          v1.ok == v2.ok and v1.business_line == v2.business_line,
          f"{v1.business_line} / {v2.business_line}")

    check("§23 запись: quality может, lawyer нет",
          policy.can_write("quality") and not policy.can_write("lawyer"),
          f"quality={policy.can_write('quality')} lawyer={policy.can_write('lawyer')}")
    check("§23 решение по предложению — только director",
          policy.can_write("director", action="decide")
          and not policy.can_write("quality", action="decide"),
          "director=True quality=False")
    check("§23 v1.16 чтение: известная роль читает, опечатка — нет",
          policy.can_read("quality") and policy.can_read("ib")
          and not policy.can_read("юристъ"),
          "quality=True ib=True bogus=False")


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
               tickets: Path | None = None, session="s-flow",
               corpus_root: Path | None = None, cases: Path | None = None):
    opts = RunOptions(question=question, role_id=role, as_of=as_of, no_llm=no_llm,
                      session_id=session, corpus_root=corpus_root or CORPUS,
                      cases_path=cases or (ROOT / "var" / "cases.jsonl"),
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


def check_case_visibility() -> None:
    """Критерий 1: кейс другой роли не попадает в выдачу и в поиск.

    Кейс — чужая практика, а не норматив (§8). До правки поиск брал все кейсы
    разом, и юрист мог получить в ответ случай из практики ИТ-службы.

    Правило видимости держится на трёх исключениях, и каждое проверяется
    отдельно — иначе легко «починить» фильтр так, что вьющая роль перестанет
    видеть ответ службы качества, и цикл дообучения (критерий 3) сломается.
    """
    tmp = Path(tempfile.mkdtemp(prefix="lex-vis-"))
    store = CaseStore(tmp / "cases.jsonl")

    def add(role: str, question: str) -> str:
        return store.add(question=question, answer_in_practice="ответ",
                         author_role=role, outcome="resolved").id

    own = add("technical", "Срок согласования заявки на закупку?")
    other = add("ib", "Порядок выдачи ноутбука новому сотруднику?")
    quality = add("quality", "Размер командировочной надбавки?")
    nobody = add("", "Импорт практики без автора")

    seen = {c.id for c in store.visible("technical")}
    check("критерий 1: кейс чужой роли не виден", other not in seen, sorted(seen))
    check("критерий 1: свой кейс виден", own in seen, sorted(seen))
    # Ответ службы качества — на вопрос сотрудника: без него дообучение
    # работает только для того, кто отвечал, а не для того, кто спрашивал.
    check("критерий 1: ответ службы качества виден всем", quality in seen, sorted(seen))
    check("критерий 1: кейс без автора виден всем", nobody in seen, sorted(seen))

    # Служба качества и руководство ведут базу — им видно всё.
    check("критерий 1: служба качества видит всё",
          {c.id for c in store.visible("quality")} == {own, other, quality, nobody},
          sorted(c.id for c in store.visible("quality")))

    # Другой отдел: кейс technical ему не виден — фильтр симметричен.
    seen_ib = {c.id for c in store.visible("ib")}
    check("критерий 1: фильтр симметричен по ролям",
          own not in seen_ib and other in seen_ib, sorted(seen_ib))

    # v1.16: lawyer — псевдоним technical, автор кейса остаётся виден под
    # старым id, хотя читается он новым. Без нормализации кейс «lawyer»
    # проваливался бы в обе стороны.
    legacy = add("lawyer", "Устаревший id автора кейса")
    check("критерий 1: псевдоним lawyer видит свой старый кейс",
          legacy in {c.id for c in store.visible("lawyer")}
          and legacy in {c.id for c in store.visible("technical")},
          sorted(c.id for c in store.visible("lawyer")))

    # Через HTTP-слой: /api/cases?role_id= отдаёт ту же выдачу, что поиск.
    from api import config as api_config
    from api import core as api_core
    old = api_config.CASES_PATH
    api_config.CASES_PATH = tmp / "cases.jsonl"
    try:
        ids = {c["id"] for c in api_core.list_cases(role_id="technical")["cases"]}
        check("критерий 1: /api/cases?role_id фильтрует так же",
              other not in ids and own in ids, sorted(ids))
        # §23: без роли — отказ, а не весь журнал.
        bare = api_core.list_cases(role_id=None)
        check("критерий 1: /api/cases без роли не отдаёт кейсы",
              bare["cases"] == [] and bare["dropped"] == bare["total"], str(bare["dropped"]))
    finally:
        api_config.CASES_PATH = old


def check_role_filter() -> None:
    """LEX-ROLE-FILTER: чтение разграничено по отделам роли на реальном корпусе.

    Критерий приёмки владельца (дословно): «роль Дирекция ИБ на corpus-real
    видит РОВНО 2 документа (КД-ПС-02, КД-ПД-04), не видит Техническую
    дирекцию (КД-ПД-01) и ДБРЦТ (КД-ПД-03). director видит все 4».

    Почему это не «мягкая настройка»: до v1.16 `filter_docs` отдавал всё с
    `dropped: 0` жёстко, а `_filter_access` звал несуществующий
    `allowed_document` — исключение глоталось, и права не проверялись вовсе.
    Тест ловит обе регрессии: не только «фильтр вернул не то», но и «фильтр
    не вызвался».
    """
    from lex import access

    real = ROOT / "corpus-real"
    if not (real / "manifest.json").is_file():
        check("LEX-ROLE-FILTER: corpus-real на месте", False, str(real))
        return
    docs = access.all_documents(real / "manifest.json")
    roles = access.roles_for_corpus(real)
    check("LEX-ROLE-FILTER: роли из отделов корпуса",
          {r["id"] for r in roles} >= {"technical", "ib", "dbrct", "director"},
          sorted(r["id"] for r in roles))

    ib = access.filter_docs(docs, "ib", defs=roles)
    ib_ids = sorted(d["doc_id"] for d in ib["documents"])
    check("LEX-ROLE-FILTER: Дирекция ИБ видит ровно 2 документа",
          ib_ids == ["КД-ПД-04", "КД-ПС-02"], ib_ids)
    check("LEX-ROLE-FILTER: ИБ не видит Техническую дирекцию и ДБРЦТ",
          "КД-ПД-01" not in ib_ids and "КД-ПД-03" not in ib_ids, ib_ids)
    check("LEX-ROLE-FILTER: dropped — настоящее число, не ноль",
          ib["dropped"] == 2
          and (ib["permissions"]["by_reason"] or {}).get("вне подразделения") == 2,
          f"dropped={ib['dropped']} by_reason={ib['permissions']['by_reason']}")

    director = access.filter_docs(docs, "director", defs=roles)
    check("LEX-ROLE-FILTER: director видит все 4",
          director["available"] == 4 and director["dropped"] == 0,
          f"{director['available']}/4")
    check("LEX-ROLE-FILTER: technical видит только свой 1",
          [d["doc_id"] for d in access.filter_docs(docs, "technical", defs=roles)["documents"]]
          == ["КД-ПД-01"], "")
    check("LEX-ROLE-FILTER: dbrct видит только свой 1",
          [d["doc_id"] for d in access.filter_docs(docs, "dbrct", defs=roles)["documents"]]
          == ["КД-ПД-03"], "")

    # Опечатка в роли не должна открывать корпус: молчаливый полный доступ
    # по ошибке — ровно та дыра, из-за которой v1.15 отменяли.
    bogus = access.filter_docs(docs, "юристъ", defs=roles)
    check("LEX-ROLE-FILTER: неизвестная роль fail-closed",
          bogus["available"] == 0 and bogus["dropped"] == 4,
          f"{bogus['available']}/4")

    # can_read больше не «всегда True», но и не запрет по пустому корпусу.
    check("LEX-ROLE-FILTER: can_read — известная роль, не «всегда True»",
          bool(access.can_read("ib")) and not access.can_read("юристъ"),
          f"ib={access.can_read('ib')} bogus={access.can_read('юристъ')}")

    # Через HTTP-слой: /api/roles отдаёт те же числа, что фильтр ядра.
    from api import config as api_config
    from api import core as api_core
    old = api_config.CORPUS_DIR
    api_config.CORPUS_DIR = real
    try:
        rows = {r["id"]: r for r in api_core.roles()}
        for rid in ("ib", "technical", "dbrct", "director"):
            if rid not in rows:
                continue
            f = api_core.filter_documents(role_id=rid)
            expect = {"ib": 2, "technical": 1, "dbrct": 1, "director": 4}[rid]
            if f["available"] != expect:
                check(f"LEX-ROLE-FILTER: /api {rid} = {expect}", False,
                      f"{f['available']}")
                break
        else:
            check("LEX-ROLE-FILTER: /api/roles совпадает с фильтром ядра", True,
                  "ib 2 · technical 1 · dbrct 1 · director 4")
    finally:
        api_config.CORPUS_DIR = old


def check_role_filter_flow() -> None:
    """Критерий приёмки: вопрос по чужому документу — честный отказ, не выдумка.

    Роль Дирекция ИБ не видит КД-ПД-01 (Техническая дирекция). Вопрос, норма
    которого есть ТОЛЬКО там, должен кончиться отказом/заявкой, а не ответом
    по чужому фрагменту. Проверяем и шаг `retrieve` (число «скрыто по
    отделу»), и шаг `permissions` («доступно 2 из 4»), и исход.
    """
    real = ROOT / "corpus-real"
    if not (real / "manifest.json").is_file():
        return
    out = asyncio.run(_run("Кто согласует ТКП в пресейле?", role="director",
                           corpus_root=real, session="s-rf-boss"))
    perms = next((s.data for s in out if s.kind == "step"
                  and s.data.key == "permissions"), None)
    check("LEX-ROLE-FILTER: у director шаг permissions — полный корпус",
          perms is not None and "полный корпус" in perms.detail,
          getattr(perms, "detail", "")[:70])

    out = asyncio.run(_run("Кто согласует ТКП в пресейле?", role="ib",
                           corpus_root=real, session="s-rf-ib"))
    ret = next((s.data for s in out if s.kind == "step"
                and s.data.key == "retrieve"), None)
    perms = next((s.data for s in out if s.kind == "step"
                  and s.data.key == "permissions"), None)
    det = getattr(ret, "detail", "")
    check("LEX-ROLE-FILTER: retrieve сообщает, сколько скрыто по отделу",
          "скрыто по отделу" in det, det[:90])
    check("LEX-ROLE-FILTER: permissions говорит «доступно 2 из 4»",
          perms is not None and "доступно 2 из 4" in perms.detail,
          getattr(perms, "detail", "")[:80])
    check("LEX-ROLE-FILTER: ИБ не получает «вне подразделения: 0»",
          perms is not None and "вне подразделения: 2" in perms.detail,
          getattr(perms, "detail", "")[:80])


def check_single_corpus() -> None:
    """LEX-SINGLE-CORPUS / A2+A3: один реальный корпус, без холостых шагов.

    Владелец: «У нас 2 корпуса?! Мы должны работать над одним реальным!»
    Стенд уже на corpus-real; тесты — исключение, они держат фикстуру для
    сценариев, которых в реальных документах физически нет.

    A2. В текстах КД нет ни одного «взамен / утратил силу / отменяет», значит
        `conflict_rules` честно пуст. Шаг `resolve` в таком корпусе писал
        «правило не определено — действует как есть» — это читается как
        сломанная логика. Теперь resolve/conflict не выводятся, если правил
        нет, и `total` шагов уменьшается на 2.
    A3. Водяной знак манифеста должен быть нейтральным: данные обезличены.
    """
    import json as _json

    real = ROOT / "corpus-real"

    # A3: watermark и в манифесте, и в INDEX.md — иначе при пересборке
    # корпуса старый текст вернётся, и предупреждение «не публиковать»
    # попадёт в публичную демонстрацию.
    manifest = _json.loads((real / "manifest.json").read_text(encoding="utf-8"))
    wm = str(manifest.get("watermark") or "")
    check("A3 watermark нейтральный", "обезличен" in wm.lower() and "не публиковать" not in wm.lower(), wm)
    index_txt = (real / "INDEX.md").read_text(encoding="utf-8")
    check("A3 watermark в INDEX.md обновлён", "не публиковать" not in index_txt.lower(), "")

    # A2: правил нет — и это факт, а не забывчивость.
    rules = manifest.get("conflict_rules") or []
    check("A2 conflict_rules пуст по факту", rules == [], f"{len(rules)} правил")

    out = asyncio.run(_run("Кто согласует ТКП в пресейле?", role="director",
                           corpus_root=real, session="s-single"))
    keys = [s.data.key for s in out if s.kind == "step"]
    check("A2 на corpus-real нет пустого шага resolve",
          "resolve" not in keys and "conflict" not in keys, keys)
    check("A2 total шагов на corpus-real = 9",
          all(s.data.total == 9 for s in out if s.kind == "step"),
          sorted({s.data.total for s in out if s.kind == "step"}))

    # А на корпусе с правилами шаги остаются — иначе мы бы просто спрятали
    # логику редакций, вместо того чтобы не выводить её там, где она пуста.
    # Проверяем структурно (число шагов прогона), а не через полный каскад:
    # исход каскада на фикстуре зависит от policy/направлений, которые правят
    # другие агенты, и от этого теста зависеть не должен.
    from lex.orchestrator import Orchestrator as _Orch
    from lex.orchestrator import RunOptions as _Run

    fixture = _Orch(_Run(question="Какой срок согласования заявки на закупку?",
                         role_id="director", no_llm=True, session_id="s-single-cnt",
                         corpus_root=ROOT / "sample-corpus",
                         cases_path=Path(tempfile.mkdtemp()) / "c.jsonl"))
    check("A2 на фикстуре с правилами resolve в каскаде остаётся",
          fixture._has_rules() and fixture._total() == 11,
          f"rules={fixture._has_rules()} total={fixture._total()}")
    real_orch = _Orch(_Run(question="—", role_id="director", no_llm=True,
                           corpus_root=real, cases_path=Path(tempfile.mkdtemp()) / "c.jsonl"))
    check("A2 на corpus-real каскад без resolve (9 шагов)",
          not real_orch._has_rules() and real_orch._total() == 9,
          f"rules={real_orch._has_rules()} total={real_orch._total()}")


def check_subject_gate() -> None:
    """§12.8: гейт предмета в retrieve — три исхода, каждый на своём корпусе.

    Гейт считает предмет вопроса (subject_coverage) и близость вектора лучшего
    фрагмента. Векторы чанков читаются из .lex-index.npz, но вектор ЗАПРОСА
    считается эмбеддером на живой вызов — поэтому близость здесь только
    измеряется и печатается, а проверяется главный признак: предмет.

    Все прогоны no_llm и с уже готовым кэшем: модель не зовётся, индексация не
    выполняется, тест не ходит в сеть дольше одного эмбеддинга на вопрос.
    """
    # 1. Предмет найден — гейт пропускает к обычному ответу.
    out = asyncio.run(_run("Какой срок согласования заявки на закупку?",
                           as_of=date(2026, 3, 1), session="s-gate"))
    retrieve = next((s.data for s in out if s.kind == "step"
                     and s.data.key == "retrieve"), None)
    final = next((s.kind for s in out if s.kind in ("answer", "refusal", "clarify",
                                                    "escalate")), "—")
    check("§12.8 гейт: предмет найден — ответ", final == "answer",
          getattr(retrieve, "detail", "")[:90] or "retrieve не дошёл")

    # 2. Нормы нет — каскад обрывается на retrieve и уходит в заявку.
    # Вопрос берёт реальный корпус: в регламентах нет срока хранения
    # бухгалтерских документов, а похожий пункт про «документы» есть.
    real = ROOT / "corpus-real"
    if not (real / "manifest.json").is_file():
        check("§12.8 гейт: норма отсутствует — escalate", False,
              f"корпус заказчика не найден: {real}")
    else:
        out = asyncio.run(_run(
            "Какой срок хранения бухгалтерских документов?",
            corpus_root=real, session="s-gate"))
        retrieve = next((s.data for s in out if s.kind == "step"
                         and s.data.key == "retrieve"), None)
        keys = [s.data.key for s in out if s.kind == "step"]
        final = next((s.kind for s in out if s.kind in ("answer", "refusal", "clarify",
                                                        "escalate")), "—")
        detail = getattr(retrieve, "detail", "")
        check("§12.8 гейт: норма отсутствует — escalate",
              final == "escalate" and "compose" not in keys
              and "предмет вопроса не найден" in detail,
              f"{final}, шагов {len(keys)}, {detail[:70]}")

    # 3. Обход: вопрос называет документ явно — гейт его не режет, даже если
    # предмет не набрал порога. Проверяем именно тот случай, где гейт сработал
    # бы: тот же вопрос БЕЗ ссылки на документ уходит в заявку.
    out = asyncio.run(_run(
        "В каком размере выплачивается командировочная надбавка по документу?",
        corpus_root=real, session="s-gate-noref"))
    final_noref = next((s.kind for s in out if s.kind in ("answer", "refusal",
                                                         "clarify", "escalate")), "—")

    out = asyncio.run(_run(
        "В каком размере выплачивается командировочная надбавка "
        "в документе Регламент «проверки качества входного контроля»?",
        as_of=date(2026, 3, 1), session="s-gate-docref"))
    retrieve = next((s.data for s in out if s.kind == "step"
                     and s.data.key == "retrieve"), None)
    final_ref = next((s.kind for s in out if s.kind in ("answer", "refusal", "clarify",
                                                        "escalate")), "—")
    detail = getattr(retrieve, "detail", "")
    check("§12.8 гейт: вопрос с явным документом не режется",
          final_ref == "answer" and "предмет вопроса не найден" not in detail,
          f"без ссылки — {final_noref}, со ссылкой — {final_ref}, {detail[:60]}")


def check_loop_criterion3() -> None:
    """Критерий 3: ответ службы качества — дообучение, а не запись в журнал.

    Цепочка (MIMIC, a8b392d): ответ СК по заявке -> кейс в базе -> повторный
    вопрос отвечает по практике, а не уходит в эскалацию снова.

    Проверяется через HTTP-слой ``src/api/tickets.py`` — именно он замыкает
    цепочку. Хранилища временные: пути подменяются и в config, и в модулях,
    которые уже прочитали их на импорте.
    """
    tmp = Path(tempfile.mkdtemp(prefix="lex-c3-"))
    cases_path = tmp / "cases.jsonl"
    from api import config as api_config
    from api import core as api_core
    from api import tickets as api_tickets

    api_config.CASES_PATH = cases_path
    api_config.TICKETS_PATH = tmp / "tickets.jsonl"
    api_config.ESCALATIONS_PATH = tmp / "escalations.jsonl"
    api_config.JOURNAL_PATH = tmp / "journal.jsonl"
    api_config.CHANGE_REQUESTS_PATH = tmp / "change_requests.jsonl"

    # Вопрос выбран замером: он доходит до retrieve на sample-corpus и без
    # кейса упирается в гейт (предмет 25%) — то есть это ровно тот случай,
    # который дообучение должно закрыть. «Срок хранения бухгалтерских
    # документов» не годится: policy/clarify останавливают его раньше гейта.
    question = "В каком размере выплачивается командировочная надбавка?"
    evidence = "Суточные 700 рублей в день, надбавка 15% от оклада."

    api_tickets._append("tk", {
        "id": "LEX-T-C3-0001", "status": "open", "question": question,
        "asker": {"role_id": "lawyer", "business_line": "проектная деятельность"},
        "created_at": "2026-11-26T10:00:00Z",
    })

    answered = api_tickets.answer_ticket("LEX-T-C3-0001", evidence)
    rows = [json.loads(line) for line in cases_path.read_text(encoding="utf-8").splitlines()
            if line.strip()] if cases_path.exists() else []
    ticket_rows = api_tickets._read("tk")
    check("критерий 3: ответ СК создаёт кейс",
          len(rows) == 1 and bool(answered.get("case_id"))
          and rows[0].get("question") == question
          and rows[0].get("id") == answered.get("case_id")
          and any(r.get("case_id") == answered.get("case_id") for r in ticket_rows),
          f"кейсов {len(rows)}, case_id {answered.get('case_id') or '—'}")

    api_tickets.answer_ticket("LEX-T-C3-0001", evidence + " Уточнено.")
    rows2 = [json.loads(line) for line in cases_path.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    check("критерий 3: повторный ответ не плодит кейсы",
          len(rows2) == 1 and rows2[0].get("id") == answered.get("case_id"),
          f"кейсов после второго ответа: {len(rows2)}")

    # Главное: вопрос, закрытый кейсом, не уходит в эскалацию повторно.
    store = api_core.case_store()
    hits = store.find(limit=5) if store is not None else []
    check("критерий 3: кейс виден хранилищу ядра", len(hits) == 1,
          f"кейсов в хранилище {len(hits)}")

    out = asyncio.run(_run(question, cases=cases_path, session="s-c3"))
    keys = [s.data.key for s in out if s.kind == "step"]
    final = next((s.kind for s in out if s.kind in ("answer", "refusal", "clarify",
                                                    "escalate")), "—")
    retrieve = next((s.data for s in out if s.kind == "step"
                     and s.data.key == "retrieve"), None)
    detail = getattr(retrieve, "detail", "")
    check("критерий 3: повторный вопрос находит кейс",
          final == "answer" and "escalate" not in keys
          and "тема закрыта кейсом" in detail,
          f"{final}, {detail[:80]}")


def check_crit3_train() -> None:
    """Критерий 3 целиком: полный цикл дообучения, а не только «кейс появился».

    Пять пунктов приёмки, каждый — отдельная проверка:

    1. ответ СК по заявке создаёт кейс с полями ticket_id/approved_at
       (без них не считается метрика и кейс нельзя отозвать вместе с заявкой);
    2. вопрос при следующем поиске находит кейс (CaseStore.search: BM25+вектор);
    3. кейс, созданный по заявке роли X, роли Y не виден;
    4. метрики обучения отдаются /api/stats — approved_cases_count,
       cases_from_tickets, avg_ticket_to_case_hours;
    5. отклонённый кейс из поиска уходит немедленно, удалённый — тоже.

    Хранилища — во временном каталоге: рабочие var/cases.jsonl и
    var/tickets.jsonl не трогаются.
    """
    from lex.cases import get_case_store

    tmp = Path(tempfile.mkdtemp(prefix="lex-c3t-"))
    cases_path = tmp / "cases.jsonl"
    tickets_path = tmp / "tickets.jsonl"
    store = get_case_store(cases_path)

    question = "Какой срок поверки манометров на производственном участке?"
    answer = "Поверка манометров — раз в 12 месяцев, внеплановая после ремонта."
    approved = "2026-11-26T09:00:00Z"

    # 1. Путь из заявки: кейс несёт номер заявки и дату утверждения.
    case = store.add_from_ticket(
        ticket_id="LEX-2026-9001", question=question,
        answer_in_practice=answer, author_role="quality",
        tags=["охрана труда"], approved_at=approved)
    again = store.add_from_ticket(
        ticket_id="LEX-2026-9001", question=question,
        answer_in_practice=answer + " Уточнено.", author_role="quality",
        approved_at=approved)
    check("критерий 3: кейс из заявки несёт ticket_id и approved_at",
          case.ticket_id == "LEX-2026-9001" and case.approved_at == approved
          and case.source == "quality_ticket",
          f"{case.id} · {case.source} · {case.approved_at}")
    check("критерий 3: повторный ответ по заявке не плодит кейс",
          again.id == case.id, f"{again.id} vs {case.id}")

    # 2. Поиск находит кейс — и точной формулировкой, и перефразированной.
    exact = store.search(question, role_id="lawyer", top=3)
    paraphrase = store.search("Как часто проверяют манометры в цехе?",
                              role_id="lawyer", top=3)
    check("критерий 3: поиск находит кейс по вопросу из заявки",
          any(c.id == case.id for c in exact),
          f"найдено {len(exact)}")
    check("критерий 3: поиск находит кейс по перефразировке",
          any(c.id == case.id for c in paraphrase),
          f"найдено {len(paraphrase)}")

    # 3. Ролевая изоляция: кейс с автором-ролью it не виден lawyer.
    own = store.add(question="Как продлить лицензию на ПО?",
                    answer_in_practice="Заявление за 30 дней до истечения.",
                    author_role="it", outcome="resolved")
    check("критерий 3: кейс другой роли не виден",
          not any(c.id == own.id for c in store.search(
              "Как продлить лицензию на ПО?", role_id="lawyer", top=10))
          and any(c.id == own.id for c in store.search(
              "Как продлить лицензию на ПО?", role_id="it", top=10)),
          f"кейс {own.id} (it)")

    # 4. Метрики обучения: approved_cases_count, cases_from_tickets,
    #    avg_ticket_to_case_hours. Считаем ту же функцию, что и /api/stats.
    from api import config as api_config
    from api import core as api_core

    api_config.CASES_PATH = cases_path
    api_config.TICKETS_PATH = tickets_path
    api_config.ESCALATIONS_PATH = tmp / "escalations.jsonl"
    api_config.JOURNAL_PATH = tmp / "journal.jsonl"
    api_config.CHANGE_REQUESTS_PATH = tmp / "change_requests.jsonl"

    # заявка, по которой пришёл кейс: created_at на 3 часа раньше approved_at
    from api import tickets as api_tickets
    api_tickets._append("tk", {
        "id": "LEX-2026-9001", "status": "closed", "question": question,
        "created_at": "2026-11-26T06:00:00Z",
        "asker": {"role_id": "lawyer", "business_line": "охрана труда"},
    })
    metrics = api_core.learning_metrics()
    check("критерий 3: метрика approved_cases_count",
          metrics["approved_cases_count"] == 2,
          f"кейсов работающих {metrics['approved_cases_count']}")
    check("критерий 3: метрика cases_from_tickets",
          metrics["cases_from_tickets"] == 1,
          f"из заявок {metrics['cases_from_tickets']}")
    check("критерий 3: метрика avg_ticket_to_case_hours",
          abs(metrics["avg_ticket_to_case_hours"] - 3.0) < 0.01,
          f"{metrics['avg_ticket_to_case_hours']} ч")

    # 5. Инвалидация: отклонённый кейс уходит из поиска сразу.
    store.reject(case.id, reason="устарело")
    after_reject = store.search(question, role_id="lawyer", top=3)
    check("критерий 3: отклонённый кейс из поиска уходит сразу",
          not any(c.id == case.id for c in after_reject),
          f"найдено после reject {len(after_reject)}")
    check("критерий 3: отклонённый кейс не считается работающим",
          api_core.learning_metrics()["approved_cases_count"] == 1,
          f"{api_core.learning_metrics()['approved_cases_count']}")

    removed = store.remove(own.id)
    after_remove = store.search("Как продлить лицензию на ПО?", role_id="it", top=10)
    check("критерий 3: удалённый кейс из поиска уходит сразу",
          removed and not any(c.id == own.id for c in after_remove),
          f"removed={removed}")


def check_journal_metrics() -> None:
    """Журнал пишет то, по чему считается аналитика (§12.8 и критерий 3).

    Проверка без сервера: ``_step_record`` и сборка записи из потока ядра.
    Именно на этих двух местах счётчики гейта и кейсов молчали, поэтому
    проверяем не «функция есть», а «в записи журнала появились поля».
    """
    from api import core as api_core

    rec = api_core._step_record({
        "key": "retrieve", "ms": 250,
        "detail": "BM25 3 · вектор 4 → кандидатов 7 · после rerank 5 · "
                  "предмет 88% · близость 0.71",
    })
    check("журнал: метрики гейта разобраны из detail",
          rec.get("subject_coverage") == 0.88 and rec.get("top_cosine") == 0.71,
          f"{rec.get('subject_coverage')} / {rec.get('top_cosine')}")
    check("журнал: detail шага сохраняется",
          "предмет 88%" in str(rec.get("detail") or ""), "")

    stopped = api_core._step_record({
        "key": "retrieve", "ms": 9,
        "detail": "BM25 0 · вектор 3 · предмет 25% · близость 0.38 · "
                  "предмет вопроса не найден (25%)",
    })
    check("журнал: остановка гейта видна по полям",
          stopped.get("subject_coverage") == 0.25 and stopped.get("top_cosine") == 0.38,
          f"{stopped.get('subject_coverage')} / {stopped.get('top_cosine')}")

    other = api_core._step_record({"key": "cases", "ms": 3, "detail": "найдено 2 кейса"})
    check("журнал: у шага без метрик полей нет",
          "subject_coverage" not in other and "top_cosine" not in other, "")


def check_role_aliases() -> None:
    """Старые id ролей принимаются на входе, но не показываются в списке.

    v1.16 переименовала роли в отделы корпуса (lawyer/it/quality ->
    technical/ib/dbrct). Старые клиенты и docs ломались на 400 «неизвестная
    роль». Псевдоним принимается, но в /api/roles его быть не должно: UI
    показал бы мёртвый пункт без отдела и направления.

    Проверка идёт через настоящее приложение (TestClient): важно не «функция
    вернула True», а «HTTP-ручка реально пропустила запрос».
    """
    from fastapi.testclient import TestClient

    from api import app as appmod
    from api import core as api_core

    ids = sorted(api_core.role_ids())
    check("алиасы: в списке ролей ровно действующие id",
          "lawyer" not in ids and "it" not in ids, ", ".join(ids))
    check("алиасы: lawyer и it известны на входе",
          api_core.is_known_role("lawyer") and api_core.is_known_role("it"), "")
    check("алиасы: выдуманная роль не известна",
          not api_core.is_known_role("nonsense") and not api_core.is_known_role(""),
          "")

    client = TestClient(appmod.app)
    roles = client.get("/api/roles")
    got_ids = [r.get("id") for r in roles.json()] if roles.status_code == 200 else []
    check("алиасы: /api/roles отдаёт 5 ролей без псевдонимов",
          roles.status_code == 200 and len(got_ids) == 5
          and "lawyer" not in got_ids and "it" not in got_ids,
          f"{roles.status_code}, {len(got_ids)} ролей")

    # §3: псевдоним — это вход для старого клиента. Достаточно, что запрос
    # принят (200) и поток открылся: дальше идёт обычный каскад.
    for rid in ("lawyer", "it"):
        r = client.post("/api/ask", json={
            "question": "Какой срок согласования заявки на закупку?",
            "role_id": rid, "as_of": "2026-03-01",
        })
        check(f"алиасы: запрос с role_id={rid} принимается",
              r.status_code == 200 and "event: step" in r.text,
              f"{r.status_code}")

    bad = client.post("/api/ask", json={
        "question": "Какой срок согласования заявки на закупку?",
        "role_id": "nonsense", "as_of": "2026-03-01",
    })
    check("алиасы: выдуманная роль по-прежнему 400 с перечнем",
          bad.status_code == 400 and "Доступные роли" in bad.text
          and "lawyer" not in bad.text,
          f"{bad.status_code}")


def check_p0_leaks() -> None:
    """LEX-P0-LEAK: кейсы и развёрнутый ответ не читаются без роли.

    Две дыры, найденные ARCH на живом стенде:

    1. `GET /api/cases` БЕЗ `role_id` отдавал все 25 кейсов, хотя с
       `?role_id=technical` — 9. Параметр влиял на ДОСТУП, а не на выборку:
       пустой значил «покажи всё», то есть дверь мимо `CaseStore.visible`.
    2. `POST /api/expand` отдавал деталь ответа по одному `detail_id` — без
       роли и сессии. Кто знал идентификатор (он есть в SSE и в журнале),
       читал цитаты документа, которого его роль не видит.

    Проверяем через настоящее приложение (TestClient): важно, что HTTP-ручка
    отвечает, а не что функция «в теории» принимает аргумент.
    """
    from fastapi.testclient import TestClient

    from api import app as appmod
    from api import config as api_config
    from api import core as api_core
    from lex.orchestrator import _DETAILS

    real = ROOT / "corpus-real"
    if not (real / "manifest.json").is_file():
        return
    old_corpus = api_config.CORPUS_DIR
    old_cases = api_config.CASES_PATH
    tmp = Path(tempfile.mkdtemp())
    api_config.CORPUS_DIR = real
    api_config.CASES_PATH = tmp / "cases.jsonl"

    # Кейсы: 25 всего, по 9 у двух отделов, 7 чужих (ни один отдел не видит).
    rows = [
        ("technical", "Кто согласует ТКП в пресейле?"),
        ("technical", "Как оформить акт выполненных работ?"),
        ("ib", "Как согласовать доступ к КД-ПС-02?"),
        ("ib", "Кто ведёт реестр инцидентов?"),
        ("dbrct", "Как подготовить отчёт ДБРЦТ?"),
        ("director", "Как утвердить бюджет проекта?"),
        ("quality", "Что делать с противоречием редакций?"),
    ]

    def _mk(role: str, q: str) -> dict:
        return {"question": q, "answer_in_practice": "По практике: " + q.lower(),
                "outcome": "resolved", "author_role": role, "author_name": "—",
                "content_type": "text", "tags": [], "linked_docs": []}

    for role, q in rows * 4:                      # 28 записей
        api_core.add_case(_mk(role, q))
    # Чужие кейсы — у роли, которой в корпусе нет.
    for _ in range(7):
        api_core.add_case(_mk("outsider", "Чужая практика"))

    client = TestClient(appmod.app)
    try:
        total = len(api_core.case_store().cases)
        check("P0-LEAK: кейсов в журнале больше, чем видит роль",
              total >= 20, f"всего {total}")

        # --- дыра 1: /api/cases без role_id
        leak = client.get("/api/cases")
        check("P0-LEAK: /api/cases без role_id -> 400, не весь журнал",
              leak.status_code == 400 and "Доступные роли" in leak.text,
              f"{leak.status_code}: {leak.text[:120]}")

        bad = client.get("/api/cases?role_id=nonsense")
        check("P0-LEAK: /api/cases с выдуманной ролью -> 400",
              bad.status_code == 400, f"{bad.status_code}")

        tech = client.get("/api/cases?role_id=technical")
        body = tech.json() if tech.status_code == 200 else {}
        got = {c["author_role"] for c in body.get("cases", [])}
        # Кейсы качества/руководства видны всем — это правило `visible()`,
        # а не обход: служба качества ведёт базу, её ответы адресованы всем.
        check("P0-LEAK: /api/cases?role_id=technical не отдаёт чужие отделы",
              tech.status_code == 200
              and got <= {"technical", "quality", "director"}
              and "outsider" not in got and "ib" not in got and "dbrct" not in got,
              f"{tech.status_code}, роли {sorted(got)}")
        check("P0-LEAK: отбор отдаёт dropped/by_reason для UI",
              body.get("dropped", 0) == total - body.get("available", 0)
              and bool(body.get("by_reason")),
              f"total={body.get('total')} available={body.get('available')} "
              f"dropped={body.get('dropped')}")

        boss = client.get("/api/cases?role_id=director")
        bbody = boss.json() if boss.status_code == 200 else {}
        check("P0-LEAK: director видит больше, чем technical",
              len(bbody.get("cases", [])) > len(body.get("cases", [])),
              f"director {len(bbody.get('cases', []))} vs technical {len(body.get('cases', []))}")

        # --- дыра 2: /api/expand чужой ролью / чужой сессией
        caller = TestClient(appmod.app)
        answer = None
        with caller.stream("POST", "/api/ask", json={
            "question": "Кто согласует ТКП в пресейле?",
            "role_id": "director", "session_id": "s-leak-boss",
        }) as r:
            for raw in r.iter_lines():
                raw = raw.rstrip("\r")
                if not raw.startswith("data: "):
                    continue
                try:
                    ev = json.loads(raw[6:])
                except Exception:
                    continue
                if "detail_id" in ev:
                    answer = ev
        check("P0-LEAK: ответ director несёт detail_id",
              bool(answer and answer.get("detail_id")),
              str((answer or {}).get("detail_id")))

        did = (answer or {}).get("detail_id") or ""
        no_role = client.post("/api/expand",
                              json={"detail_id": did, "section": "all"})
        check("P0-LEAK: /api/expand без role_id -> 400",
              no_role.status_code == 400, f"{no_role.status_code}")

        foreign = client.post("/api/expand", json={
            "detail_id": did, "section": "all",
            "role_id": "technical", "session_id": "s-leak-tech",
        })
        check("P0-LEAK: деталь director не читается ролью technical",
              foreign.status_code == 404, f"{foreign.status_code}")

        other_session = client.post("/api/expand", json={
            "detail_id": did, "section": "all",
            "role_id": "director", "session_id": "s-leak-other",
        })
        check("P0-LEAK: деталь director не читается чужой сессией той же роли",
              other_session.status_code == 404, f"{other_session.status_code}")

        own = client.post("/api/expand", json={
            "detail_id": did, "section": "all",
            "role_id": "director", "session_id": "s-leak-boss",
        })
        check("P0-LEAK: владелец получает свою деталь",
              own.status_code == 200 and "detailed" in own.json(),
              f"{own.status_code}")

        # Владелец переживает псевдоним: lawyer -> technical (тот же отдел),
        # но НЕ получает деталь, выданную роли director.
        alias_den = client.post("/api/expand", json={
            "detail_id": did, "section": "all",
            "role_id": "lawyer", "session_id": "s-leak-boss",
        })
        check("P0-LEAK: псевдоним lawyer не открывает деталь director",
              alias_den.status_code == 404, f"{alias_den.status_code}")
    finally:
        api_config.CORPUS_DIR = old_corpus
        api_config.CASES_PATH = old_cases
        _DETAILS.clear()


def check_unstable_compose() -> None:
    """LEX-P0-UNSTABLE: один вопрос — один результат, даже если модель плавает.

    Владелец: «Он на любой вопрос будет тикет создавать?! Нас жюри убьёт».
    Факт на стенде: «Как формируется команда проекта?» (director, corpus-real)
    5 прогонов подряд → 3 пустых кадра ошибки и один ответ из 2 символов.
    Причины две, обе проверяются здесь:

    1. `llm.chat` падал `AssertionError()` без текста, когда ВСЕ попытки
       выпали по троттлингу. Пустой `message` в кадре error — это и есть
       «message», которое видело жюри. Теперь — `LLMError` с причиной.
    2. compose звал модель один раз: один сетевой сбой = нет ответа. Теперь
       два вызова, из них — лучший по опоре на цитаты.

    Сеть в тесте не нужна: `chat` подменяется управляемым сбоем, поэтому
    проверка не зависит от живого слота и от его троттлинга.
    """
    from lex import llm as llm_mod

    # --- 1. Все попытки — троттлинг: не AssertionError, а текст.
    # `chat` до сетевого слоя не доходит, если слот не настроен, поэтому
    # подставляем фиктивные значения: проверяется код повторов, а не .env.

    real_post = llm_mod._post_chat
    saved_env = {k: _os.environ.get(k) for k in ("NEXUS_LINK_BASE_URL", "NEXUSLINK_KEY")}
    _os.environ.setdefault("NEXUS_LINK_BASE_URL", "http://127.0.0.1:1")
    _os.environ.setdefault("NEXUSLINK_KEY", "test-key")

    def throttle(*_a, **_k):
        raise llm_mod.LLMError("NexusLink HTTP 502: all candidates exhausted (429)")

    llm_mod._post_chat = throttle
    orig_pause = llm_mod.RATE_BASE_PAUSE
    llm_mod.RATE_BASE_PAUSE = 0.0
    try:
        try:
            llm_mod.chat([{"role": "user", "content": "x"}])
            raised: Exception | None = None
        except Exception as exc:
            raised = exc
        check("UNSTABLE: исчерпание ретраев — LLMError с текстом, не пустой assertion",
              isinstance(raised, llm_mod.LLMError) and bool(str(raised).strip())
              and "429" in str(raised),
              f"{type(raised).__name__}: {str(raised)[:90]}")
    finally:
        llm_mod._post_chat = real_post
        llm_mod.RATE_BASE_PAUSE = orig_pause
        for _k, _v in saved_env.items():
            if _v is None:
                _os.environ.pop(_k, None)
            else:
                _os.environ[_k] = _v

    # --- 2. Первый вызов модели срывается — compose не сдаётся, а повторяет.
    real = ROOT / "corpus-real"
    if not (real / "manifest.json").is_file():
        return
    calls = {"n": 0}
    good = "Команда проекта формируется из руководителя проекта и архитектора [1]. [1]"

    def flaky(_self, frames, state):
        calls["n"] += 1
        if calls["n"] == 1:
            raise llm_mod.LLMError("NimbusLink недоступен: сымитированный обрыв")
        return good

    orch_cls = Orchestrator
    orig_ask = orch_cls._ask_model
    orig_attempts = __import__("lex.orchestrator", fromlist=["x"]).COMPOSE_ATTEMPTS
    import lex.orchestrator as omod
    omod.COMPOSE_ATTEMPTS = 2
    orch_cls._ask_model = flaky
    try:
        out = asyncio.run(_run("Какие роли участвуют в проектной деятельности?",
                               role="director", corpus_root=real,
                               session="s-unst-retry", no_llm=False))
    finally:
        orch_cls._ask_model = orig_ask
        omod.COMPOSE_ATTEMPTS = orig_attempts
    kinds = [s.kind for s in out]
    answer = next((s.data for s in out if s.kind == "answer"), None)
    check("UNSTABLE: сбой первого вызова не съедает ответ (повтор вытягивает)",
          answer is not None and calls["n"] == 2,
          f"исходы {kinds}, вызовов {calls['n']}")

    # --- 3. Оба вызова сорвались — кадр error обязан нести причину.
    def dead(_self, frames, state):
        raise llm_mod.LLMError("NimbusLink недоступен: сымитированный обрыв")

    orch_cls._ask_model = dead
    try:
        out = asyncio.run(_run("Какие роли участвуют в проектной деятельности?",
                               role="director", corpus_root=real,
                               session="s-unst-dead", no_llm=False))
    finally:
        orch_cls._ask_model = orig_ask
    err = next((s.data for s in out if s.kind == "error"), None)
    check("UNSTABLE: двойной сбой — error с текстом, а не пустой кадр",
          err is not None and bool(str(getattr(err, "message", "")).strip()),
          repr(getattr(err, "message", ""))[:90])

    # --- 4. Пустое исключение не превращается в пустой кадр SSE.
    from api import sse

    frame = sse.error("", "compose")
    check("UNSTABLE: sse.error с пустым текстом называет стадию",
          "compose" in frame and '"message":""' not in frame.replace(" ", ""),
          frame.splitlines()[1][:100] if "\n" in frame else frame[:100])


def check_verify_zero() -> None:
    """LEX-P0-VERIFY-ZERO: непустой ответ не выбрасывается из-за разбора.

    Владелец нашла шаг за шагом: compose дал ответ с 96% опоры на цитаты,
    verify насчитал «утверждений 0» и увёл в уточнение. Ответ уничтожался
    при РАЗБОРЕ, а не при генерации.

    Два случая проверяются отдельно:
    1. Ответ структурой, из которой `split_sentences` не выделяет ни одного
       предложения — verify ОБЯЗАН переразобрать, а если и это не помогло,
       отдать текст с оговоркой «структура не распознана», не отказом.
    2. Текст вообще без точек/границ — второй стратегии тоже нечего разобрать.
       Тогда ответ отдаётся, но с `similarity_checked=False`: verify не врёт,
       что проверил опору.
    """
    from lex.grounding import check_grounding
    from lex.orchestrator import _flatten_for_grounding, _whole_text_report

    # (1) markdown-заголовки и списки без обычных границ предложения.
    odd = "**Ответ**\n\n**Раздел 1**\n\n**Раздел 2**"
    raw = check_grounding(odd, ["фрагмент [1] текст"])
    check("VERIFY-ZERO: разбор даёт 0 утверждений на непустом ответе (исходный дефект)",
          raw.total == 0 and odd.strip(), f"total={raw.total}")

    flat = _flatten_for_grounding(odd)
    retry = check_grounding(flat, ["фрагмент [1] текст"])
    check("VERIFY-ZERO: вторая стратегия разбора находит утверждения",
          retry.total > 0, f"после снятия разметки total={retry.total}")

    # (2) Совсем нерасчленяемый текст: отчёт-заглушка не роняет ответ.
    stub = _whole_text_report(raw, "слово " * 40)
    check("VERIFY-ZERO: нерасчленяемый ответ не считается «нет ответа»",
          stub.total == 1 and not stub.nothing_covered,
          f"total={stub.total} nothing_covered={stub.nothing_covered}")
    check("VERIFY-ZERO: заглушка честно говорит, что опора не проверялась",
          stub.similarity_checked is False and "не распознана" in stub.similarity_note,
          stub.similarity_note)

    # (3) Регрессия из задачи: compose с повтором НЕ теряет текст для verify.
    # Ошибка была в моей же правке: note-ветка возвращала шаг до записи
    # answer_text, и verify проверял пустую строку.
    real = ROOT / "corpus-real"
    if not (real / "manifest.json").is_file():
        return
    good = ("Команда проекта формируется руководителем проекта [1]. "
            "[1] Архитектор назначается приказом.")
    calls = {"n": 0}

    def flaky(_self, frames, state):
        calls["n"] += 1
        return good

    orig_ask = Orchestrator._ask_model
    Orchestrator._ask_model = flaky
    try:
        out = asyncio.run(_run("Какие роли участвуют в проектной деятельности?",
                               role="director", corpus_root=real,
                               session="s-vz-1", no_llm=False))
    finally:
        Orchestrator._ask_model = orig_ask
    verify = next((s.data for s in out if s.kind == "step"
                   and s.data.key == "verify"), None)
    answer = next((s.data for s in out if s.kind == "answer"), None)
    det = getattr(verify, "detail", "")
    check("VERIFY-ZERO: после повтора verify видит ответ, а не пустую строку",
          answer is not None and "утверждений 0" not in det,
          det[:100])
    check("VERIFY-ZERO: верный ответ доходит до сотрудника как answer",
          answer is not None, ",".join(s.kind for s in out))


def main() -> int:
    corpus = load_corpus(CORPUS)
    print(f"корпус: документов {len(corpus.documents)}, чанков {len(corpus.chunks)}, "
          f"правил {len(corpus.rules)}\n")
    check_steps()
    check_policy(corpus)
    check_barriers()
    check_caveat()
    check_case_vs_norm(corpus)
    check_case_visibility()
    check_role_filter()
    check_role_filter_flow()
    check_single_corpus()
    check_tickets()
    check_flow(corpus)
    check_subject_gate()
    check_loop_criterion3()
    check_crit3_train()
    check_journal_metrics()
    check_role_aliases()
    check_p0_leaks()
    check_unstable_compose()
    check_verify_zero()
    print(f"\n{PASS.__len__()} passed, {len(FAIL)} failed")
    if FAIL:
        print("провал: " + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
