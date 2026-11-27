"""Формат SSE по §3 + §8/§9/§10/§17/§18 контракта.

Один кадр = `event: <имя>\\ndata: <json>\\n\\n`. Сериализуем компактно и без
ASCII-эскейпа: кириллица в detail должна доехать до UI как есть.

Порядок шагов берётся из ядра (`lex.models.STEP_ORDER`) — это единственный
источник правды, потому что шаги эмитит именно ядро. Сейчас ядро отдаёт 11
шагов: §17 добавила `policy` (10 шагов), §9 добавила `inspect` (11-й). Если
ядро недоступно, используется тот же список, продублированный здесь.
"""
from __future__ import annotations

from typing import Any

try:  # порядок и цвета — из ядра, если оно есть
    from lex.models import STEP_COLORS as _CORE_COLORS
    from lex.models import STEP_ORDER as _CORE_ORDER

    _ORDER: tuple[str, ...] = tuple(_CORE_ORDER)
    _COLOR: dict[str, str] = dict(_CORE_COLORS)
except Exception:  # ядро ещё не подключено — тот же список локально
    _ORDER = (
        "accept", "policy", "decompose", "inspect", "retrieve", "cases",
        "permissions", "resolve", "conflict", "compose", "verify",
    )
    _COLOR = {
        "accept": "slate", "policy": "pink", "decompose": "violet",
        "inspect": "indigo", "retrieve": "cyan", "cases": "teal",
        "permissions": "amber", "resolve": "blue", "conflict": "rose",
        "compose": "emerald", "verify": "green",
    }

_TITLE: dict[str, str] = {
    "accept": "Запрос принят",
    "policy": "Проверка компетенции",
    "decompose": "Разбор вопроса",
    "inspect": "Разбор источников",
    "retrieve": "Поиск",
    "cases": "Поиск по кейсам",
    "permissions": "Проверка прав",
    "resolve": "Действующая редакция",
    "conflict": "Сопоставление",
    "compose": "Сборка ответа",
    "verify": "Самопроверка",
    "clarify": "Уточнение",
    "escalate": "Передано в службу качества",
}

# §10: шаг уточнения идёт вместо decompose, цвет sky.
_CLARIFY = "clarify"


def step_keys() -> list[str]:
    return list(_ORDER)


def total() -> int:
    return len(_ORDER)


def frame(event: str, data: Any) -> str:
    import json

    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


def step(n: int, key: str, detail: str, ms: int) -> str:
    return frame("step", {
        "n": n,
        "total": total(),
        "key": key,
        "title": _TITLE.get(key, key),
        "detail": detail,
        "ms": ms,
        "color": "sky" if key == _CLARIFY else _COLOR.get(key, "slate"),
    })


def answer(data: dict) -> str:
    """§13.1: короткая выжимка + detail_id; полный текст тоже отдаём.

    §20.2: оговорка — соседнее с ``short`` поле ``caveat``, а не отдельное
    событие. Оговорка не отменяет ответ (§20.3).
    """
    payload = {
        "text": data.get("text", ""),
        "short": data.get("short") or data.get("text", ""),
        "has_details": bool(data.get("has_details")),
        "detail_id": data.get("detail_id"),
        "citations": data.get("citations") or [],
        "refused": bool(data.get("refused")),
    }
    caveat = data.get("caveat")
    if caveat:
        payload["caveat"] = caveat
    for key in ("practice_only", "notes", "assumptions", "session_id",
                "grounding", "forms"):
        value = data.get(key)
        if value:
            payload[key] = value
    return frame("answer", payload)


def refusal(data: dict) -> str:
    payload = {
        "reason": data.get("reason", ""),
        "sources": data.get("sources") or [],
        "kind": data.get("kind", "not_in_corpus"),
    }
    for key in ("detail", "searched", "hint"):
        value = data.get(key)
        if value:
            payload[key] = value
    return frame("refusal", payload)


def clarify(data: dict) -> str:
    """§10: вместо answer/refusal, поток закрывается без done."""
    return frame("clarify", {
        "session_id": data.get("session_id", ""),
        "questions": data.get("questions") or [],
        "partial": data.get("partial") or {},
        "can_skip": bool(data.get("can_skip", True)),
    })


def escalate(data: dict) -> str:
    """§15.3/§18.4: заявка создана — вместо answer/refusal.

    §22.2: карточка письма — часть этого же события. ``delivery`` приходит
    из ``data["email"]`` и несёт ФАКТИЧЕСКИЙ исход отправки (``sent`` /
    ``failed``); ``simulated`` остаётся только когда письмо не отправляли.
    Раньше здесь стояло «всегда simulated» — при включённом SMTP это было
    неправдой, и карточка заявки показывала демо-режим у доставленного
    письма (камень 18).
    """
    payload = {
        "ticket": data.get("ticket", ""),
        "iterations": int(data.get("iterations", 0) or 0),
        "reason": data.get("reason", ""),
        "searched": data.get("searched") or {},
        "next": data.get("next", ""),
        "eta": data.get("eta", ""),
        "status": data.get("status", "open"),
        "case_id": data.get("case_id"),
    }
    email = data.get("email")
    if email:
        payload["channel"] = data.get("channel") or "email"
        payload["email"] = email
    return frame("escalate", payload)


def done(elapsed_ms: int, inference_path: str, journal_id: str | None,
         tokens: dict | None = None) -> str:
    """Финальный кадр. ``tokens`` — расход за весь запрос (prompt/completion).

    Счётчик берётся из ``usage`` ответов провайдера, а не оценивается нами:
    своего токенизатора у слота нет. Поле вводится аддитивно — старые клиенты
    его игнорируют.
    """
    payload = {
        "elapsed_ms": elapsed_ms,
        "inference_path": inference_path,
        "journal_id": journal_id,
    }
    if tokens:
        payload["tokens"] = {
            "prompt": int(tokens.get("prompt") or 0),
            "completion": int(tokens.get("completion") or 0),
            "total": int(tokens.get("total") or 0),
            "calls": int(tokens.get("calls") or 0),
        }
    return frame("done", payload)


def error(message: str, stage: str) -> str:
    # Пустое сообщение — не «нет ошибки», а «ошибка без текста»: жюри видит
    # кадр без причины и читает его как «агент не понял вопрос». Причину
    # называем всегда, даже если исключение пришло без str().
    text = str(message or "").strip() or f"сбой на стадии {stage or 'сервер'} без текста ошибки"
    return frame("error", {"message": text, "stage": stage})
