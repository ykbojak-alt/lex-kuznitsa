"""Диагностика живого слота: сколько пустых ответов и при каком finish_reason.

Запуск из корня:

    python demo/llm-stats.py                 # 20 вопросов corpus-real
    python demo/llm-stats.py --n 5           # быстрая проверка
    python demo/llm-stats.py --tokens 1024   # фиксировать лимит (без лестницы)

Владелец: «Нужен ЧИСЛОЙ ответ, а не "иногда падает"». Скрипт гоняет вопросы
через `lex.llm.chat` с тяжёлым промптом (корпус регламентов + барьеры §12/§21
дословно — именно он роняет слот), читает `var/llm-debug.jsonl` и печатает
распределение: попытки, пустые, finish_reason, рост бюджета.

Читает .env так же, как приложение, и токен не печатает.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

CORPUS_REAL = ROOT / "corpus-real"


def load_env() -> bool:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return False
    try:
        from dotenv import load_dotenv

        load_dotenv(env_file)
    except ImportError:
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return bool(os.environ.get("NEXUSLINK_KEY") and os.environ.get("NEXUS_LINK_BASE_URL"))


def questions(n: int) -> list[str]:
    """Вопросы по corpus-real: сроки, роли, порядок, эскалация — как спрашивают."""
    base = [
        "Какой срок установлен для согласования проектной документации?",
        "Кто отвечает за приёмку результата проекта?",
        "Каков порядок эскалации при задержке поставки?",
        "Сколько экземпляров договора нужно оформить при закупке у единственного поставщика?",
        "Какие требования к оформлению отчёта о пресейловой деятельности?",
        "Что происходит при отклонении заявки на закупку?",
        "Кто утверждает изменения в проектной документации?",
        "Какой срок хранения журнала регистрации обращений?",
        "Как оформляется результат приёмки партии?",
        "Какие роли участвуют в процессе пресейла?",
        "Какой порядок согласования договорных изменений?",
        "Что делать, если поставщик единственный, а срок сорван?",
        "Кто отвечает за маршрутизацию обращения?",
        "Какие нефункциональные требования к системе документооборота?",
        "Как оформить приёмку, если партия с дефектами?",
        "Каков срок согласования заявки на закупку у директора?",
        "Какие исключения предусмотрены для закупок до ста тысяч рублей?",
        "Кто ведёт реестр пресейловых обращений?",
        "Какой порядок хранения проектной документации?",
        "Что фиксируется при отклонении от регламента?",
    ]
    out = []
    while len(out) < n:
        out.extend(base)
    return out[:n]


def heavy_messages(question: str) -> list[dict]:
    """Промпт ядра целиком: системный запрет, кейс-правило, тон, фрагменты.

    Именно такой объём уходит в модель на шаге compose — с ним и падает.
    """
    from lex import llm

    context = "\n\n".join(
        f"[{i}] Фрагмент регламента {i}. Срок согласования заявки на закупку: "
        f"пяти рабочих дней. Порог автоматического одобрения: 150 000 рублей. "
        f"Редакция действует с 2026-01-15."
        for i in range(1, 8)
    )
    system = (
        "Ты отвечаешь ТОЛЬКО на основе фрагментов, переданных ниже. Если фрагменты "
        "не содержат ответа — верни отказ, а не общие знания. Не используй сведения, "
        "которых нет во фрагментах, даже если ты их знаешь. Каждое утверждение "
        "сопровождай номером фрагмента в квадратных скобках.\n\n"
        + llm.tone_rules("plain")
    )
    user = (
        f"Вопрос сотрудника: {question}\nРоль: lawyer\nДата отсчёта: 2026-03-01\n\n"
        f"Фрагменты базы знаний организации:\n\n{context}\n\n"
        "Ответь по правилам выше. Если ответа нет ни в одном фрагменте — "
        "ответь ровно: ОТКАЗ: <причина>."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def read_debug(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def report(rows: list[dict]) -> int:
    if not rows:
        print("журнал пуст — ни одного вызова не записано")
        return 1
    print(f"\nвсего вызовов: {len(rows)}")
    empties = [r for r in rows if r.get("empty")]
    errs = [r for r in rows if r.get("error")]
    print(f"пустых: {len(empties)} ({100 * len(empties) / len(rows):.0f}%) · "
          f"ошибок связи: {len(errs)}")

    print("\nfinish_reason по всем вызовам:")
    for reason, count in Counter(r.get("finish_reason") or "—" for r in rows).most_common():
        print(f"  {reason or '—':<18} {count}")

    if empties:
        print("\nfinish_reason у пустых:")
        for reason, count in Counter(r.get("finish_reason") or "—" for r in empties).most_common():
            print(f"  {reason or '—':<18} {count}")
        print("\nбюджет у пустых:")
        for tok, count in Counter(r.get("max_tokens") for r in empties).most_common():
            print(f"  max_tokens={tok:<6} {count}")

    print("\nпо бюджетам (все вызовы):")
    for tok in sorted({r.get("max_tokens") or 0 for r in rows}):
        group = [r for r in rows if (r.get("max_tokens") or 0) == tok]
        e = sum(1 for r in group if r.get("empty"))
        print(f"  max_tokens={tok:<6} вызовов {len(group):<3} пустых {e}")

    lat = [r.get("elapsed_ms") or 0 for r in rows]
    if lat:
        print(f"\nвремя: медиана {statistics.median(lat):.0f} мс · "
              f"максимум {max(lat)} мс")

    if empties and any(r.get("retried") for r in empties):
        print("\nВНИМАНИЕ: пустыми остались и повторы — потолка не хватает")
    return 0 if not empties and not errs else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="распределение пустых ответов слота")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--tokens", type=int, default=0,
                    help="фиксированный лимит (0 — лестница повторов)")
    ap.add_argument("--debug", default="", help="куда писать журнал")
    ap.add_argument("--keep", action="store_true", help="не чистить журнал перед прогоном")
    args = ap.parse_args()

    if not load_env():
        print("SKIP: NEXUSLINK_KEY/NEXUS_LINK_BASE_URL не заданы в .env")
        return 0

    debug_path = Path(args.debug) if args.debug else ROOT / "var" / "llm-debug.jsonl"
    os.environ["LEX_LLM_DEBUG_PATH"] = str(debug_path)
    # Журнал по умолчанию выключен (в нём текст вопросов сотрудников), а этот
    # скрипт существует ради его разбора — включаем явно, до импорта llm_debug.
    os.environ["LEX_LLM_DEBUG"] = "1"
    if not args.keep and debug_path.exists():
        debug_path.unlink()

    from lex import llm, llm_debug

    llm_debug.DEBUG_PATH = debug_path
    qs = questions(args.n)
    print(f"модель: {llm.model_name()} · вопросов: {len(qs)} · "
          f"лимит: {'лестница ' + str(llm.RETRY_BUDGETS) if not args.tokens else args.tokens}")

    failed = 0
    for i, q in enumerate(qs, 1):
        kw = {} if not args.tokens else {"max_tokens": args.tokens, "retries_on_empty": 0}
        try:
            text = llm.chat(heavy_messages(q), **kw)
            print(f"  {i:02d} OK   {len(text):>5} знаков · {q[:46]}")
        except llm.LLMError as exc:
            failed += 1
            print(f"  {i:02d} FAIL {q[:46]} · {str(exc)[:90]}")

    code = report(read_debug(debug_path))
    if failed:
        print(f"\nвопросов с исключением: {failed}")
        code = 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
