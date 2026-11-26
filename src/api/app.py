"""FastAPI-приложение LEX: HTTP-слой над ядром src/lex.

Запуск:
    uvicorn api.app:app --port 8874        (из src/)
    uvicorn src.api.app:app --port 8874    (из корня репозитория)

Эндпоинты и события — ровно по docs/API-CONTRACT.md v1. Логики RAG здесь нет.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

# Запуск из корня репозитория (`uvicorn src.api.app:app`) — ядро лежит в src/lex,
# поэтому src должен быть в sys.path. Из каталога src/ это уже так.
_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from . import cases as cases_mod, config, core, preview, probes  # noqa: E402

app = FastAPI(title="LEX API", version=config.VERSION, docs_url="/api/docs",
              openapi_url="/api/openapi.json")

# Статика отдаётся этим же приложением — UI и API на одном origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ---------- схемы ----------

class AskRequest(BaseModel):
    question: str = Field(..., min_length=1)
    role_id: str = Field(..., min_length=1)
    as_of: str | None = None


class CaseRequest(BaseModel):
    question: str = Field(..., min_length=1)
    answer_in_practice: str = Field(..., min_length=1)
    outcome: str = "open"
    author_role: str = ""
    author_name: str = "—"
    linked_docs: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    rule_key: str = ""
    content_type: str = "text"


# ---------- health / roles ----------

@app.get("/api/health")
async def health() -> dict:
    probed = await probes.probe_all()
    return {
        "status": "ok",
        "version": config.VERSION,
        "llm": probed["llm"],
        "embed": probed["embed"],
        "rerank": probed["rerank"],
        "ocr": {"available": preview.ocr_available(),
                "engine": "tesseract", "lang": "rus"},
        "cases": {"count": cases_mod.count()},
        "corpus": {**core.corpus_stats(), "content_types": core.content_type_mix()},
    }


@app.get("/api/roles")
def get_roles() -> list[dict]:
    return core.roles()


# ---------- ask (SSE) ----------

@app.post("/api/ask")
async def ask(req: AskRequest):
    if req.role_id not in core.role_ids():
        raise HTTPException(status_code=400, detail=f"неизвестная роль: {req.role_id}")
    if req.as_of:
        try:
            import datetime as _dt
            _dt.date.fromisoformat(req.as_of)
        except ValueError:
            raise HTTPException(status_code=400, detail="as_of должен быть YYYY-MM-DD") from None

    gen = core.stream_answer(req.question, req.role_id, req.as_of)
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Caddy/nginx не должны буферизовать SSE
        },
    )


# ---------- journal / analytics / ingest ----------

@app.get("/api/journal")
def get_journal(limit: int = Query(50, ge=1, le=1000), role_id: str | None = None) -> list[dict]:
    return core.journal_read(limit=limit, role_id=role_id)


@app.get("/api/analytics")
def get_analytics() -> dict:
    return core.analytics()


@app.post("/api/ingest")
def post_ingest() -> dict:
    return core.ingest()


# ---------- кейсы (§8 v1.1) ----------

@app.post("/api/cases")
def post_case(req: CaseRequest) -> dict:
    try:
        return cases_mod.add(req.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@app.get("/api/cases")
def get_cases(limit: int = Query(50, ge=1, le=1000),
              role_id: str | None = None,
              tag: str | None = None,
              rule_key: str | None = None) -> list[dict]:
    return cases_mod.list_cases(limit=limit, role_id=role_id, tag=tag, rule_key=rule_key)


# ---------- превью источника (§9 v1.1) ----------

@app.get("/api/doc/{doc_id}/preview")
def doc_preview(doc_id: str, page: int = Query(1, ge=1)):
    path = preview.resolve(doc_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"документ не найден: {doc_id}")

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        rendered = preview.pdf_page_png(path, page)
        if rendered is not None:
            data, ctype = rendered
            from fastapi import Response
            return Response(content=data, media_type=ctype,
                            headers={"Cache-Control": "public, max-age=300"})
        # движка нет — отдаём сам PDF, честно указывая, что это не страница
        return FileResponse(path, media_type="application/pdf",
                            filename=path.name,
                            headers={"X-Preview-Note": "pdf-page-render-unavailable",
                                     "Cache-Control": "public, max-age=300"})

    ctype = preview.content_type_for(path)
    if ctype.startswith("text/") or suffix in (".md", ".txt"):
        ctype = "text/plain; charset=utf-8"
    return FileResponse(path, media_type=ctype,
                        headers={"Cache-Control": "public, max-age=300"})


# ---------- статика ----------

@app.get("/")
def index():
    idx = config.STATIC_DIR / "index.html"
    if idx.exists():
        return FileResponse(idx)
    return JSONResponse(
        status_code=503,
        content={"detail": "UI не собран: нет src/api/static/index.html",
                 "hint": "скопируйте web/ в src/api/static/ (NEO собирает фронтенд)"},
    )


@app.get("/{path:path}")
def static_files(path: str, request: Request):
    """Раздача собранного UI; /api/* сюда не попадает (маршруты объявлены выше)."""
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="not found")
    target = (config.STATIC_DIR / path).resolve()
    try:
        target.relative_to(config.STATIC_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=404, detail="not found") from None
    if target.is_file():
        return FileResponse(target)
    # SPA-fallback: неизвестный путь -> index.html (если он есть)
    idx = config.STATIC_DIR / "index.html"
    if idx.exists():
        return FileResponse(idx)
    raise HTTPException(status_code=404, detail="not found")


@app.on_event("startup")
def _startup() -> None:
    config.ensure_dirs()
