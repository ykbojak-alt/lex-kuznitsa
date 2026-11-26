"""Диагностика grounding: где текст ЕСТЬ, а утверждений НОЛЬ.

Не фикс и не тест на регрессию — разбор. Вопрос поставлен так: «почему
парсер может дать total=0 на непустом тексте». Здесь перечислены все ветки
``split_sentences``, где это возможно, и каждая проверена на настоящем тексте.

    python demo/diag-grounding.py
    python demo/diag-grounding.py --answer-file some-answer.txt

Если в ``var/llm-debug.jsonl`` лежат реальные ответы модели — берутся они:
диагностика на выдуманных строках доказывает существование ветки, но не
частоту. Без файла работает на характерных для этого слота ответах.

Главная ветка (найдена 27.11): ``_HEADING_RE = r"^\\s*(?:#{1,6}\\s|\\*\\*[^*]+\\*\\*\\s*$)"``
проверяется через ``match()`` — то есть достаточно НАЧАЛА строки. Ветка
``#{1,6}\\s`` поэтому съедает не «строку-заголовок», а ЛЮБУЮ строку, что
начинается с решётки, вместе с содержимым после неё:

    "## Срок: три рабочих дня [1]."   -> split_sentences() == []  -> total=0

Это не «модель не ответила», это парсер выбросил верный ответ. Проверяется
ниже случаем «markdown-заголовок с содержимым».
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

from lex.grounding import (
    MIN_CLAIM_CHARS,
    check_grounding,
    is_complete_sentence,
    split_sentences,
)

# Реальные ответы этого слота, если файла отладки нет. Первые три —
# нормальный ответ и два обрыва по лимиту; дальше — разметка, которую модель
# выдаёт по инструкции «структурируй ответ».
SEED_ANSWERS = {
    "нормальный, со ссылкой": (
        "Согласно регламенту, срок согласования заявки на закупку — "
        "три рабочих дня [1]. Отсчёт начинается с момента поступления заявки [2]."
    ),
    "обрыв по лимиту (нет точки)": "Срок согласования заявки на закупку — трёх рабочих дней",
    "обрыв с запятой": "Срок согласования заявки составляет три рабочих дня, а для",
    "markdown-заголовок с содержимым": "## Срок: три рабочих дня [1].",
    "markdown H2 Содержание": "## Ответ\n\nтри рабочих дня [1].",
    "**жирный** заголовок одной строкой": "**Срок согласования:**",
    "**жирный** и текст в строке": "**Срок:** три рабочих дня [1].",
    "разделитель ---": "---",
    "маркеры ***": "***",
    "только буллет": "- ",
    "пробелы и переводы строк": "   \n\t\n  ",
}

# Классификатор ветки: почему при непустом тексте вышло 0. Порядок важен —
# первая подходящая ветка и есть причина.
_BRANCHES = (
    ("MARKUP-ONLY", "строка целиком разметка (---, ***, «- ») — после lstrip пусто"),
    ("HEADING-HASH", "строка с «#» в начале выброшена вместе с содержимым"),
    ("HEADING-BOLD", "строка целиком «**...**» выброшена"),
    ("NO-SENTENCES", "строки есть, но _split_line вернул пусто"),
    ("ALL-CONNECTIVES", "предложения есть, но все короче MIN_CLAIM_CHARS без ссылки"),
)


def _is_markup_only(line: str) -> bool:
    return bool(line.strip()) and not line.strip().lstrip("-•*#>\|:— ").strip()


def _is_hash_heading_with_body(line: str) -> bool:
    """Строка начинается с «#»-заголовка, но ПОСЛЕ него есть текст ответа.

    ``_HEADING_RE`` проверяется через ``match()``, а не ``fullmatch()``, поэтому
    ветка ``#{1,6}\\s`` срабатывает на любом начале строки с решёткой, и
    ``split_sentences`` выбрасывает ВСЮ строку. Здесь важно только отличить
    «голый заголовок» от «заголовок, за которым шёл ответ».
    """
    s = line.strip()
    if not s.startswith("#"):
        return False
    body = s.lstrip("#").strip()
    return bool(body.strip("*:—- "))


def _is_bold_only(line: str) -> bool:
    s = line.strip()
    return s.startswith("**") and s.endswith("**") and s.count("**") == 2


def _lost_lines(text: str) -> list[str]:
    """Строки, которые split_sentences молча выбросил, хотя в них есть текст."""
    lost = []
    for line in text.split("\n"):
        block = line.strip()
        if not block:
            continue
        if _is_hash_heading_with_body(block) or _is_bold_only(block):
            lost.append(block)
    return lost


def _load_debug_answers() -> dict[str, str]:
    path = ROOT / "var" / "llm-debug.jsonl"
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = str(row.get("content") or row.get("answer") or row.get("text") or "")
        if text.strip():
            out[f"llm-debug #{i}"] = text
    return out


def _classify(text: str, sentences: int, claims: int) -> str:
    if not text.strip():
        return "пусто (честный total=0)"
    if sentences == 0:
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if lines and all(_is_markup_only(ln) for ln in lines):
            return _BRANCHES[0][0] + ": " + _BRANCHES[0][1]
        if any(_is_hash_heading_with_body(ln) for ln in lines):
            return _BRANCHES[1][0] + ": " + _BRANCHES[1][1]
        if lines and all(_is_bold_only(ln) for ln in lines):
            return _BRANCHES[2][0] + ": " + _BRANCHES[2][1]
        return _BRANCHES[3][0] + ": " + _BRANCHES[3][1]
    if claims == 0:
        return _BRANCHES[4][0] + ": " + _BRANCHES[4][1]
    return "-"


def _verdict(text: str) -> dict:
    sents = split_sentences(text)
    report = check_grounding(text, ["фрагмент"] * 10, embedder=None)
    return {
        "chars": len(text),
        "sentences": len(sents),
        "claims": report.total,
        "covered": report.covered,
        "text_nonempty": bool(text.strip()),
        "zero_despite_text": bool(text.strip()) and report.total == 0,
        "lost_lines": _lost_lines(text),
        "sentences_sample": sents[:3],
        "first_complete": is_complete_sentence(sents[0]) if sents else None,
        "short_claim_min": MIN_CLAIM_CHARS,
    }


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    if "--answer-file" in args:
        p = Path(args[args.index("--answer-file") + 1])
        answers = {"файл " + p.name: p.read_text(encoding="utf-8")}
    else:
        answers = _load_debug_answers()
        if not answers:
            print("var/llm-debug.jsonl нет — работаю на посевных ответах "
                  "(нормальный, обрыв, разметка).\n")
            answers = SEED_ANSWERS
        else:
            print(f"взято {len(answers)} ответов из var/llm-debug.jsonl\n")

    rows = []
    print(f"{'случай':38s} {'знаков':>7s} {'предл.':>7s} {'утв.':>5s}  ветка")
    print("-" * 96)
    for name, text in answers.items():
        v = _verdict(text)
        branch = _classify(text, v["sentences"], v["claims"])
        print(f"{name[:38]:38s} {v['chars']:>7d} {v['sentences']:>7d} "
              f"{v['claims']:>5d}  {branch}")
        rows.append({"case": name, "verdict": v, "branch": branch, "text": text[:400]})

    bad = [r for r in rows if r["verdict"]["zero_despite_text"]]
    print()
    print(f"случаев «текст непуст, утверждений 0»: {len(bad)} из {len(rows)}")
    for r in bad:
        lost = r["verdict"]["lost_lines"]
        extra = f" — потеряно строк: {len(lost)}" if lost else ""
        print(f"  · {r['case']}: {r['branch']}{extra}")

    out = ROOT / "var" / "diag-grounding.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nотчёт: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
