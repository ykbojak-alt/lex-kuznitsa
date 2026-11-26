"""LEX: модель прав (контракт API-CONTRACT.md §23, v1.15).

**Чтение единое:** все роли видят весь корпус одинаково — фильтра по отделу и
направлению больше нет. Отказ ``no_access`` из чтения исчез.

**Запись** (кейсы, документы, формы) — только ``quality`` и ``director``.

``departments``/``default_lines`` в ROLE_DEFS сохранены: они нужны аналитике
(кто о чём спрашивает) и предупреждениям в UI, но не влияют на выдачу.
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

ALL = "*"
GENERAL_LINE = "общее"
WRITE_ROLES = frozenset({"quality", "director"})
READ_ROLES = frozenset({"lawyer", "quality", "it", "director"})

# Роли и направления — данные для GET /api/roles (SPARK импортирует отсюда).
# departments / default_lines НЕ фильтруют чтение (v1.15) — только аналитика.
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
ROLE_RANK: dict[str, int] = {"lawyer": 1, "quality": 1, "it": 1, "director": 3}
REASON_DEPARTMENT = "вне подразделения"
REASON_LINE = "вне направления"


def get_role(role_id: str | dict[str, Any] | None) -> dict[str, Any] | None:
    if isinstance(role_id, dict):
        return role_id
    if not role_id:
        return None
    return next((r for r in ROLE_DEFS if r.get("id") == role_id), None)


def role_id_of(role: str | dict[str, Any] | None) -> str:
    if isinstance(role, dict):
        return str(role.get("id") or "")
    return str(role or "")


def can_read(role: str | dict[str, Any] | None) -> bool:
    """Чтение: весь корпус доступен всем ролям (v1.15)."""
    return True


def can_write(role: str | dict[str, Any] | None) -> bool:
    """Запись: только служба качества и руководство."""
    return role_id_of(role) in WRITE_ROLES


def require_write(role: str | dict[str, Any] | None) -> bool:
    """Проверка для эндпоинтов записи (аналог journal.require_write)."""
    return can_write(role)


# --- аналитические срезы (НЕ влияют на выдачу) -------------------------------

def departments_of(role: dict[str, Any] | None) -> list[str]:
    return list((role or {}).get("departments") or [])


def systems_of(role: dict[str, Any] | None) -> list[str]:
    return list((role or {}).get("systems") or [])


def business_lines_of(role: dict[str, Any] | None) -> list[str]:
    role = role or {}
    lines = role.get("business_lines") or role.get("default_lines") or [ALL]
    return [str(x) for x in lines]


def doc_department(doc: dict[str, Any]) -> str:
    return str(doc.get("department") or "")


def doc_system(doc: dict[str, Any]) -> str:
    return str(doc.get("system") or "")


def doc_business_line(doc: dict[str, Any]) -> str:
    return str(doc.get("business_line") or GENERAL_LINE)


def warning_for_doc(doc: dict[str, Any], role: dict[str, Any] | None) -> str | None:
    """Информационное предупреждение «вне подразделения / вне направления».

    Только для UI/аналитики: документ всё равно выдаётся (v1.15).
    """
    if role is None or ALL in departments_of(role):
        return None
    if doc_department(doc) not in departments_of(role):
        return REASON_DEPARTMENT
    lines = business_lines_of(role)
    if ALL not in lines and doc_business_line(doc) not in lines:
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
    role: str | dict[str, Any] | None = None,
    *,
    candidates_total: int | None = None,
) -> dict[str, Any]:
    """Единое чтение: все кандидаты отдаются независимо от роли.

    Пустой список кандидатов = ``not_in_corpus``; ``no_access`` больше не
    возникает никогда (v1.15). ``permissions`` информационный: показываем
    «чтение: полный корпус · запись: раз/reshena».
    """
    role_obj = get_role(role)
    docs = [d for d in (documents or []) if isinstance(d, dict)]
    total = len(docs) if candidates_total is None else int(candidates_total)
    writable = can_write(role_obj)
    permissions = {
        "mode": "unified_read",
        "total": total,
        "available": total,
        "dropped": 0,
        "by_reason": {},
        "read": "полный корпус",
        "write": "разрешена" if writable else "недоступна",
        "can_write": writable,
    }
    return {
        "documents": docs,
        "allowed": docs,
        "permissions": permissions,
        "total": total,
        "available": total,
        "dropped": 0,
        "reasons": {},
        "refused": False,
        "refusal_kind": "not_in_corpus" if total == 0 else None,
    }


def case_allowed(case: dict[str, Any], role: str | dict[str, Any] | None = None,
                 accessible_doc_ids: set[str] | None = None) -> bool:
    """Кейсы на чтение видит каждая роль; на запись — только WRITE_ROLES."""
    return can_read(role)


def filter_cases(cases: list[dict[str, Any]],
                 role: str | dict[str, Any] | None = None,
                 *, accessible_doc_ids: set[str] | None = None) -> dict[str, Any]:
    items = [c for c in (cases or []) if isinstance(c, dict)]
    total = len(items)
    return {
        "cases": items,
        "total": total,
        "available": total,
        "dropped": 0,
        "refused": False,
        "refusal_kind": "not_in_corpus" if total == 0 else None,
    }


__all__ = [
    "ALL",
    "BUSINESS_LINES",
    "GENERAL_LINE",
    "READ_ROLES",
    "REASON_DEPARTMENT",
    "REASON_LINE",
    "ROLE_DEFS",
    "ROLE_RANK",
    "WRITE_ROLES",
    "all_documents",
    "business_lines_of",
    "can_read",
    "can_write",
    "case_allowed",
    "departments_of",
    "doc_business_line",
    "doc_department",
    "doc_system",
    "filter_cases",
    "filter_docs",
    "get_role",
    "load_cases",
    "load_manifest",
    "require_write",
    "role_id_of",
    "systems_of",
    "warning_for_doc",
]
