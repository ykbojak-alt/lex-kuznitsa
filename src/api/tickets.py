"""Эскалации (§15) и тикеты (§18), черновик кейса и подтверждение (§16).

Два разных ресурса, как и в контракте:

* ``/api/escalate`` — внутренняя ручка ядра: сырая заявка ``esc-NNNN`` с
  итерациями уточнений и «почему не решилось». Её создаёт оркестратор.
* ``/api/tickets`` — публичная очередь службы качества: человекочитаемый номер
  ``LEX-YYYY-NNNN``, назначение, SLA, ответ, закрытие. Тикет может ссылаться
  на эскалацию, из которой вырос, но живёт своей жизнью.

Логики разбора здесь нет — только хранение, переходы статусов и сборка
черновика из того, что уже есть в корпусе. В базу кейс попадает лишь после
подтверждения человеком (§16.1).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import config, core

_lock = threading.Lock()

ESC_STATUSES = ("open", "answered", "resolved")
TICKET_STATUSES = ("open", "assigned", "answered", "closed")
PRIORITIES = ("normal", "high")

# §18.1: тикет создаётся только по этим причинам. out_of_scope — никогда.
TICKET_KINDS = ("not_in_corpus", "conflict_unresolved", "ocr_degraded")


# ---------------------------------------------------------------- хранилище

def _path(kind: str) -> Path:
    config.ensure_dirs()
    p = config.ESCALATIONS_PATH if kind == "esc" else config.TICKETS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _read(kind: str) -> list[dict]:
    p = _path(kind)
    if not p.exists():
        return []
    rows: list[dict] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def _write_all(kind: str, rows: Iterable[dict]) -> None:
    p = _path(kind)
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(p)


def _append(kind: str, row: dict) -> None:
    p = _path(kind)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.UTC)
    except Exception:
        return None


# ------------------------------------------------------------------- номера

def _next_esc_id(rows: list[dict]) -> str:
    best = 0
    for r in rows:
        m = re.fullmatch(r"esc-(\d+)", str(r.get("id", "")))
        if m:
            best = max(best, int(m.group(1)))
    return f"esc-{best + 1:04d}"


def _next_ticket_id(rows: list[dict]) -> str:
    year = dt.datetime.now(dt.UTC).year
    best = 0
    for r in rows:
        m = re.fullmatch(r"LEX-(\d{4})-(\d+)", str(r.get("id", "")))
        if m and int(m.group(1)) == year:
            best = max(best, int(m.group(2)))
    return f"LEX-{year}-{best + 1:04d}"


# ---------------------------------------------------------------- эскалации

def create_escalation(payload: dict) -> dict:
    """§15.2. Обязательны: вопрос, что искали, почему не решилось."""
    question = str(payload.get("question") or "").strip()
    why = str(payload.get("why_unresolved") or "").strip()
    if not question:
        raise ValueError("question обязателен")
    if not why:
        raise ValueError("why_unresolved обязателен: заявка без анализа бесполезна")

    kind = str(payload.get("kind") or "not_in_corpus")
    if kind not in ("not_in_corpus", "conflict_unresolved", "ocr_degraded",
                    "no_edition_in_force", "out_of_scope"):
        raise ValueError(f"неизвестный kind: {kind}")

    with _lock:
        rows = _read("esc")
        eid = _next_esc_id(rows)
        row = {
            "id": eid,
            "ts": _now(),
            "session_id": str(payload.get("session_id") or ""),
            "role_id": str(payload.get("role_id") or ""),
            "business_line": str(payload.get("business_line") or ""),
            "question": question,
            "clarifications": list(payload.get("clarifications") or []),
            "searched": payload.get("searched") or {},
            "why_unresolved": why,
            "kind": kind,
            "status": "open",
        }
        _append("esc", row)
    return row


def list_escalations(status: str | None = None, limit: int = 100) -> list[dict]:
    rows = _read("esc")
    if status:
        rows = [r for r in rows if r.get("status") == status]
    return rows[::-1][:limit]


def get_escalation(eid: str) -> dict | None:
    return next((r for r in _read("esc") if r.get("id") == eid), None)


def answer_escalation(eid: str, answer: str, resolved_by: str = "quality") -> dict:
    """§15.4: служба качества ответила на заявку."""
    if not str(answer or "").strip():
        raise ValueError("answer обязателен")
    with _lock:
        rows = _read("esc")
        row = next((r for r in rows if r.get("id") == eid), None)
        if row is None:
            raise KeyError(eid)
        row["answer"] = str(answer).strip()
        row["resolved_by"] = resolved_by
        row["answered_at"] = _now()
        row["status"] = "answered"
        _write_all("esc", rows)
    return row


# ------------------------------------------------------------------- тикеты

def _sla_due(priority: str, created: dt.datetime) -> str:
    hours = config.SLA_HOURS.get(priority, config.SLA_HOURS["normal"])
    return (created + dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_ticket(payload: dict) -> dict:
    """§18.2. Дедуп по session_id: второй тикет не создаём, возвращаем первый."""
    question = str(payload.get("question") or "").strip()
    if not question:
        raise ValueError("question обязателен")
    kind = str(payload.get("kind") or "not_in_corpus")
    if kind not in TICKET_KINDS:
        # §18.7: out_of_scope и системные ошибки тикетов не создают
        raise ValueError(f"тикет не создаётся для kind={kind}: "
                         f"допустимы {', '.join(TICKET_KINDS)}")
    priority = str(payload.get("priority") or "normal")
    if priority not in PRIORITIES:
        raise ValueError(f"priority из {PRIORITIES}")

    session_id = str(payload.get("session_id") or "")
    with _lock:
        rows = _read("tk")
        if session_id:
            existing = next((r for r in rows if r.get("session_id") == session_id
                             and r.get("status") != "closed"), None)
            if existing is not None:
                return existing
        created = dt.datetime.now(dt.UTC)
        row = {
            "id": _next_ticket_id(rows),
            "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "created_by": "agent",
            "session_id": session_id,
            "question": question,
            "asker": payload.get("asker") or {"role_id": str(payload.get("role_id") or ""),
                                              "business_line": str(payload.get("business_line") or "")},
            "clarifications": list(payload.get("clarifications") or []),
            "searched": payload.get("searched") or {},
            "why_unresolved": str(payload.get("why_unresolved") or ""),
            "kind": kind,
            "priority": priority,
            "assignee": None,
            "status": "open",
            "sla_due": _sla_due(priority, created),
            "escalation_id": str(payload.get("escalation_id") or ""),
        }
        _append("tk", row)
    return row


def list_tickets(status: str | None = None, business_line: str | None = None,
                 assignee: str | None = None, limit: int = 100,
                 role_id: str | None = None) -> list[dict]:
    rows = _read("tk")
    if status:
        rows = [r for r in rows if r.get("status") == status]
    if business_line:
        rows = [r for r in rows
                if (r.get("asker") or {}).get("business_line") == business_line]
    if assignee:
        rows = [r for r in rows if r.get("assignee") == assignee]
    if role_id:
        # §18.3: сотрудник видит тикеты своих направлений (та же логика, что у документов)
        lines = _role_lines(role_id)
        if "*" not in lines:
            rows = [r for r in rows
                    if (r.get("asker") or {}).get("business_line") in lines
                    or (r.get("session_id") and r.get("asker", {}).get("role_id") == role_id)]
    return rows[::-1][:limit]


def _role_lines(role_id: str) -> list[str]:
    for r in core.roles():
        if r.get("id") == role_id:
            lines = list(r.get("business_lines") or [])
            if "*" in list(r.get("departments") or []):
                return ["*"]
            return lines or ["*"]
    return ["*"]


def get_ticket(tid: str) -> dict | None:
    return next((r for r in _read("tk") if r.get("id") == tid), None)


def assign_ticket(tid: str, assignee: str, force: bool = False) -> dict:
    """§18.3: одновременно работает один человек; взять чужой можно с force."""
    assignee = str(assignee or "").strip()
    if not assignee:
        raise ValueError("assignee обязателен")
    with _lock:
        rows = _read("tk")
        row = next((r for r in rows if r.get("id") == tid), None)
        if row is None:
            raise KeyError(tid)
        current = row.get("assignee")
        if current and current != assignee and not force:
            raise PermissionError(
                f"тикет уже в работе у {current}; передача требует force=true")
        row["assignee"] = assignee
        row["assigned_at"] = row.get("assigned_at") or _now()
        if row.get("status") == "open":
            row["status"] = "assigned"
        _write_all("tk", rows)
    return row


def answer_ticket(tid: str, answer: str, resolved_by: str = "quality") -> dict:
    if not str(answer or "").strip():
        raise ValueError("answer обязателен")
    with _lock:
        rows = _read("tk")
        row = next((r for r in rows if r.get("id") == tid), None)
        if row is None:
            raise KeyError(tid)
        row["answer"] = str(answer).strip()
        row["resolved_by"] = resolved_by
        row["answered_at"] = _now()
        row["status"] = "answered"
        _write_all("tk", rows)
    return row


def close_ticket(tid: str, reason: str = "") -> dict:
    with _lock:
        rows = _read("tk")
        row = next((r for r in rows if r.get("id") == tid), None)
        if row is None:
            raise KeyError(tid)
        row["status"] = "closed"
        row["closed_at"] = _now()
        if reason:
            row["close_reason"] = str(reason)
        _write_all("tk", rows)
    return row


def _patch_ticket(tid: str, **fields: Any) -> dict:
    with _lock:
        rows = _read("tk")
        row = next((r for r in rows if r.get("id") == tid), None)
        if row is None:
            raise KeyError(tid)
        row.update(fields)
        _write_all("tk", rows)
    return row


# --------------------------------------------------- черновик кейса (§16.2)

def _doc_exists(doc_id: str) -> bool:
    return core.doc_meta(doc_id) is not None


def _rule_keys() -> set[str]:
    c = core.corpus()
    if c is not None and hasattr(c, "rules"):
        try:
            return set(c.rules.keys())
        except Exception:
            pass
    return set()


def _suggestions(draft: dict) -> dict:
    """Чего не хватает и с чем расходится — по данным корпуса, не по догадке."""
    missing: list[dict] = []
    conflicts: list[dict] = []

    for doc_id in draft.get("linked_docs") or []:
        if not _doc_exists(doc_id):
            missing.append({
                "field": "linked_docs",
                "why": f"документа {doc_id} нет в корпусе",
                "note": "можно оставить без ссылки — кейс будет без привязки к регламенту",
            })

    rule_key = str(draft.get("rule_key") or "")
    known = _rule_keys()
    if rule_key and rule_key not in known:
        missing.append({
            "field": "rule_key",
            "why": f"правила {rule_key} нет в корпусе",
            "note": "уберите или укажите существующее правило",
        })

    # §19.2: расхождение с действующим документом по тому же правилу.
    c = core.corpus()
    if c is not None and rule_key and hasattr(c, "rules"):
        rule = None
        try:
            rule = c.rules.get(rule_key)
        except Exception:
            rule = None
        if rule is not None and getattr(rule, "revisions", None):
            rev = rule.revisions[-1]
            conflicts.append({
                "with": getattr(rev, "doc_id", ""),
                "why": (f"ответ относится к правилу {rule_key}: в регламенте "
                        f"действует «{getattr(rev, 'value', '')}»"),
                "severity": "warning",
            })

    if not draft.get("business_line"):
        missing.append({
            "field": "business_line",
            "why": "направление не определено",
            "note": "можно оставить пустым — кейс попадёт в общую очередь",
        })

    return {"missing": missing, "conflicts": conflicts}


def ticket_draft(tid: str) -> dict | None:
    """§16.2: черновик кейса + пометки. Ничего не пишет в базу."""
    row = get_ticket(tid)
    if row is None:
        return None
    answer = str(row.get("answer") or "").strip()
    asker = row.get("asker") or {}
    draft = {
        "question": row.get("question", ""),
        "answer_in_practice": answer,
        "outcome": "resolved" if answer else "open",
        "business_line": asker.get("business_line", ""),
        "linked_docs": [],
        "tags": _tags_from(row),
        "rule_key": None,
        "author_role": "quality",
        "author_name": "служба качества",
        "content_type": "text",
        "source": "escalation",
        "escalation_id": row.get("escalation_id") or "",
        "ticket": tid,
    }
    suggestions = _suggestions(draft)
    # rule_key, упомянутый в ответе и существующий в корпусе, подставляем сразу.
    known = _rule_keys()
    for key in sorted(known):
        if key.lower() in answer.lower():
            draft["rule_key"] = key
            break
    return {
        "ticket": tid,
        "status": row.get("status"),
        "quality_answer": answer,
        "resolved_by": row.get("resolved_by"),
        "answered_at": row.get("answered_at"),
        "draft": draft,
        "suggestions": suggestions,
    }


def _tags_from(row: dict) -> list[str]:
    tags: list[str] = []
    line = (row.get("asker") or {}).get("business_line")
    if line:
        tags.append(line)
    text = f"{row.get('question', '')} {row.get('answer', '')}".lower()
    for needle, tag in (("44-фз", "44-ФЗ"), ("единственн", "единственный поставщик"),
                        ("рекламац", "рекламация"), ("закуп", "закупки"),
                        ("лиценз", "лицензирование")):
        if needle in text and tag not in tags:
            tags.append(tag)
    return tags


def confirm_ticket(tid: str, payload: dict) -> dict:
    """§16.3: подтверждение человеком. Только здесь кейс попадает в базу."""
    editor_role = str(payload.get("editor_role") or "").strip()
    if not editor_role:
        raise ValueError("editor_role обязателен: должно быть видно, кто подтвердил")

    row = get_ticket(tid)
    if row is None:
        raise KeyError(tid)
    case = dict(payload.get("case") or {})
    if not case.get("question") or not case.get("answer_in_practice"):
        raise ValueError("в кейсе обязательны question и answer_in_practice")

    # ссылки на несуществующие документы — 400, не молчание
    bad = [d for d in (case.get("linked_docs") or []) if not _doc_exists(str(d))]
    if bad:
        raise ValueError(f"документов нет в корпусе: {', '.join(bad)}")

    suggestions = _suggestions(case)
    errors = [c for c in suggestions["conflicts"] if c.get("severity") == "error"]
    warnings = [c for c in suggestions["conflicts"] if c.get("severity") == "warning"]
    if errors:
        raise ValueError("кейс противоречит регламенту: "
                         + "; ".join(f"{c['with']}: {c['why']}" for c in errors))
    if warnings and not payload.get("acknowledge_warnings"):
        raise ValueError("есть предупреждения о расхождении с регламентом; "
                         "нужен acknowledge_warnings=true")

    case["author_role"] = editor_role
    case.setdefault("author_name", row.get("resolved_by") or "служба качества")
    case["source"] = "escalation"
    case["escalation_id"] = row.get("escalation_id") or ""
    case["ticket"] = tid

    added = core.add_case(case)
    _patch_ticket(tid, status="closed", closed_at=_now(), case_id=added["id"])

    change = None
    if case.get("rule_key") and warnings:
        from . import changes
        change = changes.propose_from_case(case, added["id"], warnings,
                                           escalation_id=row.get("escalation_id"))

    return {
        "ok": True,
        "case_id": added["id"],
        "in_index": bool(added.get("in_index")),
        "ticket_status": "closed",
        "change_request": change,
        "affected": f"кейсов в базе: {core.cases_count()} · вопрос теперь покрыт",
    }
