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


@dataclass
class Claim:
    text: str
    refs: list[int] = field(default_factory=list)
    best_similarity: float | None = None
    covered: bool = False
    reason: str = ""

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

    report.claims = claims
    return report
