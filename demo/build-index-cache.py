#!/usr/bin/env python3
"""build-index-cache — посчитать векторы корпуса ОДИН раз и положить кэш.

Зачем отдельный скрипт: ``SearchIndex.vector()`` при отсутствии кэша сам зовёт
``build_vectors()``, и первый же поиск растягивается на десятки минут (bge-m3 на
CPU: ~14 с на чанк при 2 потоках). Это скрытая мина — демо просто повиснет.
Кэш считается заранее и один раз, дальше поиск идёт из ``.lex-index.npz``.

Запуск (лучше отсоединённо, чтобы не держать терминал):

    python demo/build-index-cache.py                 # corpus-real
    python demo/build-index-cache.py sample-corpus
    python demo/build-index-cache.py corpus-real --corpus-norm corpus-norm --force
                                                     # объединённый корпус (12/875)

Второй корень обязателен, если стенд поднят с ``corpus-norm``: кэш сверяется
по ``chunk_ids`` со списком чанков индекса, и кэш от одного корня объединённый
корпус отвергнет — векторный поиск молча деградирует до BM25 («вектор 0»).

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

from lex.corpus import load_corpus, load_corpus_multi
from lex.index import SearchIndex


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("corpus", nargs="?", default="corpus-real")
    ap.add_argument(
        "--corpus-norm",
        default=None,
        help="второй корень (внешние акты). Кэш ляжет в root первого корня, "
             "с chunk_ids объединённого списка — иначе SearchIndex его отвергнет",
    )
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

    norm = None
    if args.corpus_norm:
        norm = Path(args.corpus_norm)
        if not norm.is_absolute():
            norm = ROOT / norm
        if not (norm / "manifest.json").exists():
            print(f"нет второго корпуса: {norm}", file=sys.stderr)
            return 1

    # При двух корнях — load_corpus_multi: SearchIndex сверяет chunk_ids кэша
    # со своим списком чанков, поэтому кэш обязан быть от объединённого корпуса.
    # corpus.root у объединённого = первый корень (load_corpus_multi, bases[0]),
    # туда же и пишется .lex-index.npz.
    corpus = load_corpus_multi([root, norm]) if norm is not None else load_corpus(root)
    cache = corpus.root / ".lex-index.npz"
    if cache.exists() and not args.force:
        print(f"кэш уже есть: {cache} ({cache.stat().st_size} байт) — не пересчитываю")
        print("если корпус менялся — пересчитайте с --force")
        return 0

    print(f"корпус: {root} · документов {len(corpus.documents)} · чанков {len(corpus.chunks)}")
    if args.force and cache.exists():
        print(f"ВНИМАНИЕ: {cache} будет перезаписан целиком")
    # Три источника, разброс втрое — печатаем все, чтобы не ждать вслепую и не
    # верить одному числу. Докстринг embed_texts: ~14 с/чанк (2 потока).
    # Замер roy1 по времени: 4.8 с/чанк. Замер по темпу батчей: 1.67 батч/мин
    # (8 текстов в батче) → 110 батчей на 875 чанков ≈ 66 мин.
    # На сервере будет своё — замерь до запуска.
    n_ch = len(corpus.chunks)
    n_batches = max(1, (n_ch + 7) // 8)
    print(
        f"оценка времени: ~{n_ch * 14.0 / 60:.0f} мин (докстринг, 14 с/чанк) … "
        f"~{n_ch * 4.8 / 60:.0f} мин (замер roy1, 4.8 с/чанк) … "
        f"~{n_batches / 1.67:.0f} мин (замер по темпу, 1.67 батч/мин)"
    )
    t0 = time.time()
    idx = SearchIndex(corpus)
    n = idx.build_vectors(force=args.force)
    dt = time.time() - t0
    print(f"готово: {n} векторов за {dt:.0f} с ({dt / max(1, n):.1f} с/чанк)")
    print(f"кэш: {cache} ({cache.stat().st_size} байт)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
