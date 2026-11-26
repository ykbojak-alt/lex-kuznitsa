"""Каскад LEX: 10 шагов из контракта §3 + §8 + §9 + §12.

Ядро не знает про HTTP: события отдаются объектами ``StepEvent`` через
async-generator, а превращение в SSE — дело ``api/app.py``.

Порядок (контракт §3, расширен §8 и §9):
    accept → decompose → inspect → retrieve → cases → permissions
           → resolve → conflict → compose → verify        (total = 10)

Три барьера §12 («только из базы знаний») живут здесь:
  * барьер 1 — системный промпт в ``SYSTEM_PROMPT``: только переданные фрагменты;
  * барьер 2 — ``grounding.check_grounding`` на шаге ``verify``;
  * барьер 3 — усечение ответа до покрытых частей, иначе ``refusal.not_in_corpus``.

Конфиденциальность §12.4: права фильтруются ДО генерации (шаг ``permissions``
идёт раньше ``compose``), в промпт уходят только найденные фрагменты, а журнал
хранит ``doc_ids`` без содержимого и цитат.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Iterable

from . import detail, editions, llm, rerank
from .cases import Case, CaseStore, get_case_store
from .corpus import Corpus, load_corpus
from .dialogue import Dialogue
from .forms import get_form
from .grounding import GroundingReport, check_grounding
from .index import EmbeddingError, SearchIndex, embed_texts, get_index
from .media import inspect_corpus, render_page
from .models import (
    STEP_COLORS,
    STEP_ORDER,
    STEP_TITLES,
    AnswerData,
    Citation,
    ClarifyData,
    ClarifyQuestion,
    DoneData,
    ErrorData,
    RefusalData,
    StepEvent,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CORPUS = ROOT / "sample-corpus"

# Сколько фрагментов уходит в промпт (контракт §12.4 п.1: не весь документ).
TOP_K = int(os.environ.get("LEX_TOP_K", "6"))
# Сколько кандидатов берём на вход реранкера.
CANDIDATES = int(os.environ.get("LEX_CANDIDATES", "24"))
# Порог близости для проверки покрытия (§12.2, барьер 2).
GROUNDING_THRESHOLD = float(os.environ.get("LEX_GROUNDING_THRESHOLD", "0.45"))

SYSTEM_PROMPT = (
    "Ты отвечаешь ТОЛЬКО на основе фрагментов, переданных ниже. Если фрагменты не "
    "содержат ответа — верни отказ, а не общие знания. Не используй сведения, которых "
    "нет во фрагментах, даже если ты их знаешь. Каждое утверждение сопровождай номером "
    "фрагмента в квадратных скобках."
)

# Кейс-подсказка живёт отдельно: это не «разрешение» добавлять знания, а правило,
# как ссылаться на практику, когда она есть во фрагментах.
CASE_HINT = (
    "Фрагменты с пометкой «КЕЙС» — это практика, а не нормативный документ. "
    "Если кейс противоречит действующей редакции регламента, пиши прямо: "
    "«по регламенту X, в практике был случай Y (кейс N)». Если регламент вопрос не "
    "покрывает, а кейс покрывает — отвечай по кейсу с пометкой «по практике, "
    "регламент не покрывает»."
)


@dataclass
class Frame:
    """Один фрагмент, попавший в контекст модели. Нумерация = ссылка [n]."""

    index: int
    chunk_id: str
    doc_id: str
    title: str
    text: str
    kind: str = "doc"
    content_type: str = "text"
    page: int | None = None
    rule_key: str = ""
    revision: int | None = None
    effective: str | None = None
    ocr_confidence: float | None = None
    source_file: str = ""
    score: float = 0.0

    def render(self) -> str:
        head = f"[{self.index}] "
        if self.kind == "case":
            head += f"КЕЙС {self.doc_id}"
        else:
            head += f"{self.doc_id}"
            if self.revision:
                head += f" · редакция {self.revision} от {self.effective or '?'}"
            head += f" · {self.title}"
        if self.content_type in ("image", "scan") and self.page:
            head += f" · {self.content_type}, страница {self.page}"
            if self.ocr_confidence is not None:
                head += f", уверенность OCR {self.ocr_confidence:.2f}"
        return f"{head}\n{self.text}"


@dataclass
class Signal:
    """Событие или финал каскада — что отдать наружу/напечатать."""

    kind: str  # step | answer | refusal | clarify | done | error
    data: object


@dataclass
class RunOptions:
    question: str
    role_id: str = "lawyer"
    as_of: date | None = None
    session_id: str | None = None
    answers: dict = field(default_factory=dict)
    corpus_root: Path = DEFAULT_CORPUS
    cases_path: Path = field(default_factory=lambda: ROOT / "var" / "cases.jsonl")
    business_line: str | None = None
    no_llm: bool = False
    top_k: int = TOP_K


def _now_ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _step(n: int, key: str, detail: str, t0: float, *, color: str | None = None) -> StepEvent:
    return StepEvent(
        n=n,
        total=len(STEP_ORDER),
        key=key,  # type: ignore[arg-type]
        title=STEP_TITLES[key],
        detail=detail,
        ms=_now_ms(t0),
        color=color or STEP_COLORS[key],
    )


class Orchestrator:
    """Один прогон вопроса по каскаду."""

    def __init__(self, opts: RunOptions) -> None:
        self.opts = opts
        self._idx: SearchIndex | None = None
        self._corpus: Corpus | None = None
        self._cases: CaseStore | None = None
        self._frames: list[Frame] = []
        self._t0 = time.monotonic()
        self._timeline: dict[str, int] = {}
        self._dlg: Dialogue | None = None
        self._auto_session: str = ""

    @property
    def dialogue(self) -> Dialogue:
        """Диалог с уточнением параметров (§10). Один на прогон."""
        if self._dlg is None:
            self._dlg = Dialogue(corpus_dir=self.opts.corpus_root)
        return self._dlg

    # ------------------------------------------------------------- ресурсы

    @property
    def corpus(self) -> Corpus:
        if self._corpus is None:
            self._corpus = load_corpus(self.opts.corpus_root)
        return self._corpus

    @property
    def index(self) -> SearchIndex:
        if self._idx is None:
            self._idx = get_index(self.opts.corpus_root)
        return self._idx

    @property
    def cases(self) -> CaseStore:
        if self._cases is None:
            self._cases = get_case_store(self.opts.cases_path)
        return self._cases

    # --------------------------------------------------------------- каскад

    async def run(self) -> AsyncIterator[Signal]:
        """Генерирует сигналы по порядку. Завершение — answer/refusal/clarify/escalate + done.

        §10: если параметров задачи не хватает, шаг clarify идёт ВМЕСТО
        decompose, и поток закрывается без done.
        §15: если поиск не дал ответа, диалог уточняет ещё (2-3 итерации),
        а на исчерпании — эскалация в службу качества: последним шагом,
        вместо verify.
        """
        state: dict = {}
        yield Signal("step", self._stage_accept(1, state))
        clarify = self._stage_clarify(2, state)
        if clarify is not None:
            yield Signal("step", self._clarify_step(2, state))
            yield clarify
            return                      # §10: после clarify поток закрывается
        async for sig in self._run_stages(state):
            yield sig
        if state.get("escalate_pending"):
            # §15: поиск не дал ответа. Либо ещё одна итерация уточнения,
            # либо — эскалация в службу качества.
            decision = self._escalation_decision(state)
            if decision.questions:
                state["clarify_questions"] = decision.questions
                yield Signal("step", self._clarify_step(len(STEP_ORDER) + 1, state))
                yield Signal("clarify", ClarifyData(
                    session_id=self._session_id(),
                    questions=[ClarifyQuestion(**q.to_dict()) for q in decision.questions],
                    partial=self._search_summary(state),
                    can_skip=True,
                ))
                return                  # §10: ждём ответа, поток закрывается без done
            ticket = decision.escalate or {}
            # §15.3: эскалация — последним шагом, вместо verify.
            yield Signal("step", self._escalate_step(len(STEP_ORDER), ticket))
            yield Signal("escalate", self.dialogue.escalate_event(ticket))
            yield Signal("done", self._done())
            return
        final = state.get("final")
        if final is not None:
            yield Signal(final[0], final[1])
        yield Signal("done", self._done())

    async def _run_stages(self, state: dict) -> AsyncIterator[Signal]:
        """Каскад decompose → verify.

        Финальное событие не отдаёт сразу, а кладёт в state["final"]:
        решение «отказ или эскалация» принимает run().
        """
        stages = [
            ("decompose", self._stage_decompose),
            ("inspect", self._stage_inspect),
            ("retrieve", self._stage_retrieve),
            ("cases", self._stage_cases),
            ("permissions", self._stage_permissions),
            ("resolve", self._stage_resolve),
            ("conflict", self._stage_conflict),
            ("compose", self._stage_compose),
            ("verify", self._stage_verify),
        ]
        for n, (key, fn) in enumerate(stages, start=2):
            t_stage = time.monotonic()
            try:
                outcome = fn(n, state)
                # Стадии могут быть сопрограммами (compose зовёт модель).
                if asyncio.iscoroutine(outcome):
                    outcome = await outcome
            except Exception as exc:  # ошибка стадии не должна терять контекст
                yield Signal("error", ErrorData(message=str(exc), stage=key))
                state["final"] = ("done", None)
                return
            if outcome is _STOP:
                state["final"] = ("done", None)
                return
            if isinstance(outcome, Signal):
                # Стадия сама решила, что на этом каскад заканчивается
                # (clarify / ранний отказ).
                yield outcome
                if outcome.kind in ("clarify", "refusal"):
                    state["final"] = (outcome.kind, outcome.data)
                    return
            elif isinstance(outcome, StepEvent):
                self._timeline[key] = _now_ms(t_stage)
                yield Signal("step", outcome)
                if "final" in state:
                    final = state.pop("final")
                    kind, payload = final
                    if kind == "refusal" and self._should_escalate(payload, state):
                        # §15: «не нашлось» — это сигнал к эскалации.
                        state["escalate_pending"] = payload
                        return
                    state["final"] = final
                    return

    # ------------------------------------------------- §15: эскалация

    def _should_escalate(self, payload: object, state: dict) -> bool:
        """§15: отказ «не нашлось» — повод для эскалации, а не для сдачи."""
        kind = getattr(payload, "kind", "")
        return kind in ("not_in_corpus", "no_access")

    def _search_summary(self, state: dict) -> dict:
        """Что именно искали — это обязательная часть заявки (§15.2)."""
        hits = state.get("doc_hits") or []
        allowed = state.get("allowed") or []
        return {
            "documents": len(self.corpus.documents),
            "cases": len(self.cases.cases),
            "accessible": len(allowed),
            "top": [getattr(h.chunk, "doc_id", "") for h in hits][:5],
        }

    def _session_id(self) -> str:
        """Сессия диалога. Без неё клиент не сможет продолжить уточнения."""
        if self.opts.session_id:
            return self.opts.session_id
        if not self._auto_session:
            self._auto_session = f"s-auto-{uuid.uuid4().hex[:8]}"
        return self._auto_session

    def _escalation_decision(self, state: dict):
        """§15: уточнить ещё или эскалировать — решает модуль диалога."""
        refusal = state.get("escalate_pending")
        kind = getattr(refusal, "kind", "not_in_corpus")
        return self.dialogue.after_search(
            self._session_id(),
            self.opts.question,
            self.opts.role_id,
            searched=self._search_summary(state),
            kind=kind,
            business_line=state.get("line"),
        )

    def _escalate_step(self, n: int, ticket: dict) -> StepEvent:
        """§15.3: шаг эскалации — цвет orange, последний, вместо verify."""
        reason = str(ticket.get("why_unresolved") or "")
        iterations = int(ticket.get("iterations") or 0)
        detail = f"заявка {ticket.get('id') or '?'}: {reason}"
        if iterations:
            detail += f" · уточнений: {iterations}"
        return StepEvent.model_construct(
            n=n, total=len(STEP_ORDER), key="escalate", title="Эскалация",
            detail=detail, ms=_now_ms(self._t0), color="orange",
        )

    # ---------------------------------------------------------------- шаги

    def _stage_accept(self, n: int, state: dict) -> StepEvent | Signal:
        # §10: уточнение — не отказ. Но первый круг всегда проходит accept,
        # чтобы UI показал, что запрос понят.
        as_of = self.opts.as_of.isoformat() if self.opts.as_of else "сегодня"
        state["as_of_label"] = as_of
        return _step(n, "accept", f"роль {self.opts.role_id}, дата отсчёта {as_of}", self._t0)

    def _stage_clarify(self, n: int, state: dict) -> Signal | None:
        """§10: хватает ли параметров для ответа.

        Уточнение — не отказ. Если параметров не хватает, возвращаем
        Signal("clarify") с вопросами; каскад на этом заканчивается, ответа
        в этом запросе не будет. Если хватает — собранные параметры и явные
        умолчания кладём в state (answer.assumptions).
        """
        dlg = self.dialogue
        questions = dlg.needs_clarification(
            self.opts.question,
            role=self.opts.role_id,
            as_of=self.opts.as_of.isoformat() if self.opts.as_of else None,
            session_id=self.opts.session_id,
            answers=self.opts.answers or None,
        )
        if not questions:
            ctx = (dlg.apply_answers(self.opts.session_id, self.opts.answers)
                   if self.opts.session_id else {})
            state["dialogue"] = ctx
            state["assumptions"] = ctx.get("assumptions") or []
            params = ctx.get("params") or {}
            if not self.opts.as_of and params.get("as_of"):
                try:
                    self.opts.as_of = date.fromisoformat(str(params["as_of"]))
                except ValueError:
                    pass
            return None
        state["clarify_questions"] = questions
        return Signal("clarify", ClarifyData(
            session_id=self.opts.session_id or "",
            questions=[ClarifyQuestion(**q.to_dict()) for q in questions],
            partial={},
            can_skip=True,
        ))

    def _clarify_step(self, n: int, state: dict) -> StepEvent:
        """Шаг «Уточнение» — цвет sky, вместо decompose (§10)."""
        questions = state.get("clarify_questions") or []
        detail = self.dialogue.clarify_step(questions, n=n, total=len(STEP_ORDER))["detail"]
        # ключа "clarify" в StepKey пока нет (models.py — файл владельца
        # контракта), поэтому model_construct: шаг не должен потеряться из-за
        # литерала, который ещё не расширен.
        return StepEvent.model_construct(
            n=n, total=len(STEP_ORDER), key="clarify", title="Уточнение",
            detail=detail, ms=_now_ms(self._t0), color="sky",
        )

    def _stage_decompose(self, n: int, state: dict) -> StepEvent:
        line = self._business_line()
        subs = self._subquestions(self.opts.question)
        state["subquestions"] = subs
        state["line"] = line
        det = (f"направление: {line} · " if line else "") + f"{len(subs)} подвопросов"
        return _step(n, "decompose", det, self._t0)

    def _stage_inspect(self, n: int, state: dict) -> StepEvent:
        insp = inspect_corpus(self.opts.corpus_root)
        state["inspection"] = insp
        line = state.get("line")
        det = insp.detail
        if line:
            det += f" · направление {line}"
        return _step(n, "inspect", det, self._t0)

    def _stage_retrieve(self, n: int, state: dict) -> StepEvent | Signal:
        subs = state.get("subquestions") or [self.opts.question]
        hits = self.index.search_many(subs, top_per_query=CANDIDATES)
        doc_hits = [h for h in hits if getattr(h.chunk, "kind", "doc") == "doc"]
        # Документные хиты идут в реранк, кейсы — отдельной стадией (§8).
        ranked = self._rerank(self.opts.question, doc_hits)
        bm_hits = len(doc_hits)
        state["doc_hits"] = ranked
        detail = f"документов {bm_hits}"
        return _step(n, "retrieve", detail, self._t0)

    def _stage_cases(self, n: int, state: dict) -> StepEvent:
        case_hits = self._search_cases()
        state["case_hits"] = case_hits
        det = f"найдено {len(case_hits)} кейсов"
        return _step(n, "cases", det, self._t0)

    def _stage_permissions(self, n: int, state: dict) -> StepEvent | Signal:
        # §12.4 п.3: права фильтруются ДО генерации.
        doc_hits = state.get("doc_hits") or []
        allowed, dropped = self._filter_access(doc_hits)
        state["allowed"] = allowed
        state["dropped"] = dropped
        det = f"доступно {len(allowed)} из {len(doc_hits)}, отброшено {dropped}"
        if not allowed and doc_hits:
            # Есть материал, но роли он недоступен — это no_access, не «не найдено».
            state["final"] = ("refusal", RefusalData(
                reason="Ни один найденный документ не доступен этой роли",
                sources=[h.chunk.doc_id for h in doc_hits][:10],
                kind="no_access",
                searched=self._searched(state),
                hint="Обратитесь к владельцу регламента или выберите роль с нужными правами",
            ))
        return _step(n, "permissions", det, self._t0)

    def _stage_resolve(self, n: int, state: dict) -> StepEvent | Signal:
        as_of = self.opts.as_of
        allowed = state.get("allowed") or []
        # §12: если в вопросе есть правило и на дату нет действующей редакции —
        # это no_edition_in_force, а не «не найдено». Отказывает только правило,
        # которое относится к вопросу (см. _rules_with_focus).
        with_focus = self._rules_with_focus(allowed)
        keys = [k for k, _ in with_focus]
        focused = {k for k, on_topic in with_focus if on_topic}
        resolutions = editions.resolve_many(self.corpus, keys, as_of)
        state["resolutions"] = resolutions
        expired = [r for r in resolutions if not r.ok and r.rule.key in focused]
        if expired and keys:
            rule = expired[0].rule
            first = rule.first_effective or (rule.revisions[0].effective if rule.revisions else "?")
            state["final"] = ("refusal", RefusalData(
                reason=(f"На {as_of.isoformat()} ни одна редакция правила {rule.key} "
                        f"не действует: первая вступила в силу {first}"
                        if as_of else f"У правила {rule.key} нет действующей редакции"),
                sources=[r.doc_id for r in rule.revisions],
                kind="no_edition_in_force",
                searched=self._searched(state),
                hint=f"Правило введено только с {first} — сдвиньте дату отсчёта",
            ))
        if resolutions:
            det = "; ".join(r.detail() for r in resolutions)
        else:
            det = "правило не определено — действует как есть"
        return _step(n, "resolve", det, self._t0)

    def _stage_conflict(self, n: int, state: dict) -> StepEvent:
        allowed = state.get("allowed") or []
        case_hits = state.get("case_hits") or []
        notes: list[str] = []
        conflicts = 0
        # В case_hits лежат объекты Case (не чанки) — у кейса есть .id/.rule_key
        # напрямую.
        for case in case_hits:
            rule_key = getattr(case, "rule_key", "")
            if not rule_key:
                continue
            res = next((r for r in (state.get("resolutions") or []) if r.rule.key == rule_key), None)
            if res and res.in_force:
                conflicts += 1
                notes.append(
                    f"по регламенту {res.rule.key} действует «{res.in_force.value}», "
                    f"в практике был случай ({case.id})"
                )
        state["conflict_notes"] = notes
        if conflicts:
            return _step(n, "conflict", f"кейсов по теме {conflicts}, регламент главнее", self._t0)
        return _step(n, "conflict", f"конфликтов: 0; фрагментов {len(allowed)}", self._t0)

    async def _stage_compose(self, n: int, state: dict) -> StepEvent | Signal:
        allowed = state.get("allowed") or []
        case_hits = state.get("case_hits") or []
        frames = self._build_frames(allowed, case_hits)
        self._frames = frames
        state["frames"] = frames
        if not frames:
            state["final"] = ("refusal", RefusalData(
                reason="В базе знаний нет документа, регулирующего этот вопрос",
                sources=[],
                kind="not_in_corpus",
                searched=self._searched(state),
                hint="Уточните направление или обратитесь к владельцу регламента",
            ))
            return _step(n, "compose", "контекста нет — отказ", self._t0)

        if self.opts.no_llm:
            text = self._extractive_answer(frames)
        else:
            text = await asyncio.to_thread(self._ask_model, frames, state)
        state["answer_text"] = text
        return _step(n, "compose", f"из {len(frames)} фрагментов", self._t0)

    def _stage_verify(self, n: int, state: dict) -> StepEvent | Signal:
        text = state.get("answer_text") or ""
        frames = state.get("frames") or []
        report = check_grounding(
            text, [f.text for f in frames],
            embedder=self._embedder(),
            threshold=GROUNDING_THRESHOLD,
        )
        state["grounding"] = report

        if report.nothing_covered:
            state["final"] = ("refusal", RefusalData(
                reason="Модель не опиралась на базу знаний: ни одно утверждение не подтверждено цитатой",
                sources=[f.doc_id for f in frames],
                kind="not_in_corpus",
                searched=self._searched(state),
                hint="Переформулируйте вопрос или уточните направление",
            ))
            return _step(n, "verify", report.detail(), self._t0, color="amber")

        # Барьер 3: усекаем до покрытых частей — и отдаём усечённый ответ.
        # Раньше здесь стоял return без state["final"], и каскад завершался
        # молчанием: 10 шагов, ноль ответа. Усечение — это всё ещё ответ,
        # просто короче, поэтому дальше идёт обычная сборка AnswerData.
        truncated = report.external
        if truncated:
            state["answer_text"] = report.covered_text()
            text = state["answer_text"]

        citations = self._citations(frames, text)
        notes: list[str] = []
        # §9: цитата, держащаяся на плохо распознанном источнике.
        weak = [c for c in citations if c.low_confidence]
        if weak:
            notes.append("источник распознан неуверенно, сверьте с оригиналом")
        notes.extend(state.get("conflict_notes") or [])

        practice_only = bool(state.get("case_hits")) and not state.get("allowed")
        if practice_only:
            notes.append("по практике, регламент не покрывает")

        # §13.1: первый ответ — выжимка; развёрнутое — по запросу, из тех же
        # фрагментов. Барьер §12 проверен выше, на полном тексте.
        full_text = state.get("answer_text") or ""
        short = detail.shorten(full_text)
        detail_id = self._remember_detail(frames, full_text, citations, state)
        form_notes = self._form_notes(state)

        state["final"] = (
            "answer",
            AnswerData(
                text=full_text,
                short=short,
                has_details=bool(frames),
                detail_id=detail_id,
                citations=citations,
                refused=False,
                practice_only=practice_only,
                notes=notes + form_notes
                + ([] if not truncated else [f"{truncated} утверждений отброшено (нет цитаты)"]),
                forms=state.get("matched_forms") or [],
                session_id=self.opts.session_id,
                grounding={"total": report.total, "covered": report.covered,
                           "external": report.external, "threshold": report.threshold},
            ),
        )
        return _step(n, "verify", report.detail(), self._t0,
                     color="amber" if truncated else None)

    # ------------------------------------------------------------- помощники

    def _done(self) -> DoneData:
        return DoneData(
            elapsed_ms=_now_ms(self._t0),
            inference_path="extractive" if self.opts.no_llm else "nexus-link",
            journal_id=None,
        )

    def _embedder(self):
        return lambda texts: embed_texts(list(texts))

    def _business_line(self) -> str:
        if self.opts.business_line:
            return self.opts.business_line
        return ""

    def _subquestions(self, question: str) -> list[str]:
        """Разбор вопроса. Без модели: режем по союзам и вопросительным знакам.

        Контракт §5: минимум три шага обязательны, поэтому decompose не может
        быть пустым. Один вопрос — уже один подвопрос, и этого достаточно:
        декомпозиция существует, чтобы поиск шёл по частям.
        """
        q = question.strip()
        if not q:
            return []
        parts = [p.strip(" ,;") for p in q.replace("?", "?|").replace(" и ", "|").split("|")]
        parts = [p for p in parts if len(p) > 3]
        return parts or [q]

    def _rerank(self, query: str, hits: list) -> list:
        if not hits:
            return []
        docs = [h.chunk.text for h in hits]
        try:
            order = rerank.rerank(query, docs, top=len(hits))
        except rerank.RerankError:
            # Без реранкера поиск продолжает работать: порядок гибрида — не мусор,
            # а реранк — второе мнение.
            return hits
        out = []
        for idx, score in order:
            if 0 <= idx < len(hits):
                h = hits[idx]
                h.score = float(score)
                out.append(h)
        return out

    def _search_cases(self) -> list[Case]:
        stored = self.cases.cases
        if not stored:
            return []
        # Кейсы ищутся теми же средствами, что документы: BM25 по тексту кейса
        # плюс эмбеддинги. Индекс кейсов строится на лету — их единицы.
        from .index import tokenize

        q_terms = set(tokenize(self.opts.question))
        if not q_terms:
            return []
        scored: list[tuple[float, Case]] = []
        for c in stored:
            terms = set(tokenize(c.text))
            if not terms:
                continue
            overlap = len(q_terms & terms)
            if overlap:
                scored.append((overlap / (len(q_terms) ** 0.5), c))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:3]]

    def _filter_access(self, hits: list) -> tuple[list, int]:
        """§11/§12.4: роль обязана видеть документ по отделу и направлению.

        access.py принадлежит ACE — если он есть, зовём его; иначе честно
        пропускаем всё, но помечаем, что фильтра нет (не выдавать отсутствие
        прав за проверку прав).
        """
        try:
            from .access import allowed_document  # type: ignore
        except Exception:
            return hits, 0
        allowed, dropped = [], 0
        for h in hits:
            try:
                ok = allowed_document(h.chunk, self.opts.role_id)
            except Exception:
                ok = True
            if ok:
                allowed.append(h)
            else:
                dropped += 1
        return allowed, dropped

    def _rule_keys(self, hits: list) -> list[str]:
        return [k for k, _ in self._rules_with_focus(hits)]

    def _rules_with_focus(self, hits: list) -> list[tuple[str, bool]]:
        """Правила с признаком «это про вопрос».

        Найденный документ сам по себе не значит, что его правило относится к
        вопросу: на вопрос про срок (SR-01) поиск исправно приносит и порог
        одобрения (SR-03). Если отказывать по любому найденному правилу, вопрос
        получит отказ из-за документа, про который его не спрашивали. Поэтому
        «фокусным» правило считается, только когда совпадает с вопросом по
        ключу, названию поля или теме правила.
        """
        question = self.opts.question.lower()
        out: list[tuple[str, bool]] = []
        seen: set[str] = set()

        for rule_key, rule in self.corpus.rules.items():
            if rule_key.lower() in question:
                out.append((rule_key, True))
                seen.add(rule_key)
                continue
            probes = [p for p in (rule.field, rule.short, rule.topic) if p]
            if any(p.lower() in question for p in probes):
                out.append((rule_key, True))
                seen.add(rule_key)

        for h in hits:
            k = getattr(h.chunk, "rule_key", "")
            if k and k not in seen:
                out.append((k, False))
                seen.add(k)
        return out

    def _build_frames(self, doc_hits: list, case_hits: list) -> list[Frame]:
        frames: list[Frame] = []
        for h in doc_hits[: self.opts.top_k]:
            ch = h.chunk
            frames.append(Frame(
                index=len(frames) + 1,
                chunk_id=ch.chunk_id,
                doc_id=ch.doc_id,
                title=ch.title,
                text=ch.text,
                kind="doc",
                content_type=getattr(ch, "content_type", "text"),
                page=getattr(ch, "page", None),
                rule_key=ch.rule_key,
                revision=ch.revision,
                effective=ch.effective,
                ocr_confidence=getattr(ch, "ocr_confidence", None),
                source_file=getattr(ch, "source_file", ""),
                score=h.score,
            ))
        for c in case_hits:
            frames.append(Frame(
                index=len(frames) + 1,
                chunk_id=f"{c.id}#case",
                doc_id=c.id,
                title=f"Кейс: {c.question[:60]}",
                text=c.text,
                kind="case",
                content_type=c.content_type,
                rule_key=c.rule_key,
            ))
        return frames

    def _ask_model(self, frames: list[Frame], state: dict) -> str:
        context = "\n\n".join(f.render() for f in frames)
        as_of = state.get("as_of_label") or "не указана"
        user = (
            f"Вопрос сотрудника: {self.opts.question}\n"
            f"Роль: {self.opts.role_id}\n"
            f"Дата отсчёта: {as_of}\n\n"
            f"Фрагменты базы знаний организации:\n\n{context}\n\n"
            "Ответь по правилам выше. Если ответа нет ни в одном фрагменте — "
            "ответь ровно: ОТКАЗ: <причина>."
        )
        return llm.chat([
            {"role": "system", "content": SYSTEM_PROMPT + "\n\n" + CASE_HINT},
            {"role": "user", "content": user},
        ])

    def _extractive_answer(self, frames: list[Frame]) -> str:
        """Ответ без модели: цитаты как есть. Честно, но некрасиво.

        Нужен для демо при недоступной модели и для тестов: барьеры §12
        проверяются независимо от LLM.
        """
        lines = []
        for f in frames[:3]:
            head = f.doc_id if f.kind == "doc" else f"кейс {f.doc_id}"
            lines.append(f"{head}: {f.text.strip()[:300]} [{f.index}]")
        return "\n".join(lines)

    def _citations(self, frames: list[Frame], answer: str) -> list[Citation]:
        used = set()
        for f in frames:
            if f"[{f.index}]" in answer:
                used.add(f.index)
        # Если модель не расставила ссылки — не выдумываем цитаты из воздуха,
        # но и не оставляем ответ без источников: отдаём использованные
        # фрагменты как есть, помечая их.
        targets = [f for f in frames if f.index in used] or frames[:1]
        out: list[Citation] = []
        for f in targets:
            low = f.ocr_confidence is not None and f.ocr_confidence < 0.6
            out.append(Citation(
                doc_id=f.doc_id,
                title=f.title,
                revision=f.revision,
                effective=f.effective,
                quote=f.text.strip()[:600],
                content_type=f.content_type if f.content_type != "text" else None,
                page=f.page,
                preview_url=(f"/api/doc/{f.doc_id}/preview?page={f.page or 1}"
                             if f.content_type in ("image", "scan", "mixed") else None),
                ocr_confidence=f.ocr_confidence,
                source_kind="case" if f.kind == "case" else "doc",
                case_id=f.doc_id if f.kind == "case" else None,
                low_confidence=low,
            ))
        return out

    def _searched(self, state: dict) -> dict:
        """§12.3: отказ говорит, что искали."""
        return {
            "documents": len(self.corpus.documents),
            "cases": len(self.cases.cases),
            "business_lines": [state["line"]] if state.get("line") else [],
        }

    # ------------------------------------------------- §13: раскрытие и формы

    def _remember_detail(self, frames: list[Frame], full_text: str,
                         citations: list[Citation], state: dict) -> str:
        """Сохраняет развёрнутую версию под ``detail_id`` — её отдаст expand().

        Хранится только то, что уже было в контексте: фрагменты + полный текст
        ответа. Никаких новых знаний раскрытие не добавляет (§13.1).
        """
        detail_id = f"d-{uuid.uuid4().hex[:6]}"
        sections = detail.sections_from_chunks([f for f in frames])
        tables = []
        for rule_key in {f.rule_key for f in frames if f.rule_key}:
            rule = self.corpus.rules.get(rule_key)
            table = detail.revisions_table(rule)
            if table is not None:
                tables.append(table.to_dict())
        _DETAILS[detail_id] = {
            "short": detail.shorten(full_text),
            "detailed": full_text,
            "sections": [s.to_dict() for s in sections],
            "tables": tables,
            "citations": [c.model_dump() for c in citations],
            "frames": [f.render() for f in frames],
            "state": {"as_of": state.get("as_of_label")},
        }
        return detail_id

    def _form_notes(self, state: dict) -> list[str]:
        """§13.3: карточка формы — если в найденных документах есть форма."""
        try:
            from .forms import load_forms
        except Exception:
            return []
        try:
            available = load_forms(self.opts.corpus_root)
        except Exception:
            return []
        if not available:
            return []
        doc_ids = {getattr(h.chunk, "doc_id", "") for h in (state.get("allowed") or [])}
        state["matched_forms"] = [
            {"form_id": f.form_id, "title": f.title, "doc_id": f.doc_id,
             "fields": len(f.fields), "format": f.format}
            for f in available if f.doc_id in doc_ids
        ]
        if not state["matched_forms"]:
            # Формы есть, но не по этому документу — не навязываем.
            state["matched_forms"] = []
        return [f"есть форма: {m['title']} ({m['form_id']})" for m in state["matched_forms"]]


# ------------------------------------------------------------------ раскрытие

_DETAILS: dict[str, dict] = {}


def expand(detail_id: str, section: str = "all") -> dict | None:
    """Развёрнутая версия ответа (§13.1). None — такого detail_id нет.

    Строится из сохранённых фрагментов, поэтому новых знаний не несёт;
    заправшиваемый раздел (``exceptions``/``procedure``/``roles``) фильтрует
    секции, а не добавляет текст.
    """
    stored = _DETAILS.get(detail_id)
    if stored is None:
        return None
    from .detail import DetailSection, pick_section

    sections = [DetailSection(**s) for s in stored.get("sections", [])]
    chosen = pick_section(sections, section) if section and section != "all" else sections
    out = dict(stored)
    out["sections"] = [s.to_dict() for s in chosen]
    out["section_requested"] = section
    return out


def available_forms(corpus_root: str | Path) -> list[dict]:
    """Список форм для ``GET /api/forms`` (§13.3)."""
    from .forms import load_forms

    return [f.to_dict() for f in load_forms(corpus_root)]


def download_form_path(corpus_root: str | Path, form_id: str) -> Path | None:
    """Путь к бланку формы. Бланк — markdown-описание полей, если файла нет."""
    form = get_form(corpus_root, form_id)
    if form is None:
        return None
    base = Path(corpus_root) / "forms"
    for name in (f"{form_id}.md", f"{form_id}.docx", f"{form_id}.pdf"):
        candidate = base / name
        if candidate.exists():
            return candidate
    return None


_STOP = object()


# ------------------------------------------------------------------ журнал


def journal_entry(
    opts: RunOptions, *, refused: bool, steps: dict[str, int], doc_ids: list[str],
    elapsed_ms: int, inference_path: str, case_ids: list[str] | None = None,
) -> dict:
    """Запись журнала. §12.4 п.2: только идентификаторы, без содержимого и цитат."""
    return {
        "id": f"q-{uuid.uuid4().hex[:6]}",
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "role_id": opts.role_id,
        "question": opts.question,
        "refused": refused,
        "elapsed_ms": elapsed_ms,
        "steps": [{"key": k, "ms": v} for k, v in steps.items()],
        "doc_ids": doc_ids,
        "case_ids": case_ids or [],
        "inference_path": inference_path,
    }


def append_journal(path: str | Path, entry: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ CLI


async def run_once(opts: RunOptions) -> list[Signal]:
    orch = Orchestrator(opts)
    return [sig async for sig in orch.run()]


def _parse(argv: list[str] | None = None) -> RunOptions:
    ap = argparse.ArgumentParser(description="LEX: каскад по одному вопросу")
    ap.add_argument("--question", required=True)
    ap.add_argument("--role", default="lawyer", dest="role_id")
    ap.add_argument("--as-of", dest="as_of", default=None)
    ap.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    ap.add_argument("--no-llm", action="store_true", help="без модели (отладочный режим)")
    ap.add_argument("--json", action="store_true", help="печатать события как JSON")
    args = ap.parse_args(argv)

    as_of = None
    if args.as_of:
        as_of = datetime.strptime(args.as_of, "%Y-%m-%d").date()
    return RunOptions(
        question=args.question,
        role_id=args.role_id,
        as_of=as_of,
        corpus_root=Path(args.corpus),
        no_llm=args.no_llm,
    )


def _print_signal(sig: Signal, as_json: bool) -> None:
    if sig.kind == "step":
        ev: StepEvent = sig.data  # type: ignore[assignment]
        if as_json:
            print(json.dumps({"event": "step", "data": ev.model_dump()}, ensure_ascii=False))
        else:
            print(f"[{ev.n}/{ev.total}] {ev.title:<18} {ev.detail}  ({ev.ms} мс)")
        return
    payload = sig.data.model_dump() if hasattr(sig.data, "model_dump") else sig.data
    if as_json:
        print(json.dumps({"event": sig.kind, "data": payload}, ensure_ascii=False))
        return
    if sig.kind == "answer":
        print("\nОТВЕТ:")
        print(payload["text"])
        for c in payload.get("citations", []):
            tag = f"{c['doc_id']}"
            if c.get("revision"):
                tag += f" · редакция {c['revision']} от {c.get('effective')}"
            if c.get("case_id"):
                tag += f" (кейс {c['case_id']})"
            print(f"  · {tag} — {c['quote'][:120]}")
        for note in payload.get("notes", []):
            print(f"  ! {note}")
    elif sig.kind == "refusal":
        print(f"\nОТКАЗ [{payload['kind']}]: {payload['reason']}")
        if payload.get("sources"):
            print(f"  источники: {', '.join(payload['sources'][:8])}")
        if payload.get("searched"):
            s = payload["searched"]
            print(f"  искали: документов {s.get('documents')}, кейсов {s.get('cases')}")
        if payload.get("hint"):
            print(f"  подсказка: {payload['hint']}")
    elif sig.kind == "clarify":
        print("\nУТОЧНЕНИЕ:")
        for q in payload["questions"]:
            print(f"  ? {q['text']} — {q['reason']}")
    elif sig.kind == "done":
        print(f"\nготово за {payload['elapsed_ms']} мс, путь: {payload['inference_path']}")
    elif sig.kind == "error":
        print(f"\nОШИБКА на стадии {payload.get('stage')}: {payload['message']}")


def main(argv: list[str] | None = None) -> int:
    opts = _parse(argv)
    as_json = "--json" in (argv if argv is not None else sys.argv)
    signals = asyncio.run(run_once(opts))
    for sig in signals:
        _print_signal(sig, as_json)
    final = next((s for s in signals if s.kind in ("answer", "refusal", "clarify")), None)
    return 0 if final and final.kind != "error" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
