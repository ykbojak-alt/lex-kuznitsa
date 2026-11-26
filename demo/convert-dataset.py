"""convert-dataset — реальные .docx заказчика -> корпус в формате LEX.

Запуск из корня репозитория:

    python demo/convert-dataset.py                 # dataset/ -> corpus-real/
    python demo/convert-dataset.py --out corpus-real

ЗАЧЕМ ОТДЕЛЬНО ОТ sample-corpus: ``sample-corpus/`` — синтетика для разработки,
в ней вымышленные ТЗ-ЭКЗ-* документы. Здесь — настоящие регламенты заказчика
(4 .docx). Они НЕ публикуются: ``dataset/`` и ``corpus-real/`` в .gitignore.

Формат выхода — тот же, что читает ``lex.corpus.load_corpus``: ``manifest.json``
+ по одному markdown-файлу на документ, заголовки секций ``## N. Название``.
Одним файлом на .docx, потому что правки и хеши считаются по файлу, а не по
секции.

Чего скрипт НЕ делает: не придумывает ``conflict_rules``. В реальных документах
нет версионированных редакций одного правила — это была синтетическая
конструкция. Пустой список честнее выдуманного.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Соответствие файлов заказчика направлению и отделу — по содержанию документов,
# не по имени. Направления совпадают с lex.policy.BUSINESS_LINES.
DOCS: dict[str, dict[str, str]] = {
    "Организация проектной деятельности Тех дирекции": {
        "doc_id": "КД-ПД-01",
        "title": "Организация проектной деятельности (Техническая дирекция)",
        "kind": "Регламент",
        "department": "Техническая дирекция",
        "business_line": "проектная деятельность",
        "role": "Техническая дирекция",
    },
    "Пресейловая деятельность Дирекции ИБ": {
        "doc_id": "КД-ПС-02",
        "title": "Пресейловая деятельность (Дирекция ИБ)",
        "kind": "Регламент",
        "department": "Дирекция ИБ",
        "business_line": "пресейл",
        "role": "Дирекция ИБ",
    },
    "Проектная деятельность ДБРЦТ": {
        "doc_id": "КД-ПД-03",
        "title": "Проектная деятельность (ДБРЦТ)",
        "kind": "Регламент",
        "department": "ДБРЦТ",
        "business_line": "проектная деятельность",
        "role": "ДБРЦТ",
    },
    "Проектная деятельность Дирекции ИБ": {
        "doc_id": "КД-ПД-04",
        "title": "Проектная деятельность (Дирекция ИБ)",
        "kind": "Регламент",
        "department": "Дирекция ИБ",
        "business_line": "проектная деятельность",
        "role": "Дирекция ИБ",
    },
}

# Заголовок секции в этих документах — одноуровневый номер: «1. Постановка
# задачи на пресейл». Пункт — «1.1. Подготовка начинается…»: два и более
# уровня. Различать надо по числу уровней, а не по точке в конце: встречаются
# пункты, заканчивающиеся «то:» или «проверку:».
HEADING_RE = re.compile(r"^(\d{1,2})\.\s+(\S.*)$")
TITLE_MAX = 90


def docx_paragraphs(path: Path) -> list[str]:
    """Абзацы документа в порядке следования (без таблиц и картинок)."""
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    out: list[str] = []
    for para in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
        runs = re.findall(r"<w:t[^>]*>(.*?)</w:t>", para, re.S)
        text = "".join(runs)
        for old, new in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                         ("&quot;", '"'), ("&apos;", "'")):
            text = text.replace(old, new)
        text = text.strip()
        if text:
            out.append(text)
    return out


def _is_heading(text: str) -> bool:
    """«1. Постановка задачи на пресейл» — заголовок; «1.3. Если Заказчик…» — нет.

    Уровень один и текст короткий — заголовок. Уровней два и больше — пункт,
    он остаётся в теле секции.
    """
    m = HEADING_RE.match(text)
    if not m:
        return False
    body = m.group(2)
    if len(body) > TITLE_MAX:
        return False
    # «1.1. …» — вторая точка внутри тела выдаёт многоуровневую нумерацию.
    return not re.match(r"\d+\.", body)


def to_markdown(paras: list[str], meta: dict[str, str]) -> str:
    """Абзацы -> markdown с секциями ``## N. Название``.

    Тело пункта остаётся как есть, построчно: маркеры списков («- », «a. ») не
    трогаем — это часть исходного текста, искажать формулировки нельзя (§12).
    """
    lines: list[str] = [f"# {meta['title']}", ""]
    section = "Преамбула"
    buf: list[str] = []

    def flush() -> None:
        if not buf:
            return
        lines.append(f"## {section}")
        lines.extend(buf)
        lines.append("")

    for text in paras:
        if _is_heading(text):
            flush()
            section = text
            buf = []
        else:
            buf.append(text)
    flush()
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", default=str(ROOT / "dataset"), help="каталог с .docx")
    ap.add_argument("--out", default=str(ROOT / "corpus-real"), help="куда писать корпус")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    src, out = Path(args.src), Path(args.out)
    if not src.is_dir():
        print(f"нет каталога {src}", file=sys.stderr)
        return 1

    files = sorted(src.glob("*.docx"))
    if not files:
        print(f"в {src} нет .docx", file=sys.stderr)
        return 1

    out.mkdir(parents=True, exist_ok=True)
    documents = []
    for path in files:
        meta = DOCS.get(path.stem)
        if meta is None:
            print(f"ПРОПУСК (нет в карте): {path.name}", file=sys.stderr)
            continue
        paras = docx_paragraphs(path)
        md = to_markdown(paras, meta)
        fname = f"{meta['doc_id']}-{re.sub(r'[^0-9a-zA-Zа-яА-ЯёЁ]+', '-', meta['business_line'])}.md"
        (out / fname).write_text(md, encoding="utf-8")
        sections = re.findall(r"^##\s+(.+?)\s*$", md, re.MULTILINE)
        documents.append({
            "doc_id": meta["doc_id"],
            "title": meta["title"],
            "kind": meta["kind"],
            "subtype": "регламент",
            "department": meta["department"],
            "system": meta["business_line"],
            "business_line": meta["business_line"],
            "owner": meta["department"],
            "version": "1",
            "issued": "",
            "file": fname,
            "sha256": hashlib.sha256((out / fname).read_bytes()).hexdigest(),
            "words": sum(len(p.split()) for p in paras),
            "sections": sections,
            "content_type": "text",
            "source": "заказчик (dataset/*.docx)",
        })
        print(f"{meta['doc_id']}  {fname}  разделов {len(sections)}  абзацев {len(paras)}")

    manifest = {
        "generator": "demo/convert-dataset.py",
        "source": "dataset/ — документы заказчика (.docx)",
        "as_of": "2026-11-26",
        "watermark": "Реальные регламенты заказчика. Не публиковать: содержит рабочие процессы организации.",
        "documents": documents,
        # Честно: версионированных редакций одного правила в этих документах нет.
        "conflict_rules": [],
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    idx = ["# Корпус заказчика — индекс", "", f"> {manifest['watermark']}", "",
           f"Документов: **{len(documents)}**", "",
           "| ID | Документ | Отдел | Направление | Файл |", "| --- | --- | --- | --- | --- |"]
    for d in documents:
        idx.append(f"| {d['doc_id']} | {d['title']} | {d['department']} | {d['system']} | `{d['file']}` |")
    (out / "INDEX.md").write_text("\n".join(idx) + "\n", encoding="utf-8")

    print(f"\nкорпус: {out}  документов {len(documents)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
