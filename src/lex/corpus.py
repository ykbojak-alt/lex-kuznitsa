"""Чтение корпуса и чанкинг.

Корпус — ``sample-corpus/``: ``manifest.json`` + markdown-документы. Чанк режется
по секциям (``## Заголовок``), внутри секции — скользящим окном ~600 токенов с
перекрытием 80. Так делается потому, что ответ почти всегда лежит внутри одной
секции: «Правило SR-01», «Приёмочные критерии». Резать документ целиком значило
бы размывать BM25 по девяти секциям сразу.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import Chunk, CorpusStats, DocumentMeta, Rule, RuleRevision

# Оценка токенов: у bge-m3 токенизатор XLM-R, для русского ~3 символа на токен.
# Точный токенизатор не тянем — это оценка размера окна, а не биллинг.
CHARS_PER_TOKEN = 3.0
CHUNK_TOKENS = 600
CHUNK_OVERLAP_TOKENS = 80

HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / CHARS_PER_TOKEN))


# Пункт регламента: «3.3.1.», «14.5.», «20.2.3.» в начале строки. Иерархия
# сохраняется — «3.3.1» не слипается с «3.3», поэтому ссылка в ответе находит
# ровно тот пункт, который имела в виду.
_CLAUSE_RE = re.compile(r"(?m)^\s*(\d+\.\d+(?:\.\d+)*)\.?\s+")

# Чанк-заголовок: короткий обрывок, который BM25 ставит на первое место.
# Короткий документ + полное покрытие запроса = максимальная плотность
# совпадения (это b=0.75 и делает). Замер NEO 28.11.2026 на 10 реалистичных
# формулировках: 6 таких чанков обгоняли содержательные пункты — «Кто отвечает
# за поиск субподрядчиков?» получал «7.4 Планирование поставок» на #1 вместо
# п. 2.11; «Как обеспечивается планирование качества?» — «7.7» вместо п. 7.7.1.
#
# Фильтр УЗКИЙ, и это важно: 17 коротких чанков, из них 11 — осмысленные
# преамбулы и пункты-перечни («20.2.2. Бюджет проекта;»), их отсекать нельзя.
# Признак заголовка по NEO: короче 40 знаков И либо строка markdown-заголовка,
# либо нет ни одного знака конца предложения (точка с запятой — это пункт
# перечня, норма).
_HEADING_MARK = re.compile(r"^\s*#")
# Номер пункта в начале — его снимаем перед проверкой конца предложения, иначе
# точка в «7.2.» засчитывается как конец и заголовок «7.2. Планирование
# поставок» уходит в индекс как будто у него есть тело.
_LEAD_NUMBER = re.compile(r"^\s*\d+(?:\.\d+)*\.?\s*")
_SENTENCE_END = re.compile(r"[.;:!?…]")
_TITLE_MAX_CHARS = 48


def _is_title_chunk(text: str) -> bool:
    """Заголовок без тела: «7.2. Планирование поставок», «# Проектная деятельность».

    Такие чанки состоят ровно из слов вопроса и ничего не сообщают, поэтому
    BM25 ставит их первыми. Отличие от настоящего пункта-перечня («20.2.2.
    Бюджет проекта;») — знак конца: у перечня это «;» или «:», у заголовка
    ничего. Поэтому снимаем ведущий номер и смотрим, остался ли знак.
    """
    body = (text or "").strip()
    if not body or len(body) >= _TITLE_MAX_CHARS:
        return False
    if _HEADING_MARK.match(body):
        return True
    rest = _LEAD_NUMBER.sub("", body).strip()
    # «7.9. Проверка расходного бюджета проекта» — 40 знаков ровно на границе,
    # и точка в номере уже снята выше; одиночной точки в конце нет, значит тело
    # не кончилось и это заголовок. Хвостовая точка в настоящем пункте
    # («12.1. Проводит РП до начала работ.») остаётся после снятия номера.
    return bool(rest) and not _SENTENCE_END.search(rest)


def _split_clauses(sec_body: str) -> list[str]:
    """Секция → куски по пунктам, сохранённым целиком.

    Зачем: медиана чанка на corpus-real — 1284 символа, и 110 чанков из 154
    содержат 3+ пункта. Модель получает п. 3.1, 3.2, 3.3 и 3.3.1 одним
    куском и берёт соседний пункт вместо спрошенного. Это подтверждено
    прогоном 28.11.2026: ВСЕ пять «неверно во всех прогонах» ответов ушли
    в соседний пункт (1.5 вместо 1.6, 21.6 вместо 21.5 и т.д.), а причина
    отказа на B-q05 — верный п. 3.3.1 (одна строка) утонул в 1732-знаковом
    чанке и не попал в восьмёрку выдачи.

    Текст до первого пункта (преамбула, перечни без нумерации) остаётся
    отдельным куском. Секция без пунктов возвращается как есть — поведение
    прежнее.
    """
    marks = list(_CLAUSE_RE.finditer(sec_body))
    if not marks:
        return [sec_body]
    out: list[str] = []
    head = sec_body[: marks[0].start()].strip()
    if head:
        out.append(head)
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(sec_body)
        piece = sec_body[m.start() : end].strip()
        if piece:
            out.append(piece)
    return out


@dataclass
class Corpus:
    """Загруженный корпус: документы, чанки, правила."""

    root: Path
    documents: list[DocumentMeta] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)
    rules: dict[str, Rule] = field(default_factory=dict)
    watermark: str = ""
    as_of: str = ""

    @property
    def stats(self) -> CorpusStats:
        return CorpusStats(documents=len(self.documents), chunks=len(self.chunks))

    def doc(self, doc_id: str) -> DocumentMeta | None:
        return next((d for d in self.documents if d.doc_id == doc_id), None)

    def departments(self) -> list[str]:
        """Отделы из манифеста — контракт §2 GET /api/roles."""
        seen: list[str] = []
        for d in self.documents:
            if d.department and d.department not in seen:
                seen.append(d.department)
        return seen


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Режет markdown на (заголовок, тело). Текст до первого ``##`` — «Преамбула»."""
    matches = list(HEADING_RE.finditer(text))
    if not matches:
        return [("", text.strip())] if text.strip() else []
    out: list[tuple[str, str]] = []
    head = text[: matches[0].start()].strip()
    if head:
        out.append(("", head))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end() : end].strip()
        if body:
            out.append((m.group(1).strip(), body))
    return out


def _overlap_tail(cur: str, overlap_chars: int) -> str:
    """Хвост для перекрытия — начатый с границы строки, а не с середины слова.

    Срез ``cur[-overlap_chars:]`` даёт чанк, который начинается посреди слова
    («…ных ресурсов»): такая цитата не находится дословно. Берём последние
    целые строки, влезающие в перекрытие; ни одной — перекрытие отменяем.
    """
    if not overlap_chars or len(cur) <= overlap_chars:
        return cur
    cut = len(cur) - overlap_chars
    nl = cur.find("\n", cut)
    return cur[nl + 1 :] if nl >= 0 else ""


def _split_long(text: str, limit_chars: int) -> list[str]:
    """Один сверхдлинный пункт: режем по последнему пробелу до лимита.

    Резка ровно по ``limit_chars`` рвёт слово пополам («…инженер» / «ных
    ресурсов»), и такая цитата не находится дословно ни в документе, ни в
    проверке. Пробел — минимальная граница, которая всегда есть.
    """
    out: list[str] = []
    rest = text
    while len(rest) > limit_chars:
        cut = rest.rfind(" ", 0, limit_chars)
        if cut <= 0:
            cut = limit_chars
        out.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        out.append(rest)
    return out


def _window(text: str, limit: int, overlap: int) -> list[str]:
    """Скользящее окно по абзацам: ~limit токенов, перекрытие overlap."""
    if estimate_tokens(text) <= limit:
        return [text]
    # В реальных регламентах пункты идут подряд без пустых строк: весь документ
    # приходит одним абзацем, и окно по абзацам не срабатывает. Поэтому режем
    # по СТРОКАМ — каждая строка «12.3. …» это целый пункт, и разрыв посреди
    # пункта ломает цитату (проверка §21 требует дословного совпадения).
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    units: list[str] = []
    for p in paras:
        lines = [ln for ln in p.split("\n") if ln.strip()]
        units.extend(lines if len(lines) > 1 else [p])
    limit_chars = int(limit * CHARS_PER_TOKEN)
    overlap_chars = int(overlap * CHARS_PER_TOKEN)
    out: list[str] = []
    cur = ""
    for p in units:
        candidate = f"{cur}\n{p}" if cur else p
        if len(candidate) <= limit_chars:
            cur = candidate
            continue
        if cur:
            out.append(cur)
            tail = _overlap_tail(cur, overlap_chars)
            cur = f"{tail}\n{p}" if tail else p
        else:
            # Один пункт длиннее окна — режем по границам слов, иначе потеряем текст.
            out.extend(_split_long(p, limit_chars))
            cur = ""
    if cur:
        out.append(cur)
    return out


def _doc_meta(d: dict) -> DocumentMeta:
    return DocumentMeta(
        doc_id=str(d.get("doc_id") or ""),
        title=str(d.get("title") or ""),
        kind=str(d.get("kind") or ""),
        subtype=str(d.get("subtype") or ""),
        department=str(d.get("department") or ""),
        system=str(d.get("system") or ""),
        business_line=str(d.get("business_line") or ""),
        owner=str(d.get("owner") or ""),
        version=str(d.get("version") or ""),
        issued=str(d.get("issued") or ""),
        rule_key=str(d.get("rule_key") or ""),
        file=str(d.get("file") or ""),
        sha256=str(d.get("sha256") or ""),
        words=int(d.get("words") or 0),
        sections=[str(s) for s in (d.get("sections") or [])],
        content_type=str(d.get("content_type") or "text"),
        # Внешний нормативный акт — по маркеру в манифесте (source/marker)
        external=str(d.get("marker") or d.get("source") or "").lower() == "внешний",
    )


def load_corpus(
    root: str | Path,
    *,
    chunk_tokens: int = CHUNK_TOKENS,
    overlap_tokens: int = CHUNK_OVERLAP_TOKENS,
) -> Corpus:
    """Читает manifest и документы, строит чанки.

    Отсутствующий файл документа не роняет загрузку: документ остаётся в
    манифесте (он нужен для прав и для отказов), но чанков не даёт. Пропажа
    файла — единственная ошибка, которую тут стоит терпеть: корпус приходит
    от заказчика и может быть неполным.
    """
    base = Path(root).resolve()
    manifest_path = base / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"нет манифеста: {manifest_path}")

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    corpus = Corpus(root=base, watermark=str(raw.get("watermark") or ""), as_of=str(raw.get("as_of") or ""))

    for entry in raw.get("documents") or []:
        meta = _doc_meta(entry)
        if not meta.doc_id:
            continue
        corpus.documents.append(meta)

        path = base / meta.file if meta.file else None
        if not path or not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        revision, effective = _revision_of(meta, raw)
        for sec_title, sec_body in _split_sections(text):
            # Секция режется на пункты ДО окна: окно нужно только для пункта,
            # который сам длиннее лимита (перечень из 20 подпунктов). Так
            # «3.3.1. …» остаётся отдельным чанком, а не тонет в 1732 знаках.
            for piece in _split_clauses(sec_body):
                for window_piece in _window(piece, chunk_tokens, overlap_tokens):
                    if _is_title_chunk(window_piece):
                        # Заголовок в индекс не идёт: он состоит ровно из слов
                        # вопроса и перетягивает поиск на себя, не неся нормы.
                        continue
                    pos = len(corpus.chunks)
                    corpus.chunks.append(
                        Chunk(
                            chunk_id=f"{meta.doc_id}#{pos:04d}",
                            doc_id=meta.doc_id,
                            title=meta.title,
                            section=sec_title,
                            text=window_piece,
                            department=meta.department,
                            system=meta.system,
                            rule_key=meta.rule_key,
                            revision=revision,
                            effective=effective,
                            position=pos,
                            external=meta.external,
                        )
                    )

    for rule in raw.get("conflict_rules") or []:
        key = str(rule.get("key") or "")
        if not key:
            continue
        corpus.rules[key] = Rule(
            key=key,
            field=str(rule.get("field") or ""),
            short=str(rule.get("short") or ""),
            topic=str(rule.get("topic") or ""),
            first_effective=rule.get("first_effective") or None,
            revisions=[
                RuleRevision(
                    revision=int(r.get("revision") or 0),
                    doc_id=str(r.get("doc_id") or ""),
                    effective=str(r.get("effective") or ""),
                    value=str(r.get("value") or ""),
                    file=str(r.get("file") or ""),
                    sha256=str(r.get("sha256") or ""),
                )
                for r in (rule.get("revisions") or [])
            ],
        )

    return corpus


def load_corpus_multi(
    roots: list[str | Path],
    *,
    chunk_tokens: int = CHUNK_TOKENS,
    overlap_tokens: int = CHUNK_OVERLAP_TOKENS,
) -> Corpus:
    """Сливает несколько корпусов в один: первый — базовый (corpus-real).

    Нужен для §12: corpus-real — регламенты заказчика, corpus-norm — внешние
    нормативные акты (кодексы, ФЗ). Ищем по обоим, но документы внешнего
    корня помечаются ``external=True``: их видят не все роли, и в гейтах
    check-core они не входят в счёт «4 внутренних документа».

    Отсутствующий второй корень — не ошибка: демо без нормативного корпуса
    работает как раньше, на одних регламентах.
    """
    roots = [Path(r).resolve() for r in roots]
    bases = [r for r in roots if r.exists()]
    if not bases:
        raise FileNotFoundError("нет ни одного корпуса: " + ", ".join(map(str, roots)))
    merged = load_corpus(bases[0], chunk_tokens=chunk_tokens, overlap_tokens=overlap_tokens)
    for extra_root in bases[1:]:
        extra = load_corpus(extra_root, chunk_tokens=chunk_tokens, overlap_tokens=overlap_tokens)
        # Документы второго корня — внешние. load_corpus уже проставил флаг
        # по маркеру манифеста (marker="внешний" / source="внешний"), но если
        # манифеста с маркером нет — принудительно ставим external на весь
        # не-первый корень: весь файл-корень заведомо нормативный.
        for meta in extra.documents:
            meta.external = True
        offset = len(merged.chunks)
        for chunk in extra.chunks:
            chunk.external = True
            # position у чанка — сквозной в объединённом корпусе
            chunk.position = offset + chunk.position
        merged.documents.extend(extra.documents)
        merged.chunks.extend(extra.chunks)
        merged.rules.update(extra.rules)
        if not merged.watermark and extra.watermark:
            merged.watermark = extra.watermark
        if not merged.as_of and extra.as_of:
            merged.as_of = extra.as_of
    return merged


def _revision_of(meta: DocumentMeta, raw: dict) -> tuple[int | None, str | None]:
    """Номер редакции и дата вступления — из conflict_rules по doc_id."""
    for rule in raw.get("conflict_rules") or []:
        for r in rule.get("revisions") or []:
            if str(r.get("doc_id") or "") == meta.doc_id:
                return int(r.get("revision") or 0) or None, str(r.get("effective") or "") or None
    return None, None
