#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Генератор синтетического обезличенного корпуса документов для спринта ИИкузницы.

Зачем: RAG-контур должен провереняться на данных, похожих на заказчика, но
**без единого реального документа заказчика**. Этот генератор собирает
синтетические регламенты и инструкции в формате технического задания,
которые можно положить в RAG-индекс, задать к ним вопросы и проверить
качество поиска.

Корпус состоит из двух частей:
  * обычные документы (регламенты, инструкции, стандарты) — по умолчанию 20;
  * **редакции конфликтующих правил** — 3 правила × 2–3 редакции (7 документов).
    Один и тот же вопрос («какой срок действует?») имеет разные ответы в
    зависимости от даты: действует редакция, чья дата вступления наступила, но
    не позже заданной даты; правило, ещё не вступившее в силу, не применяется;
    на дату раньше первой редакции правильный ответ — обоснованный отказ.
    Это проверяет логику действующего документа и отказ вместо выдумывания.

Безопасность данных:
  * все данные вымышлены — генератор не читает и не копирует никакие
    внешние источники, у него нет сетевого доступа;
  * e-mail домены только из зарезервированного RFC 2606 (`example.invalid`);
  * телефоны — маски `+7-000-000-00-01`, ИНН/ОГРН — осмысленные, но
    невалидные по контрольному разряду;
  * каждый документ помечен водяным знаком «СИНТЕТИЧЕСКИЙ ДОКУМЕНТ».

Использование:
    python demo/gen-sample-corpus.py                       # 24 документа в sample-corpus/
    python demo/gen-sample-corpus.py --count 30 --seed 7   # другой набор
    python demo/gen-sample-corpus.py --out /tmp/corpus --json
    python demo/gen-sample-corpus.py --self-test           # только самопроверка

Только стандартная библиотека: скрипт должен работать на любой машине спринта
до установки зависимостей.

Спонсор: ИИкузница (спринт-подготовка). Лицензия: MIT (как у репозитория).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

WATERMARK = "СИНТЕТИЧЕСКИЙ ДОКУМЕНТ — все данные вымышлены, реальных документов заказчика здесь нет."

DOC_TYPES = (
    ("Регламент", "регламент"),
    ("Инструкция", "инструкция"),
    ("Регламент", "регламент"),
    ("Стандарт", "стандарт"),
    ("Порядок", "порядок"),
    ("Чек-лист", "чек-лист"),
)

DEPARTMENTS = (
    "Производственный отдел",
    "Отдел закупок",
    "Служба логистики",
    "Финансово-экономический отдел",
    "Юридический отдел",
    "Отдел охраны труда",
    "ИТ-служба",
    "Отдел качества",
    "Отдел продаж",
    "Служба главного инженера",
)

COMPANIES = (
    "АО «Пример-Маш»",
    "ООО «Ромашка-Логистик»",
    "АО «Вектор-Пром»",
    "ООО «Северный Контур»",
    "АО «Гранит-Строй»",
)

PERSONS = (
    "А. П. Ковалёв",
    "М. С. Гордеева",
    "Д. А. Севастьянов",
    "Е. В. Ланская",
    "И. О. Терехин",
    "Н. А. Белова",
    "С. Ю. Мартынов",
    "К. Р. Одинцов",
)

SYSTEMS = (
    "АРМ «Заявка»",
    "ERP-контур «Контур-Факт»",
    "Портал «Регламенты»",
    "СЭД «Дело»",
    "СКУД «Пропуск»",
    "Система «Закупки-Онлайн»",
    "Ведомость «Выплаты»",
)

PRODUCTS = (
    "комплект уплотнения КУ-120",
    "подшипниковый узел ПУ-45",
    "гидравлический клапан ГК-7",
    "кабельный жгут ЖК-16",
    "корпус насоса НП-88",
)

# Требования пишутся по шаблону «Должно + глагол + объект + срок», чтобы
# документы выглядели как реальные регламенты и давали разнообразный текст.
REQUIREMENT_TEMPLATES = (
    "Система {system} должна {action} в течение {term} с момента фиксации события.",
    "Оператор обязан {action} до конца рабочего дня {term}.",
    "Ответственный за {dept} {action} не позднее {term}.",
    "При отклонении заявки система {system} {action} автоматически.",
    "Руководитель {dept} {action} с фиксацией результата в журнале.",
    "Заявка, оформленная в {system}, {action} и передаётся в работу.",
)

ACTIONS = (
    "формировать уведомление о сроке",
    "блокировать дальнейшее редактирование карточки",
    "сохранять результат проверки в реестре",
    "запрашивать подтверждение уполномоченного лица",
    "маршрутизировать обращение в профильный отдел",
    "фиксировать причину отклонения",
    "печатать акт с указанием фактических дат",
    "направлять сводку руководителю подразделения",
)

TERMS = (
    "одного рабочего дня",
    "трёх рабочих дней",
    "восьми часов",
    "следующего рабочего дня",
    "пяти календарных дней",
    "двух часов",
)

STORAGE_TERMS = (
    "не менее трёх лет",
    "не менее пяти лет",
    "не менее одного года",
    "не менее двух лет",
)

# --- Конфликтующие редакции -------------------------------------------------
# Один и тот же вопрос («какой срок действует?») должен иметь разные ответы
# в зависимости от даты: более поздняя редакция вытесняет раннюю, а правило,
# ещё не вступившее в силу, применяться не должно. Это проверяет логику
# «действующего документа» и обоснованного отказа.
AS_OF = date(2026, 11, 26)  # опорная дата: день старта спринта

CONFLICT_RULES = (
    {
        "key": "SR-01",
        "topic": "согласования заявки на закупку",
        "field": "Срок согласования заявки",
        "short": "срок согласования заявки",
        "revisions": (
            ("2026-01-15", "трёх рабочих дней"),
            ("2026-06-01", "пяти рабочих дней"),
            ("2027-01-01", "семи рабочих дней"),
        ),
    },
    {
        "key": "SR-02",
        "topic": "хранения журнала заявок",
        "field": "Срок хранения журнала",
        "short": "срок хранения журнала",
        "revisions": (
            ("2026-02-01", "не менее одного года"),
            ("2026-09-01", "не менее трёх лет"),
        ),
    },
    {
        "key": "SR-03",
        "topic": "автоматического одобрения заявки",
        "field": "Порог автоматического одобрения",
        "short": "порог автоматического одобрения",
        "revisions": (
            ("2026-03-10", "50 000 рублей"),
            ("2026-08-01", "150 000 рублей"),
        ),
    },
)

SECTIONS = (
    "Назначение и область применения",
    "Термины, определения и сокращения",
    "Нормативные ссылки",
    "Роли и ответственность",
    "Порядок выполнения процедуры",
    "Требования к оформлению результата",
    "Нефункциональные требования",
    "Приёмочные критерии",
    "Ограничения и исключения",
    "Порядок хранения и аудита",
)

CONSTRAINTS = (
    "Не допускается обработка документов, содержащих сведения ограниченного доступа, "
    "если для них не оформлено отдельное разрешение.",
    "Результат процедуры хранится в {system} не менее срока, установленном внутренним регламентом.",
    "Решение не может быть принято автоматически, если сумма операции превышает установленный порог.",
    "Замена исполнителя оформляется только по письменной заявке руководителя подразделения.",
)

ACCEPTANCE = (
    "Сценарий успешно выполняется на тестовом контуре без ручного вмешательства.",
    "Результат содержит дату, исполнителя и фактический срок.",
    "Отклонение фиксируется с указанием причины и доступно для проверки.",
    "Повторный запуск не создаёт дублирующих записей.",
    "Формат выгрузки соответствует утверждённой форме отчёта.",
)

QUESTION_TEMPLATES = (
    ("Какой срок установлен для выполнения процедуры в документе {doc_title}?",
     "doc_id", "{term}"),
    ("Кто отвечает за маршрутизацию обращения в документе {doc_title}?",
     "doc_id", "{dept}"),
    ("Что происходит при отклонении заявки по документу {doc_title}?",
     "doc_id", "{action}"),
    ("В какой системе ведётся работа по документу {doc_title}?",
     "doc_id", "{system}"),
)


@dataclass
class Doc:
    """Синтетический документ и его метаданные."""

    doc_id: str
    kind: str
    subtype: str
    title: str
    company: str
    dept: str
    owner: str
    system: str
    product: str
    issued: date
    version: str
    body: str = ""
    path: Path | None = None
    sha256: str = ""
    words: int = 0
    sections: list[str] = field(default_factory=list)
    rule_key: str = ""


def _slug(text: str) -> str:
    keep = []
    for ch in text.lower():
        if ch.isalnum():
            keep.append(ch)
        elif ch in " -_/":
            keep.append("-")
    slug = "".join(keep)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:80]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _org_inn(rng: random.Random) -> str:
    """Осмысленный, но заведомо невалидный ИНН (контрольные разряды не сходятся)."""
    return "77" + "".join(str(rng.randint(0, 9)) for _ in range(9))


def _contract_no(rng: random.Random) -> str:
    return f"{rng.randint(10, 99)}-{rng.randint(100, 999)}/{rng.randint(2026, 2027)}"


def make_doc(rng: random.Random, index: int, issued: date) -> Doc:
    kind, subtype = DOC_TYPES[index % len(DOC_TYPES)]
    dept = rng.choice(DEPARTMENTS)
    company = rng.choice(COMPANIES)
    owner = rng.choice(PERSONS)
    system = rng.choice(SYSTEMS)
    product = rng.choice(PRODUCTS)
    version = f"{rng.randint(1, 3)}.{rng.randint(0, 9)}"
    doc_id = f"ТЗ-ЭКЗ-{index + 1:04d}"

    title = f"{kind} «{_title_topic(rng, dept, product)}»"
    sections = list(SECTIONS)
    rng.shuffle(sections)
    sections = sections[: rng.randint(7, 10)]

    doc = Doc(
        doc_id=doc_id,
        kind=kind,
        subtype=subtype,
        title=title,
        company=company,
        dept=dept,
        owner=owner,
        system=system,
        product=product,
        issued=issued,
        version=version,
        sections=sections,
    )

    reqs = []
    for _ in range(rng.randint(6, 11)):
        reqs.append(
            rng.choice(REQUIREMENT_TEMPLATES).format(
                system=system,
                action=rng.choice(ACTIONS),
                term=rng.choice(TERMS),
                dept=dept,
            )
        )
    constraints = [
        c.format(system=system) if "{system}" in c else c
        for c in rng.sample(CONSTRAINTS, k=min(3, len(CONSTRAINTS)))
    ]
    acceptance = rng.sample(ACCEPTANCE, k=3)

    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"> {WATERMARK}")
    lines.append("")
    lines.append("## Паспорт документа")
    lines.append("")
    lines.append("| Поле | Значение |")
    lines.append("| --- | --- |")
    lines.append(f"| Идентификатор | {doc_id} |")
    lines.append(f"| Вид | {kind.lower()} |")
    lines.append(f"| Организация | {company} |")
    lines.append(f"| Подразделение | {dept} |")
    lines.append(f"| Ответственный | {owner} |")
    lines.append(f"| Контур | {system} |")
    lines.append(f"| Номенклатурная позиция | {product} |")
    lines.append(f"| Договор-основание | {_contract_no(rng)} |")
    lines.append(f"| ИНН контрагента (условный) | {_org_inn(rng)} |")
    lines.append(f"| Версия | {version} |")
    lines.append(f"| Дата введения | {issued.isoformat()} |")
    lines.append(f"| Срок хранения | {rng.choice(STORAGE_TERMS)} |")
    lines.append("")

    body_by_section: dict[str, list[str]] = {}
    for s in sections:
        body_by_section[s] = []

    for s in sections:
        chunk: list[str] = []
        if s == "Назначение и область применения":
            chunk.append(
                f"Документ определяет порядок работы {dept} компании {company} с "
                f"номенклатурной позицией «{product}» в контуре {system}."
            )
            chunk.append(
                "Область применения — процедуры закупки, приёмки и сопровождения "
                "внутри одного юридического лица; смежные организации не охватываются."
            )
        elif s == "Термины, определения и сокращения":
            for term, defn in (
                ("Заявка", "обращение инициатора, зарегистрированное в " + system),
                ("Исполнитель", "должностное лицо, назначенное по заявке"),
                ("Отказ", "закрытие обращения без исполнения с указанием причины"),
                ("Контур", f"информационная система {system} и её журналы"),
            ):
                chunk.append(f"**{term}** — {defn}.")
        elif s == "Нормативные ссылки":
            chunk.append(
                "Перечень внутренних документов, действующих на дату введения "
                "настоящей редакции, приводится в приложении к регламенту "
                f"подразделения «{dept}»."
            )
        elif s == "Роли и ответственность":
            chunk.append(f"Ответственный: {owner}.")
            chunk.append(
                f"Инициатор отвечает за полноту исходных данных и правильное "
                f"отнесение позиции «{product}»."
            )
            chunk.append(
                f"Руководитель {dept} согласует сроки и закрывает спорные случаи."
            )
        elif s == "Порядок выполнения процедуры":
            chunk.append("Порядок выполнения процедуры состоит из следующих шагов.")
            for i, step in enumerate(
                (
                    f"регистрация обращения в {system}",
                    "проверка комплектности исходных данных",
                    "назначение исполнителя",
                    "исполнение и фиксация результата",
                    "уведомление инициатора и закрытие обращения",
                ),
                start=1,
            ):
                chunk.append(f"{i}. {step.capitalize()}.")
        elif s == "Требования к оформлению результата":
            chunk.extend(f"- {r}" for r in reqs[: rng.randint(3, 5)])
        elif s == "Нефункциональные требования":
            chunk.append(
                "Регламент обработки не должен превышать установленный срок "
                "при штатной нагрузке подразделения."
            )
            chunk.append(
                "Журнал действий хранится в " + system + " и доступен для проверки "
                "в течение срока хранения."
            )
        elif s == "Приёмочные критерии":
            chunk.extend(f"- {a}" for a in acceptance)
        elif s == "Ограничения и исключения":
            chunk.extend(f"- {c}" for c in constraints)
        elif s == "Порядок хранения и аудита":
            chunk.append(
                "Записи журнала хранятся в " + system + "; выборка за проверяемый "
                "период оформляется отдельной заявкой."
            )
            chunk.append(
                "Доступ к журналу предоставляется только сотрудникам подразделения "
                "и по служебной необходимости."
            )
        body_by_section[s] = chunk

    lines.append("## Содержание")
    lines.append("")
    for s in sections:
        lines.append(f"- {s}")
    lines.append("")

    for s in sections:
        lines.append(f"## {s}")
        lines.append("")
        lines.extend(body_by_section[s])
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"*Синтетический документ {doc_id}, сформирован автоматически для проверки RAG-контура.*")

    doc.body = "\n".join(lines) + "\n"
    doc.words = len(doc.body.split())
    return doc


def make_conflict_doc(
    rng: random.Random,
    rule: dict,
    rule_index: int,
    revision_no: int,
    effective: date,
    value: str,
    prev_effective: date | None,
) -> Doc:
    """Документ-редакция одного правила: та же тема, другая дата вступления."""
    dept = rng.choice(DEPARTMENTS)
    company = rng.choice(COMPANIES)
    owner = rng.choice(PERSONS)
    system = rng.choice(SYSTEMS)
    doc_id = f"ТЗ-ЭКЗ-СР{rule_index:02d}-Р{revision_no}"
    title = f"Регламент «{rule['topic']}» (редакция {revision_no} от {effective.isoformat()})"

    doc = Doc(
        doc_id=doc_id,
        kind="Регламент",
        subtype="редакция правила",
        title=title,
        company=company,
        dept=dept,
        owner=owner,
        system=system,
        product=rng.choice(PRODUCTS),
        issued=effective,
        version=f"{revision_no}.0",
        sections=[
            "Назначение и область применения",
            "Роли и ответственность",
            "Порядок выполнения процедуры",
            f"Правило {rule['key']}",
            "Приёмочные критерии",
            "Порядок хранения и аудита",
        ],
        rule_key=rule["key"],
    )

    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"> {WATERMARK}")
    lines.append("")
    lines.append("## Паспорт документа")
    lines.append("")
    lines.append("| Поле | Значение |")
    lines.append("| --- | --- |")
    lines.append(f"| Идентификатор | {doc_id} |")
    lines.append("| Вид | регламент (редакция правила) |")
    lines.append(f"| Организация | {company} |")
    lines.append(f"| Подразделение | {dept} |")
    lines.append(f"| Ответственный | {owner} |")
    lines.append(f"| Контур | {system} |")
    lines.append(f"| Ключ правила | {rule['key']} |")
    lines.append(f"| Номер редакции | {revision_no} из {len(rule['revisions'])} |")
    lines.append(f"| Действует с | {effective.isoformat()} |")
    if prev_effective is not None:
        lines.append(f"| Заменяет редакцию от | {prev_effective.isoformat()} |")
    lines.append(f"| Дата введения | {effective.isoformat()} |")
    lines.append("")
    lines.append("## Содержание")
    lines.append("")
    for s in doc.sections:
        lines.append(f"- {s}")
    lines.append("")

    lines.append("## Назначение и область применения")
    lines.append("")
    lines.append(
        f"Документ устанавливает правило {rule['key']} для {dept} компании {company} "
        f"и применяется с {effective.isoformat()}."
    )
    lines.append("")
    lines.append("## Роли и ответственность")
    lines.append("")
    lines.append(f"Ответственный: {owner}.")
    lines.append("")
    lines.append("## Порядок выполнения процедуры")
    lines.append("")
    lines.append("1. Определить действующую на дату запроса редакцию правила.")
    lines.append("2. Применить её значение.")
    lines.append("3. Зафиксировать применённую редакцию в журнале.")
    lines.append("")
    lines.append(f"## Правило {rule['key']}")
    lines.append("")
    lines.append(f"**{rule['field']}:** {value}.")
    lines.append("")
    lines.append(f"Редакция {revision_no} действует с {effective.isoformat()}.")
    if prev_effective is not None:
        lines.append(
            f"С {effective.isoformat()} применяется это значение; редакция от "
            f"{prev_effective.isoformat()} и ранее — не применяется."
        )
    else:
        lines.append("Это первая редакция правила; ранее правило не действовало.")
    lines.append("")
    lines.append("## Приёмочные критерии")
    lines.append("")
    lines.append("- Значение правила определяется редакцией, действующей на указанную дату.")
    lines.append("- Будущая редакция не применяется до даты вступления в силу.")
    lines.append("- Если на дату запроса ни одна редакция не действует, правило не применяется.")
    lines.append("")
    lines.append("## Порядок хранения и аудита")
    lines.append("")
    lines.append(f"Журнал применённых редакций ведётся в {system}.")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"*Синтетический документ {doc_id} — редакция правила {rule['key']}.*")

    doc.body = "\n".join(lines) + "\n"
    doc.words = len(doc.body.split())
    return doc


def _title_topic(rng: random.Random, dept: str, product: str) -> str:
    topics = (
        "согласования заявки на закупку",
        "приёмки поступающей партии",
        "эскалации задержки поставки",
        "оформления замены комплектующих",
        "проверки качества входного контроля",
        "подготовки сводки по отгрузкам",
        "работы с рекламацией поставщику",
        "инвентаризации складских остатков",
        "согласования договорных изменений",
        "закрытия производственного заказа",
    )
    return rng.choice(topics)


def write_corpus(out_dir: Path, count: int, seed: int, clean: bool) -> dict:
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    if clean:
        for child in sorted(out_dir.glob("*.md")):
            if child.name not in {"INDEX.md"}:
                child.unlink()
        for child in sorted(out_dir.glob("*.jsonl")):
            child.unlink()

    today = date(2026, 11, 26)
    docs: list[Doc] = []
    for i in range(count):
        issued = today - timedelta(days=rng.randint(30, 700))
        doc = make_doc(rng, i, issued)
        fname = f"{doc.doc_id}-{_slug(doc.title)}.md"
        path = out_dir / fname
        path.write_text(doc.body, encoding="utf-8")
        doc.path = path
        doc.sha256 = _sha256(doc.body)
        docs.append(doc)

    # --- Конфликтующие редакции одного правила --------------------------------
    conflict_meta: list[dict] = []
    conflict_docs: list[Doc] = []
    for ri, rule in enumerate(CONFLICT_RULES, start=1):
        revisions: list[dict] = []
        prev: date | None = None
        for rev_no, (eff_str, value) in enumerate(rule["revisions"], start=1):
            eff = date.fromisoformat(eff_str)
            doc = make_conflict_doc(rng, rule, ri, rev_no, eff, value, prev)
            fname = f"{doc.doc_id}-{_slug(doc.title)}.md"
            path = out_dir / fname
            path.write_text(doc.body, encoding="utf-8")
            doc.path = path
            doc.sha256 = _sha256(doc.body)
            conflict_docs.append(doc)
            revisions.append(
                {
                    "revision": rev_no,
                    "doc_id": doc.doc_id,
                    "effective": eff_str,
                    "value": value,
                    "file": fname,
                    "sha256": doc.sha256,
                }
            )
            prev = eff
        conflict_meta.append(
            {
                "key": rule["key"],
                "field": rule["field"],
                "short": rule["short"],
                "topic": rule["topic"],
                "revisions": revisions,
                "first_effective": rule["revisions"][0][0],
                "in_force_at_as_of": _rule_answer(rule, AS_OF),
            }
        )
    docs.extend(conflict_docs)

    manifest = {
        "generator": "demo/gen-sample-corpus.py",
        "seed": seed,
        "count": len(docs),
        "base_documents": count,
        "conflict_documents": len(conflict_docs),
        "as_of": AS_OF.isoformat(),
        "watermark": WATERMARK,
        "conflict_rules": conflict_meta,
        "documents": [
            {
                "doc_id": d.doc_id,
                "title": d.title,
                "kind": d.kind,
                "subtype": d.subtype,
                "department": d.dept,
                "system": d.system,
                "owner": d.owner,
                "version": d.version,
                "issued": d.issued.isoformat(),
                "rule_key": d.rule_key,
                "file": d.path.name if d.path else None,
                "sha256": d.sha256,
                "words": d.words,
                "sections": d.sections,
            }
            for d in docs
        ],
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    index_lines = [
        "# Sample corpus — индекс",
        "",
        f"> {WATERMARK}",
        "",
        f"Документов: **{len(docs)}** (обычных {count} + редакций правил {len(conflict_docs)}). "
        f"Seed: `{seed}`. Опорная дата: `{AS_OF.isoformat()}`. Генератор: `demo/gen-sample-corpus.py`.",
        "",
        "## Конфликтующие редакции правил",
        "",
        "Один и тот же вопрос имеет разные ответы в зависимости от даты: действует та "
        "редакция, чья дата вступления наступила, но не позже заданной даты. Правило, "
        "ещё не вступившее в силу, не применяется.",
        "",
        "| Ключ | Правило | Редакции (дата → значение) | Действует на "
        f"{AS_OF.isoformat()} |",
        "| --- | --- | --- | --- |",
    ]
    for meta in conflict_meta:
        revs = "; ".join(f"{r['effective']} → {r['value']}" for r in meta["revisions"])
        in_force = meta["in_force_at_as_of"] or "ничего (правило не введено)"
        index_lines.append(f"| {meta['key']} | {meta['field']} | {revs} | {in_force} |")
    index_lines += [
        "",
        "## Все документы",
        "",
        "| ID | Вид | Документ | Раздел | Файл | SHA-256 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for d in docs:
        index_lines.append(
            f"| {d.doc_id} | {d.kind} | {d.title} | {d.dept} | `{d.path.name}` | `{d.sha256[:16]}…` |"
        )
    index_lines += [
        "",
        "## Как перегенерировать",
        "",
        "```bash",
        "python demo/gen-sample-corpus.py --count 20 --seed 20261126 --clean",
        "```",
        "",
        "## Как использовать в RAG",
        "",
        "1. Индексировать `sample-corpus/*.md` (кроме `INDEX.md`) в тестовый индекс.",
        "2. Взять вопросы из `sample-corpus/questions.jsonl` и сверить ответы.",
        "3. Проверить, что в ответе нет вымышленных фактов и что указан `doc_id`.",
        "4. Для вопросов с `refusal_expected: true` правильный ответ — обоснованный отказ.",
        "",
        "> Все содержимое синтетическое. Файлы не содержат данных реального заказчика.",
    ]
    (out_dir / "INDEX.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    q_lines = []
    for d in docs:
        if d.rule_key:
            continue  # по редакциям правил вопросы строятся отдельно
        for q_tpl, field_name, value_tpl in QUESTION_TEMPLATES:
            q = q_tpl.format(doc_title=d.title)
            answer = value_tpl.format(
                term=_first_term(d),
                dept=d.dept,
                action=_first_action(d),
                system=d.system,
            )
            q_lines.append(
                json.dumps(
                    {
                        "question": q,
                        "expected_doc_id": d.doc_id,
                        "expected_answer": answer,
                        "field": field_name,
                        "kind": "generic",
                        "rule_key": "",
                        "as_of": "",
                        "refusal_expected": False,
                    },
                    ensure_ascii=False,
                )
            )

    # Вопросы по правилам: действующая редакция, прошлая редакция, будущая
    # редакция и обоснованный отказ (дата раньше самой первой редакции).
    for meta in conflict_meta:
        rule = next(r for r in CONFLICT_RULES if r["key"] == meta["key"])
        revs = [(date.fromisoformat(r["effective"]), r) for r in meta["revisions"]]
        first_eff = revs[0][0]
        second_eff = revs[1][0] if len(revs) > 1 else first_eff
        last_eff = revs[-1][0]
        cases = [
            ("до первой редакции (ожидается отказ)", first_eff - timedelta(days=45), True),
            ("между редакциями", second_eff - timedelta(days=20), False),
            ("на опорную дату", AS_OF, False),
            ("после последней редакции", last_eff + timedelta(days=60), False),
        ]
        for label, as_of, is_refusal in cases:
            answer = _rule_answer(rule, as_of)
            expected_doc = None
            for eff, rev in revs:
                if eff <= as_of:
                    expected_doc = rev["doc_id"]
            q_lines.append(
                json.dumps(
                    {
                        "question": (
                            f"Какое значение действует для правила {rule['key']} "
                            f"({rule['short']}) на {as_of.isoformat()}?"
                        ),
                        "expected_doc_id": expected_doc,
                        "expected_answer": (
                            answer if answer else "правило на эту дату не введено — обоснованный отказ"
                        ),
                        "field": rule["field"],
                        "kind": "rule_asof",
                        "rule_key": rule["key"],
                        "as_of": as_of.isoformat(),
                        "refusal_expected": is_refusal or answer is None,
                        "case": label,
                    },
                    ensure_ascii=False,
                )
            )
    (out_dir / "questions.jsonl").write_text("\n".join(q_lines) + "\n", encoding="utf-8")

    return {"manifest": manifest, "questions": len(q_lines)}


def _first_term(doc: Doc) -> str:
    for line in doc.body.splitlines():
        if "в течение" in line or "до конца рабочего дня" in line:
            for t in TERMS:
                if t in line:
                    return t
    return TERMS[0]


def _first_action(doc: Doc) -> str:
    for line in doc.body.splitlines():
        if "фиксировать причину отклонения" in line:
            return "фиксировать причину отклонения"
    return ACTIONS[0]


def _rule_answer(rule: dict, as_of: date) -> str | None:
    """Значение правила, действующее на дату `as_of`; None — ни одна редакция не действует."""
    value = None
    for eff_str, rev_value in rule["revisions"]:
        if date.fromisoformat(eff_str) <= as_of:
            value = rev_value
    return value


def validate(out_dir: Path, manifest: dict, count: int) -> list[str]:
    """Самопроверка: количество, уникальность, непустота, отсутствие чужих доменов."""
    problems: list[str] = []
    docs = manifest["documents"]
    expected_total = count + sum(len(r["revisions"]) for r in manifest.get("conflict_rules", []))
    if len(docs) != expected_total:
        problems.append(f"ожидалось {expected_total} документов, получено {len(docs)}")
    ids = [d["doc_id"] for d in docs]
    if len(set(ids)) != len(ids):
        problems.append("идентификаторы документов не уникальны")
    for d in docs:
        p = out_dir / (d["file"] or "")
        if not p.exists():
            problems.append(f"нет файла {d['file']}")
            continue
        text = p.read_text(encoding="utf-8")
        if not text.strip():
            problems.append(f"пустой файл {d['file']}")
        if _sha256(text) != d["sha256"]:
            problems.append(f"хэш не совпадает для {d['file']}")
        if WATERMARK not in text:
            problems.append(f"нет водяного знака в {d['file']}")
        for bad in ("@example.com", "@mail.ru", "@yandex.ru", "@gmail.com"):
            if bad in text:
                problems.append(f"похожий на реальный домен {bad} в {d['file']}")

    # Конфликтующие редакции: даты строго возрастают, на опорную дату действует
    # ровно одна редакция, каждая редакция присутствует в корпусе.
    for meta in manifest.get("conflict_rules", []):
        revs = meta["revisions"]
        if len(revs) < 2:
            problems.append(f"у правила {meta['key']} меньше двух редакций — конфликта нет")
        effs = [r["effective"] for r in revs]
        if effs != sorted(effs) or len(set(effs)) != len(effs):
            problems.append(f"даты редакций правила {meta['key']} не строго возрастают: {effs}")
        if len(set(r["value"] for r in revs)) != len(revs):
            problems.append(f"редакции правила {meta['key']} не различаются по значению")
        in_force = [r for r in revs if r["effective"] <= manifest["as_of"]]
        if not in_force:
            problems.append(
                f"на {manifest['as_of']} ни одна редакция правила {meta['key']} не действует"
            )
        else:
            # Действует только последняя из вступивших редакций; более ранние вытеснены.
            latest = max(in_force, key=lambda r: r["effective"])
            if latest["value"] != meta.get("in_force_at_as_of"):
                problems.append(
                    f"не совпадает действующая редакция правила {meta['key']}: "
                    f"{latest['value']} != {meta.get('in_force_at_as_of')}"
                )
            if len(in_force) > 1 and len({r["value"] for r in in_force}) == 1:
                problems.append(
                    f"у правила {meta['key']} вступившие редакции не различаются — конфликта нет"
                )
        for r in revs:
            if not (out_dir / r["file"]).exists():
                problems.append(f"нет файла редакции {r['file']}")

    q_path = out_dir / "questions.jsonl"
    if not q_path.exists():
        problems.append("нет questions.jsonl")
    else:
        refusals = 0
        rule_qs = 0
        for line in q_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                q = json.loads(line)
            except json.JSONDecodeError:
                problems.append("questions.jsonl содержит невалидный JSON")
                break
            if q.get("kind") == "rule_asof":
                rule_qs += 1
            if q.get("refusal_expected"):
                refusals += 1
        if rule_qs == 0:
            problems.append("нет вопросов по редакциям правил")
        if refusals == 0:
            problems.append("нет вопросов, требующих обоснованного отказа")
    return problems


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    here = Path(__file__).resolve().parent.parent
    ap.add_argument("--out", default=str(here / "sample-corpus"), help="каталог корпуса (по умолчанию sample-corpus/)")
    ap.add_argument("--count", type=int, default=20, help="сколько обычных документов (сверху добавляются 7 редакций правил)")
    ap.add_argument("--seed", type=int, default=20261126, help="seed генератора")
    ap.add_argument("--clean", action="store_true", help="удалить прежние документы перед генерацией")
    ap.add_argument("--json", action="store_true", help="печатать сводку в JSON")
    ap.add_argument("--self-test", action="store_true", help="сгенерировать во временный каталог и проверить себя")
    args = ap.parse_args(list(argv) if argv is not None else None)

    if args.count < 1 or args.count > 500:
        print("count должен быть в диапазоне 1..500", file=sys.stderr)
        return 2

    if args.self_test:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "corpus"
            res = write_corpus(out, min(args.count, 5), args.seed, clean=True)
            problems = validate(out, res["manifest"], min(args.count, 5))
        if problems:
            print("SELF-TEST FAIL:")
            for p in problems:
                print(f"  - {p}")
            return 1
        print(
            f"SELF-TEST OK: сгенерировано и проверено "
            f"{res['manifest']['base_documents']} обычных + "
            f"{res['manifest']['conflict_documents']} редакций правил"
        )
        return 0

    out_dir = Path(args.out).resolve()
    res = write_corpus(out_dir, args.count, args.seed, args.clean)
    problems = validate(out_dir, res["manifest"], args.count)
    if problems:
        print("ОШИБКИ САМОПРОВЕРКИ:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    summary = {
        "out": str(out_dir),
        "documents": len(res["manifest"]["documents"]),
        "base_documents": res["manifest"]["base_documents"],
        "conflict_documents": res["manifest"]["conflict_documents"],
        "conflict_rules": len(res["manifest"]["conflict_rules"]),
        "questions": res["questions"],
        "as_of": res["manifest"]["as_of"],
        "seed": args.seed,
        "index": str(out_dir / "INDEX.md"),
        "manifest": str(out_dir / "manifest.json"),
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(
            f"Сгенерировано {summary['documents']} синтетических документов в {summary['out']} "
            f"(обычных {summary['base_documents']} + редакций правил {summary['conflict_documents']})"
        )
        print(f"Конфликтующих правил: {summary['conflict_rules']} (опорная дата {summary['as_of']})")
        print(f"Индекс: {summary['index']}")
        print(f"Манифест: {summary['manifest']}")
        print(f"Вопросов для проверки RAG: {summary['questions']} (sample-corpus/questions.jsonl)")
    return 0


if __name__ == "__main__":
    # Windows-консоль по умолчанию cp1251: кириллица в сводке превращается
    # в кракозябры (а при строгом режиме — в UnicodeEncodeError).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
