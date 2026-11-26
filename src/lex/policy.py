"""Policy на входе (контракт §17, v1.9).

Владелец: «Policy на входе должен проверять: касается ли вообще вопрос его
компетенции и роли спрашивающего».

Это проверка **до всякого поиска**: не «нашли, но не покажем», а «не ищем
вообще». Три независимых вопроса, все должны пройти:

1. тема в компетенции — иначе ``refusal.kind = out_of_scope``;
2. роль имеет доступ к направлению — иначе ``no_access``;
3. направление определимо — иначе ``clarify``.

Порядок важен: сначала «наша ли тема», потом «можно ли этой роли». Иначе вопрос
про НДС получит «нет доступа» — это дезинформирует.

Определение — по данным, а не «моделью на глаз»: статьи направления
(``docs/db/ARTICLES-DRAFT.md``, синонимы) и дешёвый BM25 по названиям и первым
абзацам. Пороги — в окружении, не в коде.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .corpus import Corpus, estimate_tokens
from .index import tokenize

# Порог близости к направлению: сколько терминов вопроса должно совпасть со
# статьёй направления. 0.15 — «одно-два ключевых слова из вопроса»; ниже —
# случайное совпадение вроде «заявка» в вопросе про бухгалтерию.
POLICY_MIN_SCORE = float(os.environ.get("POLICY_MIN_SCORE", "0.15"))
# Минимальное число документов в направлении, чтобы считать его покрытым.
POLICY_MIN_LINES = int(os.environ.get("POLICY_MIN_LINES", "1"))
# Дешёвый BM25 по названиям и первым абзацам: ниже — тема не покрыта вообще.
POLICY_MIN_COVERAGE = float(os.environ.get("POLICY_MIN_COVERAGE", "0.08"))

# Направления базы. В манифесте поля business_line пока нет (§11 разрешает
# выводить его из system и типа документа), поэтому карта живёт здесь и
# правится данными, а не логикой.
BUSINESS_LINES: dict[str, dict] = {
    "закупки": {
        "title": "Закупки",
        "keywords": (
            "закупк", "заявк", "поставщик", "согласован", "контрагент", "договор",
            "тендер", "смет", "цена", "стоимост", "сумм", "оплат",
        ),
    },
    "лицензирование": {
        "title": "Лицензирование",
        "keywords": ("лиценз", "сертификат", "аккредитац", "разрешени", "продукт x"),
    },
    "качество": {
        "title": "Качество",
        "keywords": ("качеств", "приёмк", "приемк", "входной контроль", "брак", "парти", "дефект"),
    },
    "хранение": {
        "title": "Хранение",
        "keywords": ("хранени", "журнал", "архив", "срок хранения", "аудит"),
    },
    "персонал": {
        "title": "Персонал",
        "keywords": ("сотрудник", "персонал", "отпуск", "увольнен", "приём на работу", "должност"),
    },
}

# Слова, которые не должны выдавать «в компетенции» сами по себе: они есть в
# любом регламенте и потому ничего не говорят о теме.
_GENERIC = frozenset(
    """документ регламент порядок процедур срок сроки какой какая какие сколько
    нужно надо можн должно должн организац компани систем контур работа процесс""".split()
)


# Вопросы про практику «вне организации» — прямой запрет §12.4 п.4: на «а как
# обычно делают в других компаниях» отвечаем, что база организации это не
# покрывает. Отдельный шаблон, потому что по ключевым словам такой вопрос
# выглядит «нашим» и уходил в поиск.
OUTSIDE_EXPERIENCE = re.compile(
    r"(в других компани|в других организаци|в индустри|в отрасли|по рынку|"
    r"как принято в|мировая практика|за рубежом|у конкурент)",
    re.IGNORECASE,
)


@dataclass
class PolicyVerdict:
    """Итог проверки компетенции."""

    in_scope: bool
    business_line: str | None = None
    score: float = 0.0
    coverage: float = 0.0
    accessible_docs: int = 0
    lines_available: list[str] = field(default_factory=list)
    reason: str = ""
    detail: str = ""
    kind: str = ""  # "" | out_of_scope | no_access | need_clarify
    matched_terms: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.in_scope and not self.kind

    def step_detail(self) -> str:
        if self.kind == "out_of_scope":
            return f"ВНЕ КОМПЕТЕНЦИИ: база не покрывает «{self.business_line or 'эту тему'}»"
        if self.kind == "no_access":
            return f"нет доступа роли к направлению {self.business_line}"
        if self.kind == "need_clarify":
            return "направление не определено — нужно уточнение"
        line = self.business_line or "—"
        return f"в компетенции: да · направление: {line} · доступно {self.accessible_docs} документов"


def available_lines(corpus: Corpus) -> list[dict]:
    """Направления из данных (§11): документы группируются по ключевым словам.

    Пока в манифесте нет ``business_line``, направление выводится из названия и
    типа документа. Это соответствует §11: «до появления поля — считать по
    system и типу».
    """
    counts: dict[str, int] = {line: 0 for line in BUSINESS_LINES}
    for doc in corpus.documents:
        hay = f"{doc.title} {doc.kind} {doc.system}".lower()
        for line, spec in BUSINESS_LINES.items():
            if any(k in hay for k in spec["keywords"]):
                counts[line] += 1
    out = []
    for line, spec in BUSINESS_LINES.items():
        out.append({"id": line, "title": spec["title"], "docs": counts.get(line, 0)})
    return out


def _line_of_text(text: str) -> tuple[str | None, float, list[str]]:
    """Направление по тексту вопроса: лучшее совпадение по ключевым словам."""
    low = text.lower()
    tokens = set(tokenize(low))
    for generic in _GENERIC:
        tokens.discard(generic)
    best: tuple[str | None, float, list[str]] = (None, 0.0, [])
    for line, spec in BUSINESS_LINES.items():
        hits = [k for k in spec["keywords"] if k in low]
        if not hits:
            # запасной путь: совпадение по токенам со стеммингом index.tokenize
            hits = [t for t in tokens if t.startswith(spec["keywords"][0][:5])]
        if not hits:
            continue
        score = len(hits) / max(1, len(tokens) ** 0.5)
        if score > best[1]:
            best = (line, score, hits)
    return best


def _coverage(corpus: Corpus, question: str) -> float:
    """Дешёвый BM25 по названиям и первым абзацам — покрытие темы корпусом.

    Эмбеддинги здесь не нужны: это вход, он должен быть быстрым.
    """
    q_terms = set(tokenize(question)) - _GENERIC
    if not q_terms:
        return 0.0
    best = 0.0
    for doc in corpus.documents:
        path = corpus.root / doc.file if doc.file else None
        head = ""
        if path and path.exists():
            try:
                raw = path.read_text(encoding="utf-8")
                head = raw[: int(1200)]  # примерно первые абзацы
            except OSError:
                head = ""
        terms = set(tokenize(f"{doc.title} {head}")) - _GENERIC
        if not terms:
            continue
        overlap = len(q_terms & terms) / max(1, len(q_terms) ** 0.5)
        best = max(best, overlap)
    return best


def evaluate(
    corpus: Corpus,
    question: str,
    role_id: str = "lawyer",
    *,
    business_line: str | None = None,
    role_lines: list[str] | None = None,
    accessible: dict[str, int] | None = None,
) -> PolicyVerdict:
    """Три проверки §17.2 по порядку: тема -> роль -> определимость.

    ``accessible`` — сколько документов направления роль видит (считает
    access.py, если он есть; без него — все документы направления).
    """
    lines = available_lines(corpus)
    line_counts = {x["id"]: x["docs"] for x in lines}
    known = [x["id"] for x in lines if x["docs"] >= POLICY_MIN_LINES]

    # 0. §12.4 п.4: вопрос про внешнюю практику — не наша база знаний.
    if OUTSIDE_EXPERIENCE.search(question):
        return PolicyVerdict(
            in_scope=False, score=0.0, coverage=0.0, lines_available=known,
            kind="out_of_scope",
            reason="База знаний организации этот вопрос не покрывает",
            detail="Вопрос про практику вне организации — в базе таких сведений нет. "
                   "База покрывает: " + ", ".join(known),
        )

    # 1. Тема в компетенции.
    line, score, hits = _line_of_text(question) if not business_line else (business_line, 1.0, [business_line])
    coverage = _coverage(corpus, question)

    if score < POLICY_MIN_SCORE and coverage < POLICY_MIN_COVERAGE:
        return PolicyVerdict(
            in_scope=False, business_line=None, score=round(score, 3), coverage=round(coverage, 3),
            lines_available=known, matched_terms=hits, kind="out_of_scope",
            reason="Вопрос не относится к базе знаний организации",
            detail="База покрывает: " + ", ".join(known),
        )

    # 2. Роль имеет доступ к направлению.
    allowed_lines = role_lines if role_lines is not None else known
    if business_line is None and line is not None:
        scoped = accessible or {}
        if line not in allowed_lines and "*" not in allowed_lines:
            return PolicyVerdict(
                in_scope=True, business_line=line, score=round(score, 3),
                coverage=round(coverage, 3), lines_available=known, matched_terms=hits,
                kind="no_access",
                reason=f"Вопрос относится к направлению «{line}», но роль {role_id} к нему не допущена",
                detail="Доступные направления: " + ", ".join(allowed_lines or ["—"]),
            )
        docs = scoped.get(line, line_counts.get(line, 0))
        if docs < POLICY_MIN_LINES:
            # Нет документов направления в корпусе — это «не покрыто», а не
            # «нет доступа»: права тут ни при чём, материала просто нет. Для
            # сотрудника разница важная: «пополните базу» vs «вам не положено».
            return PolicyVerdict(
                in_scope=True, business_line=line, score=round(score, 3),
                coverage=round(coverage, 3), lines_available=known, matched_terms=hits,
                kind="out_of_scope",
                reason=f"Направление «{line}» в базе знаний не покрыто: документов нет",
                detail="База покрывает: " + ", ".join(known or ["—"]),
            )
        return PolicyVerdict(
            in_scope=True, business_line=line, score=round(score, 3),
            coverage=round(coverage, 3), accessible_docs=docs,
            lines_available=known, matched_terms=hits,
        )

    # 3. Направление определимо. Не определено — не догадываемся, а спрашиваем
    # (§10), но только если направлений больше одного: при одном спрашивать
    # нечего.
    if line is None:
        if len(known) > 1:
            return PolicyVerdict(
                in_scope=True, business_line=None, score=round(score, 3),
                coverage=round(coverage, 3), lines_available=known, matched_terms=hits,
                kind="need_clarify",
                reason="Направление вопроса не определено однозначно",
                detail="Возможные направления: " + ", ".join(known),
            )
        line = known[0] if known else None

    docs = (accessible or {}).get(line, line_counts.get(line, 0)) if line else 0
    return PolicyVerdict(
        in_scope=True, business_line=line, score=round(score, 3),
        coverage=round(coverage, 3), accessible_docs=docs,
        lines_available=known, matched_terms=hits,
    )


def role_lines_from_id(role_id: str) -> list[str]:
    """Направления по умолчанию для роли (§11: ``default_lines[]``).

    Настоящие роли приходят из ``GET /api/roles``; здесь — разумное умолчание,
    чтобы policy не блокировала демо. Юрист и руководитель работают по всем
    направлениям, отраслевые роли — по своему.
    """
    mapping = {
        "lawyer": ["*"],
        "director": ["*"],
        "quality": ["качество"],
        "it": ["лицензирование"],
    }
    return mapping.get(role_id, ["*"])
