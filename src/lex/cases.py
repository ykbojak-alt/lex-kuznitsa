"""База реальных кейсов (контракт §8, v1.1).

Кейс — это **не документ**. Документ нормативен и статичен; кейс — наблюдение из
практики: что спросили, что ответили, что произошло. Кейсы участвуют в поиске
наравне с документами, но:

* помечены ``kind="case"`` — UI и оркестратор их различают;
* приоритет **ниже** любого нормативного документа: кейс не может отменить
  действующую редакцию, он может только рассказать, что в практике было иначе;
* если регламент ответа не даёт, а кейс даёт — ответ строится на кейсе с
  пометкой «по практике, регламент не покрывает».

Хранение — ``var/cases.jsonl``, одна строка = один кейс, дописывание атомарно.
Отдельной БД нет: кейсов будут десятки, а не миллионы.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .models import Chunk
from .policy import WRITE_ROLES, can_write

OUTCOMES = ("resolved", "escalated", "rejected", "open")
CONTENT_TYPES = ("text", "image", "scan", "mixed")


@dataclass
class Case:
    id: str
    ts: str
    author_role: str
    question: str
    answer_in_practice: str
    outcome: str = "open"
    author_name: str = ""
    linked_docs: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    rule_key: str = ""
    content_type: str = "text"
    # §19.2: флаг не-нормативности — обязателен и всегда False. Поле читается
    # явно, чтобы никто не спутал кейс с регламентом «по умолчанию».
    normative: bool = False
    source: str = ""
    escalation_id: str = ""
    deviates_from: list[str] = field(default_factory=list)
    deviation: str = ""
    confirmed_by: str = ""
    confirmed_at: str = ""
    # §16.3: кейс из ответа службы качества несёт номер заявки и дату
    # утверждения — без них нечего считать в метрике «тикет -> кейс».
    ticket_id: str = ""
    approved_at: str = ""
    # §23: кейс виден только автору по умолчанию. public=True — исключение,
    # когда практика общая и её должен видеть любой отдел.
    public: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts,
            "author_role": self.author_role,
            "author_name": self.author_name,
            "question": self.question,
            "answer_in_practice": self.answer_in_practice,
            "outcome": self.outcome,
            "linked_docs": list(self.linked_docs),
            "tags": list(self.tags),
            "rule_key": self.rule_key,
            "content_type": self.content_type,
            "kind": "case",
            "normative": False,
            "source": self.source,
            "escalation_id": self.escalation_id,
            "deviates_from": list(self.deviates_from),
            "deviation": self.deviation,
            "confirmed_by": self.confirmed_by,
            "confirmed_at": self.confirmed_at,
            "ticket_id": self.ticket_id,
            "approved_at": self.approved_at,
            "public": self.public,
        }

    @property
    def text(self) -> str:
        """Текст, который идёт в BM25 и эмбеддинги."""
        parts = [f"Вопрос: {self.question}", f"В практике: {self.answer_in_practice}"]
        if self.tags:
            parts.append("Теги: " + ", ".join(self.tags))
        return "\n".join(parts)

    def as_chunk(self) -> Chunk:
        """Кейс в виде чанка, чтобы индекс не знал разницы.

        ``department``/``system`` пустые: кейс не принадлежит отделу, а
        привязывается к ролям по ``author_role`` — фильтр прав к нему не
        применяется (право видеть кейс даёт роль автора, а не отдел).
        """
        return Chunk(
            chunk_id=f"{self.id}#case",
            doc_id=self.id,
            title=f"Кейс: {self.question[:80]}",
            section="практика",
            text=self.text,
            department="",
            system="",
            rule_key=self.rule_key,
            revision=None,
            effective=None,
            position=-1,
        )


class CaseStore:
    """Файловое хранилище кейсов с дозаписью."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._cases: list[Case] = []
        self._cos_cache: tuple[tuple[tuple[str, str], ...], object] | None = None
        self._load()

    def _load(self) -> None:
        self._cases = []
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                # Битая строка не должна лишать доступа ко всей базе: журнал
                # кейсов пишется вручную и частично — это рабочий режим.
                continue
            self._cases.append(_case_from_dict(raw))

    def reload(self) -> None:
        with self._lock:
            self._load()

    # ---------------------------------------------------------------- чтение

    @property
    def cases(self) -> list[Case]:
        return list(self._cases)

    def get(self, case_id: str) -> Case | None:
        return next((c for c in self._cases if c.id == case_id), None)

    def visible(self, role_id: str | None) -> list[Case]:
        """Кейсы, которые роль вправе видеть в поиске.

        Кейс — чужой опыт, а не норматив: он содержит вопрос коллеги и то, как
        его решили. Раньше поиск брал все кейсы разом, и юрист получал в ответ
        случай из практики другой роли.

        Правило намеренно не такое, как у документов: документы нормативны и
        читаются всеми (§23), а кейс принадлежит роли-автору. Видно:

        * всё — роли из ``WRITE_ROLES`` (служба качества и руководство): они
          и так ведут базу, скрывать от них нечего;
        * кейсы, записанные службой качества, — всем: это ответ на вопрос
          сотрудника, который она же и задала. Без этого исключения цикл
          дообучения ломается: кейс создаётся с ``author_role="quality"``, и
          юрист, задавший исходный вопрос, перестал бы его находить;
        * кейсы без автора — всем: это импорт практики, у него нет владельца;
        * кейсы с ``public=True`` — всем: автор явно сказал, что практика
          общая (например, ответ про порядок, не привязанный к отделу);
        * остальное — только автору.

        Сравнение идёт по ДЕЙСТВУЮЩЕМУ id роли (v1.16: ``lawyer``/``it`` —
        псевдонимы ``technical``). Без нормализации старая запись
        ``author_role="lawyer"`` и старый клиент с ``role_id=lawyer`` давали
        пустую выдачу — кейс есть, но не виден ни автору, ни его отделу.
        """
        role = _canonical_role(role_id)
        if not role or role in WRITE_ROLES:
            return list(self._cases)
        return [
            c for c in self._cases
            if c.public
            or _canonical_role(c.author_role) == role
            or c.author_role == ""
            or c.author_role in WRITE_ROLES
        ]

    def find(
        self,
        *,
        limit: int | None = 50,
        role_id: str | None = None,
        tag: str | None = None,
        rule_key: str | None = None,
    ) -> list[Case]:
        """Фильтры контракта §8: (limit, role_id, tag, rule_key)."""
        out = self._cases
        if role_id:
            out = [c for c in out if c.author_role == role_id]
        if tag:
            low = tag.lower()
            out = [c for c in out if any(low in t.lower() for t in c.tags)]
        if rule_key:
            out = [c for c in out if c.rule_key == rule_key]
        out = sorted(out, key=lambda c: c.ts, reverse=True)
        return out[:limit] if limit else out

    def search(
        self,
        question: str,
        *,
        role_id: str | None = None,
        top: int = 3,
    ) -> list[Case]:
        """Кейсы по вопросу — теми же средствами, что документы (§8).

        До этого кейсы искались самодельным пересечением слов: косинус не
        учитывался вовсе, и перефразированный вопрос («сколько времени на
        командировку» против «какой срок командировки») кейс не находил.
        Теперь гибрид как у ``SearchIndex``: BM25 + вектор, слияние RRF.
        RRF, а не сумма: BM25 не нормирован, косинус в [-1, 1].

        Отклонённые кейсы (``outcome="rejected"``) в выдачу не попадают —
        это и есть немедленная инвалидация: они остаются в файле для
        истории, но поиск их не видит.
        """
        pool = [c for c in self.visible(role_id) if c.outcome != "rejected"]
        if not pool:
            return []
        q_terms = _tokenize(question)
        if not q_terms:
            return []

        bm_scores = _bm25_scores(q_terms, [c.text for c in pool])
        cos_scores = self._case_cosines(pool, question)
        bm_rank = sorted(range(len(pool)), key=lambda i: -bm_scores[i])
        cos_rank = sorted(range(len(pool)), key=lambda i: -cos_scores[i])
        fused: dict[int, float] = {}
        for rank, i in enumerate(bm_rank):
            if bm_scores[i] > 0:
                fused[i] = fused.get(i, 0.0) + 1.0 / (60 + rank + 1)
        for rank, i in enumerate(cos_rank):
            if cos_scores[i] > 0:
                fused[i] = fused.get(i, 0.0) + 1.0 / (60 + rank + 1)
        ordered = sorted(fused.items(), key=lambda kv: -kv[1])[:top]
        return [pool[i] for i, _ in ordered]

    def _case_cosines(self, pool: list[Case], question: str) -> list[float]:
        """Косинус вопроса к каждому кейсу. Векторы кейсов кэшируются.

        Без кэша каждый вопрос эмбеддил бы все кейсы заново, и это дважды за
        прогон (стадии cases и retrieve). Ключ — набор id и текстов: правка
        кейса или удаление сбрасывают кэш сами.
        """
        key = tuple((c.id, c.text) for c in pool)
        with self._lock:
            cached = self._cos_cache
        if cached is not None and cached[0] == key:
            mat = cached[1]
        else:
            try:
                import numpy as np

                from .index import embed_texts

                raw = embed_texts([c.text for c in pool])
                mat = np.asarray(raw, dtype=np.float64)
                mat /= np.linalg.norm(mat, axis=1, keepdims=True) + 1e-12
            except Exception:
                # Без эмбеддера кейсы всё равно находятся по BM25 — деградация,
                # а не отказ. Видно в /api/health, как и у документов.
                return [0.0] * len(pool)
            with self._lock:
                self._cos_cache = (key, mat)
        try:
            import numpy as np

            from .index import embed_texts

            qv = np.asarray(embed_texts([question])[0], dtype=np.float64)
            qv /= np.linalg.norm(qv) + 1e-12
            return [float(x) for x in (mat @ qv)]
        except Exception:
            return [0.0] * len(pool)

    def next_id(self) -> str:
        n = 0
        for c in self._cases:
            if c.id.startswith("case-"):
                try:
                    n = max(n, int(c.id.split("-", 1)[1]))
                except ValueError:
                    continue
        return f"case-{n + 1:04d}"

    # --------------------------------------------------------------- запись

    def add(
        self,
        *,
        question: str,
        answer_in_practice: str,
        outcome: str = "open",
        author_role: str = "",
        author_name: str = "",
        linked_docs: Iterable[str] | None = None,
        tags: Iterable[str] | None = None,
        rule_key: str = "",
        content_type: str = "text",
        case_id: str | None = None,
        source: str = "",
        escalation_id: str = "",
        confirmed_by: str = "",
        deviation: str = "",
        ticket_id: str = "",
        approved_at: str = "",
        public: bool = False,
        corpus=None,
        as_of=None,
        by_role: str = "",
    ) -> Case:
        """§23.3: добавление кейса — запись, значит нужна роль качества или
        руководства. ``by_role=""`` означает «не из API» (внутренний вызов,
        например подтверждение тикета после проверки прав выше по стеку).
        """
        if by_role and not can_write(by_role):
            raise PermissionError(
                f"роль {by_role!r} не может добавлять кейсы: запись доступна "
                f"{sorted(WRITE_ROLES)}"
            )
        question = (question or "").strip()
        answer = (answer_in_practice or "").strip()
        if not question:
            raise ValueError("кейс без question: нечего искать в будущем")
        if not answer:
            raise ValueError("кейс без answer_in_practice: наблюдение без вывода")
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome={outcome!r} не из {OUTCOMES}")
        if content_type not in CONTENT_TYPES:
            raise ValueError(f"content_type={content_type!r} не из {CONTENT_TYPES}")

        # §19.2: deviates_from заполняется автоматически. «Противоречит» —
        # значит практика называет иное значение, чем действующая на дату
        # редакция того же правила. Проверка консервативная: сомнительное
        # молчание лучше ложного обвинения регламента.
        deviates: list[str] = []
        deviation_text = (deviation or "").strip()
        if corpus is not None:
            deviates, auto_text = detect_deviation(
                corpus, question=question, answer=answer,
                rule_key=rule_key, as_of=as_of,
            )
            deviation_text = deviation_text or auto_text

        with self._lock:
            cid = case_id or self.next_id()
            if self.get(cid):
                raise ValueError(f"кейс {cid} уже есть")
            case = Case(
                id=cid,
                ts=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                author_role=author_role or "",
                author_name=author_name or "",
                question=question,
                answer_in_practice=answer,
                outcome=outcome,
                linked_docs=list(linked_docs or []),
                tags=[str(t) for t in (tags or [])],
                rule_key=rule_key or "",
                content_type=content_type,
                source=source or ("escalation" if escalation_id else "manual"),
                escalation_id=escalation_id or "",
                deviates_from=deviates,
                deviation=deviation_text,
                confirmed_by=confirmed_by or "",
                confirmed_at=(
                    datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                    if confirmed_by else ""
                ),
                ticket_id=ticket_id or "",
                approved_at=approved_at or (
                    datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                    if ticket_id else ""
                ),
                public=bool(public),
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(case.to_dict(), ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._cases.append(case)
            return case

    # ------------------------------------------------------ §16.3: из заявки

    def add_from_ticket(
        self,
        *,
        ticket_id: str,
        question: str,
        answer_in_practice: str,
        author_role: str = "quality",
        author_name: str = "служба качества",
        tags: Iterable[str] | None = None,
        rule_key: str = "",
        escalation_id: str = "",
        approved_at: str = "",
        public: bool = False,
    ) -> Case:
        """Кейс из ответа службы качества на заявку (§16.3).

        Отдельный вход, а не ``add()`` со всеми полями: у кейса из заявки
        обязательно есть номер и дата утверждения, и записывать его без них
        нельзя — иначе метрика «тикет -> кейс» не посчитается, а кейс
        невозможно отозвать вместе с заявкой.

        Идемпотентность: одна заявка — один кейс. Повторный вызов по тому же
        ``ticket_id`` возвращает уже существующий кейс, а не создаёт второй.
        """
        tid = (ticket_id or "").strip()
        if not tid:
            raise ValueError("кейс из заявки без ticket_id: нечем связать")
        with self._lock:
            existing = next((c for c in self._cases if c.ticket_id == tid
                             and c.outcome != "rejected"), None)
        if existing is not None:
            return existing
        return self.add(
            question=question,
            answer_in_practice=answer_in_practice,
            outcome="resolved",
            author_role=author_role,
            author_name=author_name,
            tags=tags,
            rule_key=rule_key,
            source="quality_ticket",
            escalation_id=escalation_id,
            ticket_id=tid,
            approved_at=approved_at,
            public=public,
        )

    # -------------------------------------------------- §8: инвалидация кейса

    def reject(self, case_id: str, *, reason: str = "") -> Case:
        """Отклонить кейс: из поиска он уходит немедленно.

        Не удаляем запись — история отклонений нужна (почему не пошло, кто
        отклонил). Но ``outcome="rejected"`` выводит кейс из выдачи в тот же
        момент, без перезапуска и пересборки индекса: поиск читает файл через
        тот же экземпляр хранилища.
        """
        return self._set_outcome(case_id, "rejected", reason=reason)

    def remove(self, case_id: str) -> bool:
        """Удалить кейс совсем — строку вычёркиваем из файла.

        Нужно, когда кейс ошибочен настолько, что и в истории ему не место
        (описка, чужие персональные данные). Для обычного «это не подходит»
        есть ``reject``: он оставляет след.
        """
        with self._lock:
            before = len(self._cases)
            self._cases = [c for c in self._cases if c.id != case_id]
            if len(self._cases) == before:
                return False
            self._rewrite()
            return True

    def _set_outcome(self, case_id: str, outcome: str, *, reason: str = "") -> Case:
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome={outcome!r} не из {OUTCOMES}")
        with self._lock:
            case = next((c for c in self._cases if c.id == case_id), None)
            if case is None:
                raise KeyError(case_id)
            case.outcome = outcome
            if reason:
                case.deviation = (case.deviation + " · " if case.deviation else "") + reason
            self._rewrite()
            return case

    def _rewrite(self) -> None:
        """Переписать файл целиком. Вызывать только под ``self._lock``."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for c in self._cases:
                fh.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(self.path)


def _canonical_role(role_id: str | None) -> str:
    """Действующий id роли (v1.16): ``lawyer``/``it`` -> ``technical``.

    Кейсы писались под старыми id, а фильтр читает под новыми. Сравнение по
    сырой строке разрывало связь «автор видит свой кейс» — без нормализации
    кейс остаётся в базе, но невидим и автору, и отделу.
    """
    rid = (role_id or "").strip().lower()
    if not rid:
        return ""
    try:
        from .access import ROLE_ALIASES

        return ROLE_ALIASES.get(rid, rid)
    except Exception:
        return rid


def _tokenize(text: str) -> list[str]:
    """Токенизация — та же, что у документов: одно правило поиска на всё."""
    from .index import tokenize

    return tokenize(text)


def _bm25_scores(q_terms: list[str], texts: list[str]) -> list[float]:
    """BM25 кейсов против вопроса. Параметры — из index.py, не свои.

    Кейсов десятки, поэтому индекс строится на каждый запрос: пересчёт по
    всем кейсам дешевле, чем поддержка отдельного кэша IDF.
    """
    from .index import BM25_B, BM25_K1

    docs = [_tokenize(t) for t in texts]
    lengths = [len(t) or 1 for t in docs]
    avgdl = (sum(lengths) / len(lengths)) if lengths else 1.0
    n = max(1, len(docs))
    df: dict[str, int] = {}
    for terms in docs:
        for term in set(terms):
            df[term] = df.get(term, 0) + 1
    idf = {t: math.log(1.0 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    out: list[float] = []
    for terms, length in zip(docs, lengths, strict=True):
        tf: dict[str, int] = {}
        for term in terms:
            tf[term] = tf.get(term, 0) + 1
        score = 0.0
        for term in q_terms:
            freq = tf.get(term)
            tidf = idf.get(term)
            if not freq or tidf is None:
                continue
            score += tidf * freq * (BM25_K1 + 1.0) / (
                freq + BM25_K1 * (1.0 - BM25_B + BM25_B * length / avgdl))
        out.append(score)
    return out


def _case_from_dict(raw: dict) -> Case:
    return Case(
        id=str(raw.get("id") or ""),
        ts=str(raw.get("ts") or ""),
        author_role=str(raw.get("author_role") or ""),
        author_name=str(raw.get("author_name") or ""),
        question=str(raw.get("question") or ""),
        answer_in_practice=str(raw.get("answer_in_practice") or ""),
        outcome=str(raw.get("outcome") or "open"),
        linked_docs=[str(x) for x in (raw.get("linked_docs") or [])],
        tags=[str(x) for x in (raw.get("tags") or [])],
        rule_key=str(raw.get("rule_key") or ""),
        content_type=str(raw.get("content_type") or "text"),
        normative=False,          # §19.2: кейс нормой не бывает никогда
        source=str(raw.get("source") or ""),
        escalation_id=str(raw.get("escalation_id") or ""),
        deviates_from=[str(x) for x in (raw.get("deviates_from") or [])],
        deviation=str(raw.get("deviation") or ""),
        confirmed_by=str(raw.get("confirmed_by") or ""),
        confirmed_at=str(raw.get("confirmed_at") or ""),
        ticket_id=str(raw.get("ticket_id") or ""),
        approved_at=str(raw.get("approved_at") or ""),
        public=bool(raw.get("public") or False),
    )


# ------------------------------------------------- §19.2: расхождение с нормой

_QUANT_RE = re.compile(
    r"(\d+[\d\s]*(?:[.,]\d+)?)\s*"
    r"(рабоч\w*\s+дн\w*|календарн\w*\s+дн\w*|дн\w*|час\w*|руб\w*|процент\w*|%)",
    re.IGNORECASE,
)

_NUM_WORDS = {
    "один": 1, "одного": 1, "одну": 1, "два": 2, "двух": 2, "три": 3, "трёх": 3,
    "трех": 3, "четыре": 4, "четырёх": 4, "пять": 5, "пяти": 5, "шесть": 6,
    "шести": 6, "семь": 7, "семи": 7, "восемь": 8, "восьми": 8, "девять": 9,
    "десяти": 10, "десять": 10, "пятнадцать": 15, "тридцать": 30,
}

# «Три рабочих дня» и «3 рабочих дня» — одно и то же. Без нормализации
# сравнение промахивается мимо половины корпуса, где числа записаны словами.
_WORD_NUM_RE = re.compile(
    r"\b(" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def _quantities(text: str) -> set[tuple[float, str]]:
    """Сколько-чего из текста: {(3.0, "раб.дн"), (150000.0, "руб")}."""
    low = (text or "").lower()
    low = _WORD_NUM_RE.sub(lambda m: str(_NUM_WORDS[m.group(1).lower()]), low)
    out: set[tuple[float, str]] = set()
    for raw_num, unit in _QUANT_RE.findall(low):
        try:
            num = float(raw_num.replace(" ", "").replace(",", "."))
        except ValueError:
            continue
        unit_key = "раб.дн" if "раб" in unit else (
            "кал.дн" if "календар" in unit else unit.rstrip("аыи"))
        out.add((num, unit_key))
    return out


def detect_deviation(
    corpus,
    *,
    question: str,
    answer: str,
    rule_key: str = "",
    as_of=None,
) -> tuple[list[str], str]:
    """§19.2: противоречит ли практика действующей редакции.

    Возвращает ``(deviates_from, deviation)``. Пусто — не противоречит либо
    доказать нечем: обвинять регламент на догадке нельзя, это дороже молчания.

    Сравниваются **величины**: срок в днях, сумма в рублях, проценты. Если в
    практике назван другой срок по тому же правилу — это и есть расхождение.
    """
    from . import editions  # локально: cases не должен тянуть цикл импортов

    # §19.2: сверяем с редакцией, действующей НА ДАТУ, а не с самой свежей
    # существующей. ``as_of=None`` у ``resolve_rule`` означал бы «последняя
    # редакция корпуса» — то есть будущую, ещё не вступившую в силу. Кейс
    # противоречит тому, что действует сейчас.
    if as_of is None:
        as_of = datetime.now(UTC).date()

    key = (rule_key or "").strip()
    doc_ids: list[str] = []
    parts: list[str] = []

    practice = _quantities(answer)

    if key:
        rule = corpus.rules.get(key)
        if rule is None:
            # Имя правила могло быть дано вольно — ищем по подстроке.
            rule = next((r for k, r in corpus.rules.items() if k.lower() == key.lower()), None)
        if rule is not None:
            res = editions.resolve_rule(rule, as_of)
            if res is not None and res.in_force is not None:
                norm = _quantities(res.in_force.value)
                clash = _clash(practice, norm)
                if clash:
                    doc_ids.append(res.in_force.doc_id)
                    parts.append(
                        f"регламент {rule.key} ({res.in_force.doc_id}) требует "
                        f"«{res.in_force.value}»; в практике — «{answer.strip()}»"
                    )
        return doc_ids, "; ".join(parts)

    # rule_key не назван: ищем по теме вопроса. Правило считается «про это»,
    # если и наш вопрос, и значение правила дают общую величину с разными
    # числами — это единственный надёжный признак без модели.
    for rkey, rule in corpus.rules.items():
        if not practice:
            break
        res = editions.resolve_rule(rule, as_of)
        if res is None or res.in_force is None:
            continue
        norm = _quantities(res.in_force.value)
        clash = _clash(practice, norm)
        if clash:
            doc_ids.append(res.in_force.doc_id)
            parts.append(
                f"регламент {rkey} ({res.in_force.doc_id}) требует "
                f"«{res.in_force.value}»; в практике иначе"
            )
    return doc_ids, "; ".join(parts)


def _clash(practice: set[tuple[float, str]], norm: set[tuple[float, str]]) -> bool:
    """Есть ли общая единица с разными числами."""
    for p_num, p_unit in practice:
        for n_num, n_unit in norm:
            if p_unit == n_unit and abs(p_num - n_num) > 1e-9:
                return True
    return False


_STORES: dict[str, CaseStore] = {}


def get_case_store(path: str | Path) -> CaseStore:
    key = str(Path(path).resolve())
    store = _STORES.get(key)
    if store is None:
        store = CaseStore(path)
        _STORES[key] = store
    return store
