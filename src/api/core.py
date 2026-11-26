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
import time
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from lex.index import reset_index

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


def corpus():
    """Загруженный корпус ядра (кэш по корневому каталогу)."""
    key = str(config.CORPUS_DIR.resolve())
    if key in _corpus_cache:
        return _corpus_cache[key]
    m = core_modules()["corpus"]
    if m is None or not hasattr(m, "load_corpus"):
        return None
    try:
        c = m.load_corpus(config.CORPUS_DIR)
    except Exception:
        return None
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
    docs = manifest().get("documents") or []
    return [d for d in docs if isinstance(d, dict)]


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
    """Роли из §2 контракта; отделы — из корпуса."""
    c = corpus()
    if c is not None and hasattr(c, "departments"):
        try:
            departments = list(c.departments())
        except Exception:
            departments = []
    else:
        departments = sorted({d.get("department") for d in manifest_documents()
                              if d.get("department")})

    def pick(pred: Callable[[str], bool]) -> list[str]:
        return [x for x in departments if pred(x.lower())]

    return [
        {"id": "lawyer", "title": "Юридический отдел",
         "departments": pick(lambda s: "юрид" in s), "systems": []},
        {"id": "quality", "title": "Отдел качества",
         "departments": pick(lambda s: "качеств" in s), "systems": []},
        {"id": "it", "title": "ИТ-служба",
         "departments": pick(lambda s: s.startswith("ит")), "systems": []},
        {"id": "director", "title": "Руководитель",
         "departments": ["*"], "systems": ["*"]},
    ]


def roles() -> list[dict]:
    m = core_modules()["access"]
    if m is not None and hasattr(m, "roles"):
        try:
            r = m.roles()
            if isinstance(r, list) and r:
                return r
        except Exception:
            pass
    return default_roles()


def role_ids() -> set[str]:
    return {r.get("id") for r in roles()}


def role_departments(role_id: str) -> list[str]:
    for r in roles():
        if r.get("id") == role_id:
            return list(r.get("departments") or [])
    return []


def role_can_read(role_id: str, department: str) -> bool:
    deps = role_departments(role_id)
    return "*" in deps or not deps or department in deps


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
    return next((d for d in manifest_documents() if d.get("doc_id") == doc_id), None)


def doc_path(doc_id: str) -> Path | None:
    meta = doc_meta(doc_id)
    if meta and meta.get("file"):
        p = config.CORPUS_DIR / str(meta["file"])
        if p.is_file():
            return p
    for p in sorted(config.CORPUS_DIR.iterdir()):
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
                 tone: str = "plain", business_line: str | None = None):
    m = core_modules()["orchestrator"]
    RunOptions = getattr(m, "RunOptions", None)
    if RunOptions is None:
        raise RuntimeError("lex.orchestrator.RunOptions не найден")
    kwargs: dict[str, Any] = {
        "question": question,
        "role_id": role_id,
        "corpus_root": config.CORPUS_DIR,
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
    if config.NO_LLM:
        # §12: без модели ответ собирается из цитат. Тон и оговорки при этом
        # остаются — их считает grounding, а не LLM.
        kwargs["no_llm"] = True
    return RunOptions(**kwargs)


def _detail_id(entry: dict) -> str:
    return "q-" + str(entry.get("id") or uuid.uuid4().hex[:6]).removeprefix("q-")


async def stream_answer(question: str, role_id: str, as_of: str | None,
                        session_id: str | None = None,
                        answers: dict | None = None,
                        tone: str = "plain",
                        business_line: str | None = None) -> AsyncIterator[str]:
    """Поток SSE по сигналам ядра.

    Инварианты, которые держит этот слой:
      * шаг `escalate` (если пришёл) заменяет answer/refusal;
      * затем всегда done, кроме clarify — по §10 поток там закрывается;
      * при ошибке ядра — кадр error вместо финального события.
    """
    started = time.perf_counter()
    journal_id = "q-" + uuid.uuid4().hex[:10]
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
    }
    failed = False
    closed_without_done = False

    try:
        m = core_modules()["orchestrator"]
        if not core_ready():
            raise RuntimeError("ядро LEX (src/lex/orchestrator.py) недоступно")
        opts = _run_options(question, role_id, as_of, session_id, answers,
                            tone=tone, business_line=business_line)
        orch = m.Orchestrator(opts)

        async for signal in orch.run():
            kind = getattr(signal, "kind", "")
            data = _as_dict(getattr(signal, "data", {}))
            if kind == "step":
                entry["steps"].append({"key": data.get("key"), "ms": data.get("ms")})
                yield sse.step(int(data.get("n", 0)), str(data.get("key", "")),
                               str(data.get("detail", "")), int(data.get("ms", 0) or 0))
            elif kind == "answer":
                entry["doc_ids"] = sorted({
                    c.get("doc_id") for c in (data.get("citations") or [])
                    if isinstance(c, dict) and c.get("doc_id")
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
                payload["email"] = mail.email_block({
                    "question": question,
                    "created_at": entry["ts"],
                    "asker": {"role_id": role_id, "business_line": business_line or ""},
                })
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
    if not failed and not closed_without_done:
        yield sse.done(elapsed, inference_path, journal_id)
    with contextlib.suppress(Exception):
        journal_append(entry)


# ---------- раскрытие ответа (§13.1) ----------

def expand(detail_id: str, section: str = "all") -> dict | None:
    m = core_modules()["orchestrator"]
    fn = getattr(m, "expand", None) if m is not None else None
    if fn is None:
        return None
    try:
        return fn(detail_id, section)
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
    )
    return {"id": case.id, "ok": True, "in_index": True}


def list_cases(limit: int = 50, role_id: str | None = None,
               tag: str | None = None, rule_key: str | None = None) -> list[dict]:
    store = case_store()
    if store is None:
        return []
    return [c.to_dict() for c in store.find(limit=limit, role_id=role_id,
                                            tag=tag, rule_key=rule_key)]


def cases_count() -> int:
    store = case_store()
    if store is None:
        return 0
    return len(store.cases)
