"""_quality_check.py — разовый замер качества embedder/reranker на sample-corpus.

Не часть RAG-конвейера и не код продукта: нужен только чтобы обосновать выбор
моделей числами (recall@k эмбеддингов, прирост от реранкера). Ходит по HTTP в
уже поднятые серверы :8081/:8082, ничего не пишет на диск.

    python scripts/_quality_check.py --questions 30 --top-k 10
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "sample-corpus"


def post(url: str, payload: dict) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode("utf-8"))


def cos(a, b) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if not na or not nb:
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=False)) / (na * nb)


def load_docs(limit_chars: int):
    docs = []
    for p in sorted(CORPUS.glob("ТЗ-ЭКЗ-*.md")):
        text = p.read_text(encoding="utf-8")
        docs.append({"id": p.stem.split("-рег")[0].split("-инс")[0].split("-ста")[0]
                     .split("-пор")[0].split("-мет")[0], "file": p.name,
                     "text": text[:limit_chars]})
    return docs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--embed", default="http://127.0.0.1:8081")
    ap.add_argument("--rerank", default="http://127.0.0.1:8082")
    ap.add_argument("--questions", type=int, default=30)
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--doc-chars", type=int, default=2500)
    args = ap.parse_args()

    docs = load_docs(args.doc_chars)
    qs = [json.loads(line) for line in (CORPUS / "questions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    qs = [q for q in qs if not q.get("refusal_expected")][: args.questions]
    print(f"документов: {len(docs)}, вопросов: {len(qs)}, doc_chars={args.doc_chars}")

    t0 = time.time()
    doc_vecs = post(f"{args.embed}/v1/embeddings",
                    {"model": "bge-m3", "input": [d["text"] for d in docs]})["data"]
    doc_vecs = [x["embedding"] for x in doc_vecs]
    q_vecs = post(f"{args.embed}/v1/embeddings",
                  {"model": "bge-m3", "input": [q["question"] for q in qs]})["data"]
    q_vecs = [x["embedding"] for x in q_vecs]
    t_emb = time.time() - t0
    print(f"эмбеддинги: {len(docs) + len(qs)} текстов за {t_emb:.1f} с")

    hit1 = hit3 = 0
    pairs_total = 0
    t1 = time.time()
    rerank_hit1 = 0
    for q, qv in zip(qs, q_vecs, strict=False):
        scored = sorted(((cos(qv, dv), i) for i, dv in enumerate(doc_vecs)), reverse=True)
        top = [i for _, i in scored[: args.top_k]]
        if docs[top[0]]["id"] == q["expected_doc_id"]:
            hit1 += 1
        if q["expected_doc_id"] in [docs[i]["id"] for i in top[:3]]:
            hit3 += 1

        r = post(f"{args.rerank}/rerank",
                 {"query": q["question"], "documents": [docs[i]["text"] for i in top],
                  "top_n": 1, "return_documents": False})
        pairs_total += len(top)
        best = top[r["results"][0]["index"]]
        if docs[best]["id"] == q["expected_doc_id"]:
            rerank_hit1 += 1
    t_rr = time.time() - t1

    n = len(qs)
    print(f"\n--- результат на {n} вопросах ---")
    print(f"vector top-1 : {hit1}/{n} = {hit1/n:.1%}")
    print(f"vector top-3 : {hit3}/{n} = {hit3/n:.1%}")
    print(f"+rerank top-1: {rerank_hit1}/{n} = {rerank_hit1/n:.1%}   "
          f"({pairs_total} пар за {t_rr:.1f} с, {t_rr/max(pairs_total,1)*1000:.0f} мс/пара)")
    print(f"эмбеддинг-фаза: {t_emb:.1f} с")
    return 0


if __name__ == "__main__":
    sys.exit(main())
