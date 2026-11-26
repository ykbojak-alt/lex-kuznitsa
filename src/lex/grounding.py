"""Проверка покрытия ответа цитатами — барьер 2 (контракт §12).

Правило верхнего уровня: каждое утверждение в ответе обязано опираться на
фрагмент базы знаний. Модель не имеет права дополнять ответ общими знаниями,
даже если они верны.

Здесь проверяется это программно, а не «на глаз модели»:

1. ответ режется на предложения;
2. каждое предложение должно нести ссылку ``[n]`` на существующий фрагмент;
3. ссылки недостаточно — считается эмбеддинг-близость предложения к тексту
   фрагмента; ниже порога — утверждение считается непокрытым.

**Честное ограничение (§12.7):** близость на эмбеддингах — эвристика, а не
доказательство. Она ловит большинство случаев «придумал», но не все, и не даёт
права обещать «100% нет галлюцинаций». Это записано и в docs/LIMITS.md.

Контракт §21 добавляет к проверке близости ещё две программные:

* **якоря** — числа, даты, суммы, коды и номера пунктов из утверждения обязаны
  присутствовать в цитируемом фрагменте. «Пяти рабочих дней» -> «около недели»
  проходит проверку близости (эмбеддинг видит ту же тему), но проваливает
  проверку якорей — и правильно;
* **усилители** — «обычно», «как правило», «в большинстве случаев» меняют
  модальность нормы: «обязан» превращается в «принято». Это самая частая
  утечка при упрощении языка, поэтому она запрещена, если слова нет в цитате.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import numpy as np

# Порог близости: ниже — утверждение считается не покрытым фрагментом.
# 0.45 подобран на корпусе регламентов: перефразировка одной мысли даёт 0.6-0.8,
# «рядом по теме, но не про это» — 0.3-0.45. Порог живёт в конфиге, потому что
# на другом корпусе его придётся сдвинуть.
GROUNDING_THRESHOLD = float(os.environ.get("LEX_GROUNDING_THRESHOLD", "0.45"))

# Предложения короче этого не проверяем отдельно: «Итого:» или «Важно.» —
# это не утверждение, а связка. Считать их нарушением значило бы требовать
# цитату на каждое слово.
MIN_CLAIM_CHARS = 24

_REF_RE = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+)*)\]")
_SENT_RE = re.compile(r"(?<=[.!?…])\s+(?=[«\"(A-ZА-ЯЁ0-9])|\n+")
_HEADING_RE = re.compile(r"^\s*(?:#{1,6}\s|\*\*[^*]+\*\*\s*$)")

# ---------------------------------------------------------------- §21.4 якоря

# Число с необязательным разделителем тысяч и дробной частью. «150 000,50».
_NUM_RE = re.compile(r"\d[\d\s ]*(?:[.,]\d+)?")
# Единица сразу после числа — она часть реквизита: «пять *рабочих дней*» и
# «пять *календарных дней*» — разные нормы, хотя число одно.
_UNIT_RE = re.compile(
    r"\s*(рабоч\w*\s+дн\w*|календарн\w*\s+дн\w*|дн\w*|час\w*|мин\w*|"
    r"руб\w*|коп\w*|процент\w*|%|тыс\w*|млн\w*|год\w*|лет|месяц\w*|недел\w*|"
    r"шт\w*|ед\w*|раз\w*)",
    re.IGNORECASE,
)
# Коды и номера пунктов: «44-ФЗ», «п. 4 ч.1 ст.93», «ТЗ-ЭКЗ-СР01-Р2», ГОСТ.
_CODE_RE = re.compile(
    r"(?:(?:п|ч|ст|пп|подп)\.\s*\d+(?:\.\d+)*"
    r"|\d+(?:\.\d+)*\s*(?:ФЗ|ФЗ РФ|ГОСТ(?: Р)?|ТУ)"
    r"|[А-ЯЁA-Z]{2,}(?:-[А-ЯЁA-Z0-9]+)+"
    r"|№\s*[\w/-]+)",
)
_DATE_RE = re.compile(
    r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d{1,2}\s+(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*\s+\d{4}",
    re.IGNORECASE,
)

# Усилители, меняющие модальность нормы (§21.4 п.3). Список — данные, не логика:
# расширять его значит менять контракт, поэтому он здесь целиком и по-русски.
AMPLIFIERS = (
    "обычно", "как правило", "в большинстве случаев", "как показывает практика",
    "зачастую", "нередко", "порой", "в основном", "по общему правилу",
    "чаще всего", "как принято", "во многих случаях", "не обязательно",
    "в целом", "примерно", "около", "приблизительно", "порядка",
)
_AMPLIFIER_RES = tuple(
    (phrase, re.compile(r"\b" + re.escape(phrase).replace(r"\ ", r"\s+") + r"\b", re.IGNORECASE))
    for phrase in AMPLIFIERS
)


def _norm_num(raw: str) -> str:
    """«150 000,50» -> «150000.5»: сравнение чисел без форматирования."""
    s = raw.replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return f"{float(s):g}"
    except ValueError:
        return s


# Числительные словами — половина корпуса пишет «пяти рабочих дней», и без
# этой карты проверка якорей пропускала бы ровно те предложения, которые
# переписывают норму словами. Ключ — основа после отбрасывания окончания.
_NUM_WORDS: dict[str, int] = {
    "один": 1, "одн": 1, "два": 2, "дв": 2, "три": 3, "тр": 3, "четыр": 4,
    "пят": 5, "шест": 6, "сем": 7, "восем": 8, "девят": 9, "десят": 10,
    "одиннадцат": 11, "двенадцат": 12, "тринадцат": 13, "четырнадцат": 14,
    "пятнадцат": 15, "шестнадцат": 16, "семнадцат": 17, "восемнадцат": 18,
    "девятнадцат": 19, "двадцат": 20, "тридцат": 30, "сорок": 40,
    "пятьдесят": 50, "пятдесят": 50, "шестьдесят": 60, "семьдесят": 70,
    "восемьдесят": 80, "девяносто": 90, "сто": 100, "двести": 200,
    "триста": 300, "четыреста": 400, "пятьсот": 500, "тысяч": 1000,
}
_NUM_WORD_RE = re.compile(
    r"\b(" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) + r")\w*\b",
    re.IGNORECASE,
)
_REF_STRIP_RE = re.compile(r"\[\d+(?:\s*[,;]\s*\d+)*\]")


def _words_to_digits(text: str) -> str:
    """«пяти рабочих дней» -> «5 рабочих дней» (для извлечения якорей)."""
    return _NUM_WORD_RE.sub(lambda m: str(_NUM_WORDS[m.group(1).lower()]), text)


def extract_anchors(text: str) -> set[str]:
    """Реквизиты утверждения: числа с единицами, даты, коды (§21.2).

    Формат якоря выбран так, чтобы «пяти рабочих дней» и «5 рабочих дней»
    считались одним реквизитом, а «три дня» и «три рабочих дня» — разными:
    единица входит в якорь, потому что норма различает именно её.
    """
    out: set[str] = set()
    # Номер ссылки [1] — не реквизит нормы: без вырезания любое утверждение
    # «проваливало» сверку на самом себе.
    body = _REF_STRIP_RE.sub(" ", text or "")

    for code in _CODE_RE.findall(body):
        out.add("code:" + re.sub(r"\s+", " ", code.strip().lower()))

    for date in _DATE_RE.findall(body):
        out.add("date:" + date.strip().lower())

    # Даты уже сняты — иначе «01.06.2026» дало бы ещё и числа 01/06/2026.
    numeric = _DATE_RE.sub(" ", body)
    numeric = _words_to_digits(numeric)

    for m in _NUM_RE.finditer(numeric):
        num = _norm_num(m.group(0))
        tail = numeric[m.end(): m.end() + 40]
        unit_m = _UNIT_RE.match(tail)
        unit = unit_m.group(1).lower() if unit_m else ""
        # Единица нормализуется до основы: «рабочих дней» -> «раб.дн».
        if "раб" in unit:
            unit = "раб.дн"
        elif "календар" in unit:
            unit = "кал.дн"
        elif unit.startswith("дн"):
            unit = "дн"
        elif unit.startswith("час"):
            unit = "час"
        elif unit.startswith("руб") or unit.startswith("коп"):
            unit = "руб"
        elif unit.startswith("процент") or unit == "%":
            unit = "%"
        elif unit.startswith("год") or unit == "лет":
            unit = "год"
        elif unit.startswith("месяц"):
            unit = "мес"
        elif unit.startswith("недел"):
            unit = "нед"
        out.add(f"num:{num}:{unit}")
    return out


def find_amplifiers(text: str) -> list[str]:
    """Усилители в утверждении — те, которых не было в цитате (§21.4 п.3)."""
    low = text or ""
    return [phrase for phrase, rx in _AMPLIFIER_RES if rx.search(low)]


@dataclass
class Claim:
    text: str
    refs: list[int] = field(default_factory=list)
    best_similarity: float | None = None
    covered: bool = False
    reason: str = ""
    # §21.4: чего не хватило — чтобы в verify было видно причину, а не «нет».
    missing_anchors: list[str] = field(default_factory=list)
    added_amplifiers: list[str] = field(default_factory=list)

    @property
    def has_ref(self) -> bool:
        return bool(self.refs)


@dataclass
class GroundingReport:
    claims: list[Claim] = field(default_factory=list)
    threshold: float = GROUNDING_THRESHOLD

    @property
    def total(self) -> int:
        return len(self.claims)

    @property
    def covered(self) -> int:
        return sum(1 for c in self.claims if c.covered)

    @property
    def external(self) -> int:
        """Утверждения без опоры на фрагмент — «внешние знания»."""
        return self.total - self.covered

    @property
    def ok(self) -> bool:
        return self.total > 0 and self.external == 0

    @property
    def nothing_covered(self) -> bool:
        return self.covered == 0

    def detail(self) -> str:
        if self.total == 0:
            return "утверждений 0 · покрыто 0 · внешних знаний 0"
        if self.external == 0:
            return f"утверждений {self.total} · покрыто цитатами {self.covered} · внешних знаний 0"
        return (
            f"утверждений {self.total} · покрыто {self.covered} · "
            f"{self.external} отброшено (нет цитаты)"
        )

    def covered_text(self) -> str:
        """Только покрытые утверждения — для усечения ответа (барьер 3)."""
        keep = [c.text for c in self.claims if c.covered]
        return " ".join(keep).strip()


def split_sentences(text: str) -> list[str]:
    """Режет ответ на предложения, выбрасывая заголовки и пустые строки."""
    out: list[str] = []
    for block in text.split("\n"):
        block = block.strip()
        if not block or _HEADING_RE.match(block):
            continue
        for piece in _SENT_RE.split(block):
            piece = piece.strip().lstrip("-•* ").strip()
            if piece:
                out.append(piece)
    return out


def parse_refs(sentence: str) -> list[int]:
    refs: list[int] = []
    for match in _REF_RE.finditer(sentence):
        for part in re.split(r"[,;]", match.group(1)):
            part = part.strip()
            if part.isdigit():
                refs.append(int(part))
    return refs


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = (np.linalg.norm(a) * np.linalg.norm(b)) or 1e-12
    return float(np.dot(a, b) / denom)


def check_grounding(
    answer: str,
    fragments: list[str],
    *,
    embedder=None,
    threshold: float | None = None,
    min_claim_chars: int = MIN_CLAIM_CHARS,
) -> GroundingReport:
    """Проверяет, что каждое утверждение ответа опирается на фрагмент.

    ``fragments`` — ровно те фрагменты, что ушли в промпт (нумерация ``[1]..[K]``
    совпадает с ней). Фрагмент вне этого списка — не «источник», а выдумка.
    """
    thr = GROUNDING_THRESHOLD if threshold is None else threshold
    report = GroundingReport(threshold=thr)
    sentences = split_sentences(answer or "")
    if not sentences:
        return report

    claims: list[Claim] = []
    for sent in sentences:
        refs = [r for r in parse_refs(sent) if 1 <= r <= len(fragments)]
        claim = Claim(text=sent, refs=refs)
        if len(sent) < min_claim_chars and not refs:
            # Короткая связка без ссылки — не утверждение.
            claim.covered = True
            claim.reason = "связка"
            claims.append(claim)
            continue
        if not refs:
            claim.covered = False
            claim.reason = "нет ссылки [n]"
            claims.append(claim)
            continue
        claim.reason = "ссылка без проверки близости"
        claims.append(claim)

    to_compare = [c for c in claims if c.refs and c.reason == "ссылка без проверки близости"]
    if to_compare and embedder is not None:
        texts = [c.text for c in to_compare]
        frag_texts: list[str] = []
        for c in to_compare:
            frag_texts.extend(fragments[r - 1] for r in c.refs)
        try:
            vecs = embedder(texts + frag_texts)
            pos = len(texts)
            i = 0
            for c in to_compare:
                sent_vec = np.asarray(vecs[i], dtype=np.float64)
                i += 1
                best = 0.0
                for _ in c.refs:
                    frag_vec = np.asarray(vecs[pos], dtype=np.float64)
                    pos += 1
                    best = max(best, _cosine(sent_vec, frag_vec))
                c.best_similarity = round(best, 4)
                if best >= thr:
                    c.covered = True
                    c.reason = f"близость {best:.2f} >= {thr}"
                else:
                    c.covered = False
                    c.reason = f"близость {best:.2f} < {thr}"
        except Exception as exc:
            # Эмбеддер упал — не выдаём непроверенное за проверенное.
            for c in to_compare:
                c.covered = False
                c.reason = f"проверка близости не выполнена: {exc}"
    else:
        # Без эмбеддера опираемся только на наличие ссылки — и говорим об этом.
        for c in to_compare:
            c.covered = True
            c.reason = "ссылка есть, близость не проверялась"

    # §21.4: программный контроль смысла. Порядок важен — сначала якоря, потом
    # усилители: «около пяти дней [1]» проваливает оба, и в причине должно
    # остаться то, что весомее. Искажённый реквизит опаснее смягчения.
    for c in claims:
        if not (c.covered and c.refs):
            continue
        cited = " ".join(fragments[r - 1] for r in c.refs if 1 <= r <= len(fragments))
        anchors = extract_anchors(c.text)
        if anchors:
            present = extract_anchors(cited)
            missing = sorted(anchors - present)
            if missing:
                c.missing_anchors = [_anchor_label(a) for a in missing]
                c.covered = False
                c.reason = "якоря нет в цитате: " + ", ".join(c.missing_anchors[:3])
                continue
        amplified = find_amplifiers(c.text)
        if amplified:
            in_source = find_amplifiers(cited)
            added = [a for a in amplified if a not in in_source]
            if added:
                c.added_amplifiers = added
                c.covered = False
                c.reason = "усилитель, которого нет в цитате: " + ", ".join(added[:3])

    report.claims = claims
    return report


def _anchor_label(anchor: str) -> str:
    """Читаемая подпись якоря для detail — «число 5 раб.дн», не «num:5:раб.дн»."""
    kind, _, rest = anchor.partition(":")
    if kind == "num":
        num, _, unit = rest.partition(":")
        return f"{num} {unit}".strip()
    if kind == "code":
        return rest
    if kind == "date":
        return rest
    return rest
