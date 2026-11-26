"""Адаптер между HTTP-слоем и ядром src/lex.

Здесь НЕТ логики RAG: только поиск модулей ядра и приведение их ответов к форме,
которую ждёт SSE. Пока ядро не написано (FORGE), используется заглушка —
она отдаёт все шаги и честный ответ-плейсхолдер, чтобы контракт SSE можно
было проверить end-to-end.

Ожидаемый интерфейс ядра (согласовать с FORGE):
    lex.orchestrator.stream(question, role_id, as_of) -> AsyncIterator[(event, data)]
        event ∈ {"step","answer","refusal","error"}; data — как в §3 контракта.
    lex.corpus.stats() -> {"documents": int, "chunks": int}
    lex.corpus.reload() -> {"documents": int, "chunks": int, "added": [str]}
    lex.access.roles() -> list[dict]   (форма §2 /api/roles)
    lex.journal.append(entry: dict) -> None
    lex.journal.read(limit: int, role_id: str | None) -> list[dict]
    lex.journal.aggregate() -> dict    (форма §2 /api/analytics)
    lex.cases.* -> см. api/cases.py
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import time
import uuid
from typing import Any, AsyncIterator, Callable

from . import config, sse
from .sse import step_keys

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
        "access": _try("lex.access"),
        "journal": _try("lex.journal"),
        "cases": _try("lex.cases"),
    }


def core_ready() -> bool:
    m = core_modules()
    return m["orchestrator"] is not None and m["corpus"] is not None


# ---------- корпус (метаданные манифеста; не RAG) ----------

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


def has_nontext() -> bool:
    mix = content_type_mix()
    return any(mix[k] for k in ("image", "scan", "mixed"))


def corpus_stats() -> dict:
    m = core_modules()["corpus"]
    if m is not None and hasattr(m, "stats"):
        try:
            s = m.stats()
            if isinstance(s, dict):
                return {"documents": int(s.get("documents", 0)),
                        "chunks": int(s.get("chunks", 0))}
        except Exception:
            pass
    return {"documents": len(manifest_documents()), "chunks": 0}


# ---------- роли ----------

def default_roles() -> list[dict]:
    """Роли из §2 контракта; отделы — из manifest.json."""
    docs = manifest_documents()
    departments = sorted({d.get("department") for d in docs if d.get("department")})

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


# ---------- журнал ----------

def _journal_path():
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
    if m is not None and hasattr(m, "reload"):
        try:
            r = m.reload()
            if isinstance(r, dict):
                return {
                    "ok": True,
                    "documents": int(r.get("documents", 0)),
                    "chunks": int(r.get("chunks", 0)),
                    "added": list(r.get("added") or []),
                }
        except Exception as e:  # noqa: BLE001 — ошибку ядра отдаём как есть
            return {"ok": False, "documents": 0, "chunks": 0, "added": [], "error": str(e)}
    return {"ok": True, "documents": len(manifest_documents()), "chunks": 0,
            "added": [], "stub": True}


# ---------- поток ответа ----------

STUB_STEP_DELAY = float(os.environ.get("LEX_STUB_STEP_DELAY", "0.12"))


def _stub_detail(key: str, question: str, role_id: str, as_of: str | None) -> str:
    n = len(manifest_documents())
    mix = content_type_mix()
    return {
        "accept": f"роль {role_id}, дата отсчёта {as_of or 'сегодня'}",
        "decompose": "1 подвопрос: " + question[:80],
        "inspect": (f"текст {mix['text']} · картинки {mix['image']} · "
                    f"сканы {mix['scan']} · смешанные {mix['mixed']}"),
        "retrieve": f"документов {n}, кейсов 0 · вектор 0 → топ 0",
        "cases": "найдено 0 кейсов, из них по теме 0",
        "permissions": f"доступно 0 из {n}, отброшено {n}",
        "resolve": "0 редакций, действующей нет",
        "conflict": "конфликтов: 0",
        "compose": "из 0 цитат",
        "verify": "покрыто 0 из 0 утверждений",
    }.get(key, "")


async def _stub_stream(question: str, role_id: str, as_of: str | None,
                       keys: list[str]) -> AsyncIterator[CoreEvent]:
    """Заглушка: все шаги плана по порядку, затем честный ответ-плейсхолдер."""
    for i, key in enumerate(keys, start=1):
        await asyncio.sleep(STUB_STEP_DELAY)
        yield ("step", {"n": i, "key": key,
                        "detail": _stub_detail(key, question, role_id, as_of),
                        "ms": int(STUB_STEP_DELAY * 1000)})
    yield ("answer", {
        "text": "Ядро LEX (src/lex) ещё не подключено — это ответ HTTP-слоя-заглушки. "
                "SSE-контракт при этом соблюдён: шаги, затем answer, затем done.",
        "citations": [],
        "refused": False,
        "stub": True,
    })


def _core_stream(question: str, role_id: str, as_of: str | None) -> AsyncIterator[CoreEvent]:
    m = core_modules()["orchestrator"]
    fn = getattr(m, "stream", None)
    if fn is None:
        raise RuntimeError("lex.orchestrator.stream не найден")
    return fn(question, role_id, as_of)


async def stream_answer(question: str, role_id: str, as_of: str | None) -> AsyncIterator[str]:
    """Единая точка: поток кадров SSE с гарантией завершения.

    Инварианты, которые держит именно этот слой:
      * шаги идут в фиксированном порядке плана (пропущенные дополняются);
      * ровно одно answer ИЛИ refusal;
      * затем всегда done; при ошибке — event: error вместо answer/refusal.
    """
    started = time.perf_counter()
    journal_id = "q-" + uuid.uuid4().hex[:10]
    inference_path = "nexus-link" if config.NEXUS_LINK_BASE_URL else "local"
    include_inspect = has_nontext()
    keys = step_keys(include_inspect)
    emitted = 0
    terminal_sent = False
    failed = False
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
    }

    def fill_until(upto: int) -> list[str]:
        nonlocal emitted
        out = []
        ms = int((time.perf_counter() - started) * 1000)
        for idx in range(emitted, upto):
            out.append(sse.step(idx + 1, keys[idx], "шаг не сообщён ядром", ms,
                                include_inspect))
        emitted = max(emitted, upto)
        return out

    def fill_rest() -> list[str]:
        return fill_until(len(keys))

    try:
        source = (_core_stream(question, role_id, as_of) if core_ready()
                  else _stub_stream(question, role_id, as_of, keys))
        async for event, data in source:
            if event == "step":
                key = str(data.get("key", ""))
                if key not in keys:
                    continue
                n = keys.index(key) + 1
                if n <= emitted:
                    continue  # повтор/перестановка: порядок фиксирован контрактом
                for extra in fill_until(n - 1):
                    yield extra
                emitted = n
                ms = int(data.get("ms", 0) or 0)
                entry["steps"].append({"key": key, "ms": ms})
                yield sse.step(n, key, str(data.get("detail", "")), ms, include_inspect)
            elif event == "answer":
                for extra in fill_rest():
                    yield extra
                entry["refused"] = bool(data.get("refused", False))
                entry["doc_ids"] = sorted({
                    c.get("doc_id") for c in (data.get("citations") or [])
                    if isinstance(c, dict) and c.get("doc_id")
                })
                yield sse.answer(str(data.get("text", "")),
                                 list(data.get("citations") or []),
                                 bool(data.get("refused", False)),
                                 list(data.get("assumptions") or []) or None)
                terminal_sent = True
            elif event == "refusal":
                for extra in fill_rest():
                    yield extra
                entry["refused"] = True
                entry["doc_ids"] = list(data.get("sources") or [])
                entry["refusal_reason"] = str(data.get("reason", ""))
                entry["refusal_kind"] = str(data.get("kind", ""))
                yield sse.refusal(str(data.get("reason", "")),
                                  list(data.get("sources") or []),
                                  str(data.get("kind", "not_in_corpus")))
                terminal_sent = True
            elif event == "error":
                raise RuntimeError(str(data.get("message", "ошибка ядра")))
        if not terminal_sent:
            for extra in fill_rest():
                yield extra
            entry["refused"] = True
            entry["refusal_kind"] = "not_in_corpus"
            entry["refusal_reason"] = "ядро не вернуло answer или refusal"
            yield sse.refusal("ядро не вернуло answer или refusal", [], "not_in_corpus")
    except Exception as e:  # noqa: BLE001 — ошибку отдаём клиенту кадром error
        failed = True
        entry["error"] = str(e)
        yield sse.error(str(e), "orchestrator")

    elapsed = int((time.perf_counter() - started) * 1000)
    entry["elapsed_ms"] = elapsed
    if not failed:
        yield sse.done(elapsed, inference_path, journal_id)
    try:
        journal_append(entry)
    except Exception:
        pass
