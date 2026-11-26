"""LEX: журнал запросов и аналитика (контракт §§2, 6, 8, 9, 11 + v1.5).

Одна строка ``var/journal.jsonl`` = один запрос. Дозапись атомарна (один
``os.write`` под блокировкой), агрегаты считаются по файлу — отдельной БД нет.

Аналитика — не побочный продукт, а критерий «Соответствие задаче партнёра»:
``top_missing``/``missing_by_line`` показывают, чего нет в корпусе, а
``similar_questions`` с ``hot_missing`` — что спрашивают часто, а регламента нет.

Кластеризация (v1.5): эмбеддинги bge-m3 на :8081, косинус >= 0.82. Если сервис
эмбеддингов недоступен — честный лексический фолбэк с пометкой метода в
``clustering.method``. Считается по требованию (``recompute=True`` / ``?recompute=1``)
и кэшируется по числу записей + mtime файла.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import threading
import urllib.request
from collections import Counter
from copy import deepcopy
from datetime import UTC
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
JOURNAL_PATH = Path(os.environ.get("LEX_JOURNAL_PATH") or (ROOT / "var" / "journal.jsonl"))
CASES_PATH = Path(os.environ.get("LEX_CASES_PATH") or (ROOT / "var" / "cases.jsonl"))
MANIFEST_PATH = Path(os.environ.get("LEX_MANIFEST_PATH") or (ROOT / "sample-corpus" / "manifest.json"))
EMBED_URL = os.environ.get("LEX_EMBED_URL", "http://127.0.0.1:8081").rstrip("/")
EMBED_MODEL = os.environ.get("LEX_EMBED_MODEL", "bge-m3")
SIM_THRESHOLD = float(os.environ.get("LEX_SIM_THRESHOLD", "0.82"))
SIM_MIN_COUNT = int(os.environ.get("LEX_SIM_MIN_COUNT", "3"))
HOT_REFUSAL_RATE = float(os.environ.get("LEX_HOT_REFUSAL_RATE", "0.3"))
ESCALATIONS_PATH = Path(os.environ.get("LEX_ESCALATIONS_PATH") or (ROOT / "var" / "escalations.jsonl"))

# v1.15: чтение единое, запись только для службы качества и руководства.
WRITE_ROLES = frozenset({"quality", "director"})


def require_write(role: str | dict[str, Any] | None) -> bool:
    """Проверка прав на запись (кейсы, документы, формы) для эндпоинтов."""
    if isinstance(role, dict):
        rid = str(role.get("id") or "")
    else:
        rid = str(role or "")
    return rid in WRITE_ROLES

_WRITE_LOCK = threading.Lock()
_STATS_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}

CONTENT_FIELDS = {
    "text": "from_text",
    "case": "from_case",
    "image": "from_image",
    "scan": "from_scan",
    "mixed": "from_text",
}
# §23: no_access из чтения исчез. out_of_scope — главный отказ §17 (вопрос не
# нашей компетенции) — обязан быть в списке, иначе аналитика его не покажет.
REFUSAL_REASONS = (
    "not_in_corpus",
    "out_of_scope",
    "no_edition_in_force",
    "conflict_unresolved",
    "ocr_unavailable",
)
OUTCOMES = ("resolved", "escalated", "rejected", "open")

_SYNONYMS = {
    "сколько": "какой", "каков": "какой", "какая": "какой", "какие": "какой",
    "сроку": "срок", "сроки": "срок", "сроком": "срок",
    "согласовании": "согласование", "согласования": "согласование",
    "согласовать": "согласование", "утвердить": "согласование",
    "заявке": "заявка", "заявки": "заявка", "заявку": "заявка",
}
_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)


def now_iso() -> str:
    from datetime import datetime
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def append_entry(entry: dict[str, Any], path: str | Path | None = None) -> str:
    """Атомарно дописать один запрос. Возвращает id записи."""
    rec = dict(entry)
    rec.setdefault("id", "q-" + uuid4().hex[:10])
    rec.setdefault("ts", now_iso())
    rec.setdefault("role_id", "")
    rec.setdefault("question", "")
    rec.setdefault("refused", False)
    rec.setdefault("refusal_kind", None)
    rec.setdefault("elapsed_ms", 0)
    rec.setdefault("steps", [])
    rec.setdefault("doc_ids", [])
    rec.setdefault("inference_path", "")
    rec.setdefault("cases_used", [])
    rec.setdefault("content_types", [])
    line = json.dumps(rec, ensure_ascii=False) + "\n"

    target = Path(path) if path else JOURNAL_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    data = line.encode("utf-8")
    with _WRITE_LOCK:
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
    return str(rec["id"])


def read_entries(path: str | Path | None = None, limit: int | None = None,
                 role_id: str | None = None) -> list[dict[str, Any]]:
    """Прочесть журнал; битая строка не лишает доступа к остальным записям."""
    target = Path(path) if path else JOURNAL_PATH
    if not target.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and (not role_id or rec.get("role_id") == role_id):
            out.append(rec)
    return out[-limit:] if limit else out


def _read_cases(path: str | Path | None = None) -> list[dict[str, Any]]:
    target = Path(path) if path else CASES_PATH
    if not target.exists():
        return []
    out = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            if isinstance(rec, dict):
                out.append(rec)
        except json.JSONDecodeError:
            continue
    return out


def _read_escalations(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Тикеты/эскалации (§18) — те же jsonl, без отдельной БД."""
    target = Path(path) if path else ESCALATIONS_PATH
    if not target.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            if isinstance(rec, dict):
                out.append(rec)
        except json.JSONDecodeError:
            continue
    return out


def _line_for_doc(doc: dict[str, Any]) -> str:
    """Направление из данных: поле business_line, иначе выводим из system/title."""
    if doc.get("business_line"):
        return str(doc["business_line"])
    text = " ".join(str(doc.get(k) or "") for k in ("title", "system", "kind")).lower()
    rules = [
        ("закупки", ("закупк", "заявк", "поставщ")),
        ("лицензирование", ("лиценз", "дело", "регламент", "сертиф")),
        ("качество", ("качеств", "выплат", "проверк", "приёмк")),
        ("хранение", ("хранен", "склад", "логистик")),
        ("персонал", ("персонал", "охрана", "пропуск", "сreset")),
    ]
    for line, needles in rules:
        if any(n in text for n in needles):
            return line
    return "общее"


def _doc_lines() -> dict[str, str]:
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        str(d.get("doc_id")): _line_for_doc(d)
        for d in (manifest.get("documents") or []) if isinstance(d, dict)
    }


def _question_lines(question: str) -> list[str]:
    text = (question or "").lower()
    return [line for line, needles in (
        ("закупки", ("закупк", "заявк", "поставщ")),
        ("лицензирование", ("лиценз", "продукт x", "сертиф")),
        ("качество", ("качеств", "проверк", "приёмк", "брак")),
        ("хранение", ("хранен", "склад", "логистик")),
        ("персонал", ("персонал", "охрана", "пропуск")),
    ) if any(n in text for n in needles)] or ["общее"]


def normalize_question(text: str) -> str:
    """Нижний регистр, пунктуация, синонимы — каноническая форма для кластера."""
    lower = (text or "").lower()
    clean = _PUNCT.sub(" ", lower)
    words = [_SYNONYMS.get(w, w) for w in clean.split()]
    return " ".join(w for w in words if w)


def _embed(texts: list[str]) -> list[list[float]] | None:
    """Эмбеддинги bge-m3 через :8081; None — сервис недоступен."""
    payload = json.dumps({"model": EMBED_MODEL, "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        f"{EMBED_URL}/v1/embeddings", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        vectors = [row.get("embedding") for row in (body.get("data") or [])]
        if len(vectors) != len(texts) or not all(isinstance(v, list) and v for v in vectors):
            return None
        return vectors
    except Exception:
        return None


def _lexical_vectors(texts: list[str]) -> list[list[float]]:
    """Детерминированный фолбэк: TF по словам + триграммы, L2-нормировка."""
    dim = 512
    out = []
    for text in texts:
        vec = [0.0] * dim
        toks = normalize_question(text).split()
        for word in toks:
            vec[hash(word) % dim] += 1.0
        for word in toks:
            for i in range(len(word) - 2):
                vec[hash(word[i:i + 3]) % dim] += 0.3
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        out.append([x / norm for x in vec])
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(n))
    na = math.sqrt(sum(x * x for x in a[:n]))
    nb = math.sqrt(sum(x * x for x in b[:n]))
    return dot / (na * nb) if na and nb else 0.0


def _clusters(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Группировка похожих вопросов. Порог 0.82, минимум 3 записи в кластере.

    0.82 выбран как компромисс:質問 из sample-corpus — парафразы одного
    документа, но между разными документами близость ниже; более низкий порог
    склеивает разные регламенты, более высокий разбивает парафразы.
    """
    questions = [str(e.get("question") or "") for e in entries]
    if not questions:
        return [], {"method": "none", "threshold": SIM_THRESHOLD, "min_count": SIM_MIN_COUNT}
    vectors = _embed(questions)
    method = "embeddings" if vectors else "lexical-fallback"
    if vectors is None:
        vectors = _lexical_vectors(questions)

    reps: list[list[float]] = []
    groups: list[list[int]] = []
    for idx, vec in enumerate(vectors):
        best, best_i = -1.0, -1
        for gi, rep in enumerate(reps):
            sim = _cosine(vec, rep)
            if sim > best:
                best, best_i = sim, gi
        if best_i >= 0 and best >= SIM_THRESHOLD:
            groups[best_i].append(idx)
        else:
            reps.append(vec)
            groups.append([idx])

    result = []
    for members in groups:
        if len(members) < SIM_MIN_COUNT:
            continue
        variants = sorted({questions[i] for i in members if questions[i]}, key=lambda s: (len(s), s))
        forms = Counter(normalize_question(questions[i]) for i in members if questions[i])
        canonical = sorted(forms.items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]))[0][0]
        refusals = sum(1 for i in members if entries[i].get("refused"))
        rate = round(refusals / len(members), 3)
        cases: list[str] = []
        for i in members:
            for cid in (entries[i].get("cases_used") or []):
                if cid not in cases:
                    cases.append(str(cid))
        stamps = sorted(str(entries[i].get("ts") or "") for i in members if entries[i].get("ts"))
        result.append({
            "canonical": canonical or variants[0] if variants else canonical,
            "count": len(members),
            "variants": variants[:5],
            "last_seen": stamps[-1] if stamps else None,
            "refusal_rate": rate,
            "hot_missing": rate >= HOT_REFUSAL_RATE,
            "related_cases": cases[:10],
        })
    result.sort(key=lambda x: (-x["count"], x["canonical"]))
    return result, {"method": method, "threshold": SIM_THRESHOLD, "min_count": SIM_MIN_COUNT}


def aggregate(path: str | Path | None = None, *, recompute: bool = False) -> dict[str, Any]:
    """Публичное имя агрегатора для HTTP-слоя (§2, §20.5).

    ``src/api/core.analytics()`` ищет именно ``aggregate``: у ядра функция
    исторически называется ``stats``, и без этого имени слой молча уходил в
    упрощённый фолбэк — без `refusals_by_reason`, `answer_rate` и кластеров.
    Один алиас дешевле, чем переименование, которое тронет вызывающих.
    """
    return stats(path, recompute=recompute)


def stats(path: str | Path | None = None, *, recompute: bool = False,
          limit: int | None = None) -> dict[str, Any]:
    """Все агрегаты GET /api/analytics. Кэш по числу записей + mtime файла."""
    target = Path(path) if path else JOURNAL_PATH
    entries = read_entries(target, limit=limit)
    stamp = target.stat().st_mtime_ns if target.exists() else 0
    key = (str(target), len(entries), stamp, limit)
    if not recompute and key in _STATS_CACHE:
        return deepcopy(_STATS_CACHE[key])

    total = len(entries)
    refusals = sum(1 for e in entries if e.get("refused"))
    latencies = [float(e.get("elapsed_ms") or 0) for e in entries]
    avg = round(sum(latencies) / total, 1) if total else 0.0
    p95 = 0.0
    if latencies:
        ordered = sorted(latencies)
        p95 = round(ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)], 1)

    missing: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    by_role: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    content: Counter[str] = Counter({k: 0 for k in CONTENT_FIELDS.values()})
    case_uses: Counter[str] = Counter()
    line_counts: Counter[str] = Counter()
    line_refusals: Counter[str] = Counter()
    unanswered: list[dict[str, Any]] = []
    doc_lines = _doc_lines()

    for e in entries:
        role = str(e.get("role_id") or "")
        by_role[role] += 1
        docs = [str(x) for x in (e.get("doc_ids") or [])]
        for d in docs:
            sources[d] += 1
            line_counts[doc_lines.get(d, "общее")] += 1
        if e.get("refused"):
            reasons[str(e.get("refusal_kind") or "not_in_corpus")] += 1
            topic = str(e.get("topic") or e.get("refusal_topic") or e.get("question") or "")[:120]
            if topic:
                missing[topic] += 1
            lines = e.get("business_lines") or _question_lines(str(e.get("question") or ""))
            for line in lines:
                line_refusals[str(line)] += 1
            unanswered.append({"question": str(e.get("question") or ""), "ts": str(e.get("ts") or "")})
        for ct in (e.get("content_types") or []):
            key_ct = CONTENT_FIELDS.get(str(ct))
            if key_ct:
                content[key_ct] += 1
        if docs and not content["from_text"] and not (e.get("content_types") or []):
            content["from_text"] += len(docs)
        for cid in (e.get("cases_used") or []):
            case_uses[str(cid)] += 1

    cases = _read_cases()
    case_outcomes = Counter({k: 0 for k in OUTCOMES})
    case_roles: Counter[str] = Counter()
    for c in cases:
        case_outcomes[str(c.get("outcome") or "open")] += 0  # ensured below
        case_outcomes[str(c.get("outcome") or "open")] += 1
        case_roles[str(c.get("author_role") or "")] += 1

    # v1.12 — оговорки и честный answer_rate
    note_levels: Counter[str] = Counter()
    note_reasons: Counter[str] = Counter()
    plain = caveated = 0
    for e in entries:
        if e.get("refused"):
            continue
        level = e.get("caveat_level")
        if level:
            caveated += 1
            note_levels[str(level)] += 1
            for reason in (e.get("caveat_reasons") or []):
                note_reasons[str(reason)] += 1
        else:
            plain += 1
    answer_rate = {"plain": plain, "caveated": caveated, "refused": refusals}
    caveats = {
        "total": caveated,
        "by_level": {k: note_levels.get(k, 0) for k in ("low", "medium", "high")},
        "by_reason": [{"code": k, "count": v} for k, v in note_reasons.most_common(10)],
        "answer_rate": answer_rate,
    }

    # v1.10/§18 — тикеты из var/escalations.jsonl, top_topics = «чего нет в каталоге»
    escs = _read_escalations()
    esc_status = Counter(str(e.get("status") or "open") for e in escs)
    esc_lines = Counter(str(e.get("business_line") or "общее") for e in escs)
    esc_topics: Counter[str] = Counter()
    hours: list[float] = []
    for esc in escs:
        why = str(esc.get("why_unresolved") or "")
        found = re.search(r"<([^>]+)>", why)
        topic = (found.group(1) if found else str(esc.get("question") or ""))[:80]
        if topic:
            esc_topics[topic] += 1
        if esc.get("answered_at") and esc.get("ts"):
            try:
                from datetime import datetime
                t0 = datetime.fromisoformat(str(esc["ts"]).replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(str(esc["answered_at"]).replace("Z", "+00:00"))
                hours.append((t1 - t0).total_seconds() / 3600)
            except ValueError:
                pass
    tickets = {
        "total": len(escs),
        "open": esc_status.get("open", 0),
        "assigned": esc_status.get("assigned", 0),
        "answered": esc_status.get("answered", 0),
        "closed": esc_status.get("closed", 0),
        "by_business_line": [{"line": k, "count": v} for k, v in esc_lines.most_common()],
        "avg_hours_to_assign": 0.0,  # отдельного поля времени назначения в журнале нет
        "avg_hours_to_answer": round(sum(hours) / len(hours), 2) if hours else 0.0,
        "sla_breached": 0,
        "top_topics": [{"topic": k, "count": v} for k, v in esc_topics.most_common(10)],
    }

    clusters, cluster_meta = _clusters(entries)
    out: dict[str, Any] = {
        "total": total,
        "refusals": refusals,
        "refusal_rate": round(refusals / total, 3) if total else 0.0,
        "avg_latency_ms": avg,
        "p95_latency_ms": p95,
        "top_missing": [{"topic": k, "count": v} for k, v in missing.most_common(10)],
        "top_sources": [{"doc_id": k, "count": v} for k, v in sources.most_common(10)],
        "by_role": [{"role_id": k, "count": v} for k, v in by_role.most_common()],
        "unanswered": unanswered[:20],
        # v1.1 — типы источников и кейсы
        "sources_by_type": dict(content),
        "refusals_by_reason": {k: reasons.get(k, 0) for k in REFUSAL_REASONS},
        "cases_total": len(cases),
        "cases_by_role": [{"role_id": k, "count": v} for k, v in case_roles.most_common()],
        "cases_by_outcome": dict(case_outcomes),
        "top_cases": [{"id": k, "count": v} for k, v in case_uses.most_common(10)],
        # v1.3 — бизнес-направления
        "by_business_line": [
            {"line": line, "count": line_counts.get(line, 0), "refusals": line_refusals.get(line, 0)}
            for line in sorted(set(line_counts) | set(line_refusals))
        ],
        "missing_by_line": [
            {"line": line, "count": count, "note": "чаще всего нет документа"}
            for line, count in line_refusals.most_common()
        ],
        # v1.5 — кластеризация (по запросу/с кэшем)
        "similar_questions": clusters,
        "clustering": cluster_meta,
        # v1.10 / v1.12 / v1.15 — тикеты, оговорки, честный answer_rate
        "tickets": tickets,
        "caveats": caveats,
        "answer_rate": answer_rate,
        "write_roles": sorted(WRITE_ROLES),
    }
    _STATS_CACHE[key] = deepcopy(out)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LEX journal: запись и аналитика")
    ap.add_argument("--stats", action="store_true", help="печать агрегатов GET /api/analytics")
    ap.add_argument("--append", metavar="JSON", help="дописать одну запись (JSON)")
    ap.add_argument("--recompute", action="store_true", help="пересчитать кластеризацию")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)

    if args.append:
        entry = json.loads(args.append)
        print(json.dumps({"ok": True, "id": append_entry(entry)}, ensure_ascii=False))
        return 0

    print(json.dumps(stats(recompute=args.recompute, limit=args.limit), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
