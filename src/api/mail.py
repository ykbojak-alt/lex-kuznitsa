"""§22: обращение в службу качества как письмо на почту.

Два режима, переключаются ``EMAIL_MODE``:

* ``simulated`` — письмо только формируется, никуда не уходит. Режим по
  умолчанию для локальной разработки: на публичном стенде риск улетевших
  писем на реальные адреса перевешивает пользу.
* ``smtp`` — письмо реально отправляется по SMTP. Для демо-стенда это
  синтетический ящик отдела качества (Mailpit): он принимает почту по
  SMTP и показывает её в веб-интерфейсе, то есть письмо действительно
  доставляется, а не рисуется в UI.

Заказчик (TASK_STATEMENT §5 S3) требует именно реальной отправки: «отдельный
кабинет службы качества или локальная заявка без письма не заменяют этот
сценарий». Поэтому на стенде включён ``smtp``.

Отправка не имеет права ломать ответ пользователю: сбой SMTP логируется, но
не превращает эскалацию в ошибку — карточка письма всё равно показывается,
а ``delivery`` честно говорит, ушло письмо или нет.

Хранение тикетов живёт в ``tickets``; этот модуль — представление и отправка.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import smtplib
from email.message import EmailMessage

from . import config, tickets

log = logging.getLogger(__name__)

# §22.3: три состояния входящих. Внутренние статусы тикета их шире.
INBOX_STATUSES = ("new", "working", "answered")

# Тикет -> ящик. closed остаётся в «отвеченных»: письмо никуда не исчезает.
_INBOX_OF = {"open": "new", "assigned": "working",
             "answered": "answered", "closed": "answered"}


def smtp_configured() -> bool:
    """Готов ли настоящий SMTP. Без хоста отправлять некуда."""
    return bool(getattr(config, "SMTP_HOST", "") and config.EMAIL_MODE == "smtp")


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _subject(question: str) -> str:
    """Тема письма — вопрос одной строкой, без переносов."""
    text = re.sub(r"\s+", " ", str(question or "")).strip()
    if len(text) > 90:
        text = text[:87].rstrip(" ,;:") + "…"
    return f"[LEX] Вопрос без ответа в базе: {text}"


def send_letter(ticket: dict, to: str, sender: str, cc: str | None = None,
                body: str | None = None) -> str:
    """Отправить письмо по SMTP. Возвращает фактический режим доставки.

    ``sent`` — письмо принято SMTP-сервером; ``simulated`` — режим без почты;
    ``failed`` — попытка была, но сервер отказал (причина в логе). Возвращаем
    не bool, а строку: карточка в UI должна говорить правду, а не «отправлено»
    на любой исход.
    """
    if not smtp_configured():
        return "simulated"
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = _subject(ticket.get("question", ""))
    msg.set_content(body or letter_body(ticket))
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as srv:
            if getattr(config, "SMTP_USER", ""):
                srv.starttls()
                srv.login(config.SMTP_USER, config.SMTP_PASSWORD)
            srv.send_message(msg)
        return "sent"
    except Exception as exc:                     # почта не должна ронять ответ
        log.warning("письмо в качество не отправлено (%s:%s): %s",
                    config.SMTP_HOST, config.SMTP_PORT, exc)
        return "failed"


def letter_body(ticket: dict) -> str:
    """Тело письма: вопрос, уточнения и что уже проверено."""
    asker = ticket.get("asker") or {}
    body_lines = [str(ticket.get("question") or "").strip()]
    if asker.get("role_id"):
        body_lines.insert(0, f"От: {config.employee_email(asker['role_id'])}")
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
    return "\n".join(body_lines).strip()


def email_block(ticket: dict, delivery: str = "simulated") -> dict:
    """§22.2: блок письма для карточки. `delivery` — фактический исход."""
    asker = ticket.get("asker") or {}
    sent_at = ticket.get("created_at") or _now()
    sender = config.employee_email(str(asker.get("role_id") or ""))
    return {
        "to": config.QUALITY_EMAIL,
        "from": sender,
        "cc": sender,
        "subject": _subject(ticket.get("question", "")),
        "sent_at": sent_at,
        "delivery": delivery,
    }


def decorate(ticket: dict, delivery: str = "simulated") -> dict:
    """Тикет + поля письма. Используется и в /api/tickets, и в SSE escalate."""
    out = dict(ticket)
    out["channel"] = "email"
    out["email"] = email_block(ticket, delivery)
    return out


def deliver(ticket: dict) -> dict:
    """Отправить письмо и вернуть блок письма с честным ``delivery``.

    Вызывается при создании заявки: сотрудник ставится в копию, чтобы он
    видел переписку (§5 S3 п.3).
    """
    asker = ticket.get("asker") or {}
    sender = config.employee_email(str(asker.get("role_id") or ""))
    mode = send_letter(ticket, config.QUALITY_EMAIL, sender, cc=sender)
    return email_block(ticket, mode)


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

    Ответ службы качества сразу становится кейсом (§16.3); черновик здесь —
    для проверки человеком, а не для ручной записи в базу.
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
        "next": "черновик кейса готов; кейс уже в базе (дообучение)",
    }


def stats() -> dict:
    rows = tickets.list_tickets(limit=100_000)
    counts = {s: 0 for s in INBOX_STATUSES}
    for row in rows:
        counts[inbox_status(row)] += 1
    return {"mode": config.EMAIL_MODE, "quality_email": config.QUALITY_EMAIL,
            # Адрес веб-ящика для встроенной вкладки. Пусто, когда почта
            # симулирована: встраивать нечего, и клиент честно скажет об этом.
            "mail_ui": config.MAIL_UI_URL if smtp_configured() else "",
            **counts, "total": len(rows)}
