"""§22: обращение в службу качества как письмо на почту.

В демо письмо не отправляется никуда — оно имитируется. Поэтому у каждого
тикета есть блок ``email`` с ``delivery: "simulated"``: UI показывает карточку
«письмо отправлено», а на деле письмо лежит в очереди службы качества.

Настоящий SMTP в спринте не подключается: демо публичное, риск улетевших
писем на реальные адреса перевешивает пользу. Когда почту подключат
(§22.5), меняется только ``EMAIL_MODE`` — форма ответа остаётся прежней.

Этот модуль — только представление. Хранение тикетов живёт в ``tickets``.
"""
from __future__ import annotations

import datetime as dt
import re

from . import config, tickets

# §22.3: три состояния входящих. Внутренние статусы тикета их шире.
INBOX_STATUSES = ("new", "working", "answered")

# Тикет -> ящик. closed остаётся в «отвеченных»: письмо никуда не исчезает.
_INBOX_OF = {"open": "new", "assigned": "working",
             "answered": "answered", "closed": "answered"}


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _subject(question: str) -> str:
    """Тема письма — вопрос одной строкой, без переносов."""
    text = re.sub(r"\s+", " ", str(question or "")).strip()
    if len(text) > 90:
        text = text[:87].rstrip(" ,;:") + "…"
    return f"[LEX] Вопрос без ответа в базе: {text}"


def email_block(ticket: dict) -> dict:
    """§22.2: блок письма для карточки. `delivery` всегда simulated."""
    asker = ticket.get("asker") or {}
    sent_at = ticket.get("created_at") or _now()
    return {
        "to": config.QUALITY_EMAIL,
        "from": config.employee_email(str(asker.get("role_id") or "")),
        "subject": _subject(ticket.get("question", "")),
        "sent_at": sent_at,
        "delivery": "simulated",
    }


def decorate(ticket: dict) -> dict:
    """Тикет + поля письма. Используется и в /api/tickets, и в SSE escalate."""
    out = dict(ticket)
    out["channel"] = "email"
    out["email"] = email_block(ticket)
    return out


def inbox_status(ticket: dict) -> str:
    return _INBOX_OF.get(str(ticket.get("status") or "open"), "new")


def letter(ticket: dict) -> dict:
    """Открытое письмо: тело — вопрос и то, что агент уже проверил."""
    asker = ticket.get("asker") or {}
    body_lines = [str(ticket.get("question") or "").strip()]
    if ticket.get("clarifications"):
        body_lines.append("")
        body_lines.append("Уточнения в диалоге:")
        for item in ticket["clarifications"]:
            if isinstance(item, dict):
                q = str(item.get("question") or item.get("q") or "").strip()
                a = str(item.get("answer") or item.get("a") or "").strip()
                if q or a:
                    body_lines.append(f"— {q} → {a}" if q else f"— {a}")
    if ticket.get("why_unresolved"):
        body_lines.append("")
        body_lines.append(f"Почему не решено: {ticket['why_unresolved']}")
    searched = ticket.get("searched") or {}
    if searched:
        body_lines.append("")
        body_lines.append("Что искали: " + ", ".join(
            f"{k}: {v}" for k, v in searched.items() if v not in (None, "", [], {})))

    out = decorate(ticket)
    out.update({
        "inbox_status": inbox_status(ticket),
        "body": "\n".join(body_lines).strip(),
        "kind": ticket.get("kind"),
        "priority": ticket.get("priority"),
        "sla_due": ticket.get("sla_due"),
        "assignee": ticket.get("assignee"),
        "reply": ticket.get("answer") or "",
        "replied_at": ticket.get("answered_at"),
        "replied_by": ticket.get("resolved_by"),
        "asker": asker,
    })
    return out


def list_inbox(status: str | None = None, limit: int = 100,
               role_id: str | None = None) -> list[dict]:
    """§22.3: входящие письма. Обёртка над тикетами, своё хранилище не заводим."""
    if status and status not in INBOX_STATUSES:
        raise ValueError(f"status из {INBOX_STATUSES}")
    rows = tickets.list_tickets(limit=100_000, role_id=role_id)
    letters = []
    for row in rows:
        if status and inbox_status(row) != status:
            continue
        item = decorate(row)
        item["inbox_status"] = inbox_status(row)
        item["unread"] = not bool(row.get("answer"))
        letters.append(item)
    letters.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return letters[:limit]


def open_letter(tid: str) -> dict | None:
    row = tickets.get_ticket(tid)
    return letter(row) if row is not None else None


def reply(tid: str, body: str, resolved_by: str = "quality") -> dict:
    """§22.3: ответ на письмо = ответ по тикету + готовый черновик кейса.

    Кейс в базу не попадает: его подтверждает человек (§16.3).
    """
    if not str(body or "").strip():
        raise ValueError("body обязателен")
    tickets.answer_ticket(tid, body, resolved_by)
    draft = tickets.ticket_draft(tid)
    if draft is None:
        raise KeyError(tid)
    return {
        "ticket": tid,
        "status": "answered",
        "email": email_block(tickets.get_ticket(tid) or {}),
        "delivery": "simulated",
        "draft": draft.get("draft"),
        "suggestions": draft.get("suggestions"),
        "next": "черновик кейса готов; в базу попадёт после confirm",
    }


def stats() -> dict:
    rows = tickets.list_tickets(limit=100_000)
    counts = {s: 0 for s in INBOX_STATUSES}
    for row in rows:
        counts[inbox_status(row)] += 1
    return {"mode": config.EMAIL_MODE, "quality_email": config.QUALITY_EMAIL,
            **counts, "total": len(rows)}
