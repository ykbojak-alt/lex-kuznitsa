"""Краткий ответ и раскрытие по запросу (контракт §13.1, v1.5).

Владелец: «точная и краткая выжимка — далее в диалоге он может узнать подробности
уже развёрнуто».

Правила:
  * первый ответ — 2-4 предложения, не больше 400 знаков;
  * развёрнутый строится из **тех же** фрагментов и не добавляет новых знаний;
  * барьер §12 действует на оба уровня: раскрытие не повод ослабить проверку.

Раздел документа ищется по заголовку секции чанка: ``exceptions`` ->
«Ограничения и исключения», ``procedure`` -> «Порядок выполнения процедуры»,
``roles`` -> «Роли и ответственность».
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_SHORT_CHARS = 400
MAX_SHORT_SENTENCES = 4

# Ключи разделов контракта -> как они выглядят в корпусе.
SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "purpose": ("назначение", "область применения"),
    "references": ("нормативные ссылки",),
    "criteria": ("приёмочные критерии", "приемочные критерии"),
    "roles": ("роли и ответственность",),
    "exceptions": ("ограничения и исключения", "исключения"),
    "procedure": ("порядок выполнения",),
    "storage": ("порядок хранения", "хранение и аудит"),
    "format": ("требования к оформлению",),
    "nfr": ("нефункциональные требования",),
    "rule": ("правило ",),
}

_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+")


@dataclass
class DetailSection:
    title: str
    quote: str

    def to_dict(self) -> dict:
        return {"title": self.title, "quote": self.quote}


@dataclass
class DetailTable:
    caption: str
    rows: list[list[str]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"caption": self.caption, "rows": self.rows}


def shorten(text: str, *, max_chars: int = MAX_SHORT_CHARS,
            max_sentences: int = MAX_SHORT_SENTENCES) -> str:
    """Сжимает ответ до выжимки: не больше N предложений и M знаков.

    Режем по границам предложений, а не посреди слова: обрезанный посередине
    тезис хуже, чем на одно предложение короче.
    """
    body = " ".join(line.strip() for line in text.strip().splitlines() if line.strip())
    if not body:
        return ""
    sentences = [s.strip() for s in _SENT_SPLIT.split(body) if s.strip()]
    picked: list[str] = []
    for sent in sentences[:max_sentences]:
        candidate = " ".join([*picked, sent])
        if len(candidate) > max_chars and picked:
            break
        picked.append(sent)
    short = " ".join(picked) or body[:max_chars]
    if len(short) > max_chars:
        cut = short[: max_chars - 1]
        # Отрезаем по последнему пробелу, чтобы не рвать слово.
        space = cut.rfind(" ")
        short = (cut[:space] if space > max_chars * 0.6 else cut).rstrip(" ,;:—") + "…"
    return short


def section_key(title: str) -> str | None:
    """Обратный поиск: заголовок секции -> ключ раздела контракта."""
    low = (title or "").lower()
    for key, aliases in SECTION_ALIASES.items():
        if any(alias in low for alias in aliases):
            return key
    return None


def sections_from_chunks(chunks: list) -> list[DetailSection]:
    """Разделы документа из фрагментов, что уже были в контексте."""
    seen: set[tuple[str, str]] = set()
    out: list[DetailSection] = []
    for ch in chunks:
        title = (getattr(ch, "section", "") or "").strip() or "Документ"
        quote = (getattr(ch, "text", "") or "").strip()
        if not quote:
            continue
        key = (getattr(ch, "doc_id", ""), title, quote[:80])
        if key in seen:
            continue
        seen.add(key)
        out.append(DetailSection(title=title, quote=quote))
    return out


def pick_section(sections: list[DetailSection], wanted: str) -> list[DetailSection]:
    """Оставляет разделы по ключу (``exceptions``) — остальные не нужны."""
    if not wanted or wanted == "all":
        return sections
    aliases = SECTION_ALIASES.get(wanted, (wanted,))
    low_aliases = tuple(a.lower() for a in aliases)
    hit = [s for s in sections if any(a in s.title.lower() for a in low_aliases)]
    return hit or sections


def revisions_table(rule) -> DetailTable | None:
    """Таблица «Сроки по редакциям» — из правила корпуса, а не из модели."""
    if rule is None or not rule.revisions:
        return None
    rows = [["Ред.", "Дата вступления", "Значение"]]
    for rev in sorted(rule.revisions, key=lambda r: (r.effective, r.revision)):
        rows.append([f"Р{rev.revision}", rev.effective, rev.value])
    return DetailTable(caption=f"{rule.field or rule.key} по редакциям", rows=rows)
