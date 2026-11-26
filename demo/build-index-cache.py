#!/usr/bin/env python3
"""build-index-cache — посчитать векторы корпуса ОДИН раз и положить кэш.

Зачем отдельный скрипт: ``SearchIndex.vector()`` при отсутствии кэша сам зовёт
``build_vectors()``, и первый же поиск растягивается на десятки минут (bge-m3 на
CPU: ~14 с на чанк при 2 потоках). Это скрытая мина — демо просто повиснет.
Кэш считается заранее и один раз, дальше поиск идёт из ``.lex-index.npz``.

Запуск (лучше отсоединённо, чтобы не держать терминал):

    python demo/build-index-cache.py                 # corpus-real
    python demo/build-index-cache.py sample-corpus

Считает пачками и печатает прогресс, чтобы было видно, что не зависло.
Прерывание безопасно: кэш пишется только целиком, в конце.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lex.corpus import load_corpus
from lex.index import SearchIndex


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("corpus", nargs="?", default="corpus-real")
    ap.add_argument("--force", action="store_true", help="пересчитать даже при живом кэше")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    root = Path(args.corpus)
    if not root.is_absolute():
        root = ROOT / root
    if not (root / "manifest.json").exists():
        print(f"нет корпуса: {root}", file=sys.stderr)
        return 1

    corpus = load_corpus(root)
    cache = root / ".lex-index.npz"
    if cache.exists() and not args.force:
        print(f"кэш уже есть: {cache} ({cache.stat().st_size} байт) — не пересчитываю")
        return 0

    print(f"корпус: {root} · документов {len(corpus.documents)} · чанков {len(corpus.chunks)}")
    t0 = time.time()
    idx = SearchIndex(corpus)
    n = idx.build_vectors(force=args.force)
    dt = time.time() - t0
    print(f"готово: {n} векторов за {dt:.0f} с ({dt / max(1, n):.1f} с/чанк)")
    print(f"кэш: {cache} ({cache.stat().st_size} байт)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
