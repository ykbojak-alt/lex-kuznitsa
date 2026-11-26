"""Формат SSE по §3 контракта.

Один кадр = `event: <имя>\\ndata: <json>\\n\\n`. Сериализуем компактно и без
ASCII-эскейпа: кириллица в detail должна доехать до UI как есть.
"""
from __future__ import annotations

import json
from typing import Any

STEP_ORDER = [
    ("accept", "Запрос принят", "slate"),
    ("decompose", "Разбор вопроса", "violet"),
    ("retrieve", "Поиск", "cyan"),
    ("permissions", "Проверка прав", "amber"),
    ("resolve", "Действующая редакция", "blue"),
    ("conflict", "Сопоставление", "rose"),
    ("compose", "Сборка ответа", "emerald"),
    ("verify", "Самопроверка", "green"),
]

TOTAL_STEPS = len(STEP_ORDER)
_COLOR = {k: c for k, _, c in STEP_ORDER}
_TITLE = {k: t for k, t, _ in STEP_ORDER}


def frame(event: str, data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


def step(n: int, key: str, detail: str, ms: int) -> str:
    """Кадр `step` с номером, заголовком и цветом из фиксированного порядка."""
    return frame("step", {
        "n": n,
        "total": TOTAL_STEPS,
        "key": key,
        "title": _TITLE.get(key, key),
        "detail": detail,
        "ms": ms,
        "color": _COLOR.get(key, "slate"),
    })


def answer(text: str, citations: list[dict], refused: bool = False) -> str:
    return frame("answer", {"text": text, "citations": citations, "refused": refused})


def refusal(reason: str, sources: list[str], kind: str) -> str:
    return frame("refusal", {"reason": reason, "sources": sources, "kind": kind})


def done(elapsed_ms: int, inference_path: str, journal_id: str) -> str:
    return frame("done", {
        "elapsed_ms": elapsed_ms,
        "inference_path": inference_path,
        "journal_id": journal_id,
    })


def error(message: str, stage: str) -> str:
    return frame("error", {"message": message, "stage": stage})


def step_keys() -> list[str]:
    return [k for k, _, _ in STEP_ORDER]
