"""Самопроверка рабочего набора статей базы знаний (docs/db/articles).

Запуск из корня репозитория:

    python demo/check-articles.py

Проверяет требования владельца к набору статей:
  * объём набора в заданных границах (24-40 статей);
  * у каждой статьи есть все обязательные секции шаблона
    (docs/db/ARTICLE-TEMPLATE.md);
  * у каждой статьи есть обе обязательные строки про "навигационная карта,
    а не источник ответа";
  * идентификаторы демо-корпуса, упомянутые в статьях, существуют
    в sample-corpus/manifest.json;
  * термины статей попадают в словарь направлений policy.BUSINESS_LINES,
    и по каждому направлению словаря есть хотя бы одна статья;
  * в статьях нет нормативных значений (сроков, сумм, порогов) и нет
    смешения кириллицы с латиницей.

Скрипт ничего не меняет в репозитории.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lex.policy import BUSINESS_LINES

ARTICLES = ROOT / "docs" / "db" / "articles"
MANIFEST = ROOT / "sample-corpus" / "manifest.json"

MIN_ARTICLES = 24
MAX_ARTICLES = 40

REQUIRED_SECTIONS = (
    "## Суть",
    "## Синонимы / как спрашивают",
    "## Что искать в базе",
    "## Где искать",
    "## Чего в базе НЕТ",
    "## Типичные ловушки",
    "## Пример вопроса",
    "## Примеры в демо-корпусе",
)

LINE_SOURCE = "Ответ берётся из документа-источника, не из этой статьи"
LINE_NO_SOURCE = (
    "Если источника с таким классом в базе нет — агент обязан сказать, "
    "что ответа нет, а не собирать из статьи"
)

DOC_ID_RE = re.compile(
    "\u0422\u0417-\u042d\u041a\u0417-[0-9A-Z\u0410-\u042f]+"
    "(?:-[A-Z\u0410-\u042f0-9]+)*"
)
# Нормативное значение в статье быть не должно: срок, сумма, процент.
VALUE_RE = re.compile(
    r"(\d+ ?(?:рабочих дней|дней|дня|дн\.|руб\.?|рублей|%|процентов))"
    r"|(?:не менее|не более)\s+\d+"
    r"|(?:восемь|девять|десять|семь|шесть|пять|четыре|три|два)\s+"
    r"(?:рабочих дней|дней|дня|рублей|руб\.?)"
)
# Смешение кириллицы и латиницы внутри слова.
MIXED_RE = re.compile(r"[\u0400-\u04FF]+[A-Za-z]+|[A-Za-z]+[\u0400-\u04FF]+")


def flat(text: str) -> str:
    """Текст в одну строку: обязательные строки в статьях переносятся."""
    return re.sub(r"\s+", " ", text)


def fail(msg: str) -> None:
    print("FAIL:", msg)
    raise SystemExit(1)


def main() -> int:
    files = sorted(ARTICLES.glob("*.md"))
    if not MIN_ARTICLES <= len(files) <= MAX_ARTICLES:
        fail(f"число статей вне границ: {len(files)} (нужно {MIN_ARTICLES}-{MAX_ARTICLES})")

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    known_ids = {d["doc_id"] for d in manifest["documents"]}

    keywords = {k: [w.lower() for w in v["keywords"]] for k, v in BUSINESS_LINES.items()}
    covered: dict[str, list[str]] = {k: [] for k in keywords}

    for path in files:
        text = path.read_text(encoding="utf-8")
        name = path.name

        if not text.startswith("# Тема: "):
            fail(f"{name}: заголовок должен быть '# Тема: ...'")
        for section in REQUIRED_SECTIONS:
            if section not in text:
                fail(f"{name}: нет обязательной секции '{section}'")

        one_line = flat(text)
        if LINE_SOURCE not in one_line:
            fail(f"{name}: нет обязательной строки про источник ответа")
        if LINE_NO_SOURCE not in one_line:
            fail(f"{name}: нет обязательной строки про отсутствие источника")

        for match in DOC_ID_RE.finditer(text):
            doc_id = match.group(0)
            if doc_id not in known_ids:
                fail(f"{name}: идентификатор {doc_id} нет в манифесте")

        hit = VALUE_RE.search(text)
        if hit:
            fail(f"{name}: нормативное значение в статье: {hit.group(0)!r}")

        hit = MIXED_RE.search(text)
        if hit:
            fail(f"{name}: смешение кириллицы и латиницы: {hit.group(0)!r}")

        low = text.lower()
        matched = False
        for line, words in keywords.items():
            if any(word in low for word in words):
                covered[line].append(name)
                matched = True
        if not matched:
            fail(f"{name}: термины статьи не попали в словарь направлений")

    for line, names in covered.items():
        if not names:
            fail(f"направление '{line}' не покрыто ни одной статьёй")

    print(f"1. статей в наборе: {len(files)} (границы {MIN_ARTICLES}-{MAX_ARTICLES})")
    print("2. обязательные секции и строки: ok")
    print("3. идентификаторы демо-корпуса: все есть в манифесте")
    print("4. термины в словаре направлений: ok")
    for line, names in covered.items():
        print(f"   - {line}: {len(names)} ст.")
    print("5. нормативных значений и смешения кириллицы: нет")
    print("OK: набор статей прошёл проверку")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
