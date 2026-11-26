"""FastAPI-приложение LEX: HTTP-слой над ядром src/lex.

Запуск:
    uvicorn api.app:app --port 8874        (из src/)
    uvicorn src.api.app:app --port 8874    (из корня репозитория)

Эндпоинты и события — по docs/API-CONTRACT.md v1.13. Логики RAG здесь нет:
всё содержательное живёт в src/lex, этот слой только транспорт.
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

# Запуск из корня репозитория (`uvicorn src.api.app:app`) — ядро лежит в src/lex,
# поэтому src должен быть в sys.path. Из каталога src/ это уже так.
_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from . import (
    cases as cases_mod,
)
from . import (
    changes,
    config,
    core,
    documents,
    mail,
    preview,
    probes,
    tickets,
)

app = FastAPI(
    title="LEX API",
    version=config.VERSION,
    description="HTTP-слой LEX: поиск по базе знаний организации, кейсы, формы, "
                "тикеты службы качества. Логики RAG здесь нет — только транспорт.",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ---------- схемы ----------

class AskRequest(BaseModel):
    question: str = ""
    role_id: str = ""
    as_of: str | None = None
    session_id: str | None = None
    answers: dict | None = None
    tone: str = "plain"
    business_line: str | None = None


class CaseRequest(BaseModel):
    question: str = ""
    answer_in_practice: str = ""
    outcome: str = "open"
    author_role: str = ""
    author_name: str = "—"
    linked_docs: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    rule_key: str = ""
    content_type: str = "text"


class ExpandRequest(BaseModel):
    detail_id: str = ""
    section: str = "all"


class FillRequest(BaseModel):
    session_id: str = ""
    values: dict = Field(default_factory=dict)


class TicketRequest(BaseModel):
    question: str = ""
    kind: str = "not_in_corpus"
    priority: str = "normal"
    session_id: str = ""
    role_id: str = ""
    business_line: str = ""
    why_unresolved: str = ""
    clarifications: list[dict] = Field(default_factory=list)
    searched: dict = Field(default_factory=dict)
    escalation_id: str = ""


class EscalateRequest(BaseModel):
    # Обязательность проверяет tickets.create_escalation и отвечает 400 по §15.2.
    # Если объявить Field(...) здесь, FastAPI вернёт 422 — контракт этого не разрешает.
    question: str = ""
    why_unresolved: str = ""
    kind: str = "not_in_corpus"
    session_id: str = ""
    role_id: str = ""
    business_line: str = ""
    clarifications: list[dict] = Field(default_factory=list)
    searched: dict = Field(default_factory=dict)


class AnswerRequest(BaseModel):
    answer: str = ""
    resolved_by: str = "quality"


class ReplyRequest(BaseModel):
    """§22.3: почтовый клиент присылает `body`, а не `answer`."""
    body: str = ""
    resolved_by: str = "quality"


class AssignRequest(BaseModel):
    assignee: str = ""
    force: bool = False


class CloseRequest(BaseModel):
    reason: str = ""


class ConfirmRequest(BaseModel):
    case: dict = Field(default_factory=dict)
    acknowledge_warnings: bool = False
    editor_role: str = ""


class ChangeStatusRequest(BaseModel):
    status: str = ""
    by: str = ""


# ---------- health / роли / направления ----------

@app.get("/api/health")
async def health() -> dict:
    probed = await probes.probe_all()
    return {
        "status": "ok",
        "version": config.VERSION,
        "llm": probed["llm"],
        "embed": probed["embed"],
        "rerank": probed["rerank"],
        "ocr": {"available": documents.ocr_available(),
                "engine": documents.ocr_engine(), "lang": "rus"},
        "cases": {"count": cases_mod.count()},
        "tickets": {"open": len(tickets.list_tickets(status="open"))},
        "email": mail.stats(),
        "corpus": {**core.corpus_stats(), "content_types": core.content_type_mix()},
        "core": {"ready": core.core_ready()},
    }


@app.get("/api/roles")
def get_roles() -> list[dict]:
    return core.roles()


@app.get("/api/business-lines")
def get_business_lines() -> list[dict]:
    """§11: направления из данных ядра, иначе — производные от корпуса."""
    core.core_modules()["orchestrator"]
    policy_mod = core._try("lex.policy")
    c = core.corpus()
    if policy_mod is not None and c is not None and hasattr(policy_mod, "available_lines"):
        try:
            return list(policy_mod.available_lines(c))
        except Exception:
            pass
    return core.business_lines()


# ---------- ask (SSE) ----------

@app.post("/api/ask")
async def ask(req: AskRequest):
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question обязателен")
    if req.role_id not in core.role_ids():
        raise HTTPException(status_code=400, detail=f"неизвестная роль: {req.role_id}")
    if req.as_of:
        try:
            import datetime as _dt

            _dt.date.fromisoformat(req.as_of)
        except ValueError:
            raise HTTPException(status_code=400, detail="as_of должен быть YYYY-MM-DD") from None

    gen = core.stream_answer(req.question, req.role_id, req.as_of,
                             req.session_id, req.answers, req.tone,
                             business_line=req.business_line)
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Caddy/nginx не должны буферизовать SSE
        },
    )


# ---------- журнал / аналитика / ingest ----------

@app.get("/api/journal")
def get_journal(limit: int = Query(50, ge=1, le=1000),
                role_id: str | None = None) -> list[dict]:
    return core.journal_read(limit=limit, role_id=role_id)


@app.get("/api/analytics")
def get_analytics() -> dict:
    """§2 + §11 + §16.6 + §18.6 + §19.5 + §20.5 + §13 similar_questions."""
    base = core.analytics()
    caveats = _caveats(base)
    base.update({
        "by_business_line": _by_business_line(),
        "missing_by_line": _missing_by_line(base),
        "similar_questions": _similar_questions(),
        "knowledge_loop": _knowledge_loop(),
        "tickets": _tickets_analytics(),
        "change_requests": changes.analytics(),
        "caveats": caveats,
        # §20.5: самая честная метрика продукта — на верхнем уровне,
        # чтобы её не приходилось выковыривать из блока оговорок.
        "answer_rate": caveats["answer_rate"],
    })
    return base


def _by_business_line() -> list[dict]:
    counts: dict[str, dict[str, int]] = {}
    for row in core.journal_read(limit=100_000):
        line = str(row.get("business_line") or "общее")
        slot = counts.setdefault(line, {"count": 0, "refusals": 0})
        slot["count"] += 1
        if row.get("refused"):
            slot["refusals"] += 1
    return [{"line": k, **v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1]["count"])]


def _missing_by_line(base: dict) -> list[dict]:
    out: dict[str, int] = {}
    for row in core.journal_read(limit=100_000):
        if not row.get("refused"):
            continue
        line = str(row.get("business_line") or "общее")
        out[line] = out.get(line, 0) + 1
    return [{"line": k, "count": v} for k, v in sorted(out.items(), key=lambda kv: -kv[1])]


def _normalise_question(text: str) -> str:
    import re

    low = (text or "").lower().strip()
    low = re.sub(r"[^\w\s]", " ", low, flags=re.UNICODE)
    return re.sub(r"\s+", " ", low).strip()


def _similar_questions() -> list[dict]:
    """§13 (v1.6): группировка похожих вопросов по нормализованному тексту.

    Группировку журнала делает ACE; пока её нет — считаем здесь по журналу,
    и это честно помечено в отчёте. Канонический вопрос — самый частый вариант.
    """
    groups: dict[str, dict] = {}
    for row in core.journal_read(limit=100_000):
        question = str(row.get("question") or "")
        key = _normalise_question(question)
        if not key:
            continue
        slot = groups.setdefault(key, {"variants": {}, "count": 0, "refusals": 0,
                                       "last_seen": ""})
        slot["count"] += 1
        slot["refusals"] += 1 if row.get("refused") else 0
        slot["variants"][question] = slot["variants"].get(question, 0) + 1
        ts = str(row.get("ts") or "")
        if ts > slot["last_seen"]:
            slot["last_seen"] = ts

    out = []
    for slot in groups.values():
        variants = sorted(slot["variants"].items(), key=lambda kv: -kv[1])
        canonical = variants[0][0]
        out.append({
            "canonical": canonical,
            "count": slot["count"],
            "variants": [v for v, _ in variants if v != canonical][:5],
            "last_seen": slot["last_seen"],
            "refusal_rate": round(slot["refusals"] / slot["count"], 3) if slot["count"] else 0.0,
        })
    out.sort(key=lambda x: -x["count"])
    return out[:20]


def _knowledge_loop() -> dict:
    """§16.6: эскалации → ответы → кейсы."""
    esc = tickets.list_escalations(limit=10_000)
    answered = [e for e in esc if e.get("status") == "answered" or e.get("answer")]
    converted = [e for e in esc if e.get("case_id")]
    hours: list[float] = []
    for e in answered:
        import datetime as dt

        try:
            t0 = dt.datetime.strptime(str(e.get("ts")), "%Y-%m-%dT%H:%M:%SZ")
            t1 = dt.datetime.strptime(str(e.get("answered_at")), "%Y-%m-%dT%H:%M:%SZ")
            hours.append((t1 - t0).total_seconds() / 3600)
        except Exception:
            continue
    not_converted = [
        {"ticket": e.get("id"), "why": "ответ не оформлен кейсом"}
        for e in answered if not e.get("case_id")
    ]
    return {
        "escalations_total": len(esc),
        "answered": len(answered),
        "converted_to_cases": len(converted),
        "avg_hours_to_answer": round(sum(hours) / len(hours), 1) if hours else 0.0,
        "questions_now_covered": len(converted),
        "not_converted": not_converted[:20],
    }


def _tickets_analytics() -> dict:
    """§18.6: очередь службы качества."""
    rows = tickets.list_tickets(limit=10_000)
    by_status: dict[str, int] = {}
    by_line: dict[str, int] = {}
    topics: dict[str, int] = {}
    to_assign: list[float] = []
    to_answer: list[float] = []
    breached = 0
    import datetime as dt

    now = dt.datetime.now(dt.UTC)

    def _ts(value) -> dt.datetime | None:
        try:
            return dt.datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=dt.UTC)
        except Exception:
            return None

    for r in rows:
        st = str(r.get("status") or "open")
        by_status[st] = by_status.get(st, 0) + 1
        line = str((r.get("asker") or {}).get("business_line") or "общее")
        by_line[line] = by_line.get(line, 0) + 1
        topic = _normalise_question(str(r.get("question") or ""))[:60]
        if topic:
            topics[topic] = topics.get(topic, 0) + 1
        created = _ts(r.get("created_at"))
        assigned = _ts(r.get("assigned_at"))
        answered = _ts(r.get("answered_at"))
        if created and assigned:
            to_assign.append((assigned - created).total_seconds() / 3600)
        if created and answered:
            to_answer.append((answered - created).total_seconds() / 3600)
        due = _ts(r.get("sla_due"))
        if due and st not in ("closed",) and now > due:
            breached += 1

    return {
        "total": len(rows),
        "open": by_status.get("open", 0),
        "assigned": by_status.get("assigned", 0),
        "answered": by_status.get("answered", 0),
        "closed": by_status.get("closed", 0),
        "by_business_line": [{"line": k, "count": v}
                             for k, v in sorted(by_line.items(), key=lambda kv: -kv[1])],
        "avg_hours_to_assign": round(sum(to_assign) / len(to_assign), 1) if to_assign else 0.0,
        "avg_hours_to_answer": round(sum(to_answer) / len(to_answer), 1) if to_answer else 0.0,
        "sla_breached": breached,
        "top_topics": [{"topic": k, "count": v}
                       for k, v in sorted(topics.items(), key=lambda kv: -kv[1])[:10]],
    }


def _caveats(base: dict) -> dict:
    """§20.5: оговорки по журналу.

    Ключи `caveat_level` / `caveat_reasons` пишет транспорт (`core.stream_answer`):
    ядро отдаёт оговорку вложенно в `AnswerData.caveat`, а в журнал сам её не
    кладёт. Уровни, которых не было, отдаём нулями — UI не должен падать.
    """
    rows = core.journal_read(limit=100_000)
    by_level = {"low": 0, "medium": 0, "high": 0}
    by_reason: dict[str, int] = {}
    caveated = 0
    refused = 0
    plain = 0
    for r in rows:
        level = r.get("caveat_level")
        if level:
            caveated += 1
            if level in by_level:
                by_level[level] += 1
            else:
                by_level[str(level)] = by_level.get(str(level), 0) + 1
            for code in r.get("caveat_reasons") or []:
                key = code.get("code") if isinstance(code, dict) else str(code)
                if key:
                    by_reason[key] = by_reason.get(key, 0) + 1
        elif r.get("refused"):
            refused += 1
        else:
            plain += 1
    return {
        "total": caveated,
        "by_level": by_level,
        "by_reason": [{"code": k, "count": v}
                      for k, v in sorted(by_reason.items(), key=lambda kv: -kv[1])],
        "answer_rate": {"plain": plain, "caveated": caveated, "refused": refused},
    }


@app.post("/api/ingest")
def post_ingest() -> dict:
    return core.ingest()


# ---------- раскрытие ответа (§13.1) ----------

@app.post("/api/expand")
def post_expand(req: ExpandRequest) -> dict:
    if not req.detail_id.strip():
        raise HTTPException(status_code=400, detail="detail_id обязателен")
    data = core.expand(req.detail_id, req.section)
    if data is None:
        raise HTTPException(
            status_code=404,
            detail=f"detail_id не найден: {req.detail_id} "
                   "(развёрнутая версия живёт в памяти ядра того процесса, "
                   "который ответил; после перезапуска её нет)")
    out = {k: data.get(k) for k in ("short", "detailed", "sections", "tables", "citations")}
    if data.get("section_requested"):
        out["section_requested"] = data["section_requested"]
    return out


# ---------- кейсы (§8) ----------

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


# ---------- формы (§13.3-§13.4) ----------

@app.get("/api/forms")
def get_forms() -> list[dict]:
    out = []
    for f in core.forms():
        out.append({
            "form_id": f.get("form_id"),
            "title": f.get("title"),
            "doc_id": f.get("doc_id"),
            "fields": len(f.get("fields") or []),
            "format": f.get("format", "md"),
            "business_line": f.get("business_line", ""),
        })
    return out


@app.get("/api/forms/{form_id}")
def get_form(form_id: str) -> dict:
    data = core.form(form_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"форма не найдена: {form_id}")
    return data


@app.get("/api/forms/{form_id}/download")
def download_form(form_id: str):
    path = core.form_download(form_id)
    if path is None or not Path(path).is_file():
        raise HTTPException(status_code=404, detail=f"бланк формы не найден: {form_id}")
    return FileResponse(path, media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": documents.disposition(Path(path).name)})


@app.post("/api/forms/{form_id}/fill")
def fill_form(form_id: str, req: FillRequest) -> dict:
    result = core.form_fill(form_id, req.values)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"форма не найдена или ядро не умеет проверку: {form_id}")
    return result


# ---------- документы (§13.2) ----------

def _doc_or_error(doc_id: str, role_id: str) -> dict:
    try:
        return documents.check_access(doc_id, role_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"документ не найден: {doc_id}") from None
    except documents.AccessDenied as e:
        raise HTTPException(status_code=403, detail=e.reason) from None


@app.get("/api/doc/{doc_id}", response_class=HTMLResponse)
def get_document(doc_id: str, role_id: str = Query("lawyer")):
    _doc_or_error(doc_id, role_id)
    try:
        return HTMLResponse(documents.document_html(doc_id, role_id))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"файл документа отсутствует: {doc_id}") from None


@app.get("/api/doc/{doc_id}/download")
def download_document(doc_id: str, role_id: str = Query("lawyer")):
    _doc_or_error(doc_id, role_id)
    path = documents.resolve(doc_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"файл документа отсутствует: {doc_id}")
    return FileResponse(path, media_type=documents.content_type_for(path),
                        headers={"Content-Disposition": documents.disposition(path.name)})


@app.get("/api/doc/{doc_id}/preview")
def doc_preview(doc_id: str, page: int = Query(1, ge=1), role_id: str = Query("lawyer")):
    _doc_or_error(doc_id, role_id)
    got = documents.preview_bytes(doc_id, page)
    if got is not None:
        data, ctype = got
        return Response(content=data, media_type=ctype,
                        headers={"Cache-Control": "public, max-age=300"})

    path = documents.resolve(doc_id)
    if path is None:
        raise HTTPException(status_code=404, detail=f"файл документа отсутствует: {doc_id}")
    suffix = path.suffix.lower()
    if suffix in documents.PDF_EXT:
        return FileResponse(path, media_type="application/pdf",
                            headers={"Content-Disposition": documents.disposition(path.name),
                                     "X-Preview-Note": "pdf-page-render-unavailable",
                                     "Cache-Control": "public, max-age=300"})
    ctype = preview.content_type_for(path)
    if ctype.startswith("text/") or suffix in (".md", ".txt"):
        ctype = "text/plain; charset=utf-8"
    return FileResponse(path, media_type=ctype,
                        headers={"Cache-Control": "public, max-age=300"})


# ---------- тикеты (§18) ----------

@app.post("/api/tickets")
def post_ticket(req: TicketRequest) -> dict:
    try:
        return mail.decorate(tickets.create_ticket(req.model_dump()))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@app.get("/api/tickets")
def get_tickets(status: str | None = None, business_line: str | None = None,
                assignee: str | None = None, role_id: str | None = None,
                limit: int = Query(100, ge=1, le=1000)) -> list[dict]:
    return [mail.decorate(r) for r in tickets.list_tickets(
        status=status, business_line=business_line,
        assignee=assignee, role_id=role_id, limit=limit)]


def _ticket_or_404(tid: str) -> dict:
    row = tickets.get_ticket(tid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"тикет не найден: {tid}")
    return row


@app.get("/api/tickets/{tid}")
def get_ticket(tid: str) -> dict:
    return mail.decorate(_ticket_or_404(tid))


@app.post("/api/tickets/{tid}/assign")
def assign_ticket(tid: str, req: AssignRequest) -> dict:
    try:
        return mail.decorate(tickets.assign_ticket(tid, req.assignee, force=req.force))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"тикет не найден: {tid}") from None
    except PermissionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@app.post("/api/tickets/{tid}/answer")
def answer_ticket(tid: str, req: AnswerRequest) -> dict:
    try:
        return mail.decorate(tickets.answer_ticket(tid, req.answer, req.resolved_by))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"тикет не найден: {tid}") from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@app.post("/api/tickets/{tid}/close")
def close_ticket(tid: str, req: CloseRequest) -> dict:
    try:
        return mail.decorate(tickets.close_ticket(tid, req.reason))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"тикет не найден: {tid}") from None


# ---------- входящие службы качества (§22.3) ----------

@app.get("/api/inbox")
def get_inbox(status: str | None = None, role_id: str | None = None,
              limit: int = Query(100, ge=1, le=1000)) -> dict:
    """Входящие как почтовый ящик: обёртка над тикетами, своего хранилища нет."""
    try:
        letters = mail.list_inbox(status=status, role_id=role_id, limit=limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return {
        "mode": config.EMAIL_MODE,
        "mailbox": config.QUALITY_EMAIL,
        "delivery": "simulated",
        "stats": mail.stats(),
        "letters": letters,
    }


@app.get("/api/inbox/{tid}")
def get_inbox_letter(tid: str) -> dict:
    letter = mail.open_letter(tid)
    if letter is None:
        raise HTTPException(status_code=404, detail=f"письмо не найдено: {tid}")
    return letter


@app.post("/api/inbox/{tid}/reply")
def reply_inbox(tid: str, req: ReplyRequest) -> dict:
    try:
        return mail.reply(tid, req.body, req.resolved_by)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"письмо не найдено: {tid}") from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@app.get("/api/tickets/{tid}/draft")
def ticket_draft(tid: str) -> dict:
    data = tickets.ticket_draft(tid)
    if data is None:
        raise HTTPException(status_code=404, detail=f"тикет не найден: {tid}")
    return data | {"email": mail.email_block(_ticket_or_404(tid)), "channel": "email"}


@app.post("/api/tickets/{tid}/confirm")
def confirm_ticket(tid: str, req: ConfirmRequest) -> dict:
    try:
        return tickets.confirm_ticket(tid, req.model_dump())
    except KeyError:
        raise HTTPException(status_code=404, detail=f"тикет не найден: {tid}") from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


# ---------- эскалации (§15) ----------

@app.post("/api/escalate")
def post_escalation(req: EscalateRequest) -> dict:
    try:
        return tickets.create_escalation(req.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@app.get("/api/escalate")
def get_escalations(status: str | None = None,
                    limit: int = Query(100, ge=1, le=1000)) -> list[dict]:
    return tickets.list_escalations(status=status, limit=limit)


@app.get("/api/escalate/{eid}")
def get_escalation(eid: str) -> dict:
    row = tickets.get_escalation(eid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"заявка не найдена: {eid}")
    return row


@app.post("/api/escalate/{eid}/answer")
def answer_escalation(eid: str, req: AnswerRequest) -> dict:
    try:
        return tickets.answer_escalation(eid, req.answer, req.resolved_by)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"заявка не найдена: {eid}") from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@app.get("/api/escalate/{eid}/draft")
def escalation_draft(eid: str) -> dict:
    row = tickets.get_escalation(eid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"заявка не найдена: {eid}")
    return {"ticket": eid, "status": row.get("status"),
            "quality_answer": row.get("answer", ""),
            "resolved_by": row.get("resolved_by"),
            "answered_at": row.get("answered_at"),
            "draft": {"question": row.get("question", ""),
                      "answer_in_practice": row.get("answer", ""),
                      "outcome": "resolved" if row.get("answer") else "open",
                      "business_line": row.get("business_line", ""),
                      "linked_docs": [], "tags": [],
                      "rule_key": None, "author_role": "quality",
                      "author_name": "служба качества", "content_type": "text",
                      "source": "escalation", "escalation_id": eid},
            "suggestions": {"missing": [], "conflicts": []}}


@app.post("/api/escalate/{eid}/confirm")
def confirm_escalation(eid: str, req: ConfirmRequest) -> dict:
    row = tickets.get_escalation(eid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"заявка не найдена: {eid}")
    try:
        case = dict(req.case)
        case.setdefault("source", "escalation")
        case.setdefault("escalation_id", eid)
        return core.add_case(case) | {"ticket_status": "resolved"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from None


# ---------- предложения об изменении (§19) ----------

@app.get("/api/change-requests")
def get_change_requests(status: str | None = None,
                        limit: int = Query(100, ge=1, le=1000)) -> list[dict]:
    return changes.list_requests(status=status, limit=limit)


@app.get("/api/change-requests/{crid}")
def get_change_request(crid: str) -> dict:
    row = changes.get_request(crid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"предложение не найдено: {crid}")
    return row


@app.post("/api/change-requests/{crid}/status")
def set_change_status(crid: str, req: ChangeStatusRequest) -> dict:
    try:
        return changes.set_status(crid, req.status, req.by)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"предложение не найдено: {crid}") from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


# ---------- статика ----------

@app.get("/")
def index():
    idx = config.STATIC_DIR / "index.html"
    if idx.exists():
        return FileResponse(idx)
    return JSONResponse(
        status_code=503,
        content={"detail": "UI не собран: нет src/api/static/index.html",
                 "hint": "скопируйте собранный фронтенд в src/api/static/ (зона NEO); "
                         "API при этом полностью работает — см. /api/docs"},
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
    idx = config.STATIC_DIR / "index.html"
    if idx.exists():
        return FileResponse(idx)
    raise HTTPException(status_code=404, detail="not found")


@app.on_event("startup")
def _startup() -> None:
    config.ensure_dirs()
