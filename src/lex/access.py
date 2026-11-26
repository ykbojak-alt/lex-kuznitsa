"""LEX: фильтр прав доступа (контракт API-CONTRACT.md §§2, 4, 8, 11).

Роль — список отделов + список систем + направления по умолчанию (``default_lines``).
``*`` означает «все». Права обязательны по брифу организаторов: документ доступен
только при пересечении **И** отдела, **И** бизнес-направления.

Важно различать два состояния (§4):
* ``no_access`` — документы по вопросу есть, но не для этой роли;
* ``not_in_corpus`` — в корпусе вообще ничего не нашлось.

У документа без ``business_line`` (наш текущий корпус) направление считается
``общее`` и не блокирует доступ (§11 «Что делать с нашим корпусом»).

Только чтение: этот модуль не меняет чужие файлы и не пишет в корпус.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = Path(
    os.environ.get("LEX_MANIFEST_PATH") or (ROOT / "sample-corpus" / "manifest.json")
)
CASES_PATH = Path(os.environ.get("LEX_CASES_PATH") or (ROOT / "var" / "cases.jsonl"))

GENERAL_LINE = "общее"
ALL = "*"

# Роли и направления — данные для GET /api/roles (SPARK импортирует отсюда).
ROLE_DEFS: list[dict[str, Any]] = [
    {
        "id": "lawyer",
        "title": "Юридический отдел",
        "departments": ["Юридический отдел"],
        "systems": [],
        "default_lines": ["лицензирование", "закупки"],
    },
    {
        "id": "quality",
        "title": "Отдел качества",
        "departments": ["Отдел качества"],
        "systems": [],
        "default_lines": ["качество", "хранение"],
    },
    {
        "id": "it",
        "title": "ИТ-служба",
        "departments": ["ИТ-служба"],
        "systems": [],
        "default_lines": ["персонал", "закупки"],
    },
    {
        "id": "director",
        "title": "Руководитель",
        "departments": [ALL],
        "systems": [ALL],
        "default_lines": [ALL],
    },
]

BUSINESS_LINES: list[str] = [
    "закупки", "лицензирование", "качество", "хранение", "персонал", GENERAL_LINE,
]

# «Все выше» — только руководитель; остальные роли равны по уровню.
ROLE_RANK: dict[str, int] = {"lawyer": 1, "quality": 1, "it": 1, "director": 3}

REASON_DEPARTMENT = "вне подразделения"
REASON_LINE = "вне направления"


def get_role(role_id: str | dict[str, Any] | None) -> dict[str, Any] | None:
    if isinstance(role_id, dict):
        return role_id
    if not role_id:
        return None
    return next((r for r in ROLE_DEFS if r.get("id") == role_id), None)


def departments_of(role: dict[str, Any] | None) -> list[str]:
    return list((role or {}).get("departments") or [])


def systems_of(role: dict[str, Any] | None) -> list[str]:
    return list((role or {}).get("systems") or [])


def business_lines_of(role: dict[str, Any] | None) -> list[str]:
    """Направления роли: явные business_lines, иначе default_lines (§11)."""
    role = role or {}
    lines = role.get("business_lines") or role.get("default_lines") or [ALL]
    return [str(x) for x in lines]


def doc_department(doc: dict[str, Any]) -> str:
    return str(doc.get("department") or "")


def doc_system(doc: dict[str, Any]) -> str:
    return str(doc.get("system") or "")


def doc_business_line(doc: dict[str, Any]) -> str:
    """Без поля business_line — «общее», не блокируем (§11)."""
    return str(doc.get("business_line") or GENERAL_LINE)


def _matches(value: str, allowed: list[str]) -> bool:
    return ALL in allowed or value in allowed or (not value and ALL in allowed)


def access_reason(doc: dict[str, Any], role: dict[str, Any] | None) -> str | None:
    """None — доступен; иначе причина отказа по конкретному признаку."""
    if role is None:
        return "роль не задана"
    if not _matches(doc_department(doc), departments_of(role)):
        return REASON_DEPARTMENT
    if not _matches(doc_business_line(doc), business_lines_of(role)):
        return REASON_LINE
    return None


def load_manifest(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else MANIFEST_PATH
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"documents": []}


def all_documents(path: str | Path | None = None) -> list[dict[str, Any]]:
    docs = load_manifest(path).get("documents") or []
    return [d for d in docs if isinstance(d, dict)]


def load_cases(path: str | Path | None = None) -> list[dict[str, Any]]:
    target = Path(path) if path else CASES_PATH
    if not target.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            if isinstance(raw, dict):
                out.append(raw)
        except json.JSONDecodeError:
            continue
    return out


def filter_docs(
    documents: list[dict[str, Any]],
    role: str | dict[str, Any] | None,
    *,
    candidates_total: int | None = None,
) -> dict[str, Any]:
    """Фильтр документов по роли. Возвращает доступные + отчёт о правах.

    ``documents`` — уже найденные по вопросу кандидаты. Пустой список =
    ``not_in_corpus``; непустой, но всё отброшено = ``no_access``.
    """
    role_obj = get_role(role)
    total = len(documents) if candidates_total is None else int(candidates_total)
    allowed: list[dict[str, Any]] = []
    reasons: dict[str, int] = {}
    for doc in documents or []:
        why = access_reason(doc, role_obj)
        if why is None:
            allowed.append(doc)
        else:
            reasons[why] = reasons.get(why, 0) + 1

    refused = total > 0 and not allowed
    refusal_kind = None
    if refused:
        refusal_kind = "no_access"
    elif total == 0:
        refusal_kind = "not_in_corpus"

    permissions = {
        "total": total,
        "available": len(allowed),
        "dropped": max(0, total - len(allowed)),
        "by_reason": dict(reasons),
    }
    return {
        "documents": allowed,
        "allowed": allowed,
        "permissions": permissions,
        "total": total,
        "available": len(allowed),
        "dropped": permissions["dropped"],
        "reasons": dict(reasons),
        "refused": refused,
        "refusal_kind": refusal_kind,
    }


def case_allowed(
    case: dict[str, Any],
    role: dict[str, Any] | None,
    accessible_doc_ids: set[str] | None = None,
) -> bool:
    """Кейс доступен, если его автор — роль пользователя/руководитель,
    либо доступны связанные документы. Кейс руководителя виден всем; кейс
    отдела — только своему отделу и всех выше (§8 +补充 v1.1)."""
    if role is None:
        return False
    role_id = str(role.get("id") or "")
    author = str(case.get("author_role") or "")
    if role_id == "director" or ALL in departments_of(role):
        return True
    if author == role_id or author == "director":
        return True
    if ROLE_RANK.get(role_id, 0) > ROLE_RANK.get(author, 1):
        return True
    linked = {str(x) for x in (case.get("linked_docs") or [])}
    if accessible_doc_ids and linked & accessible_doc_ids:
        return True
    return False


def filter_cases(
    cases: list[dict[str, Any]],
    role: str | dict[str, Any] | None,
    *,
    accessible_doc_ids: set[str] | None = None,
) -> dict[str, Any]:
    role_obj = get_role(role)
    allowed = [c for c in (cases or []) if case_allowed(c, role_obj, accessible_doc_ids)]
    total = len(cases or [])
    return {
        "cases": allowed,
        "total": total,
        "available": len(allowed),
        "dropped": total - len(allowed),
        "refused": total > 0 and not allowed,
        "refusal_kind": "no_access" if total > 0 and not allowed else ("not_in_corpus" if total == 0 else None),
    }


__all__ = [
    "ROLE_DEFS", "BUSINESS_LINES", "ROLE_RANK", "GENERAL_LINE", "ALL",
    "REASON_DEPARTMENT", "REASON_LINE", "get_role", "departments_of", "systems_of",
    "business_lines_of", "doc_department", "doc_system", "doc_business_line",
    "access_reason", "load_manifest", "all_documents", "load_cases",
    "filter_docs", "case_allowed", "filter_cases",
]
