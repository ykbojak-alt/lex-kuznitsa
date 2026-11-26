"""Экран аналитики для жюри: три критерия + таймлайн.

Только реальные файлы ``var/*.jsonl``. Никаких заглушек: если в журнале нет
поля/события, счётчик честно равен нулю, а пустой журнал даёт ``empty=true``.

Эндпоинт ``GET /api/stats`` подключается в ``api/app.py``.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter

ROOT = Path(__file__).resolve().parents[2]
VAR = Path(os.environ.get("LEX_VAR_DIR") or (ROOT / "var"))
JOURNAL = Path(os.environ.get("LEX_JOURNAL_PATH") or (VAR / "journal.jsonl"))
SESSIONS = VAR / "sessions.jsonl"
ESCALATIONS = VAR / "escalations.jsonl"
CASES = VAR / "cases.jsonl"
TICKETS = VAR / "tickets.jsonl"

SUBJECT_COVERAGE_MIN = 0.75
TOP_COSINE_MIN = 0.50

router = APIRouter()


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def outcomes(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter()
    for r in rows:
        keys = {str(s.get("key") or "") for s in (r.get("steps") or []) if isinstance(s, dict)}
        if r.get("escalated") or "escalate" in keys:
            counts["escalate"] += 1
        elif r.get("clarified") or "clarify" in keys:
            counts["clarify"] += 1
        elif r.get("refused"):
            counts["refusal"] += 1
        else:
            counts["answer"] += 1
    total = len(rows)
    return {
        "total": total,
        "counts": {k: counts.get(k, 0) for k in ("answer", "clarify", "escalate", "refusal")},
        "shares": {k: round(counts.get(k, 0) / total, 3) if total else 0.0
                   for k in ("answer", "clarify", "escalate", "refusal")},
    }


def escalation_reasons(escalations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for e in escalations:
        why = re.sub(r"\s+", " ", str(e.get("why_unresolved") or e.get("kind") or "не указано")).strip()
        kind = str(e.get("kind") or "")
        label = (kind + ": " if kind else "") + why[:90]
        counter[label] += 1
    return [{"reason": k, "count": v} for k, v in counter.most_common(5)]


def relevance_gate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Счётчик остановок гейтом релевантности — из retrieve-шагов.

    Если журнал ещё не пишет нужные поля — ``stops=None`` (не измерялось),
    а не 0: ноль означает «измеряли, остановок не было», это разные вещи.
    """
    stops = 0
    checked = 0
    measured = 0
    for r in rows:
        extra = r.get("retrieval") if isinstance(r.get("retrieval"), dict) else {}
        for s in (r.get("steps") or []):
            if not isinstance(s, dict) or str(s.get("key") or "") != "retrieve":
                continue
            checked += 1
            cov = s.get("subject_coverage", extra.get("subject_coverage"))
            cos = s.get("top_cosine", extra.get("top_cosine"))
            try:
                cov = float(cov) if cov is not None else None
                cos = float(cos) if cos is not None else None
            except (TypeError, ValueError):
                cov = cos = None
            if cov is not None or cos is not None:
                measured += 1
            if (cov is not None and cov < SUBJECT_COVERAGE_MIN) or (cos is not None and cos < TOP_COSINE_MIN):
                stops += 1
    return {
        "stops": stops if measured else None,
        "measured": bool(measured),
        "steps_with_metrics": measured,
        "retrieve_steps": checked,
        "subject_coverage_min": SUBJECT_COVERAGE_MIN,
        "top_cosine_min": TOP_COSINE_MIN,
        "note": "Считается по полям retrieve-шагов; без полей — «пока не измерялось» (null), а не 0.",
    }


def clarification_stats(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    dialogues = 0
    rounds: list[int] = []
    params: Counter[str] = Counter()
    for s in sessions:
        st = s.get("state") if isinstance(s.get("state"), dict) else {}
        history = st.get("history") or []
        clarifies = [h for h in history if isinstance(h, dict) and h.get("kind") == "clarify"]
        if clarifies:
            dialogues += 1
            for h in clarifies:
                for q in (h.get("questions") or []):
                    if isinstance(q, dict) and q.get("key"):
                        params[str(q["key"])] += 1
        r = st.get("rounds")
        if isinstance(r, (int, float)):
            rounds.append(int(r))
        elif clarifies:
            rounds.append(len(clarifies))
    return {
        "dialogues_with_clarify": dialogues,
        "avg_rounds": round(sum(rounds) / len(rounds), 2) if rounds else 0.0,
        "top_params": [{"key": k, "count": v} for k, v in params.most_common(10)],
    }


def case_stats(rows: list[dict[str, Any]], escalations: list[dict[str, Any]],
               cases: list[dict[str, Any]]) -> dict[str, Any]:
    esc_by_id = {str(e.get("id")): e for e in escalations}
    from_tickets = [c for c in cases if c.get("escalation_id")]
    hours: list[float] = []
    for c in from_tickets:
        t0 = _ts(esc_by_id.get(str(c.get("escalation_id")), {}).get("ts"))
        t1 = _ts(c.get("confirmed_at") or c.get("ts"))
        if t0 and t1:
            hours.append((t1 - t0).total_seconds() / 3600)

    answered_from_cases = 0
    cases_measured = 0
    for r in rows:
        if "cases_used" in r or "case_ids" in r:
            cases_measured += 1
        has_case = bool(r.get("cases_used") or r.get("case_ids"))
        no_docs = not (r.get("doc_ids") or [])
        if has_case and no_docs:
            answered_from_cases += 1

    # Цикл «заявка -> ответ СК -> кейс» по номеру тикета: у кейса из заявки
    # есть ticket_id и approved_at, у заявки — created_at. Пары без любого из
    # трёх полей не считаем: среднее по половине данных хуже честного нуля.
    tickets_by_id = {str(t.get("id")): t for t in _read(TICKETS)}
    approved = [c for c in cases if str(c.get("outcome") or "") != "rejected"]
    ticket_cases = [c for c in approved if c.get("ticket_id")]
    ticket_hours: list[float] = []
    for c in ticket_cases:
        t0 = _ts(tickets_by_id.get(str(c.get("ticket_id")), {}).get("created_at"))
        t1 = _ts(c.get("approved_at"))
        if t0 and t1 and t1 >= t0:
            ticket_hours.append((t1 - t0).total_seconds() / 3600)

    return {
        "approved_cases_count": len(approved),
        "cases_from_tickets": len(from_tickets),
        "avg_ticket_to_case_hours": round(sum(ticket_hours) / len(ticket_hours), 2)
        if ticket_hours else None,
        "ticket_to_case_measured": bool(ticket_hours),
        "cases_total": len(cases),
        "avg_hours_ticket_to_case": round(sum(hours) / len(hours), 2) if hours else None,
        "answered_from_cases": answered_from_cases if cases_measured else None,
        "answered_from_cases_measured": bool(cases_measured),
        "note": "Источник — var/escalations.jsonl, var/cases.jsonl и поля cases_used/case_ids журнала.",
    }


def latency(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Скорость ответа: за всё время и скользящее окно последние 20 записей."""
    vals = [float(r["elapsed_ms"]) for r in rows
            if isinstance(r.get("elapsed_ms"), (int, float))]
    if not vals:
        return {"empty": True, "hint": "В журнале нет elapsed_ms — скорость пока не измерялась."}

    def p95(v: list[float]) -> int:
        ordered = sorted(v)
        return int(ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)])

    def summarise(v: list[float]) -> dict[str, Any]:
        return {
            "records": len(v),
            "avg_ms": round(sum(v) / len(v), 1),
            "p95_ms": p95(v),
        }

    recent = vals[-20:]
    return {
        "empty": False,
        "all_time": summarise(vals),
        "recent20": summarise(recent),
        "note": "elapsed_ms из journal.jsonl; recent20 — последние 20 запросов (скользящее окно).",
    }


def token_usage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Расход токенов из journal.jsonl. Меньше 5 записей — честное пустое состояние."""
    measured = []
    for r in rows:
        t = r.get("tokens")
        if isinstance(t, dict) and any(k in t for k in ("prompt", "completion", "total")):
            measured.append(t)
    if len(measured) < 5:
        return {
            "empty": True,
            "records": len(measured),
            "hint": "Нужно минимум 5 запросов с данными о токенах; сейчас "
                    + str(len(measured)) + ". Пустое состояние, а не нули.",
        }
    prompts = [int(t.get("prompt") or 0) for t in measured]
    completions = [int(t.get("completion") or 0) for t in measured]
    totals = [int(t.get("total") or 0) or (p + c) for t, p, c in zip(measured, prompts, completions, strict=True)]
    costs = []
    for t in measured:
        if t.get("cost") is not None:
            try:
                costs.append(float(t["cost"]))
            except (TypeError, ValueError):
                pass
    ordered = sorted(totals)
    p95 = ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]
    calls = [int(t["calls"]) for t in measured if t.get("calls") is not None]
    recent = sorted(totals[-20:])
    return {
        "empty": False,
        "records": len(measured),
        "prompt_tokens": sum(prompts),
        "completion_tokens": sum(completions),
        "total_tokens": sum(totals),
        "avg_total_per_request": round(sum(totals) / len(totals), 1),
        "p95_total_per_request": p95,
        "window20": {
            "records": len(recent),
            "avg_total_per_request": round(sum(recent) / len(recent), 1),
            "p95_total_per_request": recent[min(len(recent) - 1, math.ceil(0.95 * len(recent)) - 1)],
        },
        "llm_calls": sum(calls) if calls else None,
        "llm_calls_measured": bool(calls),
        "cost_estimate": round(sum(costs), 4) if costs else None,
        "cost_measured": bool(costs),
        "note": "Источник — поле tokens журнала; цена берётся только если usage её писал.",
    }


def timeline(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    days: Counter[str] = Counter()
    for r in rows:
        t = _ts(r.get("ts"))
        if t:
            days[t.date().isoformat()] += 1
    return [{"day": d, "count": days[d]} for d in sorted(days)]


def compute() -> dict[str, Any]:
    rows = _read(JOURNAL)
    sessions = _read(SESSIONS)
    escalations = _read(ESCALATIONS)
    cases = _read(CASES)
    return {
        "empty": not rows,
        "hint": "" if rows else "Журнал var/journal.jsonl пуст: задайте вопрос в чате, затем откройте аналитику.",
        "criterion1": {
            "outcomes": outcomes(rows),
            "top_escalation_reasons": escalation_reasons(escalations),
            "relevance_gate": relevance_gate(rows),
        },
        "criterion2": clarification_stats(sessions),
        "criterion3": case_stats(rows, escalations, cases),
        "token_usage": token_usage(rows),
        "latency": latency(rows),
        "timeline": timeline(rows),
        "last_request_ts": max((str(r.get("ts") or "") for r in rows), default="") or None,
    }


@router.get("/api/stats")
def get_stats() -> dict[str, Any]:
    return compute()


__all__ = [
    "case_stats",
    "clarification_stats",
    "compute",
    "escalation_reasons",
    "latency",
    "outcomes",
    "relevance_gate",
    "router",
    "timeline",
    "token_usage",
]
