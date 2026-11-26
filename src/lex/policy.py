"""Policy на входе (контракт §17 v1.9, упрощён §23 v1.15).

Владелец: «Policy на входе должен проверять: касается ли вообще вопрос его
компетенции и роли спрашивающего».

**§23 отменил вторую половину этого требования:** «Права доступа к базе на
чтение у пользователей единые». Значит, проверять «роль имеет доступ к
направлению» больше не нужно — при едином чтении каждый видит весь корпус.

Остаются две проверки:

1. тема в компетенции — иначе ``refusal.kind = out_of_scope``;
2. направление определимо — иначе ``clarify``.

Итог: ``in_scope`` (да/нет) + ``business_line`` (определено/нет).
Отказ ``no_access`` из чтения исчез: если документ существует, его видно всем.

Определение — по данным, а не «моделью на глаз»: статьи направления
(``docs/db/ARTICLES-DRAFT.md``, синонимы) и дешёвый BM25 по названиям и первым
абзацам. Пороги — в окружении, не в коде.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .corpus import Corpus
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
    "проектная деятельность": {
        "title": "Проектная деятельность",
        "keywords": (
            "проект", "ртз", "расчёт трудозатрат", "расчет трудозатрат", "ткп",
            "карточк", "гип", "главный инженер", "роль проект", "этап",
            "инициац", "закрыти проекта", "акт выполненных", "смет",
        ),
    },
    "пресейл": {
        "title": "Пресейл",
        "keywords": (
            "пресейл", "возможн сделк", "аккаунт-менеджер", "менеджер по продаже решений",
            "пресейл-архитектор", "bdm", "tps", "технический пресейл",
            "квалификац", "тендер", "хардоффер",
        ),
        # Аббревиатуры «АМ», «ПА», «SS» — отдельным списком и только целым
        # словом: как подстрока «па» ловится в «паспорт», «компания»,
        # «программа», и направление уезжает на каждый такой вопрос.
        "abbrev": ("ам", "па", "ss", "ртз", "ткп", "гип"),
    },
    "поставка": {
        "title": "Поставка и субподряд",
        "keywords": (
            "поставк", "спецификац", "вендор", "подрядчик", "субподряд",
            "партнёр", "партнер", "оборудован", "лиценз",
        ),
    },
    "договор": {
        "title": "Договор и финансы",
        "keywords": (
            "договор", "согласован договор", "маржинальн", "себестоимост",
            "ставк себестоимост", "бюджет", "оплат", "акт",
        ),
    },
    "команда": {
        "title": "Команда и ресурсы",
        "keywords": (
            "команд", "ресурс", "менеджер ресурс", "назначени", "занятост",
            "роли", "архитектор", "координатор департамент", "руководитель проекта",
        ),
    },
    # --- синтетический корпус sample-corpus (разработка и тесты) ---
    # Эти направления описывают ТОЛЬКО sample-corpus. В corpus-real таких
    # направлений нет (там «проектная деятельность» и «пресейл»), поэтому
    # рантайм на реальном корпусе их подмешивать не должен: иначе вопрос про
    # «закупки» объявляется «в компетенции», поиск идёт по документам, где
    # закупок нет, и агент либо галлюцинирует, либо эскалирует без причины.
    # Включаются только при явном LEX_SYNTHETIC_LINES=1 (тесты).
    "закупки": {
        "title": "Закупки",
        "keywords": (
            "закупк", "заявк", "поставщик", "согласован", "контрагент", "договор",
            "тендер", "смет", "цена", "стоимост", "сумм", "оплат",
        ),
        "synthetic": True,
    },
    "лицензирование": {
        "title": "Лицензирование",
        "keywords": ("лиценз", "сертификат", "аккредитац", "разрешени", "продукт x"),
        "synthetic": True,
    },
    "качество": {
        "title": "Качество",
        "keywords": ("качеств", "приёмк", "приемк", "входной контроль", "брак", "парти", "дефект"),
        "synthetic": True,
    },
    "хранение": {
        "title": "Хранение",
        "keywords": ("хранени", "журнал", "архив", "срок хранения", "аудит"),
        "synthetic": True,
    },
    "персонал": {
        "title": "Персонал",
        "keywords": ("сотрудник", "персонал", "отпуск", "увольнен", "приём на работу", "должност"),
        "synthetic": True,
    },
}

# Слова, которые не должны выдавать «в компетенции» сами по себе: они есть в
# любом регламенте и потому ничего не говорят о теме.
_GENERIC = frozenset(
    ["документ", "регламент", "порядок", "процедур", "срок", "сроки", "какой", "какая", "какие", "сколько", "нужно", "надо", "можн", "должно", "должн", "организац", "компани", "систем", "контур", "работа", "процесс"]
)

# Синтетические направления включаются только явно. По умолчанию рантайм
# работает на corpus-real, где этих направлений нет: включённое «закупки» на
# реальном корпусе означало бы «вопрос в компетенции», хотя документов по
# закупкам в базе не существует.
_SYNTHETIC_LINES_ON = os.environ.get("LEX_SYNTHETIC_LINES", "").lower() in (
    "1", "true", "yes", "on"
)


def active_lines() -> dict[str, dict]:
    """Направления, действующие в текущем режиме корпуса.

    На corpus-real — только реальные (проектная деятельность, пресейл и т.д.);
    синтетические добавляются при ``LEX_SYNTHETIC_LINES=1`` (тесты и прогоны
    на sample-corpus).
    """
    if _SYNTHETIC_LINES_ON:
        return BUSINESS_LINES
    return {k: v for k, v in BUSINESS_LINES.items() if not v.get("synthetic")}


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
    kind: str = ""  # "" | out_of_scope | need_clarify
    matched_terms: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.in_scope and not self.kind

    def step_detail(self) -> str:
        if self.kind == "out_of_scope":
            if self.business_line:
                return f"ВНЕ КОМПЕТЕНЦИИ: база не покрывает «{self.business_line}»"
            # Тема не опознана. Врать «эту тему» нельзя: называем то, что база
            # реально покрывает, либо честно говорим, что направление не определено.
            _lines = active_lines()
            lines = ", ".join(_lines[x]["title"] for x in self.lines_available
                              if x in _lines) if self.lines_available else ""
            if lines:
                return f"ВНЕ КОМПЕТЕНЦИИ: направление не определено · база покрывает: {lines}"
            return "ВНЕ КОМПЕТЕНЦИИ: направление не определено"
        if self.kind == "need_clarify":
            return "направление не определено — нужно уточнение"
        line = self.business_line or "—"
        return f"в компетенции: да · направление: {line}"


def available_lines(corpus: Corpus) -> list[dict]:
    """Направления из данных (§11): документы группируются по ключевым словам.

    Пока в манифесте нет ``business_line``, направление выводится из названия и
    типа документа. Это соответствует §11: «до появления поля — считать по
    system и типу».
    """
    lines_spec = active_lines()
    counts: dict[str, int] = {line: 0 for line in lines_spec}
    for doc in corpus.documents:
        hay = f"{doc.title} {doc.kind} {doc.system}".lower()
        words = set(re.findall(r"[\w-]+", hay))
        for line, spec in lines_spec.items():
            by_word = words & set(spec.get("abbrev") or ())
            if by_word or any(k in hay for k in spec["keywords"]):
                counts[line] += 1
    out = []
    for line, spec in lines_spec.items():
        out.append({"id": line, "title": spec["title"], "docs": counts.get(line, 0)})
    return out


def _line_of_text(text: str) -> tuple[str | None, float, list[str]]:
    """Направление по тексту вопроса: лучшее совпадение по ключевым словам."""
    low = text.lower()
    tokens = set(tokenize(low))
    for generic in _GENERIC:
        tokens.discard(generic)
    words = set(re.findall(r"[\w-]+", low))
    best: tuple[str | None, float, list[str]] = (None, 0.0, [])
    for line, spec in active_lines().items():
        hits = [k for k in spec["keywords"] if k in low]
        # Аббревиатуры — только целым словом, иначе «па» = «паспорт».
        hits += [a for a in (spec.get("abbrev") or ()) if a in words]
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
                head = raw[: 1200]  # примерно первые абзацы
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
) -> PolicyVerdict:
    """Две проверки §23: тема в компетенции -> направление определимо.

    Проверки «роль допущена к направлению» больше нет — §23 сделал чтение
    единым для всех ролей. ``role_id`` остаётся в сигнатуре для аналитики
    (кто о чём спрашивает), но на выдачу не влияет.
    """
    lines = available_lines(corpus)
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

    # 2. Направление определимо. Не определено — не догадываемся, а идём в
    # поиск (§17): направление помогает сузить выдачу, но не является
    # условием ответа. Уточнять его у сотрудника бессмысленно и вредно.
    #
    # §10 v1.17: раньше здесь стоял clarify, и это дало ровно тот луп, на
    # который указал владелец: «Какой срок хранения первичных бухгалтерских
    # документов?» три круга подряд спрашивал «проектная деятельность или
    # пресейл». Ответ на такой вопрос от выбора направления не зависит —
    # направление определяет документ, а не наоборот. На corpus-real это
    # стоило 5 уточнений из 8 вопросов при наличии ответа в базе.
    #
    # Направление остаётся подсказкой (`line_hint`): шаг decompose покажет
    # его, если оно вычислилось из текста; если нет — поиск идёт по всему
    # корпусу и сам находит нужный фрагмент. Уточнение теперь живёт только
    # там, где тема ОПОЗНАНА и не хватает параметра (дата, роль, документ) —
    # это решает dialogue, а не policy.
    return PolicyVerdict(
        in_scope=True, business_line=line, score=round(score, 3),
        coverage=round(coverage, 3), lines_available=known, matched_terms=hits,
        detail="" if line else "направление не названо — поиск по всему корпусу",
    )


# ----------------------------------------------------- §23: чтение и запись

# Кто может писать в базу. Список закрытый и короткий: «править может не
# всякий» — это и есть защита целостности. Руководство утверждает изменения
# регламентов, качество отвечает на вопросы; сотрудник не делает ни того, ни
# другого.
WRITE_ROLES = frozenset({"quality", "director"})

# Решение по предложению об изменении регламента — только руководство (§23.3):
# качество собирает доказательства, но не решает, менять ли норму.
DECIDE_ROLES = frozenset({"director"})


def can_read(role_id: str | None) -> bool:
    """§23 v1.16: чтение разрешено любой известной роли, но не «всем без разбора».

    v1.15 отвечал здесь ``True`` безусловно — и это было честно только потому,
    что фильтр чтения тогда не действовал. Теперь действует: роль читает
    документы своих отделов, значит «может читать» = «роль опознана». Пустой
    корпус для роли — валидное состояние (``not_in_corpus``), а не запрет.
    """
    from .access import resolve_role

    if role_id is None or not str(role_id).strip():
        # Пустая роль — внутренний вызов из ядра, без API-пользователя.
        return True
    return resolve_role(str(role_id)) is not None


def can_write(role_id: str | None, *, action: str = "") -> bool:
    """§23.3: запись — только качество и руководство.

    ``action="decide"`` (смена статуса предложения об изменении) сужается до
    руководства: это решение об изменении нормы, а не ответ на вопрос.
    """
    role = (role_id or "").strip().lower()
    if action == "decide":
        return role in DECIDE_ROLES
    return role in WRITE_ROLES


def write_denied(role_id: str | None, *, write: bool = True) -> dict:
    """Тело ошибки записи (§23.3): 403 с понятной причиной, не тишина."""
    need = sorted(DECIDE_ROLES) if not write else sorted(WRITE_ROLES)
    return {"error": "write_forbidden", "role": role_id or "", "need": need}


def role_lines_from_id(role_id: str) -> list[str]:
    """Направления по умолчанию для роли (аналитика, не доступ).

    §23 v1.16: чтение теперь фильтруется — но по ОТДЕЛУ, а не по направлению
    (направление пересекается у отделов). Функция остаётся для аналитики
    (§23.5): кто о чём спрашивает — нужно для приоритизации обновления
    регламентов. На выдачу не влияет.
    """
    mapping = {
        "lawyer": ["*"],
        "director": ["*"],
        "quality": ["*"],
        "it": ["*"],
        # Реальные отделы корпуса: направление задаётся документом, не ролью.
        "technical": ["*"],
        "ib": ["*"],
        "dbrct": ["*"],
    }
    return mapping.get(role_id, ["*"])
