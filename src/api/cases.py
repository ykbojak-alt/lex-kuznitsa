"""Кейсы (контракт §8) — пополняемая база практики, не документы корпуса.

HTTP-слой отвечает за приём/список и хранение; поиск по кейсам — в ядре
(``lex.cases``, когда появится). До этого кейсы всё равно сохраняются и
видны в /api/cases, а счётчик для шага `cases` считается по файлу.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from . import config

CONTENT_TYPES = {"text", "image", "scan", "mixed"}
OUTCOMES = {"resolved", "escalated", "rejected", "open"}

_lock = threading.Lock()


def _path() -> Path:
    p = Path(os.environ.get("LEX_CASES_PATH") or (config.VAR_DIR / "cases.jsonl"))
    config.ensure_dirs()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _read_all() -> list[dict]:
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


def _next_id(rows: list[dict]) -> str:
    best = 0
    for r in rows:
        m = re.fullmatch(r"case-(\d+)", str(r.get("id", "")))
        if m:
            best = max(best, int(m.group(1)))
    return f"case-{best + 1:04d}"


def add(payload: dict) -> dict:
    """Добавить кейс. Возвращает {"id","ok","in_index"} по §8."""
    question = str(payload.get("question") or "").strip()
    answer = str(payload.get("answer_in_practice") or "").strip()
    if not question or not answer:
        raise ValueError("question и answer_in_practice обязательны")

    outcome = str(payload.get("outcome") or "open")
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome должен быть одним из {sorted(OUTCOMES)}")
    ctype = str(payload.get("content_type") or "text")
    if ctype not in CONTENT_TYPES:
        raise ValueError(f"content_type должен быть одним из {sorted(CONTENT_TYPES)}")

    with _lock:
        rows = _read_all()
        cid = _next_id(rows)
        entry = {
            "id": cid,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "author_role": str(payload.get("author_role") or ""),
            "author_name": str(payload.get("author_name") or "—"),
            "question": question,
            "answer_in_practice": answer,
            "outcome": outcome,
            "linked_docs": [str(x) for x in (payload.get("linked_docs") or [])],
            "tags": [str(x) for x in (payload.get("tags") or [])],
            "rule_key": str(payload.get("rule_key") or ""),
            "content_type": ctype,
        }
        with _path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    in_index = False
    core_cases = _core_cases()
    if core_cases is not None and hasattr(core_cases, "add"):
        try:
            core_cases.add(entry)
            in_index = True
        except Exception:
            in_index = False
    return {"id": cid, "ok": True, "in_index": in_index}


def _core_cases():
    try:
        import importlib
        return importlib.import_module("lex.cases")
    except Exception:
        return None


def list_cases(limit: int = 50, role_id: str | None = None,
               tag: str | None = None, rule_key: str | None = None) -> list[dict]:
    core_cases = _core_cases()
    if core_cases is not None and hasattr(core_cases, "list"):
        try:
            rows = core_cases.list(limit=limit, role_id=role_id, tag=tag, rule_key=rule_key)
            if isinstance(rows, list):
                return rows
        except Exception:
            pass
    rows = _read_all()
    if role_id:
        rows = [r for r in rows if r.get("author_role") == role_id]
    if tag:
        rows = [r for r in rows if tag in (r.get("tags") or [])]
    if rule_key:
        rows = [r for r in rows if r.get("rule_key") == rule_key]
    return rows[::-1][:limit]


def count() -> int:
    core_cases = _core_cases()
    if core_cases is not None and hasattr(core_cases, "count"):
        try:
            return int(core_cases.count())
        except Exception:
            pass
    return len(_read_all())
