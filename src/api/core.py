"""Адаптер между HTTP-слоем и ядром src/lex.

Здесь НЕТ логики RAG: только вызов ядра и приведение его сигналов к кадрам SSE.
Ядро (`lex.orchestrator.Orchestrator`) отдаёт ``Signal(kind, data)``, где data —
pydantic-модели из ``lex.models``; HTTP-слой превращает их в строки.

Интерфейс ядра, на который опираемся (согласован с FORGE):
    lex.orchestrator.Orchestrator(RunOptions).run() -> AsyncIterator[Signal]
        kind ∈ step|answer|refusal|clarify|escalate|done|error
    lex.orchestrator.expand(detail_id, section) -> dict | None
    lex.orchestrator.available_forms(corpus_root) -> list[dict]
    lex.orchestrator.download_form_path(corpus_root, form_id) -> Path | None
    lex.orchestrator.journal_entry(opts, ...) -> dict
    lex.forms.validate(form, values, corpus=corpus) -> ValidationResult
    lex.forms.get_form(corpus_root, form_id) -> Form | None
    lex.corpus.load_corpus(root) -> Corpus
    lex.access.roles() -> list[dict]           (появится у ACE; пока свои роли)

Пока какого-то куска нет — используется честная заглушка, и это видно в ответе
(поле ``stub``) и в отчёте. Молча подменять ядро нельзя.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import importlib
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from lex.index import SearchIndex, reset_index

from . import config, sse

CoreEvent = tuple[str, dict]


# ---------- поиск модулей ядра ----------

def _try(module: str):
    try:
        return importlib.import_module(module)
    except Exception:
        return None


def core_modules() -> dict[str, Any]:
    return {
        "orchestrator": _try("lex.orchestrator"),
        "corpus": _try("lex.corpus"),
        "models": _try("lex.models"),
        "forms": _try("lex.forms"),
        "access": _try("lex.access"),
        "journal": _try("lex.journal"),
        "cases": _try("lex.cases"),
        "media": _try("lex.media"),
    }


def core_ready() -> bool:
    m = core_modules()
    return m["orchestrator"] is not None and hasattr(m["orchestrator"], "Orchestrator")


def _as_dict(obj: Any) -> dict:
    if isinstance(obj, dict):
        return obj
    for attr in ("model_dump", "dict", "to_dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                continue
    return {}


# ---------- корпус ----------

_corpus_cache: dict[str, Any] = {}


def corpus_roots() -> list[Path]:
    """Все корни корпуса: corpus-real + опционально corpus-norm (§12).

    Единственный источник списка корней — для corpus(), doc_path(),
    manifest_documents(), content_type_mix(). Внешний корень не ломает
    существующее поведение: если corpus-norm отсутствует — только corpus-real.
    """
    roots = [config.CORPUS_DIR]
    norm_dir = getattr(config, "CORPUS_NORM_DIR", None)
    if norm_dir and norm_dir.exists():
        roots.append(norm_dir)
    return roots


def corpus():
    """Загруженный корпус ядра (кэш по корневому каталогу).

    По §12 загружает corpus-real + corpus-norm (внешние нормативные акты)
    через load_corpus_multi; если norm-корня нет — только corpus-real.
    """
    key = str(config.CORPUS_DIR.resolve())
    if key in _corpus_cache:
        return _corpus_cache[key]
    m = core_modules()["corpus"]
    if m is None or not hasattr(m, "load_corpus"):
        return None
    try:
        c = m.load_corpus_multi(corpus_roots())
    except Exception:
        c = m.load_corpus(config.CORPUS_DIR)
    _corpus_cache[key] = c
    return c


def reload_corpus() -> None:
    _corpus_cache.clear()
    # Индекс кэшируется отдельно от корпуса (index._CACHE): без сброса
    # /api/ingest перечитывал корпус, а поиск продолжал работать по старой
    # матрице и старым чанкам. Дообучение через службу качества выглядело бы
    # «ничего не изменилось».
    reset_index(config.CORPUS_DIR)
    m = core_modules()["corpus"]
    if m is not None and hasattr(m, "load_corpus"):
        corpus()


def manifest() -> dict:
    path = config.CORPUS_DIR / "manifest.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"documents": []}


def manifest_documents() -> list[dict]:
    docs: list[dict] = []
    for root in corpus_roots():
        path = root / "manifest.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        docs.extend(d for d in (data.get("documents") or []) if isinstance(d, dict))
    return docs


def content_type_mix() -> dict[str, int]:
    """Сколько в корпусе источников каждого типа (§9). Нет поля — значит text."""
    mix = {"text": 0, "image": 0, "scan": 0, "mixed": 0}
    for d in manifest_documents():
        ct = str(d.get("content_type") or "text")
        mix[ct if ct in mix else "text"] += 1
    return mix


def corpus_stats() -> dict:
    c = corpus()
    if c is not None and hasattr(c, "stats"):
        try:
            s = c.stats
            return {"documents": int(getattr(s, "documents", 0)),
                    "chunks": int(getattr(s, "chunks", 0))}
        except Exception:
            pass
    return {"documents": len(manifest_documents()), "chunks": 0}


def vector_state() -> dict:
    """Состояние векторного канала: есть ли кэш под текущий состав корпуса.

    ``probe_embed`` пингует только сервис :8081 и про кэш не знает, поэтому
    «Embed доступен» мог стоять рядом с «вектор 0» в шаге поиска. Здесь —
    фактическая готовность векторов.

    Единственная точка правды — ``SearchIndex._load_cache()``: health не должен
    судить о кэше своим разбором npz, иначе он разойдётся с поиском (сверка
    chunk_ids без формы матрицы давала «готовы» рядом с «вектор 0»).
    """
    try:
        c = corpus()
        if c is None:
            return {"ready": False, "reason": "корпус не загружен"}
        root = getattr(c, "root", None)
        n_chunks = len(getattr(c, "chunks", []) or [])
        cache = Path(root) / ".lex-index.npz" if root is not None else None
        if cache is None or not cache.exists():
            return {"ready": False, "reason": "кэш не собран", "chunks": n_chunks,
                    "cache": "нет файла"}
        # Индекс строится по корню самого загруженного корпуса и по чанкам,
        # которые он уже держит: сверка идёт ровно против того же списка,
        # против которого будет искать search(), — не против config.
        idx = SearchIndex(c)
        vecs = idx._load_cache()
        try:
            import numpy as np
            with np.load(cache, allow_pickle=True) as z:
                built_model = str(z["embed_model"][0]) if "embed_model" in z.files else None
                rows = len(z["chunk_ids"])
        except Exception as exc:
            return {"ready": False, "reason": f"кэш не читается: {type(exc).__name__}",
                    "chunks": n_chunks}
        if vecs is not None:
            return {"ready": True, "chunks": n_chunks, "vectors": vecs.shape[0],
                    "model": built_model, "expected_model": embed_model_name(),
                    "reason": "совпал", "cache": str(cache.name)}
        if built_model is not None and built_model != embed_model_name():
            reason = f"кэш собран другой моделью: {built_model}, нужна {embed_model_name()}"
        elif rows != n_chunks:
            reason = f"состав не совпал (кэш под другой корпус: {rows} из {n_chunks})"
        else:
            reason = "форма матрицы не совпала (поиск этот кэш отвергнет)"
        return {"ready": False, "chunks": n_chunks, "vectors": rows,
                "model": built_model, "expected_model": embed_model_name(),
                "reason": reason, "cache": str(cache.name)}
    except Exception as exc:
        return {"ready": False, "reason": f"ошибка проверки: {type(exc).__name__}"}


def embed_model_name() -> str:
    """Имя модели, которой считается векторный канал (env LEX_EMBED_MODEL)."""
    try:
        from lex.index import EMBED_MODEL
        return str(EMBED_MODEL)
    except Exception:
        return "bge-m3"


def business_lines() -> list[dict]:
    """§11: направления из данных. Поля business_line в манифесте ещё нет —
    выводим из ``system`` и честно помечаем ``derived: true``."""
    docs = manifest_documents()
    counts: dict[str, int] = {}
    explicit = False
    for d in docs:
        line = d.get("business_line")
        if line:
            explicit = True
        else:
            line = _line_from_system(str(d.get("system") or ""))
        counts[line] = counts.get(line, 0) + 1
    out = [{"id": k, "title": k.capitalize(), "docs": v} for k, v in counts.items()]
    out.sort(key=lambda x: -x["docs"])
    for item in out:
        item["derived"] = not explicit
    return out


_LINE_HINTS: list[tuple[str, str]] = [
    ("закуп", "закупки"),
    ("продаж", "продажи"),
    ("логист", "логистика"),
    ("качеств", "качество"),
    ("инженер", "производство"),
    ("производ", "производство"),
    ("финанс", "финансы"),
    ("выплат", "персонал"),
    ("юрид", "юридическое"),
    ("охраны труда", "охрана труда"),
    ("пропуск", "охрана труда"),
    ("ит-", "ИТ"),
    ("заявк", "закупки"),
]


def _line_from_system(system: str) -> str:
    low = system.lower()
    for needle, line in _LINE_HINTS:
        if needle in low:
            return line
    return "общее"


# ---------- роли ----------

def default_roles() -> list[dict]:
    """Роли по отделам текущего корпуса (fallback, если lex.access недоступен)."""
    m = core_modules()["access"]
    if m is not None and hasattr(m, "roles_for_corpus"):
        try:
            r = m.roles_for_corpus(config.CORPUS_DIR)
            if isinstance(r, list) and r:
                return r
        except Exception:
            pass

    departments = sorted({d.get("department") for d in manifest_documents()
                          if d.get("department")})
    out = [{"id": "d" + str(i), "title": dep, "departments": [dep], "systems": []}
           for i, dep in enumerate(departments, start=1)]
    out.append({"id": "quality", "title": "Служба качества",
                "departments": ["*"], "systems": ["*"]})
    out.append({"id": "director", "title": "Руководитель",
                "departments": ["*"], "systems": ["*"]})
    return out


def roles() -> list[dict]:
    """Активные роли — по манифесту корпуса (v1.16).

    Раньше брался статический ROLE_DEFS с отделами синтетического корпуса:
    «Юридический отдел», «Отдел качества», «ИТ-служба». На реальном корпусе
    таких отделов нет, и при включённом фильтре чтения юрист получил бы ноль
    документов. Роли выводятся из данных.
    """
    m = core_modules()["access"]
    if m is not None and hasattr(m, "roles_for_corpus"):
        try:
            r = m.roles_for_corpus(config.CORPUS_DIR)
            if isinstance(r, list) and r:
                return r
        except Exception:
            pass
    return default_roles()


def role_ids() -> set[str]:
    return {r.get("id") for r in roles()}


def role_aliases() -> dict[str, str]:
    """Старые id -> действующие роли (v1.16: lawyer/it -> technical).

    Отдельно от ``roles()`` намеренно: алиас принимается на входе, но в
    списке ролей его быть не должно — UI показал бы мёртвый пункт, у
    которого нет ни отдела, ни направления.
    """
    m = core_modules()["access"]
    if m is not None and hasattr(m, "ROLE_ALIASES"):
        try:
            raw = dict(m.ROLE_ALIASES)
            if raw:
                return {str(k).lower(): str(v) for k, v in raw.items()}
        except Exception:
            pass
    # access.py нет (урезанная сборка) — держим тот же минимум, что в ядре.
    return {"lawyer": "technical", "it": "technical"}


def resolve_role_id(role_id: str | None) -> str:
    """Старый id -> действующий. Неизвестный возвращается как есть."""
    rid = (role_id or "").strip().lower()
    return role_aliases().get(rid, rid)


def is_known_role(role_id: str | None) -> bool:
    """Принимается ли роль на входе: действующий id или псевдоним старого."""
    rid = (role_id or "").strip().lower()
    if not rid:
        return False
    if rid in {str(x).lower() for x in role_ids()}:
        return True
    return rid in role_aliases()


def role_departments(role_id: str) -> list[str]:
    m = core_modules()["access"]
    if m is not None and hasattr(m, "resolve_role"):
        try:
            r = m.resolve_role(role_id, roles())
            if r:
                return list(r.get("departments") or [])
        except Exception:
            pass
    for r in roles():
        if r.get("id") == role_id:
            return list(r.get("departments") or [])
    return []


def role_can_read(role_id: str, department: str) -> bool:
    m = core_modules()["access"]
    if m is not None and hasattr(m, "role_allows_department"):
        try:
            return bool(m.role_allows_department(role_id, department, roles()))
        except Exception:
            pass
    deps = role_departments(role_id)
    return "*" in deps or not deps or department in deps


def filter_documents(documents: list[dict] | None = None,
                     role_id: str = "") -> dict:
    """§23 v1.16: отбор документов по отделам роли — один путь для всех.

    UI (`/api/roles`) и шаг `permissions` должны показывать ОДНИ И ТЕ ЖЕ
    числа, что отдало ядро при чтении. Поэтому фильтр не дублируется ни в
    HTTP-слое, ни в оркестраторе: все зовут ``lex.access.filter_docs``.
    """
    docs = manifest_documents() if documents is None else documents
    m = core_modules()["access"]
    if m is not None and hasattr(m, "filter_docs"):
        try:
            return m.filter_docs(docs, role_id, defs=roles())
        except Exception:
            pass
    # access.py недоступен: честный полный доступ помечаем, а не выдаём
    # отсутствие фильтра за проверку прав.
    total = len(docs)
    return {
        "documents": docs,
        "allowed": docs,
        "total": total,
        "available": total,
        "dropped": 0,
        "reasons": {},
        "permissions": {"mode": "no_access_module", "total": total,
                        "available": total, "dropped": 0, "by_reason": {},
                        "read": f"полный корпус ({total} документов)",
                        "write": "недоступна", "can_write": False},
        "refused": False,
        "refusal_kind": None,
    }


# ---------- журнал ----------

def _journal_path() -> Path:
    config.ensure_dirs()
    return config.JOURNAL_PATH


def journal_append(entry: dict) -> None:
    m = core_modules()["journal"]
    if m is not None and hasattr(m, "append"):
        try:
            m.append(entry)
            return
        except Exception:
            pass
    with _journal_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def journal_read(limit: int = 50, role_id: str | None = None) -> list[dict]:
    m = core_modules()["journal"]
    if m is not None and hasattr(m, "read"):
        try:
            rows = m.read(limit=limit, role_id=role_id)
            if isinstance(rows, list):
                return rows
        except Exception:
            pass
    p = _journal_path()
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
            if role_id and obj.get("role_id") != role_id:
                continue
            rows.append(obj)
    return rows[-limit:][::-1]


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def analytics() -> dict:
    m = core_modules()["journal"]
    if m is not None and hasattr(m, "aggregate"):
        try:
            a = m.aggregate()
            if isinstance(a, dict):
                return a
        except Exception:
            pass

    rows = journal_read(limit=100_000)
    total = len(rows)
    refusals = sum(1 for r in rows if r.get("refused"))
    lat = [float(r.get("elapsed_ms") or 0) for r in rows]
    missing: dict[str, int] = {}
    sources: dict[str, int] = {}
    by_role: dict[str, int] = {}
    unanswered: list[dict] = []
    for r in rows:
        rid = r.get("role_id") or "?"
        by_role[rid] = by_role.get(rid, 0) + 1
        for d in r.get("doc_ids") or []:
            sources[d] = sources.get(d, 0) + 1
        if r.get("refused"):
            unanswered.append({"question": r.get("question", ""), "ts": r.get("ts", "")})
            topic = (r.get("refusal_reason") or r.get("question") or "").strip()[:80]
            if topic:
                missing[topic] = missing.get(topic, 0) + 1
    return {
        "total": total,
        "refusals": refusals,
        "refusal_rate": round(refusals / total, 3) if total else 0.0,
        "avg_latency_ms": round(sum(lat) / total) if total else 0,
        "p95_latency_ms": round(_percentile(lat, 0.95)),
        "top_missing": [{"topic": k, "count": v}
                        for k, v in sorted(missing.items(), key=lambda kv: -kv[1])[:10]],
        "top_sources": [{"doc_id": k, "count": v}
                        for k, v in sorted(sources.items(), key=lambda kv: -kv[1])[:10]],
        "by_role": [{"role_id": k, "count": v}
                    for k, v in sorted(by_role.items(), key=lambda kv: -kv[1])],
        "unanswered": unanswered[-10:],
    }


# ---------- ingest ----------

def ingest() -> dict:
    m = core_modules()["corpus"]
    if m is not None and hasattr(m, "load_corpus"):
        try:
            c = m.load_corpus(config.CORPUS_DIR)
            reload_corpus()
            stats = c.stats
            return {"ok": True, "documents": int(getattr(stats, "documents", 0)),
                    "chunks": int(getattr(stats, "chunks", 0)), "added": []}
        except Exception as e:
            return {"ok": False, "documents": 0, "chunks": 0, "added": [], "error": str(e)}
    return {"ok": True, "documents": len(manifest_documents()), "chunks": 0,
            "added": [], "stub": True}


# ---------- документы (§13.2) ----------

def doc_meta(doc_id: str) -> dict | None:
    c = corpus()
    if c is not None and hasattr(c, "doc"):
        try:
            d = c.doc(doc_id)
            if d is not None:
                return _as_dict(d)
        except Exception:
            pass
    # Поиск по doc_id (без расширения) — основной путь.
    for d in manifest_documents():
        if d.get("doc_id") == doc_id:
            return d
    # Поиск по полному имени файла (как приходит в URL /api/doc/{doc_id}.md).
    for d in manifest_documents():
        if d.get("file") == doc_id or (d.get("file") and d["file"].endswith(doc_id)):
            return d
    return None


def doc_path(doc_id: str) -> Path | None:
    meta = doc_meta(doc_id)
    if meta and meta.get("file"):
        for root in corpus_roots():
            p = root / str(meta["file"])
            if p.is_file():
                return p
    for root in corpus_roots():
        if not root.is_dir():
            continue
        for p in sorted(root.iterdir()):
            if p.is_file() and p.name.startswith(doc_id):
                return p
    return None


def doc_sections(doc_id: str) -> list[dict]:
    """Разделы документа из чанков корпуса — для оглавления."""
    c = corpus()
    if c is None:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for ch in getattr(c, "chunks", []):
        if getattr(ch, "doc_id", "") != doc_id:
            continue
        title = (getattr(ch, "section", "") or "").strip() or "Документ"
        if title in seen:
            continue
        seen.add(title)
        out.append({"title": title, "anchor": _anchor(title)})
    return out


def _anchor(title: str) -> str:
    import re
    low = title.lower().strip()
    low = re.sub(r"[^\w\s-]", "", low, flags=re.UNICODE)
    return "#" + re.sub(r"[\s_]+", "-", low)


# ---------- поток ответа ----------

def _run_options(question: str, role_id: str, as_of: str | None,
                 session_id: str | None, answers: dict | None,
                 tone: str = "plain", business_line: str | None = None,
                 model: str | None = None):
    m = core_modules()["orchestrator"]
    RunOptions = getattr(m, "RunOptions", None)
    if RunOptions is None:
        raise RuntimeError("lex.orchestrator.RunOptions не найден")
    kwargs: dict[str, Any] = {
        "question": question,
        "role_id": role_id,
        "corpus_root": config.CORPUS_DIR,
        "corpus_norm_root": config.CORPUS_NORM_DIR,
        "cases_path": config.CASES_PATH,
        # без этого ядро само запишет тикеты/предложения в свои пути по умолчанию
        "tickets_path": config.TICKETS_PATH,
        "changes_path": config.CHANGE_REQUESTS_PATH,
    }
    if as_of:
        kwargs["as_of"] = dt.date.fromisoformat(as_of)
    if session_id:
        kwargs["session_id"] = session_id
    if answers:
        kwargs["answers"] = answers
    if tone in ("plain", "formal"):
        kwargs["tone"] = tone
    if business_line:
        kwargs["business_line"] = business_line
    if model:
        # §23: псевдоним модели из UI. Картинка в стиле 42: один из трёх
        # слотов NexusLink, отображаемый под своим именем. Дефолт — NEXUS_LINK_MODEL.
        kwargs["model"] = model
    if config.NO_LLM:
        # §12: без модели ответ собирается из цитат. Тон и оговорки при этом
        # остаются — их считает grounding, а не LLM.
        kwargs["no_llm"] = True
    return RunOptions(**kwargs)


def _detail_id(entry: dict) -> str:
    return "q-" + str(entry.get("id") or uuid.uuid4().hex[:6]).removeprefix("q-")


def _open_quality_ticket(data: dict, question: str, role_id: str,
                         business_line: str | None, session_id: str | None) -> dict:
    """Тикет в очередь службы качества из заявки (эскалации).

    Ядро в эскалации пишет заявку в escalations.jsonl (esc-NNNN), а очередь
    СК — это tickets.jsonl (LEX-YYYY-NNNN). Без тикета заявка не видна в
    почтовом ящике СК (/api/inbox читает только тикеты) и на неё нечем
    ответить.
    """
    from . import tickets as tickets_mod

    eid = str(data.get("ticket") or "")
    kind = str(data.get("kind") or "not_in_corpus")
    if kind not in ("not_in_corpus", "conflict_unresolved", "ocr_degraded"):
        kind = "not_in_corpus"
    rows = tickets_mod.list_tickets(limit=1000)
    for row in rows:
        if eid and str(row.get("escalation_id") or "") == eid:
            return row
    for row in rows:
        if row.get("status") != "closed" and \
                str(row.get("question") or "").strip() == question.strip():
            # Тикет уже открыт (агент открыл его при отказе) — привязываем заявку.
            if eid:
                try:
                    return tickets_mod.link_escalation(str(row.get("id") or ""), eid)
                except KeyError:
                    return row
            return row
    try:
        return tickets_mod.create_ticket({
            "question": question,
            "kind": kind,
            "session_id": session_id or "",
            "role_id": role_id,
            "business_line": business_line or "",
            "escalation_id": eid,
        })
    except Exception:
        return {}


_GATE_COVERAGE_RE = re.compile(r"предмет\s+(\d+)\s*%")
_GATE_COSINE_RE = re.compile(r"близость\s+([\d.,]+)")


def _step_record(data: dict) -> dict:
    """Шаг каскада для журнала: ключ, время, текст и — для поиска — метрики гейта.

    Раньше в журнал писалось только ``{key, ms}``, и аналитика гейта §12.8
    всегда показывала ноль: числа жили в тексте шага, а не в полях. Теперь
    из ``detail`` retrieve-шага вынимаются предмет и близость отдельными
    полями, поэтому /api/stats считает остановки гейта по факту, а не по
    надежде, что кто-то их допишет вручную.
    """
    rec: dict[str, Any] = {
        "key": data.get("key"),
        "ms": data.get("ms"),
        "detail": str(data.get("detail") or ""),
    }
    if rec["key"] == "retrieve":
        cov = _GATE_COVERAGE_RE.search(rec["detail"])
        cos = _GATE_COSINE_RE.search(rec["detail"])
        if cov:
            rec["subject_coverage"] = round(int(cov.group(1)) / 100, 2)
        if cos:
            try:
                rec["top_cosine"] = float(str(cos.group(1)).replace(",", "."))
            except ValueError:
                pass
    return rec


_LINE_EMPTY = frozenset({"", "—", "–", "-", "не определено", "не определено —", "нет"})
_LINE_RE = re.compile(r"направление:\s*([^·]+)")


def _line_from_policy_detail(detail: str) -> str:
    """Направление из detail шага policy. Пустая строка — направления нет.

    Ядро пишет «направление: —», когда направление не определено. Прочерк —
    это законный символ «нет», и записывать его в аналитику как направление
    значит завести направление по имени «—»: в разрезе появится строка,
    которой не существует.
    """
    m = _LINE_RE.search(str(detail or ""))
    if not m:
        return ""
    value = m.group(1).strip()
    return "" if value.lower() in _LINE_EMPTY else value


async def stream_answer(question: str, role_id: str, as_of: str | None,
                        session_id: str | None = None,
                        answers: dict | None = None,
                        tone: str = "plain",
                        business_line: str | None = None,
                        model: str | None = None) -> AsyncIterator[str]:
    """Поток SSE по сигналам ядра.

    Инварианты, которые держит этот слой:
      * шаг `escalate` (если пришёл) заменяет answer/refusal;
      * затем всегда done, кроме clarify — по §10 поток там закрывается;
      * при ошибке ядра — кадр error вместо финального события.
    """
    started = time.perf_counter()
    journal_id = "q-" + uuid.uuid4().hex[:10]
    # Счётчик токенов обнуляется на каждый вопрос: человеку нужен расход
    # именно этого запроса, а не накопленный за сессию. Модуль llm берём
    # через _try напрямую: core_modules() собирает только ядро, llm в нём нет.
    _llm = _try("lex.llm")
    if _llm is not None and hasattr(_llm, "_tokens_reset"):
        with contextlib.suppress(Exception):
            _llm._tokens_reset()
    inference_path = "nexus-link" if config.NEXUS_LINK_BASE_URL else "local"
    entry: dict[str, Any] = {
        "id": journal_id,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "role_id": role_id,
        "question": question,
        "refused": False,
        "elapsed_ms": 0,
        "steps": [],
        "doc_ids": [],
        "inference_path": inference_path,
        "as_of": as_of,
        "session_id": session_id,
        "tone": tone,
        "business_line": business_line,
        "model": model,
    }
    failed = False
    closed_without_done = False

    try:
        m = core_modules()["orchestrator"]
        if not core_ready():
            raise RuntimeError("ядро LEX (src/lex/orchestrator.py) недоступно")
        opts = _run_options(question, role_id, as_of, session_id, answers,
                            tone=tone, business_line=business_line, model=model)
        orch = m.Orchestrator(opts)

        async for signal in orch.run():
            kind = getattr(signal, "kind", "")
            data = _as_dict(getattr(signal, "data", {}))
            if kind == "step":
                rec = _step_record(data)
                entry["steps"].append(rec)
                # Направление определяет шаг policy, а не клиент: параметр
                # business_line в запросе почти всегда пуст, поэтому аналитика
                # видела «общее» на всех вопросах. Пишем то, что реально
                # определило ядро — из detail шага или из поля, если оно есть.
                if data.get("key") == "policy" and not entry.get("business_line"):
                    detected = data.get("business_line") or data.get("line") or ""
                    if not detected:
                        detected = _line_from_policy_detail(str(data.get("detail") or ""))
                    if detected and detected.lower() not in ("не определено", "нет"):
                        entry["business_line"] = detected
                if "subject_coverage" in rec or "top_cosine" in rec:
                    # Второй путь для аналитики: /api/stats читает и steps,
                    # и блок retrieval. Держим оба в согласии, чтобы счётчик
                    # не зависел от того, какое поле кто читает.
                    entry["retrieval"] = {
                        "subject_coverage": rec.get("subject_coverage"),
                        "top_cosine": rec.get("top_cosine"),
                    }
                yield sse.step(int(data.get("n", 0)), str(data.get("key", "")),
                               str(data.get("detail", "")), int(data.get("ms", 0) or 0),
                               total=int(data.get("total", 0) or 0) or None)
            elif kind == "answer":
                citations = [c for c in (data.get("citations") or [])
                             if isinstance(c, dict)]
                # §8: кейс — не документ. В doc_ids только нормативные
                # источники, иначе топ-источники и «где не хватает
                # документов» начинали бы считать кейсы регламентами.
                entry["doc_ids"] = sorted({
                    c.get("doc_id") for c in citations
                    if c.get("doc_id") and c.get("source_kind") != "case"
                })
                # §8/критерий 3: какой кейс реально попал в ответ. Без этого
                # поля аналитика дообучения показывает ноль, хотя ответ мог
                # быть построен только по практике. Цитата кейса несёт
                # source_kind="case" и case_id — берём из неё, а не из шага
                # cases: там все найденные, а в ответ попали не все.
                entry["cases_used"] = sorted({
                    str(c.get("case_id") or c.get("doc_id")) for c in citations
                    if c.get("source_kind") == "case" and (c.get("case_id") or c.get("doc_id"))
                })
                entry["refused"] = bool(data.get("refused", False))
                if data.get("detail_id"):
                    entry["detail_id"] = data["detail_id"]
                # §20.5: без этого аналитика оговорок всегда пустая
                caveat = data.get("caveat")
                if isinstance(caveat, dict) and caveat.get("level"):
                    entry["caveat_level"] = caveat["level"]
                    entry["caveat_reasons"] = [
                        r.get("code") for r in caveat.get("reasons") or []
                        if isinstance(r, dict) and r.get("code")
                    ]
                yield sse.answer(data)
            elif kind == "refusal":
                entry["refused"] = True
                entry["doc_ids"] = list(data.get("sources") or [])
                entry["refusal_reason"] = str(data.get("reason", ""))
                entry["refusal_kind"] = str(data.get("kind", ""))
                yield sse.refusal(data)
            elif kind == "clarify":
                entry["clarified"] = True
                closed_without_done = True
                yield sse.clarify(data)
            elif kind == "escalate":
                entry["escalated"] = True
                entry["ticket"] = data.get("ticket")
                # §22.2: карточка письма едет тем же событием
                from . import mail

                payload = dict(data)
                payload.setdefault("channel", "email")
                # §18: заявка должна лежать в очереди службы качества —
                # тикетом (LEX-YYYY-NNNN). Иначе СК её не видит: /api/inbox
                # читает только тикеты, а ядро пишет эскалацию в
                # escalations.jsonl.
                queue_ticket = _open_quality_ticket(
                    payload, question, role_id, business_line, session_id)
                if queue_ticket.get("id"):
                    payload["ticket_id"] = str(queue_ticket["id"])
                    entry["ticket_id"] = payload["ticket_id"]
                # §5 S3 заказчика: письмо должно РЕАЛЬНО уйти, а не нарисоваться.
                # Отправка не роняет ответ: сбой SMTP даёт delivery=failed, и
                # карточка честно говорит, что письмо не ушло.
                letter = mail.deliver({
                    "id": payload.get("ticket_id") or "",
                    "question": question,
                    "created_at": entry["ts"],
                    "why_unresolved": str(payload.get("reason") or ""),
                    "searched": payload.get("searched") or {},
                    "kind": payload.get("kind") or "",
                    "asker": {"role_id": role_id, "business_line": business_line or ""},
                })
                payload["email"] = letter
                # Факт доставки сохраняем в тикете. Иначе карточка заявки
                # собирается заново с дефолтом «simulated» и врёт: письмо
                # ушло по SMTP, а человек читает «демонстрационный режим».
                if payload.get("ticket_id") and letter.get("delivery"):
                    with contextlib.suppress(Exception):
                        from . import tickets as _tickets_mod
                        _tickets_mod._patch_ticket(
                            str(payload["ticket_id"]), delivery=letter["delivery"])
                yield sse.escalate(payload)
            elif kind == "error":
                raise RuntimeError(str(data.get("message", "ошибка ядра")))
            # done от ядра не транслируем: elapsed/journal_id наши (§2)
    except Exception as e:
        failed = True
        entry["error"] = str(e)
        yield sse.error(str(e), "orchestrator")

    elapsed = int((time.perf_counter() - started) * 1000)
    entry["elapsed_ms"] = elapsed
    tokens: dict | None = None
    if _llm is not None and hasattr(_llm, "tokens_total"):
        with contextlib.suppress(Exception):
            tokens = _llm.tokens_total()
            if tokens:
                entry["tokens"] = tokens
    if not failed and not closed_without_done:
        yield sse.done(elapsed, inference_path, journal_id, tokens)
    with contextlib.suppress(Exception):
        journal_append(entry)


# ---------- раскрытие ответа (§13.1) ----------

def expand(detail_id: str, section: str = "all", *,
           role_id: str | None = None,
           session_id: str | None = None) -> dict | None:
    """§13.1: развёрнутая версия ответа — только его владельцу.

    ``role_id`` и ``session_id`` передаются в ядро: деталь помнит, кто её
    создал, и чужой запрос получает ``None`` (HTTP-слой отдаёт 404 — не
    подтверждаем существование чужой детали).
    """
    m = core_modules()["orchestrator"]
    fn = getattr(m, "expand", None) if m is not None else None
    if fn is None:
        return None
    try:
        return fn(detail_id, section, role_id=role_id, session_id=session_id)
    except TypeError:
        # Старая сигнатура без владельца (урезанная сборка): не отдаём
        # деталь вовсе, чем отдаём её кому угодно. Fail-closed.
        return None
    except Exception:
        return None


# ---------- формы (§13.3-§13.4) ----------

def forms() -> list[dict]:
    m = core_modules()["orchestrator"]
    fn = getattr(m, "available_forms", None) if m is not None else None
    if fn is not None:
        try:
            return list(fn(config.CORPUS_DIR))
        except Exception:
            pass
    fm = core_modules()["forms"]
    if fm is not None and hasattr(fm, "load_forms"):
        try:
            return [f.to_dict() for f in fm.load_forms(config.CORPUS_DIR)]
        except Exception:
            pass
    return []


def form(form_id: str) -> dict | None:
    return next((f for f in forms() if f.get("form_id") == form_id), None)


def form_download(form_id: str) -> Path | None:
    """Файл бланка. Если в корпусе файла нет — собираем бланк из описания полей.

    Ядро (`download_form_path`) обещает бланк-описание, но возвращает None,
    когда рядом с `forms.json` нет файла. Пустой 404 тут хуже: форма есть,
    полей хватает на бланк, и это честная производная от данных корпуса,
    а не выдумка.
    """
    m = core_modules()["orchestrator"]
    fn = getattr(m, "download_form_path", None) if m is not None else None
    if fn is not None:
        try:
            path = fn(config.CORPUS_DIR, form_id)
        except Exception:
            path = None
        if path is not None and Path(path).is_file():
            return Path(path)
    return _form_blank(form_id)


def _form_blank(form_id: str) -> Path | None:
    data = form(form_id)
    if data is None:
        return None
    fields = data.get("fields") or []
    lines = [f"# {data.get('title') or form_id}", "",
             f"Документ-основание: {data.get('doc_id') or '—'}", ""]
    if fields:
        lines.append("| Поле | Тип | Обязательное | Подсказка |")
        lines.append("|---|---|---|---|")
        for f in fields:
            if not isinstance(f, dict):
                continue
            hint = f.get("hint") or f.get("pattern") or f.get("example") or ""
            lines.append(f"| {f.get('label') or f.get('key')} | {f.get('type', 'text')} "
                         f"| {'да' if f.get('required') else 'нет'} | {hint} |")
        lines.append("")
        lines.append("## Заполнение")
        lines.append("")
        for f in fields:
            if isinstance(f, dict):
                lines.append(f"- **{f.get('label') or f.get('key')}**: ______________________")
    else:
        lines.append("_Полей в описании формы нет._")
    text = "\n".join(lines) + "\n"

    config.ensure_dirs()
    out_dir = config.VAR_DIR / "form-blanks"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{form_id}.md"
    path.write_text(text, encoding="utf-8")
    return path


def form_fill(form_id: str, values: dict) -> dict | None:
    """Проверка заполнения силами ядра (`lex.forms.validate`)."""
    fm = core_modules()["forms"]
    if fm is None or not hasattr(fm, "get_form") or not hasattr(fm, "validate"):
        return None
    try:
        f = fm.get_form(config.CORPUS_DIR, form_id)
        if f is None:
            return None
        result = fm.validate(f, values, corpus=corpus())
        return _as_dict(result)
    except Exception:
        return None


# ---------- кейсы (§8) ----------

def case_store():
    m = core_modules()["cases"]
    if m is None or not hasattr(m, "get_case_store"):
        return None
    try:
        return m.get_case_store(config.CASES_PATH)
    except Exception:
        return None


def add_case(payload: dict) -> dict:
    store = case_store()
    if store is None:
        raise RuntimeError("lex.cases недоступен")

    ticket_id = str(payload.get("ticket_id") or "")
    # §16.3: кейс из заявки идёт своим входом — у него обязательны номер и
    # дата утверждения, и повторный вызов по той же заявке второй кейс не
    # создаёт. Обычный POST /api/cases (ручной кейс) работает через add().
    if ticket_id:
        case = store.add_from_ticket(
            ticket_id=ticket_id,
            question=str(payload.get("question") or ""),
            answer_in_practice=str(payload.get("answer_in_practice") or ""),
            author_role=str(payload.get("author_role") or "quality"),
            author_name=str(payload.get("author_name") or "служба качества"),
            tags=[str(x) for x in (payload.get("tags") or [])],
            rule_key=str(payload.get("rule_key") or ""),
            escalation_id=str(payload.get("escalation_id") or ""),
            approved_at=str(payload.get("approved_at") or ""),
            public=bool(payload.get("public") or False),
        )
        return {"id": case.id, "ok": True, "in_index": True}

    case = store.add(
        question=str(payload.get("question") or ""),
        answer_in_practice=str(payload.get("answer_in_practice") or ""),
        outcome=str(payload.get("outcome") or "open"),
        author_role=str(payload.get("author_role") or ""),
        author_name=str(payload.get("author_name") or ""),
        linked_docs=[str(x) for x in (payload.get("linked_docs") or [])],
        tags=[str(x) for x in (payload.get("tags") or [])],
        rule_key=str(payload.get("rule_key") or ""),
        content_type=str(payload.get("content_type") or "text"),
        # связь кейса с заявкой: source и escalation_id прокидываются дальше
        source=str(payload.get("source") or ""),
        escalation_id=str(payload.get("escalation_id") or ""),
        confirmed_by=str(payload.get("confirmed_by") or ""),
        approved_at=str(payload.get("approved_at") or ""),
        public=bool(payload.get("public") or False),
    )
    return {"id": case.id, "ok": True, "in_index": True}


def list_cases(limit: int = 50, role_id: str | None = None,
               tag: str | None = None, rule_key: str | None = None) -> dict:
    """Кейсы, которые роль вправе видеть, плюс честные числа отбора.

    ``role_id`` здесь не «чьи кейсы показать», а «кто смотрит»: выдача
    проходит через ``CaseStore.visible`` — то же правило, что в поиске
    оркестратора. Иначе API отдавал бы юристу кейсы другой роли, хотя в
    ответе на вопрос они ему уже не видны, — два разных ответа на один вопрос.

    **Пустая роль больше не значит «покажи все»**. До этого ветка ``else``
    звала ``store.find`` без фильтра, и ``GET /api/cases`` без параметра
    отдавал весь журнал — включая кейсы чужих отделов. Видимость кейса от
    параметра запроса зависеть не может: это дверь, а не настройка. HTTP-слой
    отказывает на пустой роли ещё до сюда (400), но функция тоже fail-closed —
    вызов из скрипта не должен становиться обходом.

    Форма ответа: ``{"cases": [...], "total": M, "available": N,
    "dropped": M-N, "by_reason": {"вне отдела": k}}`` — то же, что у
    ``filter_documents`` (§23), чтобы UI мог показать «доступно 9 из 25».
    """
    store = case_store()
    if store is None:
        return {"cases": [], "total": 0, "available": 0, "dropped": 0, "by_reason": {}}
    role = (role_id or "").strip().lower()
    if not role or not is_known_role(role):
        # Чужой id — не «все», а ничего: опечатка в роли не должна открывать
        # базу. HTTP-слой отвечает 400 с перечнем ролей, сюда попадает только
        # прямой вызов.
        return {"cases": [], "total": len(store.cases), "available": 0,
                "dropped": len(store.cases), "by_reason": {"неизвестная роль": len(store.cases)}}
    role = resolve_role_id(role)
    total = len(store.cases)
    cases = store.visible(role)
    if tag:
        low = tag.lower()
        cases = [c for c in cases if any(low in t.lower() for t in c.tags)]
    if rule_key:
        cases = [c for c in cases if c.rule_key == rule_key]
    cases = sorted(cases, key=lambda c: c.ts, reverse=True)
    # Дробление считается ДО limit: «отброшено по правам» — про видимость,
    # а не про то, сколько поместилось на страницу.
    dropped = total - len(cases)
    by_reason = {"вне отдела": dropped} if dropped else {}
    shown = cases[:limit]
    return {
        "cases": [c.to_dict() for c in shown],
        "total": total,
        "available": len(cases),
        "shown": len(shown),
        "dropped": dropped,
        "by_reason": by_reason,
    }


def cases_count() -> int:
    store = case_store()
    if store is None:
        return 0
    return len(store.cases)


def reject_case(case_id: str, reason: str = "") -> dict:
    """§8: отклонить кейс — он уходит из поиска немедленно (не из файла)."""
    store = case_store()
    if store is None:
        raise RuntimeError("lex.cases недоступен")
    case = store.reject(case_id, reason=reason)
    return {"id": case.id, "ok": True, "outcome": case.outcome, "in_index": False}


def remove_case(case_id: str) -> dict:
    """§8: удалить кейс совсем — строки в базе больше нет."""
    store = case_store()
    if store is None:
        raise RuntimeError("lex.cases недоступен")
    return {"id": case_id, "ok": store.remove(case_id), "in_index": False}


def learning_metrics() -> dict:
    """§16.6: метрика дообучения для аналитики.

    Три числа, которые жюри спрашивает про критерий 3:

    * ``approved_cases_count`` — сколько кейсов в базе сейчас работают
      (resolved/escalated/open; отклонённые не в счёт — они из поиска ушли);
    * ``cases_from_tickets`` — сколько кейсов пришло из заявок службы
      качества, а не написано вручную;
    * ``avg_ticket_to_case_hours`` — среднее время от создания заявки до
      утверждения кейса. Считается по ``approved_at`` кейса и ``created_at``
      заявки из tickets.jsonl, поэтому пары без любого из полей пропускаются
      (не выдумываем ноль).
    """
    store = case_store()
    if store is None:
        return {"approved_cases_count": 0, "cases_from_tickets": 0,
                "avg_ticket_to_case_hours": 0.0}
    cases = store.cases
    approved = [c for c in cases if c.outcome != "rejected"]
    from_tickets = [c for c in approved if c.ticket_id]

    created: dict[str, str] = {}
    try:
        from . import tickets as tickets_mod

        for row in tickets_mod.list_tickets(limit=100_000):
            created[str(row.get("id") or "")] = str(row.get("created_at") or "")
    except Exception:
        created = {}

    hours: list[float] = []
    for c in from_tickets:
        t0 = _iso_epoch(created.get(c.ticket_id, ""))
        t1 = _iso_epoch(c.approved_at)
        if t0 is not None and t1 is not None and t1 >= t0:
            hours.append((t1 - t0) / 3600.0)

    return {
        "approved_cases_count": len(approved),
        "cases_from_tickets": len(from_tickets),
        "avg_ticket_to_case_hours": round(sum(hours) / len(hours), 1) if hours else 0.0,
    }


def _iso_epoch(value: str) -> float | None:
    """Метка ``YYYY-MM-DDTHH:MM:SSZ`` -> секунды эпохи (UTC). None — если не метка."""
    import datetime as _dt

    try:
        return _dt.datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.UTC).timestamp()
    except Exception:
        return None
