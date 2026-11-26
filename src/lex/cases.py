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
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(case.to_dict(), ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._cases.append(case)
            return case


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
