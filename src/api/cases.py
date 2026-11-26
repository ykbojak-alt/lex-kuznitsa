"""Кейсы (§8) — тонкая обёртка над хранилищем ядра.

Логика хранения и поиска — в ``lex.cases.CaseStore``. Здесь только проверки
входа и форма ответа контракта (§8): ``{"id","ok","in_index"}``.
"""
from __future__ import annotations

from . import core

CONTENT_TYPES = {"text", "image", "scan", "mixed"}
OUTCOMES = {"resolved", "escalated", "rejected", "open"}


def add(payload: dict) -> dict:
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

    for doc_id in payload.get("linked_docs") or []:
        if core.doc_meta(str(doc_id)) is None:
            raise ValueError(f"документа {doc_id} нет в корпусе")

    payload = dict(payload, outcome=outcome, content_type=ctype)
    return core.add_case(payload)


def list_cases(limit: int = 50, role_id: str | None = None,
               tag: str | None = None, rule_key: str | None = None) -> dict:
    return core.list_cases(limit=limit, role_id=role_id, tag=tag, rule_key=rule_key)


def count() -> int:
    return core.cases_count()
