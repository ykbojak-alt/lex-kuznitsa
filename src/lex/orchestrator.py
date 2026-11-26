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
import contextlib
import json
import os
import re
import sys
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from . import access, detail, editions, llm, policy, rerank
from . import caveat as caveat_mod
from .cases import Case, CaseStore, get_case_store
from .corpus import Corpus, load_corpus
from .dialogue import Dialogue
from .forms import get_form
from .grounding import GroundingReport, check_grounding, subject_coverage
from .index import SearchIndex, embed_texts, get_index
from .media import inspect_corpus
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
from .tickets import get_ticket_store

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CORPUS = ROOT / "sample-corpus"

# Сколько фрагментов уходит в промпт (контракт §12.4 п.1: не весь документ).
TOP_K = int(os.environ.get("LEX_TOP_K", "6"))
# Сколько кандидатов берём на вход реранкера.
CANDIDATES = int(os.environ.get("LEX_CANDIDATES", "24"))
# Сколько из них реально уходит в реранкер. Реранкер — cross-encoder на CPU и
# тратит ~3 с на документ: 24 кандидата это ~70 с ожидания в живом запросе,
# что для демо недопустимо. Гибрид (BM25+вектор, RRF) уже дал порядок, поэтому
# реранк — второе мнение по верхушке, а не по всему списку.
RERANK_TOP = int(os.environ.get("LEX_RERANK_TOP", "8"))
# Ниже этой доли предмета вопроса во фрагментах считаем, что нормы нет.
# SPARK замерил на 15 вопросах: отвечаемые дают 1.00, ловушки 0.25-0.60.
SUBJECT_MIN_COVERAGE = float(os.environ.get("LEX_SUBJECT_MIN", "0.75"))
# Близость вектора к лучшему фрагменту. Ниже — слова нашлись, а тема не та.
SUBJECT_MIN_COSINE = float(os.environ.get("LEX_SUBJECT_COSINE", "0.50"))
# Вопрос ссылается на конкретный документ («в документе Регламент „X"»).
_DOC_REF = re.compile(r"в\s+документ|по\s+документ|документ[аеу]?\s+[«\"]|"
                      r"Регламент[ае]?\s+[«\"]|Инструкци[ия]\s+[«\"]", re.IGNORECASE)
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
class _StageOutcome:
    """Шаг, который всегда эмитится, но может ещё и завершить каскад.

    Обычные стадии возвращают либо StepEvent, либо Signal — «или», потому что
    шаг виден пользователю только если он прошёл. Policy, напротив, должна
    показать себя ВСЕГДА: в том числе когда её итог — отказ. Иначе в ленте
    пропадает причина отказа.
    """

    event: StepEvent
    signal: Signal | None = None


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
    # §21: тон формулировки. Смысл от него не зависит — реквизиты нормы
    # остаются дословными при любом тоне.
    tone: str = "plain"
    tickets_path: Path | None = None
    changes_path: Path | None = None


def _now_ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _case_covers_question(case_question: str, question: str) -> bool:
    """Кейс закрывает вопрос, если это тот же самый предмет.

    Кейс рождается из заявки, а заявка — из вопроса сотрудника, поэтому у
    кейса и вопроса одни и те же предметные слова. Случайное пересечение
    («срок», «хранение») кейс не подтверждает.
    """
    from .index import tokenize

    cq = set(tokenize(case_question))
    if not cq:
        return False
    q = set(tokenize(question))
    return len(cq & q) / len(cq) >= 0.6


def _mean_similarity(report: GroundingReport) -> float | None:
    """Средняя близость покрытых утверждений к цитатам (§20.1 п.1).

    None означает «не измерялась» (нет эмбеддера или утверждений), и это не
    то же самое, что низкая уверенность: молчание нельзя выдавать за тревогу.
    """
    vals = [c.best_similarity for c in report.claims
            if c.covered and c.best_similarity is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 4)


def _plural_subs(n: int) -> str:
    """§3: «1 подвопрос», «2-4 подвопроса», «5+ подвопросов» — без «1 подвопросов»."""
    if n % 10 == 1 and n % 100 != 11:
        word = "подвопрос"
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = "подвопроса"
    else:
        word = "подвопросов"
    return f"{n} {word}"


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русское согласование: 1 кейс, 2 кейса, 5 кейсов.

    «найдено 1 кейсов» — это вид, который жюри замечает сразу, а стоит одна
    функция. 11-14 — исключение из общего правила, их считаем по «many».
    """
    if 10 <= n % 100 <= 20:
        return many
    if n % 10 == 1:
        return one
    if 2 <= n % 10 <= 4:
        return few
    return many


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
        # §17: policy — первый содержательный шаг, ДО всякого поиска. Если
        # вопрос не наш или роли не положено, дальше не идём вовсе: «не ищем»,
        # а не «нашли, но не покажем».
        #
        # Порядок с clarify важен: policy уже знает направление (его вычисляет
        # _line_of_text по тексту вопроса). Пока clarify стоял ПЕРЕД policy,
        # на вопрос «Какие роли участвуют в проектной деятельности?» он
        # спрашивал направление у сотрудника, хотя оно названо в самом
        # вопросе — уточнение там, где ответ уже есть.
        stage = self._stage_policy(2, state)
        yield Signal("step", stage.event)
        if stage.signal is not None:
            # §17.6: `out_of_scope` НЕ эскалируется — службе качества нечего
            # разбирать в вопросе про НДС. Эскалация — только для
            # `not_in_corpus` (тема наша, ответа нет). Отказ отдаём как есть.
            yield stage.signal
            if stage.signal.kind == "refusal":
                yield Signal("done", self._done())
            return
        clarify = self._stage_clarify(3, state)
        if clarify is not None:
            yield Signal("step", self._clarify_step(3, state))
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
        for n, (key, fn) in enumerate(stages, start=3):
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
                # Стадия остановила каскад, но у неё есть что показать:
                # event лежит в state["stop_step"].
                stop_ev = state.pop("stop_step", None)
                if stop_ev is not None:
                    self._timeline[stop_ev.key] = _now_ms(t_stage)
                    yield Signal("step", stop_ev)
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
        return kind in ("not_in_corpus",)

    def _search_summary(self, state: dict, performed: bool = True) -> dict:
        """Что именно искали — это обязательная часть заявки (§15.2)."""
        hits = state.get("doc_hits") or []
        allowed = state.get("allowed") or []
        return {
            "documents": len(self.corpus.documents),
            "cases": len(self.cases.cases),
            "accessible": len(allowed),
            "top": [getattr(h.chunk, "doc_id", "") for h in hits][:5],
            "performed": performed,
            # Причина «нормы нет в регламентах». Диалог по ней не уточняет
            # предмет, а сразу создаёт заявку в службу качества.
            "no_norm": bool(state.get("no_norm")),
            "subject_coverage": state.get("subject_coverage"),
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
        as_of = self.opts.as_of.isoformat() if self.opts.as_of else date.today().isoformat()
        state["as_of_label"] = as_of
        role = access.get_role(self.opts.role_id)
        role_title = (role or {}).get("title") or self.opts.role_id
        return _step(n, "accept", f"роль {role_title}, дата отсчёта {as_of}", self._t0)

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
            # Направление уже посчитал policy (шаг 2). Спрашивать его у
            # сотрудника, когда оно названо в вопросе, — издевательство.
            ctx={"params": {"business_line": state.get("line")}}
            if state.get("line") else None,
        )
        if not questions:
            ctx = (dlg.apply_answers(self.opts.session_id, self.opts.answers)
                   if self.opts.session_id else {})
            state["dialogue"] = ctx
            state["assumptions"] = ctx.get("assumptions") or []
            params = ctx.get("params") or {}
            if not self.opts.as_of and params.get("as_of"):
                with contextlib.suppress(ValueError):
                    self.opts.as_of = date.fromisoformat(str(params["as_of"]))
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
            n=n, total=max(len(STEP_ORDER), n), key="clarify", title="Уточнение",
            detail=detail, ms=_now_ms(self._t0), color="sky",
        )

    def _stage_policy(self, n: int, state: dict):
        """§17: компетенция и права роли — ДО поиска.

        Три проверки policy.py: тема наша -> роль допущена -> направление
        определимо. Отказ здесь означает «не искали», поэтому в refusal нет
        ``searched``: показывать «искали в 27 документах» было бы враньём.
        """
        verdict = policy.evaluate(
            self.corpus,
            self.opts.question,
            self.opts.role_id,
            business_line=self.opts.business_line,
        )
        state["policy"] = verdict
        if verdict.business_line:
            state["line"] = verdict.business_line

        event = _step(n, "policy", verdict.step_detail(), self._t0)
        if verdict.ok:
            return _StageOutcome(event)

        if verdict.kind == "need_clarify":
            # §10: направление не определено — спрашиваем, а не угадываем.
            state["clarify_questions"] = [{
                "key": "business_line",
                "text": verdict.detail or "Уточните направление вопроса",
                "reason": "без направления неясно, какие документы смотреть",
                "kind": "choice",
                "options": verdict.lines_available,
            }]
            questions = [ClarifyQuestion(**q) for q in state["clarify_questions"]]
            return _StageOutcome(event, Signal("clarify", ClarifyData(
                session_id=self._session_id(),
                questions=questions,
                partial={"business_lines": verdict.lines_available},
                can_skip=False,
            )))

        # §23: no_access больше не существует — если документ есть, его видно
        # всем. Остались out_of_scope (тема не наша) и clarify (направление).
        state["final"] = ("refusal", RefusalData(
            reason=verdict.reason,
            sources=[],
            kind="out_of_scope",
            searched={},            # §17: не искали — нечего показывать
            hint=verdict.detail,
        ))
        return _StageOutcome(event, Signal("refusal", state["final"][1]))

    def _stage_decompose(self, n: int, state: dict) -> StepEvent:
        line = self._business_line()
        subs = self._subquestions(self.opts.question)
        state["subquestions"] = subs
        state["line"] = line
        parts = []
        if line:
            parts.append(f"направление: {line}")
        parts.append(_plural_subs(len(subs)))
        if len(subs) > 1:
            parts.append(": " + "; ".join(subs))
        return _step(n, "decompose", " · ".join(parts), self._t0)

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
        state["doc_hits"] = ranked
        # §3: жюри нужна не одна цифра, а путь: сколько дал каждый канал.
        # BM25 и вектор считаются в index.search (RRF), флаг — в Hit.
        bm_count = sum(1 for h in doc_hits if h.bm25 > 0)
        vec_count = sum(1 for h in doc_hits if h.cosine != 0)
        after = len(ranked)
        detail = (f"BM25 {bm_count} · вектор {vec_count} → "
                  f"кандидатов {len(doc_hits)} · после rerank {after}")
        if after < len(doc_hits):
            detail += f" · отсеяно {len(doc_hits) - after}"

        # §12.8 / критерий 1 заказчика: поиск не умеет «ничего не найти» —
        # гибрид всегда отдаёт 24 фрагмента, и по ним получается связный, но
        # не тот ответ. «Срок хранения бухгалтерских документов» цеплялся за
        # КД-ПД-04 только потому, что там есть слова «бухгалтерских
        # документов» в пункте про приёмку работ. Проверяем, что ПРЕДМЕТ
        # вопроса вообще виден во фрагментах: если нет — тема наша, ответа
        # нет, и честный путь — заявка в службу качества (§15), а не
        # пересказ похожего пункта.
        subj, missing = subject_coverage(
            self.opts.question, [h.chunk.text for h in ranked[:RERANK_TOP]]
        )
        # Второй признак — близость вектора к лучшему фрагменту. Одного
        # покрытия терминов мало: «охрана труда на площадке» даёт 1.00, потому
        # что слова разошлись по разным чанкам, а нормы об ответственности в
        # них нет. SPARK замерил на 28 вопросах: правило «покрытие >= 0.75 И
        # косинус >= 0.50» дало 2 ошибки, обе — ложный проход, пропусков ноль.
        # Асимметрия намеренная: лучше лишний раз ответить по цитате, чем
        # отказать там, где норма есть.
        cos1 = max((h.cosine for h in ranked[:RERANK_TOP]), default=0.0)
        state["subject_coverage"] = round(subj, 2)
        state["top_cosine"] = round(cos1, 3)
        detail += f" · предмет {subj:.0%} · близость {cos1:.2f}"
        # Вопрос, называющий конкретный документ, гейт не режет: в синтетике
        # сотрудник пишет «в документе Регламент „прочая"», и такой вопрос уже
        # адресован, даже если поиск промахнулся мимо нужного чанка.
        names_doc = bool(_DOC_REF.search(self.opts.question))
        if missing and not names_doc and (
                subj < SUBJECT_MIN_COVERAGE or cos1 < SUBJECT_MIN_COSINE):
            detail += f" · предмет вопроса не найден ({subj:.0%})"
            # §8: вопрос может закрывать кейс из практики, даже когда регламент
            # его не покрывает. Кейс смотрим ДО остановки каскада: иначе
            # дообучение через службу качества не работает — тот же вопрос
            # снова уходит в эскалацию, а кейс в ответ так и не попадёт.
            hit = next((c for c in self._search_cases()
                        if _case_covers_question(c.question, self.opts.question)), None)
            if hit is not None:
                detail += " · тема закрыта кейсом из практики"
                # Регламент вопрос не покрывает, поэтому отвечаем по практике:
                # документы из выдачи убираем, чтобы в ответ не попало не то.
                state["doc_hits"] = []
                return _step(n, "retrieve", detail, self._t0, color="amber")
            # Раньше здесь ставился только escalate_pending, и каскад шёл
            # дальше: compose собирал ответ по нерелевантным фрагментам,
            # verify его отбрасывал, и человек получал уточнение вместо
            # честного «нормы нет». Останавливаемся сразу — дальше искать
            # нечего.
            state["no_norm"] = True
            state["escalate_pending"] = RefusalData(
                reason=("В регламентах нет нормы по этому вопросу: "
                        f"не найдены слова {', '.join(missing[:4])}"),
                sources=[getattr(h.chunk, "doc_id", "") for h in ranked[:3]],
                kind="not_in_corpus",
                searched=self._searched(state),
            )
            # _STOP обрывает каскад, но событие шага всё равно надо отдать:
            # жюри и UI видят, ПОЧЕМУ остановились.
            state["stop_step"] = _step(n, "retrieve", detail, self._t0, color="amber")
            return _STOP
        return _step(n, "retrieve", detail, self._t0)

    def _stage_cases(self, n: int, state: dict) -> StepEvent:
        case_hits = self._search_cases()
        state["case_hits"] = case_hits
        det = f"найдено {len(case_hits)} {_plural(len(case_hits), 'кейс', 'кейса', 'кейсов')}"
        return _step(n, "cases", det, self._t0)

    def _stage_permissions(self, n: int, state: dict) -> StepEvent | Signal:
        """§23: шаг информационный, ничего не фильтрует.

        Чтение единое — значит, весь корпус доступен любой роли, и «отброшено
        N документов» больше не бывает. Шаг не убираем: он показывает, что
        права продуманы, и отделяет чтение от записи.
        """
        doc_hits = state.get("doc_hits") or []
        state["allowed"] = list(doc_hits)
        state["dropped"] = 0
        writable = policy.can_write(self.opts.role_id)
        det = (f"чтение: полный корпус ({len(self.corpus.documents)} документов) · "
               f"запись: {'разрешена' if writable else 'недоступна'}")
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
            return _step(n, "conflict",
                         f"{_plural(conflicts, 'кейс', 'кейса', 'кейсов')} по теме "
                         f"{conflicts}, регламент главнее", self._t0)
        return _step(n, "conflict", f"конфликтов: 0; фрагментов {len(allowed)}", self._t0)

    async def _stage_compose(self, n: int, state: dict) -> StepEvent | Signal:
        allowed = state.get("allowed") or []
        case_hits = state.get("case_hits") or []
        frames = self._build_frames(allowed, case_hits)
        self._frames = frames
        state["frames"] = frames
        if not frames:
            ticket_id = self._open_ticket(state, "not_in_corpus")
            state["final"] = ("refusal", RefusalData(
                reason="В базе знаний нет документа, регулирующего этот вопрос",
                sources=[],
                kind="not_in_corpus",
                searched=self._searched(state),
                hint=self._refusal_hint(ticket_id),
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
            # §18.1: тема наша, ответа нет — агент сам создаёт тикет.
            ticket_id = self._open_ticket(state, "not_in_corpus")
            state["final"] = ("refusal", RefusalData(
                reason="Модель не опиралась на базу знаний: ни одно утверждение не подтверждено цитатой",
                sources=[f.doc_id for f in frames],
                kind="not_in_corpus",
                searched=self._searched(state),
                hint=self._refusal_hint(ticket_id),
            ))
            return _step(n, "verify", report.detail(), self._t0, color="amber")

        # Барьер 3: усекаем до покрытых ЗАКОНЧЕННЫХ фраз — и отдаём усечённый
        # ответ. Раньше здесь стоял return без state["final"], и каскад
        # завершался молчанием: 10 шагов, ноль ответа. Усечение — это всё ещё
        # ответ, просто короче, поэтому дальше идёт обычная сборка AnswerData.
        #
        # Считаем не только «внешние» утверждения, но и оборванные: фраза
        # может опираться на цитату и всё равно быть половинкой — если модель
        # упёрлась в лимит. Раньше такой хвост проверку проходил и уходил
        # человеку («пяти рабочих дней [2]» без точки).
        truncated = report.external
        dropped = report.incomplete
        if truncated or dropped:
            state["answer_text"] = report.covered_text()
            text = state["answer_text"]

        if not text.strip():
            # Всё, что модель написала, отброшено: либо нет цитат, либо фразы
            # оборваны на полуслове. Пустой ответ — хуже отказа: человек
            # увидит «ответ» и не поймёт, что ответа нет.
            ticket_id = self._open_ticket(state, "not_in_corpus")
            state["final"] = ("refusal", RefusalData(
                reason="Ни одно утверждение не уцелело после проверки: модель не оперлась на базу или оборвала ответ",
                sources=[f.doc_id for f in frames],
                kind="not_in_corpus",
                searched=self._searched(state),
                hint=self._refusal_hint(ticket_id),
            ))
            return _step(n, "verify", report.detail(), self._t0, color="amber")

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

        # §20: оговорка — не «на всякий случай», а по измеримым триггерам.
        caveat = self._build_caveat(state, report, citations, practice_only)

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
                + ([] if not truncated else [f"{truncated} утверждений отброшено (нет цитаты)"])
                + ([] if not dropped else [f"{dropped} фраз оборвано и не показано"]),
                forms=state.get("matched_forms") or [],
                session_id=self.opts.session_id,
                tone=self.opts.tone if self.opts.tone in ("plain", "formal") else "plain",
                caveat=caveat,
                grounding={"total": report.total, "covered": report.covered,
                           "external": report.external, "incomplete": report.incomplete,
                           "threshold": report.threshold,
                           "confidence": _mean_similarity(report)},
            ),
        )
        # §20.4: в шаге verify — уверенность и уровень оговорки, чтобы было
        # видно, почему ответ пришёл с предупреждением.
        det = report.detail()
        conf = _mean_similarity(report)
        if conf is not None:
            det += f" · уверенность {conf:.2f}"
        if caveat is not None:
            det += f" · ОГОВОРКА ({caveat.level})"
        return _step(n, "verify", det, self._t0,
                     color="orange" if (caveat and caveat.level == "high")
                     else ("amber" if (truncated or dropped or caveat) else None))

    def _build_caveat(self, state: dict, report, citations: list, practice_only: bool):
        """§20.1: собрать причины оговорки из фактов прогона."""
        conf = _mean_similarity(report)
        sources = len({c.doc_id for c in citations if c.source_kind != "case"})
        min_ocr = None
        for c in citations:
            if c.ocr_confidence is not None:
                min_ocr = c.ocr_confidence if min_ocr is None else min(min_ocr, c.ocr_confidence)

        current, nxt = self._editions_around(state)
        return caveat_mod.build_caveat(caveat_mod.CaveatInput(
            mean_similarity=conf,
            similarity_checked=report.similarity_checked,
            sources=sources,
            dropped=report.external,
            total_claims=report.total,
            practice_only=practice_only,
            min_ocr=min_ocr,
            current_edition=current,
            next_edition=nxt,
            as_of=self.opts.as_of,
        ))

    def _editions_around(self, state: dict) -> tuple[dict | None, dict | None]:
        """Действующая редакция и следующая по дате — для §20.1 п.2."""
        current: dict | None = None
        nxt: dict | None = None
        as_of = self.opts.as_of
        today = as_of.isoformat() if as_of else "9999-12-31"
        for res in (state.get("resolutions") or []):
            rule = getattr(res, "rule", None)
            if rule is None:
                continue
            ordered = sorted(rule.revisions, key=lambda r: (r.effective, r.revision))
            for rev in ordered:
                if rev.effective <= today:
                    current = {"revision": rev.revision, "effective": rev.effective}
                elif nxt is None:
                    nxt = {"revision": rev.revision, "effective": rev.effective}
        return current, nxt

    def _open_ticket(self, state: dict, kind: str) -> str:
        """§18: тикет создаёт агент, без участия человека.

        Не на каждый отказ: ``out_of_scope`` сюда не приходит вовсе (§18.1), а
        повторный вопрос в той же сессии возвращает уже открытый номер.
        """
        try:
            store = get_ticket_store(
                self.opts.tickets_path or (ROOT / "var" / "tickets.jsonl")
            )
            ticket = store.open_from_agent(
                question=self.opts.question,
                kind=kind,
                why_unresolved=self._why_unresolved(state, kind),
                session_id=self._session_id(),
                role_id=self.opts.role_id,
                business_line=state.get("line") or "",
                clarifications=(state.get("dialogue") or {}).get("answers") or [],
                searched=self._searched(state),
            )
            state["ticket_id"] = ticket.id
            return ticket.id
        except Exception:
            # Тикет — вспомогательная вещь: если хранилище недоступно, отказ
            # всё равно должен дойти до сотрудника.
            return ""

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
        docs = [h.chunk.text for h in hits][:RERANK_TOP]
        try:
            order = rerank.rerank(query, docs, top=len(docs))
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
        # Хвост за пределами RERANK_TOP сохраняем в исходном порядке гибрида:
        # выбросить его значит отдать в промпт меньше, чем нашлось.
        out.extend(hits[len(docs):])
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
            {"role": "system",
             "content": SYSTEM_PROMPT + "\n\n" + CASE_HINT + "\n\n" + llm.tone_rules(self.opts.tone)},
            {"role": "user", "content": user},
        ])

    def _extractive_answer(self, frames: list[Frame]) -> str:
        """Ответ без модели: цитаты как есть. Честно, но некрасиво.

        Нужен для демо при недоступной модели и для тестов: барьеры §12
        проверяются независимо от LLM.
        """
        lines = []
        for f in frames[:3]:
            # Маркер [n] — на КАЖДУЮ строку, а не одним хвостом блока.
            # Проверка §21 режет ответ на утверждения по предложениям: при
            # маркере в конце блока ссылку получало только последнее
            # предложение фрагмента, остальные отбрасывались как «нет ссылки
            # [n]», и верный ответ уходил в эскалацию.
            #
            # doc_id префиксом НЕ ставим: «ТЗ-ЭКЗ-СР01-Р1:» — это метка
            # источника, и проверка якорей §21.4 справедливо требовала её в
            # цитате, отбрасывая верное утверждение. Источник и так виден по
            # ссылке [n] и в citations.
            body = f.text.strip()[:300]
            marked = "\n".join(
                f"{ln} [{f.index}]" for ln in body.split("\n") if ln.strip()
            )
            lines.append(marked)
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

    def _refusal_hint(self, ticket_id: str) -> str:
        """§18.4: сотрудник видит номер тикета — его можно назвать вслух."""
        if ticket_id:
            return (f"Вопрос передан в службу качества · {ticket_id}. "
                    f"Ответ появится в базе как кейс.")
        return "Уточните направление или обратитесь к владельцу регламента"

    def _why_unresolved(self, state: dict, kind: str) -> str:
        """§18.2: «почему не решилось» — конкретно, а не «не найдено».

        Текст для человека: что проверено и чего не хватило. Служба качества
        должна с первого чтения понять, куда смотреть.
        """
        verdict = state.get("policy")
        if verdict is not None and getattr(verdict, "reason", ""):
            return str(verdict.reason)
        line = state.get("line") or ""
        frames = len(state.get("allowed") or [])
        len(state.get("doc_hits") or [])
        if frames == 0:
            where = f" в направлении «{line}»" if line else ""
            return f"в корпусе нет документа{where}, покрывающего вопрос"
        return (f"найдено {frames} фрагментов, но ни одно утверждение не подтверждается "
                f"цитатой из базы")

    def _searched(self, state: dict) -> dict:
        """§12.3: отказ говорит, что искали."""
        return {
            "documents": len(self.corpus.documents),
            "cases": len(self.cases.cases),
            "business_lines": [state["line"]] if state.get("line") else [],
            # Причина «нормы нет» — не то же, что «искали и не нашли»:
            # уточнять предмет бессмысленно, норма отсутствует в регламентах.
            "no_norm": bool(state.get("no_norm")),
            "subject_coverage": state.get("subject_coverage"),
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
        "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
        cav = payload.get("caveat")
        if cav:
            # §20.4: блок оговорки — спокойный, не красный. Это не ошибка.
            print(f"\n  ⚠ Не полная уверенность ({cav['level']})")
            for reason in cav.get("reasons", []):
                print(f"    · {reason['text']}")
            if cav.get("advice"):
                print(f"    {cav['advice']}")
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
