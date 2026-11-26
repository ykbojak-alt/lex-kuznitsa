"""Формат SSE по §3 + §8/§9 контракта.

Один кадр = `event: <имя>\\ndata: <json>\\n\\n`. Сериализуем компактно и без
ASCII-эскейпа: кириллица в detail должна доехать до UI как есть.

Порядок шагов (v1.1): базовые 9 без `inspect` — это контракт §8, total=9.
Шаг `inspect` (§9) вставляется перед `retrieve`, когда в корпусе есть
нетекстовые источники (image/scan/mixed); тогда total становится 10.
Так оба раздела контракта соблюдены, а UI получает `total` из каждого кадра.
"""
from __future__ import annotations

import json
from typing import Any

BASE_STEPS: list[tuple[str, str, str]] = [
    ("accept", "Запрос принят", "slate"),
    ("decompose", "Разбор вопроса", "violet"),
    ("retrieve", "Поиск", "cyan"),
    ("cases", "Поиск по кейсам", "teal"),
    ("permissions", "Проверка прав", "amber"),
    ("resolve", "Действующая редакция", "blue"),
    ("conflict", "Сопоставление", "rose"),
    ("compose", "Сборка ответа", "emerald"),
    ("verify", "Самопроверка", "green"),
]

INSPECT_STEP = ("inspect", "Разбор источников", "indigo")

# Совместимость: старые импорты ожидают STEP_ORDER/step_keys/total.
STEP_ORDER = BASE_STEPS
_COLOR = {k: c for k, _, c in BASE_STEPS + [INSPECT_STEP]}
_TITLE = {k: t for k, t, _ in BASE_STEPS + [INSPECT_STEP]}


def plan(include_inspect: bool = False) -> list[tuple[str, str, str]]:
    """Полный порядок шагов для конкретного запроса."""
    if not include_inspect:
        return list(BASE_STEPS)
    out = list(BASE_STEPS)
    out.insert(2, INSPECT_STEP)  # перед retrieve
    return out


def step_keys(include_inspect: bool = False) -> list[str]:
    return [k for k, _, _ in plan(include_inspect)]


def total(include_inspect: bool = False) -> int:
    return len(plan(include_inspect))


def frame(event: str, data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


def step(n: int, key: str, detail: str, ms: int, include_inspect: bool = False) -> str:
    return frame("step", {
        "n": n,
        "total": total(include_inspect),
        "key": key,
        "title": _TITLE.get(key, key),
        "detail": detail,
        "ms": ms,
        "color": _COLOR.get(key, "slate"),
    })


def answer(text: str, citations: list[dict], refused: bool = False,
           assumptions: list[dict] | None = None) -> str:
    payload: dict[str, Any] = {"text": text, "citations": citations, "refused": refused}
    if assumptions:
        payload["assumptions"] = assumptions
    return frame("answer", payload)


def refusal(reason: str, sources: list[str], kind: str) -> str:
    return frame("refusal", {"reason": reason, "sources": sources, "kind": kind})


def clarify(session_id: str, questions: list[dict], partial: dict | None = None,
            can_skip: bool = True) -> str:
    """§10 v1.2: вместо answer/refusal, поток закрывается без done."""
    return frame("clarify", {
        "session_id": session_id,
        "questions": questions,
        "partial": partial or {},
        "can_skip": can_skip,
    })


def done(elapsed_ms: int, inference_path: str, journal_id: str) -> str:
    return frame("done", {
        "elapsed_ms": elapsed_ms,
        "inference_path": inference_path,
        "journal_id": journal_id,
    })


def error(message: str, stage: str) -> str:
    return frame("error", {"message": message, "stage": stage})
