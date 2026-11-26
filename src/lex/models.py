"""Pydantic-схемы LEX: общие для ядра (``lex``) и HTTP-слоя (``api``).

Контракт — docs/API-CONTRACT.md. Ядро не знает про HTTP: события здесь
описаны как данные, а не как SSE-строки. Превращение в SSE — дело api/app.py.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

# Порядок фиксирован контрактом §3 + §8 (v1.1) + §9 (v1.1) — это не «примерно»,
# а закон. v1.1 добавила два шага: `cases` (после retrieve) и `inspect`
# (перед retrieve), поэтому total = 10, а не 9: §8 говорит «total становится 9»
# до того, как §9 добавила inspect, и сам же §8 требует порядок
# accept → decompose → retrieve → cases → …. Фактическая очерёдность ниже —
# единственная, которая вмещает оба требования.
StepKey = Literal[
    "accept",
    "policy",
    "decompose",
    "inspect",
    "retrieve",
    "cases",
    "permissions",
    "resolve",
    "conflict",
    "compose",
    "verify",
]

STEP_ORDER: tuple[str, ...] = (
    "accept",
    "policy",
    "decompose",
    "inspect",
    "retrieve",
    "cases",
    "permissions",
    "resolve",
    "conflict",
    "compose",
    "verify",
)

STEP_TITLES: dict[str, str] = {
    "accept": "Запрос принят",
    "policy": "Проверка компетенции",
    "decompose": "Разбор вопроса",
    "inspect": "Разбор источников",
    "retrieve": "Поиск",
    "cases": "Поиск по кейсам",
    "permissions": "Проверка прав",
    "resolve": "Действующая редакция",
    "conflict": "Сопоставление",
    "compose": "Сборка ответа",
    "verify": "Самопроверка",
}

STEP_COLORS: dict[str, str] = {
    "accept": "slate",
    "policy": "pink",
    "decompose": "violet",
    "inspect": "indigo",
    "retrieve": "cyan",
    "cases": "teal",
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
    # §17: не искали вовсе — вопрос не про базу организации. Отличать от
    # not_in_corpus: там тема наша, а ответа нет.
    "out_of_scope",
]

# Контракт §9: разная форма БД заказчика — текст, картинки, сканы.
ContentType = Literal["text", "image", "scan", "mixed"]

# Контракт §8: исход практического кейса.
CaseOutcome = Literal["resolved", "escalated", "rejected", "open"]


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
    # §9: цитата из нетекстового источника несёт тип, страницу, уверенность
    # распознавания и ссылку на превью — иначе «показать источник» не работает.
    content_type: ContentType | None = None
    page: int | None = None
    preview_url: str | None = None
    ocr_confidence: float | None = None
    # §8: пометки о происхождении — кейс или нормативный документ.
    source_kind: Literal["doc", "case"] | None = None
    case_id: str | None = None
    low_confidence: bool = False


class CaveatReason(BaseModel):
    """§20.2: одна причина оговорки — с кодом, чтобы считалась аналитика."""

    code: str
    text: str


class Caveat(BaseModel):
    """§20: ответ есть, но уверенность ниже порога.

    ``level``: low — отвечаю уверенно, оговорка информационная; medium — стоит
    проверить; high — лучше не действовать без человека. Оговорка НИКОГДА не
    превращается в отказ: отказ дороже, он отправляет человека искать сам.
    """

    level: Literal["low", "medium", "high"] = "low"
    reasons: list[CaveatReason] = Field(default_factory=list)
    advice: str = ""

    @property
    def present(self) -> bool:
        return bool(self.reasons)


class AnswerData(BaseModel):
    text: str
    # §13.1: первый ответ — выжимка (2-4 предложения, <=400 знаков), подробности
    # по отдельному запросу через detail_id.
    short: str = ""
    has_details: bool = False
    detail_id: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    refused: bool = False
    # §8: кейс противоречит регламенту либо регламент не покрывает.
    practice_only: bool = False
    notes: list[str] = Field(default_factory=list)
    # §10: что предположили, если сотрудник пропустил уточнение.
    assumptions: list[dict] = Field(default_factory=list)
    session_id: str | None = None
    # §12: результат проверки покрытия — то же, что в шаге verify.
    grounding: dict | None = None
    # §13.3: если в ответе есть форма — карточка «Есть форма: …».
    forms: list[dict] = Field(default_factory=list)
    # §21: тон формулировки. Смысл от него не зависит.
    tone: Literal["plain", "formal"] = "plain"
    # §20: третий исход — ответ с оговоркой. None означает «оговорки нет»,
    # и это нормальное состояние: оговорка на каждом ответе обесценивается.
    caveat: Caveat | None = None
    # §18: если ответить нечем — тикет, созданный агентом. Номер человекочитаемый.
    ticket_id: str | None = None


class ClarifyQuestion(BaseModel):
    key: str
    text: str
    reason: str
    kind: Literal["date", "choice", "text", "doc_id"] = "text"
    options: list[str] = Field(default_factory=list)


class ClarifyData(BaseModel):
    """§10: уточнение вместо ответа. Это не отказ — вопрос просто неполон."""

    session_id: str
    questions: list[ClarifyQuestion] = Field(default_factory=list)
    partial: dict = Field(default_factory=dict)
    can_skip: bool = True


class RefusalData(BaseModel):
    """§12.3: пустое «не знаю» — плохой отказ. Всегда говорим, что искали."""

    reason: str
    sources: list[str] = Field(default_factory=list)
    kind: RefusalKind
    searched: dict = Field(default_factory=dict)
    hint: str = ""


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
    # §21.3: переключатель тона. Смысл ответа от него не зависит.
    tone: Literal["plain", "formal"] = "plain"


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
    # §9: у каждого элемента корпуса есть тип содержимого.
    content_type: ContentType = "text"
    ocr_confidence: float | None = None
    ocr_status: str = ""


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
    # §8/§9: различаем документ и кейс, текст и скан.
    kind: Literal["doc", "case"] = "doc"
    content_type: ContentType = "text"
    page: int | None = None
    ocr_confidence: float | None = None
    source_file: str = ""


class CorpusStats(BaseModel):
    documents: int
    chunks: int
