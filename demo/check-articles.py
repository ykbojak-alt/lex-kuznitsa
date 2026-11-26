"""Самопроверка рабочего набора статей базы знаний (docs/db/articles).

Запуск из корня репозитория:

    python demo/check-articles.py

Набор проверяется по реальному корпусу заказчика (corpus-real/manifest.json).
Если локального реального корпуса нет, скрипт честно сообщает об этом и
проверяет набор по демонстрационному манифесту (sample-corpus/manifest.json).

Проверки:
  * объём набора в заданных границах (24-40 статьи: 32 статьи по домену плюс
    статьи-пробелы по направлениям без документов и статьи-глоссарии);
  * у каждой статьи есть все обязательные секции шаблона
    (docs/db/ARTICLE-TEMPLATE.md);
  * у каждой статьи есть обе обязательные строки про "навигационная карта,
    а не источник ответа";
  * идентификаторы документов, упомянутые в статьях, существуют в манифесте;
  * каждый документ корпуса упомянут хотя бы в одной статье (навигация
    не оставляет документ без статьи);
  * термины статей попадают в словарь направлений policy.BUSINESS_LINES;
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
REAL_MANIFEST = ROOT / "corpus-real" / "manifest.json"
DEMO_MANIFEST = ROOT / "sample-corpus" / "manifest.json"

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
    "## Примеры в локальном корпусе",
)

LINE_SOURCE = "Ответ берётся из документа-источника, не из этой статьи"
LINE_NO_SOURCE = (
    "Если источника с таким классом в базе нет — агент обязан сказать, "
    "что ответа нет, а не собирать из статьи"
)

# Идентификатор документа реального корпуса: КД-ПД-01, КД-ПС-02.
DOC_ID_RE = re.compile(r"КД-[А-Я]{2}-\d{2}")
# Нормативное значение в статье быть не должно: срок, сумма, процент.
VALUE_RE = re.compile(
    r"(\d+ ?(?:рабочих дней|дней|дня|дн\.|руб\.?|рублей|%|процентов|недель|месяцев))"
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

    if REAL_MANIFEST.exists():
        manifest_path = REAL_MANIFEST
        corpus = "реальный (corpus-real)"
    else:
        manifest_path = DEMO_MANIFEST
        corpus = "демонстрационный (sample-corpus) — реального корпуса нет локально"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    known_ids = {d["doc_id"] for d in manifest["documents"]}

    keywords = {k: [w.lower() for w in v["keywords"]] for k, v in BUSINESS_LINES.items()}
    covered: dict[str, list[str]] = {k: [] for k in keywords}
    cited: dict[str, set[str]] = {doc_id: set() for doc_id in known_ids}

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
            cited[doc_id].add(name)

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

    for doc_id, names in cited.items():
        if not names:
            fail(f"документ {doc_id} не упомянут ни в одной статье — навигация неполна")

    print(f"1. корпус для проверки: {corpus}")
    print(f"2. статей в наборе: {len(files)} (границы {MIN_ARTICLES}-{MAX_ARTICLES})")
    print("3. обязательные секции и строки: ok")
    print(f"4. идентификаторы в манифесте: ok ({len(known_ids)} документов)")
    for doc_id in sorted(cited):
        print(f"   - {doc_id}: {len(cited[doc_id])} ст.")
    print("5. термины в словаре направлений: ok")
    for line, names in covered.items():
        print(f"   - {line}: {len(names)} ст.")
    print("6. нормативных значений и смешения кириллицы: нет")
    print("OK: набор статей прошёл проверку")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
