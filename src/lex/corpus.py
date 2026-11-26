"""Чтение корпуса и чанкинг.

Корпус — ``sample-corpus/``: ``manifest.json`` + markdown-документы. Чанк режется
по секциям (``## Заголовок``), внутри секции — скользящим окном ~600 токенов с
перекрытием 80. Так делается потому, что ответ почти всегда лежит внутри одной
секции: «Правило SR-01», «Приёмочные критерии». Резать документ целиком значило
бы размывать BM25 по девяти секциям сразу.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import Chunk, CorpusStats, DocumentMeta, Rule, RuleRevision

# Оценка токенов: у bge-m3 токенизатор XLM-R, для русского ~3 символа на токен.
# Точный токенизатор не тянем — это оценка размера окна, а не биллинг.
CHARS_PER_TOKEN = 3.0
CHUNK_TOKENS = 600
CHUNK_OVERLAP_TOKENS = 80

HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / CHARS_PER_TOKEN))


@dataclass
class Corpus:
    """Загруженный корпус: документы, чанки, правила."""

    root: Path
    documents: list[DocumentMeta] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)
    rules: dict[str, Rule] = field(default_factory=dict)
    watermark: str = ""
    as_of: str = ""

    @property
    def stats(self) -> CorpusStats:
        return CorpusStats(documents=len(self.documents), chunks=len(self.chunks))

    def doc(self, doc_id: str) -> DocumentMeta | None:
        return next((d for d in self.documents if d.doc_id == doc_id), None)

    def departments(self) -> list[str]:
        """Отделы из манифеста — контракт §2 GET /api/roles."""
        seen: list[str] = []
        for d in self.documents:
            if d.department and d.department not in seen:
                seen.append(d.department)
        return seen


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Режет markdown на (заголовок, тело). Текст до первого ``##`` — «Преамбула»."""
    matches = list(HEADING_RE.finditer(text))
    if not matches:
        return [("", text.strip())] if text.strip() else []
    out: list[tuple[str, str]] = []
    head = text[: matches[0].start()].strip()
    if head:
        out.append(("", head))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end() : end].strip()
        if body:
            out.append((m.group(1).strip(), body))
    return out


def _window(text: str, limit: int, overlap: int) -> list[str]:
    """Скользящее окно по абзацам: ~limit токенов, перекрытие overlap."""
    if estimate_tokens(text) <= limit:
        return [text]
    paras = [p for p in re.split(r"\n{2,}", text) if p.strip()]
    limit_chars = int(limit * CHARS_PER_TOKEN)
    overlap_chars = int(overlap * CHARS_PER_TOKEN)
    out: list[str] = []
    cur = ""
    for p in paras:
        candidate = f"{cur}\n\n{p}" if cur else p
        if len(candidate) <= limit_chars:
            cur = candidate
            continue
        if cur:
            out.append(cur)
            tail = cur[-overlap_chars:] if overlap_chars and len(cur) > overlap_chars else cur
            cur = f"{tail}\n\n{p}" if tail else p
        else:
            # Один абзац длиннее окна — режем по символам, иначе потеряем текст.
            for i in range(0, len(p), limit_chars):
                out.append(p[i : i + limit_chars])
            cur = ""
    if cur:
        out.append(cur)
    return out


def _doc_meta(d: dict) -> DocumentMeta:
    return DocumentMeta(
        doc_id=str(d.get("doc_id") or ""),
        title=str(d.get("title") or ""),
        kind=str(d.get("kind") or ""),
        subtype=str(d.get("subtype") or ""),
        department=str(d.get("department") or ""),
        system=str(d.get("system") or ""),
        owner=str(d.get("owner") or ""),
        version=str(d.get("version") or ""),
        issued=str(d.get("issued") or ""),
        rule_key=str(d.get("rule_key") or ""),
        file=str(d.get("file") or ""),
        sha256=str(d.get("sha256") or ""),
        words=int(d.get("words") or 0),
        sections=[str(s) for s in (d.get("sections") or [])],
    )


def load_corpus(
    root: str | Path,
    *,
    chunk_tokens: int = CHUNK_TOKENS,
    overlap_tokens: int = CHUNK_OVERLAP_TOKENS,
) -> Corpus:
    """Читает manifest и документы, строит чанки.

    Отсутствующий файл документа не роняет загрузку: документ остаётся в
    манифесте (он нужен для прав и для отказов), но чанков не даёт. Пропажа
    файла — единственная ошибка, которую тут стоит терпеть: корпус приходит
    от заказчика и может быть неполным.
    """
    base = Path(root).resolve()
    manifest_path = base / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"нет манифеста: {manifest_path}")

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    corpus = Corpus(root=base, watermark=str(raw.get("watermark") or ""), as_of=str(raw.get("as_of") or ""))

    for entry in raw.get("documents") or []:
        meta = _doc_meta(entry)
        if not meta.doc_id:
            continue
        corpus.documents.append(meta)

        path = base / meta.file if meta.file else None
        if not path or not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        revision, effective = _revision_of(meta, raw)
        for sec_title, sec_body in _split_sections(text):
            for piece in _window(sec_body, chunk_tokens, overlap_tokens):
                pos = len(corpus.chunks)
                corpus.chunks.append(
                    Chunk(
                        chunk_id=f"{meta.doc_id}#{pos:04d}",
                        doc_id=meta.doc_id,
                        title=meta.title,
                        section=sec_title,
                        text=piece,
                        department=meta.department,
                        system=meta.system,
                        rule_key=meta.rule_key,
                        revision=revision,
                        effective=effective,
                        position=pos,
                    )
                )

    for rule in raw.get("conflict_rules") or []:
        key = str(rule.get("key") or "")
        if not key:
            continue
        corpus.rules[key] = Rule(
            key=key,
            field=str(rule.get("field") or ""),
            short=str(rule.get("short") or ""),
            topic=str(rule.get("topic") or ""),
            first_effective=rule.get("first_effective") or None,
            revisions=[
                RuleRevision(
                    revision=int(r.get("revision") or 0),
                    doc_id=str(r.get("doc_id") or ""),
                    effective=str(r.get("effective") or ""),
                    value=str(r.get("value") or ""),
                    file=str(r.get("file") or ""),
                    sha256=str(r.get("sha256") or ""),
                )
                for r in (rule.get("revisions") or [])
            ],
        )

    return corpus


def _revision_of(meta: DocumentMeta, raw: dict) -> tuple[int | None, str | None]:
    """Номер редакции и дата вступления — из conflict_rules по doc_id."""
    for rule in raw.get("conflict_rules") or []:
        for r in rule.get("revisions") or []:
            if str(r.get("doc_id") or "") == meta.doc_id:
                return int(r.get("revision") or 0) or None, str(r.get("effective") or "") or None
    return None, None
