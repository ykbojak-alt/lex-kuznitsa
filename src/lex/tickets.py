"""Тикеты в службу качества (контракт §18, v1.10).

Владелец: «Если агент не может ответить — он сам создаёт тикет в службу
качества, к этому вопросу подключаются сотрудники службы качества.»

Сотрудник ничего не нажимает: тикет создаёт **агент**. Поэтому здесь нет
«создать тикет» для человека — есть `TicketStore.open_from_agent`, и отдельно
действия службы качества: назначить, ответить, закрыть.

Номер человекочитаемый — ``LEX-2026-0042``: его называют вслух и вписывают в
письмо. Счётчик годовой, как в делопроизводстве, а не сквозной.

Триггеры (§18.1): ``not_in_corpus`` после 2–3 уточнений, ``conflict_unresolved``,
``ocr_degraded``. ``out_of_scope`` тикет НЕ создаёт — это не наша тема, и
грузить качество чужими вопросами значит обесценить очередь.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import policy

# Что вообще заслуживает тикета. Список закрытый: расширять его — решение
# владельца контракта, а не удобство вызывающего кода.
TRIGGER_KINDS = ("not_in_corpus", "conflict_unresolved", "ocr_degraded",
                 # §12: модельный отказ — тоже повод службе качества
                 # разобрать (фрагменты слабые, промпт не сработал), но
                 # в заявке он должен называться честно, а не «нет в корпусе».
                 "model_refusal")

STATUSES = ("open", "assigned", "answered", "closed")

# SLA из конфига: обычный вопрос — двое суток, срочный — смена.
SLA_HOURS = float(os.environ.get("TICKET_SLA_HOURS", "48"))
SLA_HOURS_HIGH = float(os.environ.get("TICKET_SLA_HOURS_HIGH", "8"))


def _now() -> datetime:
    return datetime.now(UTC)


def _require_write(role_id: str, action: str) -> None:
    """§23.3: запись — только качество и руководство. Нарушение — ошибка.

    Пустая роль означает «не из API»: внутренние вызовы (оркестратор сам
    создаёт тикет) прав не требуют, иначе §18.1 был бы невыполним.
    """
    if role_id and not policy.can_write(role_id):
        raise PermissionError(
            f"роль {role_id!r} не может {action}: запись доступна "
            f"{sorted(policy.WRITE_ROLES)}"
        )


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Ticket:
    id: str
    created_at: str
    created_by: str
    session_id: str
    question: str
    asker: dict = field(default_factory=dict)
    clarifications: list = field(default_factory=list)
    searched: dict = field(default_factory=dict)
    why_unresolved: str = ""
    kind: str = "not_in_corpus"
    priority: str = "normal"
    assignee: str | None = None
    status: str = "open"
    sla_due: str = ""
    # §18.3: что служба качества ответила и чем закончилось — нужно, чтобы
    # цикл «тикет -> кейс» (§16) не терял связь.
    answer: str = ""
    closed_reason: str = ""
    case_id: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "session_id": self.session_id,
            "question": self.question,
            "asker": dict(self.asker),
            "clarifications": list(self.clarifications),
            "searched": dict(self.searched),
            "why_unresolved": self.why_unresolved,
            "kind": self.kind,
            "priority": self.priority,
            "assignee": self.assignee,
            "status": self.status,
            "sla_due": self.sla_due,
            "answer": self.answer,
            "closed_reason": self.closed_reason,
            "case_id": self.case_id,
        }

    @property
    def overdue(self) -> bool:
        return self.status not in ("answered", "closed") and _iso(_now()) > self.sla_due


class TicketStore:
    """``var/tickets.jsonl``: дозапись, полная перезапись при смене статуса.

    JSONL с дозаписью хорош для неизменяемых событий, а тикет меняет статус.
    Поэтому файл — это журнал версий: при изменении пишется новая строка, при
    чтении побеждает последняя по ``id``. Так не теряется история: видно, кто
    взял в работу и когда.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._tickets: dict[str, Ticket] = {}
        self._load()

    def _load(self) -> None:
        self._tickets = {}
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = _ticket_from_dict(raw)
            if t.id:
                self._tickets[t.id] = t

    # ---------------------------------------------------------------- чтение

    @property
    def tickets(self) -> list[Ticket]:
        return sorted(self._tickets.values(), key=lambda t: t.created_at)

    def get(self, ticket_id: str) -> Ticket | None:
        return self._tickets.get(ticket_id)

    def by_session(self, session_id: str) -> Ticket | None:
        """§18.7: дубликат по сессии не создаём — возвращаем существующий."""
        for t in self._tickets.values():
            if session_id and t.session_id == session_id:
                return t
        return None

    def for_asker(self, *, session_id: str = "", role_id: str = "") -> list[Ticket]:
        """§18.4: сотрудник видит только свои тикеты."""
        return [
            t for t in self.tickets
            if (session_id and t.session_id == session_id)
            or (role_id and t.asker.get("role_id") == role_id)
        ]

    def queue(self, *, business_line: str | None = None, status: str | None = None) -> list[Ticket]:
        """§18.3: очередь направления; без направления — общая."""
        out = self.tickets
        if business_line:
            out = [t for t in out if t.asker.get("business_line") == business_line]
        if status:
            out = [t for t in out if t.status == status]
        return out

    # --------------------------------------------------------------- запись

    def next_id(self, year: int | None = None) -> str:
        prefix = f"LEX-{year or _now().year}-"
        n = 0
        for tid in self._tickets:
            if tid.startswith(prefix):
                tail = tid[len(prefix):]
                if tail.isdigit():
                    n = max(n, int(tail))
        return f"{prefix}{n + 1:04d}"

    def open_from_agent(
        self,
        *,
        question: str,
        kind: str,
        why_unresolved: str,
        session_id: str = "",
        role_id: str = "",
        business_line: str = "",
        clarifications: list | None = None,
        searched: dict | None = None,
        priority: str = "normal",
    ) -> Ticket:
        """§18.1: тикет создаёт оркестратор, не человек."""
        if kind not in TRIGGER_KINDS:
            # out_of_scope сюда попасть не должен, но лучше упасть на месте,
            # чем тихо засорить очередь качества чужими вопросами.
            raise ValueError(f"тикет на kind={kind!r} не создаётся (§18.1)")
        with self._lock:
            if session_id:
                existing = self.by_session(session_id)
                if existing is not None and existing.status in ("open", "assigned"):
                    return existing
            now = _now()
            hours = SLA_HOURS_HIGH if priority == "high" else SLA_HOURS
            ticket = Ticket(
                id=self.next_id(now.year),
                created_at=_iso(now),
                created_by="agent",
                session_id=session_id,
                question=(question or "").strip(),
                asker={"role_id": role_id, "business_line": business_line},
                clarifications=list(clarifications or []),
                searched=dict(searched or {}),
                why_unresolved=why_unresolved,
                kind=kind,
                priority=priority,
                assignee=None,
                status="open",
                sla_due=_iso(now + timedelta(hours=hours)),
            )
            self._append(ticket)
            self._tickets[ticket.id] = ticket
            return ticket

    def assign(self, ticket_id: str, assignee: str, *, by_role: str = "") -> Ticket:
        """Взять в работу. §18.3: один человек на тикет — предупреждение, но
        не запрет: в реальности людей двое, а вопрос один.

        §23.3: назначение — запись, только качество и руководство.
        """
        _require_write(by_role, "назначить тикет")
        ticket = self._require(ticket_id)
        with self._lock:
            ticket.assignee = assignee
            ticket.status = "assigned"
            self._append(ticket)
        return ticket

    def answer(self, ticket_id: str, text: str, *, by_role: str = "") -> Ticket:
        _require_write(by_role, "ответить на тикет")
        ticket = self._require(ticket_id)
        if not (text or "").strip():
            raise ValueError("пустой ответ не закрывает тикет")
        with self._lock:
            ticket.answer = text.strip()
            ticket.status = "answered"
            self._append(ticket)
        return ticket

    def close(self, ticket_id: str, reason: str = "", *, by_role: str = "") -> Ticket:
        _require_write(by_role, "закрыть тикет")
        ticket = self._require(ticket_id)
        with self._lock:
            ticket.closed_reason = reason or ticket.closed_reason
            ticket.status = "closed"
            self._append(ticket)
        return ticket

    def link_case(self, ticket_id: str, case_id: str, *, by_role: str = "") -> Ticket:
        """§18.5: ответ службы качества превращается в кейс — связь хранится."""
        _require_write(by_role, "связать тикет с кейсом")
        ticket = self._require(ticket_id)
        with self._lock:
            ticket.case_id = case_id
            self._append(ticket)
        return ticket

    # ------------------------------------------------------------- служебное

    def _require(self, ticket_id: str) -> Ticket:
        ticket = self.get(ticket_id)
        if ticket is None:
            raise KeyError(f"тикет {ticket_id} не найден")
        return ticket

    def _append(self, ticket: Ticket) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(ticket.to_dict(), ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def stats(self) -> dict:
        """§18.6: то, что нужно аналитике, — и ничего лишнего.

        ``top_topics`` считается по первому значимому слову вопроса: грубо, но
        именно так видно «три тикета про единственного поставщика» без чтения
        тикетов. Точная тематика — дело службы качества, не счётчика.
        """
        all_t = self.tickets
        by_status = {s: 0 for s in STATUSES}
        by_line: dict[str, int] = {}
        topics: dict[str, int] = {}
        for t in all_t:
            by_status[t.status] = by_status.get(t.status, 0) + 1
            line = t.asker.get("business_line") or "—"
            by_line[line] = by_line.get(line, 0) + 1
            key = _topic_key(t.question)
            if key:
                topics[key] = topics.get(key, 0) + 1
        return {
            "total": len(all_t),
            "open": by_status.get("open", 0),
            "assigned": by_status.get("assigned", 0),
            "answered": by_status.get("answered", 0),
            "closed": by_status.get("closed", 0),
            "by_business_line": [
                {"line": k, "count": v} for k, v in sorted(by_line.items(), key=lambda x: -x[1])
            ],
            "sla_breached": sum(1 for t in all_t if t.overdue),
            "top_topics": [
                {"topic": k, "count": v}
                for k, v in sorted(topics.items(), key=lambda x: -x[1])[:5]
                if v > 1
            ],
        }


_STOP = frozenset(
    ["как", "какой", "какая", "какие", "что", "кто", "где", "когда", "почему", "сколько", "можно", "нужно", "надо", "если", "ли", "для", "про", "при", "над", "под", "это", "этот", "эта", "эти", "есть", "или", "не"]
)
_WORD_RE = re.compile(r"[а-яёa-z]{4,}")


def _topic_key(question: str) -> str:
    for word in _WORD_RE.findall((question or "").lower()):
        if word not in _STOP:
            return word
    return ""


def _ticket_from_dict(raw: dict) -> Ticket:
    asker = raw.get("asker") or {}
    return Ticket(
        id=str(raw.get("id") or ""),
        created_at=str(raw.get("created_at") or ""),
        created_by=str(raw.get("created_by") or "agent"),
        session_id=str(raw.get("session_id") or ""),
        question=str(raw.get("question") or ""),
        asker={"role_id": str(asker.get("role_id") or ""),
               "business_line": str(asker.get("business_line") or "")},
        clarifications=list(raw.get("clarifications") or []),
        searched=dict(raw.get("searched") or {}),
        why_unresolved=str(raw.get("why_unresolved") or ""),
        kind=str(raw.get("kind") or "not_in_corpus"),
        priority=str(raw.get("priority") or "normal"),
        assignee=raw.get("assignee"),
        status=str(raw.get("status") or "open"),
        sla_due=str(raw.get("sla_due") or ""),
        answer=str(raw.get("answer") or ""),
        closed_reason=str(raw.get("closed_reason") or ""),
        case_id=str(raw.get("case_id") or ""),
    )


_STORES: dict[str, TicketStore] = {}


def get_ticket_store(path: str | Path) -> TicketStore:
    key = str(Path(path).resolve())
    store = _STORES.get(key)
    if store is None:
        store = TicketStore(path)
        _STORES[key] = store
    return store
