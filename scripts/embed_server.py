"""embed_server.py — локальный embedding-сервер LEX (GO/NO-GO №8, порт :8081).

OpenAI-совместимый: POST /v1/embeddings {model, input} -> {data:[{embedding}], ...}.
Модель BAAI/bge-m3 (мультиязычная, ~568M, dim 1024), CPU. Тяжёлые веса грузятся
один раз при старте; сервер не отвечает 200 на /health, пока модель не готова —
health-check в скриптах запуска опирается именно на это.

Запуск (обычно через scripts/start-embed.ps1):
    python scripts/embed_server.py --port 8081 --model D:/models/bge-m3
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, List, Union

# CPU-инференс: torch должен видеть только CPU, иначе sentence-transformers
# полезет искать CUDA и упадёт на отсутствии библиотек.
os.environ.setdefault("OMP_NUM_THREADS", "4")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger("lex.embed")

MODEL_NAME = "bge-m3"
DEFAULT_MODEL_PATH = r"D:\models\bge-m3"


class EmbedRequest(BaseModel):
    model: str | None = None
    input: Union[str, List[str]] = Field(...)
    encoding_format: str | None = None  # принимаем, но всегда отдаём float
    dimensions: int | None = None       # принимаем для совместимости, игнорируем
    user: str | None = None


class _State:
    model: Any = None
    dim: int = 0
    device: str = "cpu"
    loaded_at: float = 0.0


state = _State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global state
    model_path = os.environ.get("LEX_EMBED_MODEL", DEFAULT_MODEL_PATH)
    t0 = time.time()
    log.info("loading %s from %s (cpu)", MODEL_NAME, model_path)
    from sentence_transformers import SentenceTransformer

    try:
        m = SentenceTransformer(model_path, device="cpu")
    except Exception as e:
        log.error("не удалось загрузить модель: %s", e)
        raise
    try:
        dim = int(m.get_sentence_embedding_dimension() or 0)
    except Exception:
        dim = 0
    if not dim:
        # bge-m3 = 1024; страховка, если ST не сообщил размер
        dim = 1024
    state.model = m
    state.dim = dim
    state.loaded_at = time.time()
    log.info("model ready: dim=%d in %.1fs", dim, time.time() - t0)
    yield
    state.model = None


app = FastAPI(title="LEX embeddings", version="1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    ready = state.model is not None
    return {
        "status": "ok" if ready else "loading",
        "model": MODEL_NAME,
        "dim": state.dim,
        "device": state.device,
        "ready": ready,
    }


@app.get("/v1/models")
def models() -> dict:
    return {
        "object": "list",
        "data": [
            {
                "id": MODEL_NAME,
                "object": "model",
                "owned_by": "local",
                "created": int(state.loaded_at) if state.loaded_at else 0,
            }
        ],
    }


def _as_texts(inp: Union[str, List[str]]) -> List[str]:
    if isinstance(inp, str):
        return [inp]
    if isinstance(inp, list) and all(isinstance(x, str) for x in inp):
        return inp
    raise HTTPException(status_code=400, detail="input должен быть строкой или списком строк")


@app.post("/v1/embeddings")
def embeddings(req: EmbedRequest) -> dict:
    if state.model is None:
        raise HTTPException(status_code=503, detail="модель ещё загружается")
    texts = _as_texts(req.input)
    if not texts:
        return {"object": "list", "data": [], "model": req.model or MODEL_NAME,
                "usage": {"prompt_tokens": 0, "total_tokens": 0}}
    try:
        vecs = state.model.encode(
            texts,
            normalize_embeddings=True,
            batch_size=16,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
    except Exception as e:  # noqa: BLE001 — отдаём клиенту текст ошибки модели
        log.exception("encode failed")
        raise HTTPException(status_code=500, detail=f"encode failed: {e}") from e

    vecs = np.asarray(vecs, dtype=np.float32)
    data = [
        {"object": "embedding", "index": i, "embedding": v.tolist()}
        for i, v in enumerate(vecs)
    ]
    # usage по числу символов — приближение; OpenAI-клиенты это поле не проверяют
    approx = sum(len(t) for t in texts)
    return {
        "object": "list",
        "data": data,
        "model": req.model or MODEL_NAME,
        "usage": {"prompt_tokens": approx, "total_tokens": approx},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LEX embedding server (:8081)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--model", default=DEFAULT_MODEL_PATH,
                    help="локальный каталог модели (по умолчанию D:\\models\\bge-m3)")
    ap.add_argument("--log-level", default="info")
    args = ap.parse_args(argv)

    os.environ["LEX_EMBED_MODEL"] = args.model
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    import uvicorn

    log.info("LEX embed server -> http://%s:%d  (POST /v1/embeddings, GET /health)",
             args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())
