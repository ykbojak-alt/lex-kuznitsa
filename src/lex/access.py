"""LEX: модель прав (контракт API-CONTRACT.md §23, v1.16).

**Чтение — по отделу.** Документ виден, если его отдел входит в подразделения
роли; ``director`` видит всё. Роли — это реальные отделы организации, а не
абстракции: набор определяется манифестом корпуса, и при смене корпуса
(``sample-corpus`` -> ``corpus-real``) меняется вместе с ним.

v1.15 на время отключал фильтр чтения целиком («чтение единое»). Это оказалось
неверным для приёмки: ``src/web/index.html`` обещает, что «от роли зависит,
какие документы агент вправе показать», а фильтр был выключен. v1.16
возвращает фильтр, но привязывает роли к реальным отделам — иначе юрист
получал бы 0 документов, потому что в манифесте нет «Юридического отдела».

**Запись** (кейсы, документы, формы) — только ``quality`` и ``director``.

Роли определяются по манифесту, а не по списку в коде: список в коде
(``lawyer``/``quality``/``it``) соответствовал синтетическому корпусу и
разошёлся с данными заказчика. Канонический список собирает
``roles_for_corpus(path)``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = Path(
    os.environ.get("LEX_MANIFEST_PATH") or (ROOT / "corpus-real" / "manifest.json")
)
CASES_PATH = Path(os.environ.get("LEX_CASES_PATH") or (ROOT / "var" / "cases.jsonl"))

ALL = "*"
GENERAL_LINE = "общее"
WRITE_ROLES = frozenset({"quality", "director"})
READ_ROLES = frozenset({"lawyer", "quality", "it", "director"})

# Роли синтетического корпуса. Сохранены ради совместимости: на них держатся
# демо-сценарии и тесты, и на ``sample-corpus`` они по-прежнему осмысленны —
# отделы там ровно такие («Юридический отдел», «Отдел качества», «ИТ-служба»).
# Для реального корпуса роли другие (см. ROLE_DEFS_REAL); какой набор
# действует, решает roles_for_corpus() по манифесту.
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

# Роли реального корпуса заказчика: id = транслитерация отдела, title = отдел
# как в ``manifest.json``. ``director`` — единственная сквозная роль (видит
# все отделы), ``quality`` остаётся даже здесь: служба качества отвечает на
# заявки по любому направлению и должна видеть корпус, чтобы отвечать.
ROLE_DEFS_REAL: list[dict[str, Any]] = [
    {
        "id": "technical",
        "title": "Техническая дирекция",
        "departments": ["Техническая дирекция"],
        "systems": [],
        "default_lines": ["проектная деятельность"],
    },
    {
        "id": "ib",
        "title": "Дирекция ИБ",
        "departments": ["Дирекция ИБ"],
        "systems": [],
        "default_lines": ["пресейл", "проектная деятельность"],
    },
    {
        "id": "dbrct",
        "title": "ДБРЦТ",
        "departments": ["ДБРЦТ"],
        "systems": [],
        "default_lines": ["проектная деятельность"],
    },
    {
        "id": "quality",
        "title": "Служба качества",
        "departments": [ALL],
        "systems": [ALL],
        "default_lines": [ALL],
    },
    {
        "id": "director",
        "title": "Руководитель",
        "departments": [ALL],
        "systems": [ALL],
        "default_lines": [ALL],
    },
]

# Псевдонимы id из старого списка. Сначала ищется роль с подходящим отделом
# (``LEGACY_ROLE_HINTS``): на синтетическом корпусе «юрист» и есть
# «Юридический отдел», только id теперь слагом. Если отдела нет вовсе
# (реальный корпус), псевдоним падает в сквозную роль своей функции:
# юридическая экспертиза и ИТ в структуре заказчика живут в Технической
# дирекции, поэтому ``lawyer``/``it`` -> ``technical``, а не в ``director``:
# дать «юристу» права руководителя на весь корпус — это не совместимость, а
# утечка.
LEGACY_ROLE_HINTS: dict[str, tuple[str, ...]] = {
    "lawyer": ("юрид",),
    "quality": ("качеств", "служба качества"),
    "it": ("ит-служба", "информац", "ит "),
}
ROLE_ALIASES: dict[str, str] = {
    "lawyer": "technical",
    "it": "technical",
}

BUSINESS_LINES: list[str] = [
    "закупки", "лицензирование", "качество", "хранение", "персонал", GENERAL_LINE,
]
ROLE_RANK: dict[str, int] = {"lawyer": 1, "quality": 1, "it": 1, "director": 3}
REASON_DEPARTMENT = "вне подразделения"
REASON_LINE = "вне направления"

# Отделы реального корпуса, по которым заведены именованные роли. Отдел,
# которого здесь нет, всё равно получит роль — из манифеста, с id-слагом.
REAL_DEPARTMENT_IDS: dict[str, str] = {
    "Техническая дирекция": "technical",
    "Дирекция ИБ": "ib",
    "ДБРЦТ": "dbrct",
}


def _manifest_path(corpus_root: str | Path | None = None) -> Path:
    if corpus_root is None:
        env = os.environ.get("LEX_CORPUS_DIR")
        corpus_root = Path(env) if env else ROOT / "corpus-real"
    root = Path(corpus_root)
    return root if root.name.endswith(".json") else root / "manifest.json"


def _slug(title: str) -> str:
    table = str.maketrans({
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
        "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    })
    return "".join(ch if ch.isalnum() else "-"
                   for ch in title.lower().translate(table)).strip("-")


def roles_from_departments(departments: list[str]) -> list[dict[str, Any]]:
    """Роли по отделам корпуса — единственный источник истины.

    Раньше роли были списком в коде, и он разошёлся с данными: код объявлял
    «Юридический отдел», а в корпусе заказчика таких отделов нет вовсе. При
    включённом фильтре это дало бы юристу 0 документов и вечную эскалацию.
    Поэтому набор ролей выводится из манифеста; именованные id берутся из
    ``REAL_DEPARTMENT_IDS``, безымянные получают слаг отдела.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for title in departments:
        title = str(title or "").strip()
        if not title:
            continue
        rid = REAL_DEPARTMENT_IDS.get(title) or _slug(title)
        if rid in seen:
            continue
        seen.add(rid)
        out.append({
            "id": rid,
            "title": title,
            "departments": [title],
            "systems": [],
            "default_lines": [],
        })
    # Служба качества и руководство — сквозные: они отвечают по всем
    # направлениям, поэтому их отдел не привязан к манифесту.
    if "quality" not in seen:
        out.append({
            "id": "quality",
            "title": "Служба качества",
            "departments": [ALL],
            "systems": [ALL],
            "default_lines": [ALL],
        })
    if "director" not in seen:
        out.append({
            "id": "director",
            "title": "Руководитель",
            "departments": [ALL],
            "systems": [ALL],
            "default_lines": [ALL],
        })
    return out


def roles_for_corpus(corpus_root: str | Path | None = None) -> list[dict[str, Any]]:
    """Активный набор ролей — по манифесту корпуса.

    Пустой манифест не должен обнулять роли: возвращаем синтетический набор,
    чтобы API и демо отвечали, а не падали.
    """
    docs = all_documents(_manifest_path(corpus_root))
    departments = sorted({str(d.get("department") or "").strip()
                          for d in docs if d.get("department")})
    if not departments:
        return [dict(r) for r in ROLE_DEFS]
    return roles_from_departments(departments)


def _default_role_defs() -> list[dict[str, Any]]:
    """Роли корпуса плюс известные объявления из обоих наборов.

    Вызов ``filter_docs(docs, "ib")`` без явного списка ролей не должен
    падать в «роль неизвестна»: id из ``ROLE_DEFS_REAL`` объявлены в модуле,
    и их подразделения там же. Набор корпуса приоритетнее — он отражает
    реальные данные, а статический список остаётся запасным.
    """
    out = roles_for_corpus()
    known = {str(r.get("id")) for r in out}
    for src in (ROLE_DEFS_REAL, ROLE_DEFS):
        for r in src:
            rid = str(r.get("id"))
            if rid not in known:
                out.append(dict(r))
                known.add(rid)
    return out


def resolve_role(
    role: str | dict[str, Any] | None,
    defs: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Роль по id с учётом псевдонимов старого списка.

    ``lawyer``/``it`` в реальной структуре отделов нет. Раньше это дало бы
    «роль не найдена» на каждом запросе старого клиента; псевдоним переводит
    в сквозную роль, чтобы демо и тесты продолжали работать.
    """
    if isinstance(role, dict):
        return role
    rid = str(role or "").strip().lower()
    if not rid:
        return None
    pool = defs if defs is not None else _default_role_defs()
    hit = next((r for r in pool if str(r.get("id")).lower() == rid), None)
    if hit is not None:
        return hit
    # Старый id: сперва ищем отдел по смыслу (юрид./качеств./ИТ) — так
    # синтетический корпус продолжает отдавать РОВНО тот же документ, что до
    # v1.16, и демо не ломается. Только если отдела нет, берём псевдоним.
    for hint in LEGACY_ROLE_HINTS.get(rid, ()):
        found = next((r for r in pool if hint in str(r.get("title") or "").lower()), None)
        if found is not None:
            return found
    alias = ROLE_ALIASES.get(rid)
    if alias:
        return next((r for r in pool if str(r.get("id")).lower() == alias), None)
    return None


def roles() -> list[dict[str, Any]]:
    """Роли текущего корпуса — стабильное имя для потребителей (dialogue, api).

    Ядро и HTTP-слой зовут ``access.roles()``; набор зависит от манифеста,
    поэтому имя живёт здесь, а не только ``roles_for_corpus``.
    """
    return roles_for_corpus()


def get_role(role_id: str | dict[str, Any] | None) -> dict[str, Any] | None:
    return resolve_role(role_id)


def role_id_of(role: str | dict[str, Any] | None) -> str:
    if isinstance(role, dict):
        return str(role.get("id") or "")
    return str(role or "")


def can_read(role: str | dict[str, Any] | None) -> bool:
    """§23 v1.16: чтение разрешено известной роли.

    Отказ по пустому корпусу — это ``not_in_corpus``, а не запрет чтения:
    роль может читать, просто читать нечего. Возврат False здесь — только
    «такой роли нет», и это ошибка запроса.
    """
    return resolve_role(role) is not None


def can_write(role: str | dict[str, Any] | None) -> bool:
    """Запись: только служба качества и руководство."""
    rid = role_id_of(role).strip().lower()
    return rid in WRITE_ROLES or ROLE_ALIASES.get(rid, "") in WRITE_ROLES


def require_write(role: str | dict[str, Any] | None) -> bool:
    """Проверка для эндпоинтов записи (аналог journal.require_write)."""
    return can_write(role)


def role_allows_department(
    role: str | dict[str, Any] | None,
    department: str,
    defs: list[dict[str, Any]] | None = None,
) -> bool:
    """Документ отдела виден роли: отдел в списке подразделений или ALL.

    Пустой отдел у документа трактуется как «общий»: норма без владельца
    (преамбула, справочник) не должна исчезать от смены роли.
    """
    obj = resolve_role(role, defs)
    if obj is None:
        return False
    deps = [str(x) for x in (obj.get("departments") or [])]
    if ALL in deps:
        return True
    dep = str(department or "").strip()
    if not dep:
        return True
    return dep in deps


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
    """Почему документ не виден роли. ``None`` — виден.

    В v1.15 функция звалась «информационным предупреждением»: документ всё
    равно выдавался. В v1.16 фильтр снова действует, и та же проверка стала
    решающей — поэтому имя оставлено (его видит UI), но смысл другой.

    Неизвестная роль (``None``) — это не «видит всё», а «роль не опознана»:
    фильтр обязана отсекать (fail-closed). Иначе опечатка в id роли молча
    открывает весь корпус — ровно та ошибка, из-за которой §23 v1.15 и был
    выключен.
    """
    if role is None:
        return REASON_DEPARTMENT
    if ALL in departments_of(role):
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
    defs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """§23 v1.16: документ виден, если его отдел доступен роли.

    Фильтр только по отделу, не по направлению: направление — тема документа,
    а тема пересекается у отделов («проектная деятельность» есть у трёх). Если
    фильтровать и по направлению, юрист теряет документы своего отдела,
    помеченные соседним направлением, — это уже невидимое ограничение.

    Пустой результат = ``not_in_corpus``, а не запрет: роль читать вправе,
    просто в базе нет документов её отдела. Отказ ``no_access`` из чтения
    остаётся удалённым — он вводил в заблуждение («нет доступа» вместо
    «нет документа»).
    """
    role_obj = resolve_role(role, defs)
    docs = [d for d in (documents or []) if isinstance(d, dict)]
    total = len(docs) if candidates_total is None else int(candidates_total)
    allowed: list[dict[str, Any]] = []
    dropped = 0
    by_reason: dict[str, int] = {}
    for d in docs:
        reason = warning_for_doc(d, role_obj)
        if reason is None:
            allowed.append(d)
        else:
            dropped += 1
            by_reason[reason] = by_reason.get(reason, 0) + 1
    writable = can_write(role_obj)
    available = len(allowed)
    departments = sorted({str(d.get("department") or "") for d in allowed
                          if d.get("department")})
    permissions = {
        "mode": "department_read",
        "role": role_id_of(role_obj),
        "departments": [str(x) for x in departments_of(role_obj)],
        "total": total,
        "available": available,
        "dropped": dropped,
        "by_reason": by_reason,
        "read": (f"полный корпус ({total} документов)" if ALL in departments_of(role_obj)
                 else f"{available} из {total} документов · отделы: "
                      + (", ".join(departments) or "—")),
        "write": "разрешена" if writable else "недоступна",
        "can_write": writable,
    }
    return {
        "documents": allowed,
        "allowed": allowed,
        "permissions": permissions,
        "total": total,
        "available": available,
        "dropped": dropped,
        "reasons": by_reason,
        "refused": False,
        "refusal_kind": "not_in_corpus" if total == 0 else None,
    }


def _case_department(case: dict[str, Any],
                     accessible_doc_ids: set[str] | None = None) -> str:
    """Отдел кейса: явное поле, иначе — отдел связанного документа.

    У кейса отдела нет в модели (§8: кейс принадлежит роли-автору). Но если
    он ссылается на документ, документ отдел знает — этого достаточно, чтобы
    не показывать практику чужого отдела.
    """
    dep = str(case.get("department") or "").strip()
    if dep:
        return dep
    if accessible_doc_ids is None:
        return ""
    linked = case.get("linked_docs") or []
    return str(linked[0]) if linked else ""


def case_allowed(
    case: dict[str, Any],
    role: str | dict[str, Any] | None = None,
    accessible_doc_ids: set[str] | None = None,
    *,
    defs: list[dict[str, Any]] | None = None,
) -> bool:
    """§23 v1.16: кейс виден, если виден его отдел — либо кейс публичный.

    ``public=True`` — автор явно сказал, что практика общая (порядок действий,
    не привязанный к отделу). Без этого флага чужой отдел практику не видит:
    кейс — это опыт конкретного отдела, а не норматив.
    """
    if not isinstance(case, dict):
        return False
    if case.get("public"):
        return True
    obj = resolve_role(role, defs)
    if obj is None:
        return False
    if ALL in departments_of(obj):
        return True
    dep = _case_department(case, accessible_doc_ids)
    if not dep:
        # Кейс без отдела и без документа-основания: автор есть — сужаем до
        # него; автора нет (импорт практики) — считаем общим.
        author = str(case.get("author_role") or "").strip().lower()
        return not author or role_id_of(obj).lower() == author
    return str(dep) in departments_of(obj)


def filter_cases(
    cases: list[dict[str, Any]],
    role: str | dict[str, Any] | None = None,
    *,
    accessible_doc_ids: set[str] | None = None,
    defs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    items = [c for c in (cases or []) if isinstance(c, dict)]
    total = len(items)
    allowed = [c for c in items
               if case_allowed(c, role, accessible_doc_ids, defs=defs)]
    dropped = total - len(allowed)
    permissions = {
        "mode": "department_read",
        "role": role_id_of(role),
        "total": total,
        "available": len(allowed),
        "dropped": dropped,
        "by_reason": {REASON_DEPARTMENT: dropped} if dropped else {},
    }
    return {
        "cases": allowed,
        "permissions": permissions,
        "total": total,
        "available": len(allowed),
        "dropped": dropped,
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
    "ROLE_ALIASES",
    "ROLE_DEFS",
    "ROLE_DEFS_REAL",
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
    "resolve_role",
    "role_allows_department",
    "role_id_of",
    "roles_for_corpus",
    "roles_from_departments",
    "systems_of",
    "warning_for_doc",
]
