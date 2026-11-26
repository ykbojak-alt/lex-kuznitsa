"""rerank_server.py — локальный cross-encoder реранкер LEX (GO/NO-GO №8, порт :8082).

POST /rerank {query, documents:[...], top_n?} -> {results:[{index, score, document}]}.
Модель BAAI/bge-reranker-base (xlm-roberta-base, ~278M), CPU. Оценка — логит
релевантности пары (query, document); чем выше, тем релевантнее. Возвращаем
отсортированный список, как ждут реранкеры в RAG (Cohere/Jina-подобный формат).

Запуск (обычно через scripts/start-rerank.ps1):
    python scripts/rerank_server.py --port 8082 --model D:/models/bge-reranker-base
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Any

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger("lex.rerank")

MODEL_NAME = "bge-reranker-base"
DEFAULT_MODEL_PATH = r"D:\models\bge-reranker-base"
MAX_LENGTH = 512


class RerankRequest(BaseModel):
    query: str = Field(..., description="поисковый запрос")
    documents: list[str] = Field(..., description="кандидаты на ранжирование")
    top_n: int | None = Field(None, description="вернуть только N лучших")
    return_documents: bool = Field(True, description="включать текст документа в ответ")
    model: str | None = None


class _State:
    model: Any = None
    tokenizer: Any = None
    device: str = "cpu"
    loaded_at: float = 0.0


state = _State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    model_path = os.environ.get("LEX_RERANK_MODEL", DEFAULT_MODEL_PATH)
    t0 = time.time()
    log.info("loading %s from %s (cpu)", MODEL_NAME, model_path)
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    try:
        _kw = {"local_files_only": True} if (os.path.isabs(model_path) and os.path.isdir(model_path)) else {}
        tok = AutoTokenizer.from_pretrained(model_path, **_kw)
        model = AutoModelForSequenceClassification.from_pretrained(model_path, **_kw)
        model.eval()
        model.to("cpu")
    except Exception as e:
        log.error("не удалось загрузить модель: %s", e)
        raise
    state.tokenizer = tok
    state.model = model
    state.loaded_at = time.time()
    log.info("model ready in %.1fs", time.time() - t0)
    yield
    state.model = None
    state.tokenizer = None


app = FastAPI(title="LEX reranker", version="1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    ready = state.model is not None
    return {
        "status": "ok" if ready else "loading",
        "model": MODEL_NAME,
        "device": state.device,
        "ready": ready,
    }


@app.get("/v1/models")
def models() -> dict:
    return {
        "object": "list",
        "data": [{
            "id": MODEL_NAME,
            "object": "model",
            "owned_by": "local",
            "created": int(state.loaded_at) if state.loaded_at else 0,
        }],
    }


def _score_pairs(query: str, docs: list[str]) -> list[float]:
    import torch

    out: list[float] = []
    bs = 8
    for i in range(0, len(docs), bs):
        batch = docs[i:i + bs]
        enc = state.tokenizer(
            [query] * len(batch),
            batch,
            padding=True,
            truncation=True,
            max_length=MAX_LENGTH,
            return_tensors="pt",
        )
        with torch.inference_mode():
            logits = state.model(**enc).logits.view(-1).float()
        out.extend(logits.tolist())
    return out


@app.post("/rerank")
def rerank(req: RerankRequest) -> dict:
    if state.model is None:
        raise HTTPException(status_code=503, detail="модель ещё загружается")
    if not req.documents:
        return {"model": req.model or MODEL_NAME, "results": []}
    try:
        scores = _score_pairs(req.query, req.documents)
    except Exception as e:
        log.exception("rerank failed")
        raise HTTPException(status_code=500, detail=f"rerank failed: {e}") from e

    order = np.argsort(np.asarray(scores))[::-1]
    results = []
    for idx in order:
        idx = int(idx)
        item = {"index": idx, "score": float(scores[idx])}
        if req.return_documents:
            item["document"] = req.documents[idx]
        results.append(item)
    if req.top_n is not None and req.top_n >= 0:
        results = results[: req.top_n]
    return {"model": req.model or MODEL_NAME, "results": results}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LEX reranker server (:8082)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8082)
    ap.add_argument("--model", default=DEFAULT_MODEL_PATH,
                    help="локальный каталог модели (по умолчанию D:\\models\\bge-reranker-base)")
    ap.add_argument("--log-level", default="info")
    args = ap.parse_args(argv)

    os.environ["LEX_RERANK_MODEL"] = args.model
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    import uvicorn

    log.info("LEX rerank server -> http://%s:%d  (POST /rerank, GET /health)",
             args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())
