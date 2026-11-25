#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Генератор синтетического обезличенного корпуса документов для спринта ИИкузницы.

Зачем: RAG-контур должен провереняться на данных, похожих на заказчика, но
**без единого реального документа заказчика**. Этот генератор собирает
синтетические регламенты и инструкции в формате технического задания (ТЗ),
которые можно положить в RAG-индекс, задать к ним вопросы и проверить
качество поиска.

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

    manifest = {
        "generator": "demo/gen-sample-corpus.py",
        "seed": seed,
        "count": len(docs),
        "watermark": WATERMARK,
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
        f"Документов: **{len(docs)}**. Seed: `{seed}`. Генератор: `demo/gen-sample-corpus.py`.",
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
        "python demo/gen-sample-corpus.py --count 24 --seed 20261126 --clean",
        "```",
        "",
        "## Как использовать в RAG",
        "",
        "1. Индексировать `sample-corpus/*.md` (кроме `INDEX.md`) в тестовый индекс.",
        "2. Взять вопросы из `sample-corpus/questions.jsonl` и сверить ответы.",
        "3. Проверить, что в ответе нет вымышленных фактов и что указан `doc_id`.",
        "",
        "> Все содержимое синтетическое. Файлы не содержат данных реального заказчика.",
    ]
    (out_dir / "INDEX.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    q_lines = []
    for d in docs:
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


def validate(out_dir: Path, manifest: dict, count: int) -> list[str]:
    """Самопроверка: количество, уникальность, непустота, отсутствие чужих доменов."""
    problems: list[str] = []
    docs = manifest["documents"]
    if len(docs) != count:
        problems.append(f"ожидалось {count} документов, получено {len(docs)}")
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
    q_path = out_dir / "questions.jsonl"
    if not q_path.exists():
        problems.append("нет questions.jsonl")
    else:
        for line in q_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    problems.append("questions.jsonl содержит невалидный JSON")
                    break
    return problems


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    here = Path(__file__).resolve().parent.parent
    ap.add_argument("--out", default=str(here / "sample-corpus"), help="каталог корпуса (по умолчанию sample-corpus/)")
    ap.add_argument("--count", type=int, default=24, help="сколько документов (20-30 для спринта)")
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
        print(f"SELF-TEST OK: сгенерировано и проверено {min(args.count, 5)} документов")
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
        "questions": res["questions"],
        "seed": args.seed,
        "index": str(out_dir / "INDEX.md"),
        "manifest": str(out_dir / "manifest.json"),
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"Сгенерировано {summary['documents']} синтетических документов в {summary['out']}")
        print(f"Индекс: {summary['index']}")
        print(f"Манифест: {summary['manifest']}")
        print(f"Вопросов для проверки RAG: {summary['questions']} (sample-corpus/questions.jsonl)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
