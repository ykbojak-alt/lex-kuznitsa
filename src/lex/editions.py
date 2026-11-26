"""Логика действующей редакции.

ARCHITECTURE §4: не «выбрать самый свежий», а «выбрать самый сильный действующий
на дату». Для правил с редакциями (``manifest.conflict_rules``) это значит:
редакция действует, если её ``effective`` не позже ``as_of`` и это последняя
такая. Если такой нет — правило на эту дату не введено, и честный ответ —
отказ ``no_edition_in_force`` со списком источников, а не «нет в корпусе».

Порядок уровней документов (ARCHITECTURE §4) держим здесь же: он нужен стадии
conflict, когда редакции разных уровней говорят разное про один предмет.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .corpus import Corpus
from .models import DocumentMeta, Rule, RuleRevision


def _plural(n: int, one: str, few: str, many: str) -> str:
    """1 редакция, 2 редакции, 5 редакций. 11–14 — по «many»."""
    if 10 <= n % 100 <= 20:
        return many
    if n % 10 == 1:
        return one
    if 2 <= n % 10 <= 4:
        return few
    return many


# Сильный -> слабый. Чем меньше число, тем сильнее документ.
LEVELS: dict[str, int] = {
    "приказ": 1,
    "распоряжение": 1,
    "политика": 2,
    "регламент": 3,
    "процесс": 3,
    "инструкция": 4,
    "методичка": 4,
    "методические указания": 4,
    "описание продукта": 5,
    "правила лицензирования": 5,
    "стандарт": 5,
    "faq": 6,
    "разъяснение": 6,
}


def level_of(doc: DocumentMeta | None) -> int:
    """Уровень приоритета по виду документа. Неизвестный вид — слабее FAQ."""
    if doc is None:
        return 7
    for field in (doc.subtype, doc.kind):
        key = (field or "").strip().lower()
        if not key:
            continue
        if key in LEVELS:
            return LEVELS[key]
        for name, lvl in LEVELS.items():
            if name in key:
                return lvl
    return 7


def level_name(level: int) -> str:
    for name, lvl in LEVELS.items():
        if lvl == level:
            return name
    return "документ"


@dataclass
class Resolution:
    """Результат стадии resolve для одного правила."""

    rule: Rule
    in_force: RuleRevision | None
    superseded: list[RuleRevision]
    as_of: date | None

    @property
    def ok(self) -> bool:
        return self.in_force is not None

    def detail(self) -> str:
        total = len(self.rule.revisions)
        if self.in_force is None:
            first = self.rule.first_effective or (self.rule.revisions[0].effective if self.rule.revisions else "?")
            return (
                f"{total} {_plural(total, 'редакция', 'редакции', 'редакций')}, "
                f"ни одна не действует на {self.as_of}: "
                f"первая — с {first}"
            )
        tag = f"Р{self.in_force.revision}"
        return (f"{total} {_plural(total, 'редакция', 'редакции', 'редакций')}, "
                f"действует {tag} от {self.in_force.effective}")


def resolve_rule(rule: Rule, as_of: date | None) -> Resolution:
    in_force = rule.effective_on(as_of)
    sup = [r for r in rule.revisions if in_force is None or (r.effective, r.revision) < (in_force.effective, in_force.revision)]
    return Resolution(rule=rule, in_force=in_force, superseded=sup, as_of=as_of)


def resolve_many(corpus: Corpus, keys: list[str], as_of: date | None) -> list[Resolution]:
    out: list[Resolution] = []
    for key in keys:
        rule = corpus.rules.get(key)
        if rule:
            out.append(resolve_rule(rule, as_of))
    return out


def revision_of_doc(corpus: Corpus, doc_id: str) -> tuple[int | None, str | None]:
    """(revision, effective) документа по любой редакции правила из манифеста."""
    for rule in corpus.rules.values():
        for rev in rule.revisions:
            if rev.doc_id == doc_id:
                return rev.revision, rev.effective
    return None, None


def doc_in_force_at(corpus: Corpus, doc_id: str, as_of: date | None) -> bool:
    """Действует ли этот документ-редакция на дату.

    Документы, не привязанные к правилу, всегда действуют: у них нет редакций,
    и отменять их нечем.
    """
    if as_of is None:
        return True
    revision, effective = revision_of_doc(corpus, doc_id)
    if revision is None or not effective:
        return True
    return effective <= as_of.isoformat()
