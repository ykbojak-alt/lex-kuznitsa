"""Предложения об изменении регламента (§19.4).

Агент собирает кейсы, противоречащие действующему документу, считает частоту
и формулирует, что расходится. Он не правит регламент и не утверждает
изменение — только предлагает. Статус меняет человек.

Порог `CR_MIN_CASES` (по умолчанию 3): один кейс — наблюдение, три — сигнал.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
from pathlib import Path
from typing import Iterable

from . import config, core

_lock = threading.Lock()

STATUSES = ("proposed", "accepted", "rejected", "merged")


def _path() -> Path:
    config.ensure_dirs()
    p = config.CHANGE_REQUESTS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _read() -> list[dict]:
    p = _path()
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


def _write_all(rows: Iterable[dict]) -> None:
    p = _path()
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(p)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _next_id(rows: list[dict]) -> str:
    year = dt.datetime.now(dt.timezone.utc).year
    best = 0
    for r in rows:
        m = re.fullmatch(r"CR-(\d{4})-(\d+)", str(r.get("id", "")))
        if m and int(m.group(1)) == year:
            best = max(best, int(m.group(2)))
    return f"CR-{year}-{best + 1:04d}"


def list_requests(status: str | None = None, limit: int = 100) -> list[dict]:
    rows = _read()
    if status:
        rows = [r for r in rows if r.get("status") == status]
    return rows[::-1][:limit]


def get_request(crid: str) -> dict | None:
    return next((r for r in _read() if r.get("id") == crid), None)


def set_status(crid: str, status: str, by: str) -> dict:
    if status not in STATUSES:
        raise ValueError(f"status из {STATUSES}")
    if not str(by or "").strip():
        raise ValueError("by обязателен: должно быть видно, кто изменил статус")
    with _lock:
        rows = _read()
        row = next((r for r in rows if r.get("id") == crid), None)
        if row is None:
            raise KeyError(crid)
        row["status"] = status
        row["status_changed_by"] = by
        row["status_changed_at"] = _now()
        _write_all(rows)
    return row


def _current_norm(rule_key: str, doc_id: str) -> str:
    c = core.corpus()
    if c is None or not rule_key or not hasattr(c, "rules"):
        return ""
    try:
        rule = c.rules.get(rule_key)
    except Exception:
        return ""
    if rule is None or not getattr(rule, "revisions", None):
        return ""
    rev = rule.revisions[-1]
    if doc_id and getattr(rev, "doc_id", "") != doc_id:
        # действующая редакция может быть другим документом — берём по правилу
        pass
    return str(getattr(rev, "value", "") or "")


def propose_from_case(case: dict, case_id: str, conflicts: list[dict],
                      escalation_id: str = "") -> dict | None:
    """§19.4: предложение из подтверждённого кейса с расхождением.

    Создаётся только при наличии `rule_key`: без правила нечего менять.
    """
    rule_key = str(case.get("rule_key") or "")
    target = ""
    for c in conflicts:
        if c.get("with"):
            target = str(c["with"])
            break
    if not rule_key:
        return None

    with _lock:
        rows = _read()
        # Дедуп: открытое предложение по тому же правилу дополняем доказательством.
        existing = next((r for r in rows
                         if r.get("rule_key") == rule_key
                         and r.get("status") in ("proposed", "accepted")), None)
        if existing is not None:
            evidence = existing.setdefault("evidence", [])
            evidence.append({"case_id": case_id, "count": 1})
            if escalation_id:
                for e in evidence:
                    if "tickets" in e:
                        e["tickets"].append(escalation_id)
                        break
                else:
                    evidence.append({"tickets": [escalation_id]})
            existing["observed"] = str(case.get("answer_in_practice") or "")
            _write_all(rows)
            return existing

        row = {
            "id": _next_id(rows),
            "created_at": _now(),
            "created_by": "agent",
            "target_doc": target,
            "rule_key": rule_key,
            "observed": str(case.get("answer_in_practice") or ""),
            "current_norm": _current_norm(rule_key, target),
            "evidence": [{"case_id": case_id, "count": 1}],
            "proposal": "проверить раздел «Исключения»: практика расходится с нормой",
            "status": "proposed",
            "assignee": None,
        }
        if escalation_id:
            row["evidence"].append({"tickets": [escalation_id]})
        rows.append(row)
        _write_all(rows)
    return row


def analytics() -> dict:
    rows = _read()
    by_status: dict[str, int] = {}
    by_doc: dict[str, int] = {}
    for r in rows:
        st = str(r.get("status") or "proposed")
        by_status[st] = by_status.get(st, 0) + 1
        doc = str(r.get("target_doc") or "")
        if doc:
            by_doc[doc] = by_doc.get(doc, 0) + 1
    return {
        "total": len(rows),
        "by_status": [{"status": k, "count": v} for k, v in by_status.items()],
        "proposed": by_status.get("proposed", 0),
        "accepted": by_status.get("accepted", 0),
        "rejected": by_status.get("rejected", 0),
        "merged": by_status.get("merged", 0),
        "top_docs": [{"doc_id": k, "count": v}
                     for k, v in sorted(by_doc.items(), key=lambda kv: -kv[1])[:10]],
    }
