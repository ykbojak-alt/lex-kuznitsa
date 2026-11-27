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

import base64
import datetime as dt
import logging
import re
import smtplib
from email.mime.text import MIMEText
from urllib.parse import quote

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


def encode_subject(text: str, max_word_bytes: int = 42) -> str:
    """Закодировать тему по RFC 2047 так, чтобы фолдинг не съел пробелы.

    Стандартный генератор Python 3.12 режет заголовок ровно по пробелу и
    получает два соседних encoded-word, а RFC 2047 §6.2 требует выбрасывать
    пробел между ними. В Mailpit тема приходила как «годовойпремии»: буквы
    на месте, а пробел на стыке исчез. Поэтому режем сами и пробел на границе
    кладём внутрь предыдущего слова — тогда он кодируется вместе с текстом и
    декодируется обратно.

    Чистый ASCII возвращается как есть: кодировать нечего.
    """
    if not text:
        return ""
    raw = text.encode("utf-8")
    if len(raw) <= max_word_bytes:
        return f"=?utf-8?b?{base64.b64encode(raw).decode('ascii')}?="

    chunks: list[bytes] = []
    start = 0
    while start < len(raw):
        end = min(start + max_word_bytes, len(raw))
        # Не разрывать многобайтовый символ: откатываемся к его началу.
        while start < end < len(raw) and (raw[end] & 0xC0) == 0x80:
            end -= 1
        # Пробел на границе переносим в текущее слово: иначе он окажется
        # разделителем двух encoded-word и будет отброшен при декодировании.
        while end < len(raw) and raw[end] == 0x20:
            end += 1
        chunks.append(raw[start:end])
        start = end

    return " ".join(f"=?utf-8?b?{base64.b64encode(c).decode('ascii')}?=" for c in chunks)


def _message(ticket: dict, to: str, sender: str, cc: str | None,
             body: str) -> MIMEText:
    """Собрать письмо так, чтобы тема доходила до получателя целой.

    ``EmailMessage`` с политикой ``SMTP`` при сериализации переписывает
    заголовок Subject и снова режет его по пробелу — уже закодированные слова
    склеиваются, и пробел на стыке теряется. ``MIMEText`` живёт на ``compat32``
    и пишет заголовок ровно так, как он задан, поэтому кодируем тему сами один
    раз и отдаём готовую строку.
    """
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = encode_subject(_subject(ticket.get("question", "")))
    return msg


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
    msg = _message(ticket, to, sender, cc, body or letter_body(ticket))
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
    """Тело письма: вопрос, уточнения, что проверено и ссылка на заявку.

    Ссылка на заявку — первым делом: сотрудник качества должен открыть её и
    сразу работать в ней (ответ там же становится черновиком кейса), а не
    искать тикет по номеру руками.
    """
    asker = ticket.get("asker") or {}
    body_lines: list[str] = []

    link = ticket_link(ticket)
    if link:
        body_lines.append(f"Заявка: {link}")
        body_lines.append("")

    if asker.get("role_id"):
        body_lines.append(f"От: {config.employee_email(asker['role_id'])}")
    body_lines.append(str(ticket.get("question") or "").strip())
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


def ticket_link(ticket: dict) -> str:
    """Прямая ссылка на заявку в веб-интерфейсе стенда.

    Ссылка собирается из ``STAND_URL``. Без него ссылки нет: на локальном
    стенде адрес неизвестен, и подставлять выдуманный нельзя — письмо должно
    вести туда, где заявка действительно открывается. Пробельная строка в
    ``STAND_URL`` считается пустой: иначе в письмо уходил бы ` «  /#ticket=»`.
    """
    base = getattr(config, "STAND_URL", "").strip().rstrip("/")
    tid = str(ticket.get("id") or ticket.get("ticket") or "").strip()
    if not base or not tid:
        return ""
    return f"{base}/#ticket={quote(tid)}"


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
        "ticket_url": ticket_link(ticket),
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
