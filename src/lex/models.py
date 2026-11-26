"""Pydantic-схемы LEX: общие для ядра (``lex``) и HTTP-слоя (``api``).

Контракт — docs/API-CONTRACT.md. Ядро не знает про HTTP: события здесь
описаны как данные, а не как SSE-строки. Превращение в SSE — дело api/app.py.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

# Порядок фиксирован контрактом §3 — это не «примерно», а закон.
StepKey = Literal[
    "accept",
    "decompose",
    "retrieve",
    "permissions",
    "resolve",
    "conflict",
    "compose",
    "verify",
]

STEP_ORDER: tuple[StepKey, ...] = (
    "accept",
    "decompose",
    "retrieve",
    "permissions",
    "resolve",
    "conflict",
    "compose",
    "verify",
)

STEP_TITLES: dict[str, str] = {
    "accept": "Запрос принят",
    "decompose": "Разбор вопроса",
    "retrieve": "Поиск",
    "permissions": "Проверка прав",
    "resolve": "Действующая редакция",
    "conflict": "Сопоставление",
    "compose": "Сборка ответа",
    "verify": "Самопроверка",
}

STEP_COLORS: dict[str, str] = {
    "accept": "slate",
    "decompose": "violet",
    "retrieve": "cyan",
    "permissions": "amber",
    "resolve": "blue",
    "conflict": "rose",
    "compose": "emerald",
    "verify": "green",
}

RefusalKind = Literal[
    "no_edition_in_force",
    "no_access",
    "not_in_corpus",
    "conflict_unresolved",
]


class StepEvent(BaseModel):
    """Один шаг каскада (контракт §3)."""

    n: int
    total: int = len(STEP_ORDER)
    key: StepKey
    title: str
    detail: str
    ms: int
    color: str


class Citation(BaseModel):
    doc_id: str
    title: str
    revision: int | None = None
    effective: str | None = None
    quote: str


class AnswerData(BaseModel):
    text: str
    citations: list[Citation] = Field(default_factory=list)
    refused: bool = False


class RefusalData(BaseModel):
    reason: str
    sources: list[str] = Field(default_factory=list)
    kind: RefusalKind


class DoneData(BaseModel):
    elapsed_ms: int
    inference_path: str
    journal_id: str | None = None


class ErrorData(BaseModel):
    message: str
    stage: str | None = None


class AskRequest(BaseModel):
    question: str
    role_id: str
    as_of: date | None = None


# ------------------------------------------------------------------ корпус


class RuleRevision(BaseModel):
    revision: int
    doc_id: str
    effective: str
    value: str
    file: str = ""
    sha256: str = ""


class Rule(BaseModel):
    """Правило с редакциями (``conflict_rules`` в manifest)."""

    key: str
    field: str = ""
    short: str = ""
    topic: str = ""
    revisions: list[RuleRevision] = Field(default_factory=list)
    first_effective: str | None = None

    def effective_on(self, as_of: date | None) -> RuleRevision | None:
        """Редакция, действующая на дату: последняя с effective <= as_of.

        ``as_of=None`` — опорная дата корпуса (манифест собран «сейчас»),
        поэтому берём самую свежую редакцию.
        """
        if not self.revisions:
            return None
        ordered = sorted(self.revisions, key=lambda r: (r.effective, r.revision))
        if as_of is None:
            return ordered[-1]
        target = as_of.isoformat()
        alive = [r for r in ordered if r.effective <= target]
        return alive[-1] if alive else None


class DocumentMeta(BaseModel):
    doc_id: str
    title: str
    kind: str = ""
    subtype: str = ""
    department: str = ""
    system: str = ""
    owner: str = ""
    version: str = ""
    issued: str = ""
    rule_key: str = ""
    file: str = ""
    sha256: str = ""
    words: int = 0
    sections: list[str] = Field(default_factory=list)


class Chunk(BaseModel):
    """Единица поиска: кусок документа в пределах одной секции."""

    chunk_id: str
    doc_id: str
    title: str
    section: str
    text: str
    department: str = ""
    system: str = ""
    rule_key: str = ""
    revision: int | None = None
    effective: str | None = None
    position: int = 0


class CorpusStats(BaseModel):
    documents: int
    chunks: int
