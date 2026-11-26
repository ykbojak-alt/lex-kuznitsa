"""База реальных кейсов (контракт §8, v1.1).

Кейс — это **не документ**. Документ нормативен и статичен; кейс — наблюдение из
практики: что спросили, что ответили, что произошло. Кейсы участвуют в поиске
наравне с документами, но:

* помечены ``kind="case"`` — UI и оркестратор их различают;
* приоритет **ниже** любого нормативного документа: кейс не может отменить
  действующую редакцию, он может только рассказать, что в практике было иначе;
* если регламент ответа не даёт, а кейс даёт — ответ строится на кейсе с
  пометкой «по практике, регламент не покрывает».

Хранение — ``var/cases.jsonl``, одна строка = один кейс, дописывание атомарно.
Отдельной БД нет: кейсов будут десятки, а не миллионы.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import Chunk

OUTCOMES = ("resolved", "escalated", "rejected", "open")
CONTENT_TYPES = ("text", "image", "scan", "mixed")


@dataclass
class Case:
    id: str
    ts: str
    author_role: str
    question: str
    answer_in_practice: str
    outcome: str = "open"
    author_name: str = ""
    linked_docs: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    rule_key: str = ""
    content_type: str = "text"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts,
            "author_role": self.author_role,
            "author_name": self.author_name,
            "question": self.question,
            "answer_in_practice": self.answer_in_practice,
            "outcome": self.outcome,
            "linked_docs": list(self.linked_docs),
            "tags": list(self.tags),
            "rule_key": self.rule_key,
            "content_type": self.content_type,
        }

    @property
    def text(self) -> str:
        """Текст, который идёт в BM25 и эмбеддинги."""
        parts = [f"Вопрос: {self.question}", f"В практике: {self.answer_in_practice}"]
        if self.tags:
            parts.append("Теги: " + ", ".join(self.tags))
        return "\n".join(parts)

    def as_chunk(self) -> Chunk:
        """Кейс в виде чанка, чтобы индекс не знал разницы.

        ``department``/``system`` пустые: кейс не принадлежит отделу, а
        привязывается к ролям по ``author_role`` — фильтр прав к нему не
        применяется (право видеть кейс даёт роль автора, а не отдел).
        """
        return Chunk(
            chunk_id=f"{self.id}#case",
            doc_id=self.id,
            title=f"Кейс: {self.question[:80]}",
            section="практика",
            text=self.text,
            department="",
            system="",
            rule_key=self.rule_key,
            revision=None,
            effective=None,
            position=-1,
        )


class CaseStore:
    """Файловое хранилище кейсов с дозаписью."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._cases: list[Case] = []
        self._load()

    def _load(self) -> None:
        self._cases = []
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                # Битая строка не должна лишать доступа ко всей базе: журнал
                # кейсов пишется вручную и частично — это рабочий режим.
                continue
            self._cases.append(_case_from_dict(raw))

    def reload(self) -> None:
        with self._lock:
            self._load()

    # ---------------------------------------------------------------- чтение

    @property
    def cases(self) -> list[Case]:
        return list(self._cases)

    def get(self, case_id: str) -> Case | None:
        return next((c for c in self._cases if c.id == case_id), None)

    def find(
        self,
        *,
        limit: int | None = 50,
        role_id: str | None = None,
        tag: str | None = None,
        rule_key: str | None = None,
    ) -> list[Case]:
        """Фильтры контракта §8: (limit, role_id, tag, rule_key)."""
        out = self._cases
        if role_id:
            out = [c for c in out if c.author_role == role_id]
        if tag:
            low = tag.lower()
            out = [c for c in out if any(low in t.lower() for t in c.tags)]
        if rule_key:
            out = [c for c in out if c.rule_key == rule_key]
        out = sorted(out, key=lambda c: c.ts, reverse=True)
        return out[:limit] if limit else out

    def next_id(self) -> str:
        n = 0
        for c in self._cases:
            if c.id.startswith("case-"):
                try:
                    n = max(n, int(c.id.split("-", 1)[1]))
                except ValueError:
                    continue
        return f"case-{n + 1:04d}"

    # --------------------------------------------------------------- запись

    def add(
        self,
        *,
        question: str,
        answer_in_practice: str,
        outcome: str = "open",
        author_role: str = "",
        author_name: str = "",
        linked_docs: Iterable[str] | None = None,
        tags: Iterable[str] | None = None,
        rule_key: str = "",
        content_type: str = "text",
        case_id: str | None = None,
    ) -> Case:
        question = (question or "").strip()
        answer = (answer_in_practice or "").strip()
        if not question:
            raise ValueError("кейс без question: нечего искать в будущем")
        if not answer:
            raise ValueError("кейс без answer_in_practice: наблюдение без вывода")
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome={outcome!r} не из {OUTCOMES}")
        if content_type not in CONTENT_TYPES:
            raise ValueError(f"content_type={content_type!r} не из {CONTENT_TYPES}")

        with self._lock:
            cid = case_id or self.next_id()
            if self.get(cid):
                raise ValueError(f"кейс {cid} уже есть")
            case = Case(
                id=cid,
                ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                author_role=author_role or "",
                author_name=author_name or "",
                question=question,
                answer_in_practice=answer,
                outcome=outcome,
                linked_docs=list(linked_docs or []),
                tags=[str(t) for t in (tags or [])],
                rule_key=rule_key or "",
                content_type=content_type,
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(case.to_dict(), ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._cases.append(case)
            return case


def _case_from_dict(raw: dict) -> Case:
    return Case(
        id=str(raw.get("id") or ""),
        ts=str(raw.get("ts") or ""),
        author_role=str(raw.get("author_role") or ""),
        author_name=str(raw.get("author_name") or ""),
        question=str(raw.get("question") or ""),
        answer_in_practice=str(raw.get("answer_in_practice") or ""),
        outcome=str(raw.get("outcome") or "open"),
        linked_docs=[str(x) for x in (raw.get("linked_docs") or [])],
        tags=[str(x) for x in (raw.get("tags") or [])],
        rule_key=str(raw.get("rule_key") or ""),
        content_type=str(raw.get("content_type") or "text"),
    )


_STORES: dict[str, CaseStore] = {}


def get_case_store(path: str | Path) -> CaseStore:
    key = str(Path(path).resolve())
    store = _STORES.get(key)
    if store is None:
        store = CaseStore(path)
        _STORES[key] = store
    return store
