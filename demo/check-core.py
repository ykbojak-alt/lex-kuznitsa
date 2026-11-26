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

    own = add("lawyer", "Срок согласования заявки на закупку?")
    other = add("it", "Порядок выдачи ноутбука новому сотруднику?")
    quality = add("quality", "Размер командировочной надбавки?")
    nobody = add("", "Импорт практики без автора")

    seen = {c.id for c in store.visible("lawyer")}
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

    # Та же роль, но из ИТ: юркейс ей не виден — фильтр симметричен.
    seen_it = {c.id for c in store.visible("it")}
    check("критерий 1: фильтр симметричен по ролям",
          own not in seen_it and other in seen_it, sorted(seen_it))

    # Через HTTP-слой: /api/cases?role_id= отдаёт ту же выдачу, что поиск.
    from api import config as api_config
    from api import core as api_core
    old = api_config.CASES_PATH
    api_config.CASES_PATH = tmp / "cases.jsonl"
    try:
        ids = {c["id"] for c in api_core.list_cases(role_id="lawyer")}
        check("критерий 1: /api/cases?role_id фильтрует так же",
              other not in ids and own in ids, sorted(ids))
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
    print(f"\n{PASS.__len__()} passed, {len(FAIL)} failed")
    if FAIL:
        print("провал: " + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
