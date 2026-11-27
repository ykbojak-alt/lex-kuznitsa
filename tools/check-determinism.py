"""Проверка воспроизводимости ответа: один вопрос — один ответ.

Дефект владельца 28.11.2026: «на один и тот же вопрос получаем 2 совершенно
разных ответа». Оба были верны по цитатам, но опирались на разные регламенты,
и для демонстрации это выглядит как нестабильность системы.

Причина — температура 0.2 на генерации. Проверка делает два прогона подряд
и сравнивает: при temperature=0 формулировка должна совпасть (провайдеры не
гарантируют побитового равенства, но расхождение по существу — это уже дефект
настройки, а не сети).

Запуск на стенде:
    /opt/lex-kuznitsa/.venv/bin/python tools/check-determinism.py --runs 3
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

QUESTIONS = (
    "Что делать, если данных для проведения расчетов недостаточно?",
    "Кого обязательно должен иметь субподрядчик в команде проекта?",
)


def ask(url: str, question: str, role: str = "director", timeout: float = 180.0) -> dict:
    body = json.dumps({"question": question, "role_id": role}).encode("utf-8")
    req = urllib.request.Request(
        f"{url.rstrip('/')}/api/ask", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    final: dict = {}
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if ev.get("text") is not None or ev.get("refused") is not None:
                final = ev
    return final


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8090")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    rows = []
    failed = 0
    for question in QUESTIONS:
        texts, docs = [], []
        for i in range(args.runs):
            ev = ask(args.url, question)
            text = (ev.get("short") or ev.get("text") or "").strip()
            cited = [c.get("doc_id") for c in (ev.get("citations") or [])]
            texts.append(text)
            docs.append(cited)
            print(f"  прогон {i + 1}: {len(text)} знаков, источники {cited}")
        same_text = len(set(texts)) == 1
        same_docs = len(set(tuple(d) for d in docs)) == 1
        ok = same_text and same_docs
        failed += 0 if ok else 1
        print(f"  {'OK  ' if ok else 'РАСХОЖДЕНИЕ'} · текст совпал={same_text} · источники совпали={same_docs}")
        rows.append({
            "question": question, "same_text": same_text, "same_docs": same_docs,
            "texts": texts, "docs": docs,
        })
        print()

    if args.out:
        Path(args.out).write_text(
            json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"вопросов: {len(rows)} · расхождений: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
