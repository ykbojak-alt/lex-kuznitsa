"""Ответ с оговоркой (контракт §20, v1.12).

Владелец: «Если агент не уверен в корректности своих ответов — он должен дать
ответ "с оговоркой".»

До этого было два исхода: ответ и отказ. Оговорка — среднее состояние, которого
не хватало. Отказ дороже: он отправляет человека искать самому, поэтому
**сначала оговорка, отказ — последнее средство**.

Триггеры здесь только измеримые — ни одного «модели кажется, что она не
уверена». Модель на этот счёт не спрашивают: она всегда считает, что уверена.

    | триггер              | как измеряется                              |
    |----------------------|---------------------------------------------|
    | low_coverage         | средняя близость утверждений < порога       |
    | edition_conflict     | редакции близки по дате, приоритет неявный  |
    | ocr_low              | ocr_confidence < 0.6 у цитируемого источника|
    | partial_answer       | часть утверждений отброшена (§12.2)         |
    | practice_only        | регламента нет, отвечаем по кейсу (§19.3)   |
    | single_source        | ответ держится на одном фрагменте            |

Уровень (`low`/`medium`/`high`) — не «на глаз», а по таблице соответствия
триггера и его веса. Пороги — в конфиге, не в коде (CAVEAT_MIN_COVERAGE,
CAVEAT_MIN_SOURCES).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date

from .models import Caveat, CaveatReason

# Порог «средней уверенности»: ниже — оговорка «формулировка выведена из
# фрагментов, прямой цитаты нет». 0.72 подобрано так, чтобы точное совпадение
# с цитатой (0.85-0.95) оговорки не давало, а пересказ «своими словами»
# (0.6-0.7) — давал.
CAVEAT_MIN_COVERAGE = float(os.environ.get("CAVEAT_MIN_COVERAGE", "0.72"))
# Ниже этого числа независимых источников — «подтверждения нет».
CAVEAT_MIN_SOURCES = int(os.environ.get("CAVEAT_MIN_SOURCES", "2"))
# Тот же порог, что §9: ниже — «распознан неуверенно».
OCR_LOW = float(os.environ.get("LEX_OCR_LOW_CONFIDENCE", "0.6"))
# Насколько близкими должны быть даты вступления редакций, чтобы считать
# приоритет неявным (дней).
EDITION_NEAR_DAYS = int(os.environ.get("CAVEAT_EDITION_NEAR_DAYS", "30"))

# Вес причины -> уровень. Основание: насколько опасно ошибиться, если сотрудник
# поверит ответу без проверки.
_WEIGHT = {
    "ocr_low": "high",          # текст мог быть распознан неверно целиком
    "edition_conflict": "high",  # ответ может быть устаревшим на дату
    "low_coverage": "medium",    # смысл верен, формулировка не дословная
    "partial_answer": "medium",  # ответ неполон — человек должен знать
    "practice_only": "medium",   # практика не норма
    "single_source": "low",      # один документ — обычное дело, но не подтверждено
}

_LEVEL_ORDER = {"low": 0, "medium": 1, "high": 2}

_ADVICE = {
    "low": "Ответ опирается на базу; оговорка информационная.",
    "medium": "Сверьтесь с цитатой или владельцем регламента, прежде чем действовать.",
    "high": "Не принимайте решение по этому ответу без человека из службы качества.",
}


@dataclass
class CaveatInput:
    """Что оговорка знает о факте ответа. Собирается оркестратором, не моделью."""

    # Средняя близость покрытых утверждений к цитатам (None — не измерялась).
    mean_similarity: float | None = None
    # Сколько независимых документов подтверждают ответ.
    sources: int = 0
    # Отброшено утверждений (§12.2 барьер 3).
    dropped: int = 0
    total_claims: int = 0
    # Ответ по практике без регламента (§19.3).
    practice_only: bool = False
    # Минимальная уверенность OCR среди цитируемых источников (None — нет сканов).
    min_ocr: float | None = None
    # Редакции: что действует на дату и что вступает следом.
    current_edition: dict | None = None
    next_edition: dict | None = None
    as_of: date | None = None


@dataclass
class CaveatBuilder:
    reasons: list[CaveatReason] = field(default_factory=list)

    def add(self, code: str, text: str) -> None:
        if any(r.code == code for r in self.reasons):
            return
        self.reasons.append(CaveatReason(code=code, text=text))

    def build(self) -> Caveat | None:
        if not self.reasons:
            return None
        level = "low"
        for r in self.reasons:
            w = _WEIGHT.get(r.code, "low")
            if _LEVEL_ORDER[w] > _LEVEL_ORDER[level]:
                level = w
        return Caveat(level=level, reasons=list(self.reasons), advice=_ADVICE[level])


def build_caveat(inp: CaveatInput) -> Caveat | None:
    """Собирает оговорку по §20.1. None — уверенность достаточная, оговорки нет."""
    b = CaveatBuilder()

    if inp.mean_similarity is not None and inp.mean_similarity < CAVEAT_MIN_COVERAGE:
        b.add(
            "low_coverage",
            f"формулировка выведена из фрагментов, прямой цитаты нет "
            f"(близость {inp.mean_similarity:.2f} при пороге {CAVEAT_MIN_COVERAGE:.2f})",
        )

    if inp.practice_only:
        b.add("practice_only", "по практике, регламент этот вопрос не покрывает")

    if inp.min_ocr is not None and inp.min_ocr < OCR_LOW:
        b.add(
            "ocr_low",
            f"источник — скан, распознан неуверенно (уверенность {inp.min_ocr:.2f}); "
            f"сверьте с оригиналом",
        )

    if inp.dropped > 0 and inp.total_claims > 0:
        covered = inp.total_claims - inp.dropped
        b.add(
            "partial_answer",
            f"отвечено на {covered} из {inp.total_claims} частей; остальное в базе не найдено",
        )

    if inp.sources and inp.sources < CAVEAT_MIN_SOURCES:
        b.add(
            "single_source",
            f"единственный источник, подтверждения в других документах нет "
            f"(источников {inp.sources} при пороге {CAVEAT_MIN_SOURCES})",
        )

    edition = _edition_reason(inp)
    if edition:
        b.add("edition_conflict", edition)

    return b.build()


def _edition_reason(inp: CaveatInput) -> str:
    """§20.1 п.2: конфликт редакций не разрешён однозначно.

    Неявный приоритет — это когда следующая редакция вступает «вот-вот»: на
    дату ответа она ещё не действует, но человек может принять решение уже
    после её вступления. Молчать об этом нельзя.
    """
    nxt = inp.next_edition or {}
    cur = inp.current_edition or {}
    if not nxt:
        return ""
    try:
        nxt_date = date.fromisoformat(str(nxt.get("effective")))
    except (TypeError, ValueError):
        return ""
    as_of = inp.as_of or date.today()
    gap = (nxt_date - as_of).days
    if gap < 0 or gap > EDITION_NEAR_DAYS:
        return ""
    cur_label = f"ред. {cur.get('revision')} от {cur.get('effective')}" if cur else "действующая редакция"
    return (
        f"на {as_of.isoformat()} действует {cur_label}; ред. {nxt.get('revision')} "
        f"вступает {nxt_date.isoformat()} — после этой даты значение изменится"
    )


def merge_into_notes(caveat: Caveat | None) -> list[str]:
    """Оговорка идёт отдельным полем, но в ленте её удобно видеть и в notes."""
    if caveat is None:
        return []
    return [r.text for r in caveat.reasons]
