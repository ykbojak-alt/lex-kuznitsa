"""Харнесс стабильности ответов LEX: один вопрос — много прогонов.

Задача инструмента — не судить, а показать. Один и тот же вопрос прогоняется
M раз, и видно, меняется ли исход между прогонами. Именно это ломает демо:
вопрос, который «обычно отвечает», иногда отдаёт уточнение или пустой текст,
и на сцене это выглядит как «агент не понял».

    python demo/check-stability.py --questions 5 --runs 3
    python demo/check-stability.py --questions 8 --runs 5 --json out.json
    python demo/check-stability.py --question "Как формируется команда проекта?"

Исход считается по событиям потока, а не по тексту ответа:
    answer   — кадр answer (есть ответ)
    clarify  — кадр clarify (нужно уточнение)
    escalate — кадр escalate (заявка в службу качества)
    error    — кадр error (сбой) + первые 200 знаков причины
    empty    — поток закрылся без финального события
    message  — answer с пустым текстом: формально ответ, фактически пусто

Прогоны с --no-llm идут без модели: там стабильность проверяет каскад, а не
провайдера. По умолчанию модель включена, если она сконфигурирована.

Побочные файлы: журнал и тикеты пишутся во временный каталог, рабочие
``var/*.jsonl`` не трогаются.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Вопросы по умолчанию — из корпуса заказчика, разные по «трудности»: первый
# про процесс, второй про срок (за ним стоит правило), третий про роль.
DEFAULT_QUESTIONS = [
    "Как формируется команда проекта?",
    "Какой срок согласования заявки на закупку?",
    "Кто отвечает за входной контроль?",
    "Какие этапы проекта закреплены в регламенте?",
    "Как оформляется приёмка работ по проекту?",
    "Сколько дней отводится на согласование документа?",
    "Что входит в обязанности руководителя проекта?",
    "Как передаются результаты проекта заказчику?",
]

EXIT_OK = 0
EXIT_UNSTABLE = 1


def _widen_stdout() -> None:
    """Консоль Windows по умолчанию cp1251 — «×», «→» и «·» роняют печать.

    Не меняем кодировку принудительно (тогда в старом терминале выйдет
    кракозябра), а разрешаем замену символов: таблица должна напечататься
    целиком, даже если конкретный глиф терминал не умеет.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _outcome(events: list[str]) -> str:
    """Исход прогона по сырым SSE-кадрам."""
    kinds = [e.split("\n")[0].replace("event: ", "") for e in events if e.startswith("event:")]
    if "error" in kinds:
        return "error"
    if "escalate" in kinds:
        return "escalate"
    if "clarify" in kinds:
        return "clarify"
    if "answer" in kinds:
        for ev in events:
            if ev.startswith("event: answer"):
                data = _data_of(ev)
                text = str((data or {}).get("text") or "").strip()
                return "answer" if text else "message"
        return "answer"
    if "done" in kinds:
        return "empty"
    return "no-final"


def _data_of(event: str) -> dict | None:
    for line in event.split("\n"):
        if line.startswith("data: "):
            try:
                return json.loads(line[6:])
            except json.JSONDecodeError:
                return None
    return None


def _configure(no_llm: bool = False) -> None:
    """Временные хранилища: инструмент не должен пачкать рабочий var/."""
    from api import config

    tmp = Path(tempfile.mkdtemp(prefix="lex-stab-"))
    config.JOURNAL_PATH = tmp / "journal.jsonl"
    config.TICKETS_PATH = tmp / "tickets.jsonl"
    config.CASES_PATH = tmp / "cases.jsonl"
    config.ESCALATIONS_PATH = tmp / "escalations.jsonl"
    config.CHANGE_REQUESTS_PATH = tmp / "change_requests.jsonl"
    # Отключение модели живёт в config.NO_LLM, а не в аргументе stream_answer:
    # каскад читает флаг оттуда. Без этой строки --no-llm молча ничего не
    # делал и прогон всё равно ходил в провайдера.
    if no_llm:
        config.NO_LLM = True


async def _one(question: str, *, role: str, as_of: str | None, no_llm: bool) -> tuple[str, float]:
    from api import core

    t0 = time.perf_counter()
    events: list[str] = []
    try:
        async for chunk in core.stream_answer(question, role, as_of, session_id="s-stab"):
            events.append(chunk)
    except Exception as exc:
        events.append(f"event: error\ndata: {json.dumps({'message': str(exc)})}\n\n")
    return _outcome(events), time.perf_counter() - t0


def run(questions: list[str], runs: int, *, role: str, as_of: str | None,
        no_llm: bool) -> dict:
    _configure(no_llm=no_llm)
    table: list[dict] = []
    for q in questions:
        outs: list[str] = []
        times: list[float] = []
        for _ in range(runs):
            outcome, dt = asyncio.run(_one(q, role=role, as_of=as_of, no_llm=no_llm))
            outs.append(outcome)
            times.append(dt)
        counts = Counter(outs)
        best = counts.most_common(1)[0][0]
        table.append({
            "question": q,
            "outcomes": outs,
            "counts": dict(counts),
            "dominant": best,
            "stable": len(counts) == 1,
            "answer_rate": round(counts.get("answer", 0) / runs, 2),
            "avg_seconds": round(sum(times) / len(times), 1),
        })
    return {
        "role": role,
        "as_of": as_of,
        "no_llm": no_llm,
        "runs": runs,
        "questions": table,
        "summary": {
            "asked": len(questions),
            "stable": sum(1 for t in table if t["stable"]),
            "unstable": sum(1 for t in table if not t["stable"]),
            "answer_rate": round(
                sum(t["counts"].get("answer", 0) for t in table) / (len(questions) * runs), 2)
            if questions else 0.0,
        },
    }


_CODE = {
    "answer": "ans",
    "message": "MSG",
    "clarify": "clr",
    "escalate": "esc",
    "error": "ERR",
    "empty": "EMP",
    "no-final": "---",
}


def _print(data: dict) -> None:
    runs = data["runs"]
    head = "вопрос".ljust(52) + " | " + " | ".join(str(i + 1) for i in range(runs)) + " | итог"
    print(head)
    print("-" * len(head))
    for row in data["questions"]:
        cells = " | ".join(_CODE.get(o, o).rjust(len(str(runs))) for o in row["outcomes"])
        q = row["question"]
        if len(q) > 50:
            q = q[:49] + "…"
        verdict = "стабильно" if row["stable"] else "НЕСТАБИЛЬНО"
        print(f"{q.ljust(52)} | {cells} | {verdict} ({row['dominant']})")
    s = data["summary"]
    print()
    print(f"вопросов {s['asked']} × прогонов {runs} = {s['asked'] * runs} запросов")
    print(f"стабильных {s['stable']}, нестабильных {s['unstable']}, "
          f"доля answer {s['answer_rate']:.0%}")
    print("коды: ans=ответ, MSG=answer с пустым текстом, clr=уточнение, "
          "esc=заявка, ERR=сбой, EMP=пустой поток")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Стабильность ответов LEX на повторных прогонах.")
    ap.add_argument("--questions", type=int, default=5,
                    help="сколько вопросов взять из набора (по умолчанию 5)")
    ap.add_argument("--runs", type=int, default=3,
                    help="сколько раз прогнать каждый вопрос (по умолчанию 3)")
    ap.add_argument("--question", action="append", default=[],
                    help="свой вопрос (можно несколько); отменяет --questions")
    ap.add_argument("--role", default="director", help="роль спрашивающего")
    ap.add_argument("--as-of", default=None, help="дата отсчёта ГГГГ-ММ-ДД")
    ap.add_argument("--no-llm", action="store_true", help="без модели: проверяет каскад")
    ap.add_argument("--json", default=None, help="куда сохранить отчёт (по умолчанию рядом)")
    args = ap.parse_args(argv)
    _widen_stdout()

    if args.question:
        questions = args.question
    else:
        questions = DEFAULT_QUESTIONS[: max(1, args.questions)]

    data = run(questions, max(1, args.runs), role=args.role, as_of=args.as_of,
               no_llm=args.no_llm)
    _print(data)

    out = Path(args.json) if args.json else (
        ROOT / "var" / f"stability-{time.strftime('%Y%m%d-%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nотчёт: {out}")
    return EXIT_OK if data["summary"]["unstable"] == 0 else EXIT_UNSTABLE


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())
