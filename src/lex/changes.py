"""Предложения об изменении регламента (контракт §19.4, v1.11).

Владелец: «информацию в регламент вносят люди. Агент только собирает полезную
информацию для Службы качества.»

Поэтому здесь **нет** метода «изменить регламент» и нет «утвердить». Есть:

* сбор доказательств — какие кейсы расходятся с нормой и в чём;
* счёт частоты — один кейс наблюдение, три сигнал (`CR_MIN_CASES`);
* формулировка расхождения — что именно и где не совпадает;
* двусторонняя связь ``case -> change_request -> новый документ``.

Решает человек: служба качества берёт предложение, владелец регламента
принимает решение, новая редакция появляется обычным путём. И только тогда
агент начнёт отвечать по ней.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import policy

# Порог «это уже система, а не случайность». Один случай — наблюдение,
# три — сигнал (§19.6). Порог в конфиге, потому что в настоящей организации
# он зависит от объёма: 3 кейса из 5 — сигнал, 3 из 500 — ещё нет.
# Каноническое имя — LEX_CR_MIN_CASES (как в api/config.py). Имя без
# префикса LEX_ читается как запасное, чтобы старые .env не потеряли порог.
CR_MIN_CASES = int(
    os.environ.get("LEX_CR_MIN_CASES") or os.environ.get("CR_MIN_CASES", "3")
)

STATUSES = ("proposed", "accepted", "rejected", "merged")


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ChangeRequest:
    id: str
    created_at: str
    created_by: str
    target_doc: str
    rule_key: str
    observed: str
    current_norm: str
    evidence: list = field(default_factory=list)
    proposal: str = ""
    status: str = "proposed"
    assignee: str | None = None
    # §19.5: когда и кем закрыто — чтобы в аналитике было видно, кто решил.
    decided_by: str = ""
    decided_at: str = ""
    merged_doc: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "target_doc": self.target_doc,
            "rule_key": self.rule_key,
            "observed": self.observed,
            "current_norm": self.current_norm,
            "evidence": list(self.evidence),
            "proposal": self.proposal,
            "status": self.status,
            "assignee": self.assignee,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
            "merged_doc": self.merged_doc,
        }

    @property
    def case_count(self) -> int:
        """Сколько кейсов собрано в доказательства."""
        n = 0
        for ev in self.evidence:
            if isinstance(ev, dict):
                n += int(ev.get("count") or (1 if ev.get("case_id") else 0))
        return n

    @property
    def is_signal(self) -> bool:
        """§19.6: наблюдение или сигнал."""
        return self.case_count >= CR_MIN_CASES


class ChangeRequestStore:
    """``var/changes.jsonl`` — журнал версий, как у тикетов.

    Предложение меняет статус (proposed -> accepted -> merged), поэтому при
    изменении дописывается новая строка; при чтении побеждает последняя.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._items: dict[str, ChangeRequest] = {}
        self._load()

    def _load(self) -> None:
        self._items = {}
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
            cr = _from_dict(raw)
            if cr.id:
                self._items[cr.id] = cr

    @property
    def items(self) -> list[ChangeRequest]:
        return sorted(self._items.values(), key=lambda c: c.created_at)

    def get(self, cr_id: str) -> ChangeRequest | None:
        return self._items.get(cr_id)

    def for_rule(self, rule_key: str) -> list[ChangeRequest]:
        """§19.6: связь в обе стороны — по правилу тоже ищем."""
        return [c for c in self._items.values() if c.rule_key == rule_key]

    def for_case(self, case_id: str) -> list[ChangeRequest]:
        out = []
        for cr in self._items.values():
            for ev in cr.evidence:
                if isinstance(ev, dict) and ev.get("case_id") == case_id:
                    out.append(cr)
                    break
        return out

    def next_id(self, year: int | None = None) -> str:
        prefix = f"CR-{year or datetime.now(UTC).year}-"
        n = 0
        for cid in self._items:
            if cid.startswith(prefix):
                tail = cid[len(prefix):]
                if tail.isdigit():
                    n = max(n, int(tail))
        return f"{prefix}{n + 1:04d}"

    def upsert_from_case(
        self,
        *,
        case_id: str,
        target_doc: str,
        rule_key: str,
        observed: str,
        current_norm: str,
        proposal: str = "",
        ticket_ids: list[str] | None = None,
    ) -> ChangeRequest:
        """Кейс с непустым ``deviates_from`` создаёт/дополняет предложение.

        Одно расхождение на правило — одно предложение: три отдельных CR про
        один и тот же срок бесполезны, важна частота. Поэтому кейс
        добавляется в существующее предложение, а не открывает новое.
        """
        with self._lock:
            existing = next(
                (c for c in self._items.values()
                 if c.rule_key == rule_key and c.target_doc == target_doc
                 and c.status in ("proposed", "accepted")),
                None,
            )
            if existing is not None:
                for ev in existing.evidence:
                    if isinstance(ev, dict) and ev.get("case_id") == case_id:
                        break
                else:
                    existing.evidence.append({"case_id": case_id, "count": 1})
                if ticket_ids:
                    existing.evidence.append({"tickets": list(ticket_ids),
                                              "note": "вопросы, породившие кейс"})
                self._append(existing)
                return existing

            cr = ChangeRequest(
                id=self.next_id(),
                created_at=_now_iso(),
                created_by="agent",
                target_doc=target_doc,
                rule_key=rule_key,
                observed=observed,
                current_norm=current_norm,
                evidence=[{"case_id": case_id, "count": 1}]
                + ([{"tickets": list(ticket_ids)}] if ticket_ids else []),
                proposal=proposal or "рассмотреть расхождение с практикой",
            )
            self._append(cr)
            self._items[cr.id] = cr
            return cr

    def set_status(self, cr_id: str, status: str, *, by: str = "",
                   merged_doc: str = "", by_role: str = "") -> ChangeRequest:
        """§19.4/§23.3: статус меняет человек, и только руководство.

        Качество собирает доказательства, но не решает, менять ли норму —
        поэтому здесь `director`, а не «качество или руководство».
        """
        if status not in STATUSES:
            raise ValueError(f"status={status!r} не из {STATUSES}")
        if by_role and not policy.can_write(by_role, action="decide"):
            raise PermissionError(
                f"роль {by_role!r} не может решать судьбу предложения: это "
                f"доступно {sorted(policy.DECIDE_ROLES)}"
            )
        cr = self._items.get(cr_id)
        if cr is None:
            raise KeyError(f"предложение {cr_id} не найдено")
        with self._lock:
            cr.status = status
            cr.decided_by = by or cr.decided_by
            cr.decided_at = _now_iso()
            if merged_doc:
                cr.merged_doc = merged_doc
            self._append(cr)
        return cr

    def stats(self) -> dict:
        """§19.5: в аналитике — сколько предложено, принято, отклонено."""
        items = list(self._items.values())
        by_status = {s: 0 for s in STATUSES}
        by_doc: dict[str, int] = {}
        for c in items:
            by_status[c.status] = by_status.get(c.status, 0) + 1
            by_doc[c.target_doc] = by_doc.get(c.target_doc, 0) + 1
        return {
            "total": len(items),
            "by_status": by_status,
            "top_documents": [
                {"doc_id": k, "count": v}
                for k, v in sorted(by_doc.items(), key=lambda x: -x[1])[:5]
            ],
            "min_cases": CR_MIN_CASES,
        }

    def _append(self, cr: ChangeRequest) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(cr.to_dict(), ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def _from_dict(raw: dict) -> ChangeRequest:
    return ChangeRequest(
        id=str(raw.get("id") or ""),
        created_at=str(raw.get("created_at") or ""),
        created_by=str(raw.get("created_by") or "agent"),
        target_doc=str(raw.get("target_doc") or ""),
        rule_key=str(raw.get("rule_key") or ""),
        observed=str(raw.get("observed") or ""),
        current_norm=str(raw.get("current_norm") or ""),
        evidence=list(raw.get("evidence") or []),
        proposal=str(raw.get("proposal") or ""),
        status=str(raw.get("status") or "proposed"),
        assignee=raw.get("assignee"),
        decided_by=str(raw.get("decided_by") or ""),
        decided_at=str(raw.get("decided_at") or ""),
        merged_doc=str(raw.get("merged_doc") or ""),
    )


_STORES: dict[str, ChangeRequestStore] = {}


def get_change_store(path: str | Path) -> ChangeRequestStore:
    key = str(Path(path).resolve())
    store = _STORES.get(key)
    if store is None:
        store = ChangeRequestStore(path)
        _STORES[key] = store
    return store


def recheck_after_merge(cases, corpus, *, rule_key: str, as_of=None) -> list[str]:
    """§19.4: при ``merged`` перепроверить кейсы по правилу.

    Возвращает id кейсов, которые больше не противоречат норме, — у них флаг
    ``deviates_from`` снимается. Проверка идёт по **новой** редакции корпуса,
    поэтому вызывать это надо после того, как документ уже там.
    """
    from .cases import detect_deviation

    cleared: list[str] = []
    for case in cases.cases:
        if case.rule_key != rule_key or not case.deviates_from:
            continue
        deviates, _text = detect_deviation(
            corpus, question=case.question, answer=case.answer_in_practice,
            rule_key=rule_key, as_of=as_of,
        )
        if not deviates:
            case.deviates_from = []
            case.deviation = ""
            cleared.append(case.id)
    return cleared
