"""Диалог с уточнением параметров задачи — контракт API-CONTRACT.md §10 (v1.2).

Задача владельца: агент должен уметь вести диалог с сотрудником, уточнять у него
необходимые параметры задачи. Половина реальных вопросов неполна, и агент обязан
спросить, а не догадываться.

Модуль самодостаточен:
  * не знает про HTTP — отдаёт данные (dataclass/dict), SSE собирает api-слой;
  * не знает про шаги ядра — оркестратор сам решает, куда подставить результат;
  * читает только манифест корпуса (правила, отделы, пороги) и ничего в корпус
    не пишет.

Подключение в оркестраторе (три строки):

    dlg = Dialogue()
    questions = dlg.needs_clarification(question, role=role_id, as_of=as_of,
                                         session_id=sid)
    if questions is not None:
        yield ("clarify", dlg.clarify_payload(sid, questions, partial=partial))
        return                      # поток закрывается БЕЗ done (контракт §10)
    ctx = dlg.apply_answers(sid, answers)      # параметры + явные умолчания

Ограничения, зашитые в модуль:
  * максимум 3 вопроса за один круг;
  * максимум 2 круга уточнений на сессию, дальше — явные умолчания;
  * сессия живёт 30 минут без активности;
  * пропущенный параметр всегда превращается в явное умолчание (answer.assumptions).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

QuestionKind = Literal["date", "choice", "text", "doc_id"]

MAX_QUESTIONS_PER_ROUND = 3
MAX_CLARIFY_ROUNDS = 2
# §15 (v1.7): уточняющих итераций должно быть 2..3. Меньше двух — рано
# сдаваться (сотрудник не успел уточнить), больше трёх — допрос. Дальше
# не уточнение, а эскалация в службу качества.
MIN_CLARIFY_ITERATIONS = 2
MAX_CLARIFY_ITERATIONS = 3
DEFAULT_TTL_SECONDS = 30 * 60

#: Канцелярит, которого в уточняющих вопросах быть не должно (§21): уточнять
#: нужно ПОНЯТНО, а не канцелярски. Реквизиты при этом не трогаются.
FORMAL_MARKERS = (
    "согласно", "в соответствии с", "осуществляется", "данный вопрос",
    "вышеуказанн", "определить действующую", "надлежащ",
)

#: Основы слов, по которым вопрос считается «процессным» даже без совпадений
#: со словарём корпуса: такие вопросы уточнить полезно, поэтому «вне базы» их
#: не считаем (§15.1: если не уверен — уточняй, это безопаснее).
GENERIC_QUESTION_STEMS = {
    "срок", "поряд", "проце", "регла", "правил", "докум", "заяв", "догов",
    "заказ", "сделк", "оплат", "постав", "клиент", "сотруд", "сколь",
    "систем", "отдел", "требован",
}

#: Ответы, которые считаются «пропуском» (контракт §10: «не знаю» или отсутствие ключа).
SKIP_ANSWERS = {
    "не знаю", "незнаю", "неизвестно", "не определено", "не определил",
    "нет данных", "нет", "н/д", "нд", "пропустить", "пропуск", "skip",
    "без разницы", "любая", "-", "—", "",
}

#: Человеческие подписи параметров — для detail шага clarify и текста вопросов.
PARAM_LABELS: dict[str, str] = {
    "as_of": "дата отсчёта",
    "department": "подразделение",
    "amount": "сумма",
    "doc_type": "вид документа",
    "rule_key": "правило",
}

#: Умолчания и честные пояснения к ним (контракт §10: answer.assumptions).
PARAM_DEFAULTS: dict[str, tuple[str, str]] = {
    "as_of": ("", "дата не указана — считаю сегодняшней"),
    "department": ("", "подразделение не указано — беру из роли сотрудника"),
    "amount": ("не указана", "сумма не указана — применяю правило целиком"),
    "doc_type": ("любой применимый регламент",
                 "вид документа не указан — беру все применимые"),
    "business_line": ("", "направление не указано — беру все доступные"),
    "rule_key": ("", "правило не указано — выбираю по теме вопроса"),
}

_NUM_WORDS = {2: "две", 3: "три", 4: "четыре", 5: "пять"}


def _num_word(n: int) -> str:
    return _NUM_WORDS.get(n, str(n))


def _norm(text: str | None) -> str:
    """Нормализация текста для сравнения: регистр, «ё», пробелы."""
    if not text:
        return ""
    out = text.lower().replace("\u0451", "е").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", out).strip()


def _fmt_num(value: int) -> str:
    """150000 -> '150 000' (неразрывный пробел — как в регламентах)."""
    return f"{value:,}".replace(",", "\u00a0")


# ------------------------------------------------------------------ извлечение


_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11,
    "декабря": 12,
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6,
    "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11,
    "декабрь": 12,
}

_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_DOT_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
_TEXT_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\s+(\d{4})\b"
)

_AMOUNT_CUR_RE = re.compile(
    r"(\d[\d\s\u00a0]{2,})\s*(?:руб(?:\.|лей|ля)?|р\.|₽)", re.IGNORECASE
)
_AMOUNT_SHORT_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(тыс(?:яч|и|а)?|млн(?:а)?|млрд)\b", re.IGNORECASE
)
_AMOUNT_SPACED_RE = re.compile(r"\b(\d{1,3}(?:[\s\u00a0]\d{3})+)\b")


def extract_date(text: str | None) -> str | None:
    """Ищет явную дату отсчёта в тексте вопроса. Возвращает ISO YYYY-MM-DD."""
    raw = text or ""
    for pattern, order in ((_ISO_DATE_RE, "ymd"), (_DOT_DATE_RE, "dmy"),
                            (_TEXT_DATE_RE, "dmt")):
        m = pattern.search(raw)
        if not m:
            continue
        if order == "ymd":
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        elif order == "dmy":
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:
            d, mo, y = int(m.group(1)), _MONTHS.get(_norm(m.group(2)), 0), int(m.group(3))
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            continue
    return None


def extract_amount(text: str | None) -> str | None:
    """Ищет сумму в тексте вопроса. Возвращает цифры ('150000')."""
    raw = text or ""
    m = _AMOUNT_CUR_RE.search(raw)
    if m:
        digits = re.sub(r"[^0-9]", "", m.group(1))
        if digits:
            return digits
    m = _AMOUNT_SHORT_RE.search(raw)
    if m:
        base = float(m.group(1).replace(",", "."))
        mult = 1000 if m.group(2).lower().startswith("тыс") else (
            1_000_000 if m.group(2).lower().startswith("млн") else 1_000_000_000
        )
        return str(int(base * mult))
    m = _AMOUNT_SPACED_RE.search(raw)
    if m:
        return re.sub(r"[^0-9]", "", m.group(1))
    return None


def _stems_of(text: str | None) -> set[str]:
    """Основы слов (первые 4 буквы) — для сравнения словоформ."""
    out: set[str] = set()
    for word in _norm(text).split():
        if len(word) >= 4:
            out.add(word[:4])
    return out


def match_department(text: str | None, departments: Sequence[str]) -> str | None:
    """Ищет название подразделения в тексте (сначала самые длинные)."""
    hay = _norm(text)
    if not hay:
        return None
    for dep in sorted(departments, key=len, reverse=True):
        needle = _norm(dep)
        if needle and needle in hay:
            return dep
    return None


# ------------------------------------------------------------------ модели


@dataclass
class Question:
    """Один уточняющий вопрос (контракт §10, поле questions[])."""

    key: str
    text: str
    reason: str
    kind: QuestionKind = "text"
    options: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key,
            "text": self.text,
            "reason": self.reason,
            "kind": self.kind,
        }
        if self.kind == "choice" and self.options:
            out["options"] = list(self.options)
        return out

    def __repr__(self) -> str:  # читаемый вывод в DoD-проверке
        opts = f", options={self.options!r}" if self.options else ""
        return (f"Question(key={self.key!r}, kind={self.kind!r}, "
                f"text={self.text!r}, reason={self.reason!r}{opts})")


@dataclass
class Assumption:
    """Явное умолчание, применённое вместо пропущенного параметра."""

    key: str
    value: str
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "value": self.value, "note": self.note}


@dataclass
class Session:
    """Состояние диалога: история, собранные параметры, круги уточнений."""

    session_id: str
    role_id: str = ""
    question: str = ""
    params: dict[str, str] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[dict[str, Any]] = field(default_factory=list)
    rounds: int = 0
    # §15 (v1.7): сколько уточняющих итераций прошло (2..3), что спрашивали
    # и что ответили, что искали, и заявка в службу качества, если она есть.
    clarification_iterations: int = 0
    clarifications: list[dict[str, Any]] = field(default_factory=list)
    searches: list[dict[str, Any]] = field(default_factory=list)
    # Параметры, закрытые умолчанием. Их нельзя считать «названными
    # сотрудником»: по умолчанию спросить про них ещё раз можно.
    defaulted: list[str] = field(default_factory=list)
    escalation: dict[str, Any] | None = None
    exhausted: bool = False
    created_at: float = 0.0
    last_active: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "role_id": self.role_id,
            "question": self.question,
            "params": dict(self.params),
            "history": list(self.history),
            "assumptions": list(self.assumptions),
            "rounds": self.rounds,
            "clarification_iterations": self.clarification_iterations,
            "clarifications": list(self.clarifications),
            "searches": list(self.searches),
            "defaulted": list(self.defaulted),
            "escalation": self.escalation,
            "exhausted": self.exhausted,
            "created_at": self.created_at,
            "last_active": self.last_active,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        return cls(
            session_id=str(data.get("session_id") or ""),
            role_id=str(data.get("role_id") or ""),
            question=str(data.get("question") or ""),
            params={str(k): str(v) for k, v in (data.get("params") or {}).items()},
            history=list(data.get("history") or []),
            assumptions=list(data.get("assumptions") or []),
            rounds=int(data.get("rounds") or 0),
            clarification_iterations=int(
                data.get("clarification_iterations")
                or data.get("rounds") or 0
            ),
            clarifications=list(data.get("clarifications") or []),
            searches=list(data.get("searches") or []),
            defaulted=list(data.get("defaulted") or []),
            escalation=data.get("escalation"),
            exhausted=bool(data.get("exhausted")),
            created_at=float(data.get("created_at") or 0.0),
            last_active=float(data.get("last_active") or 0.0),
        )

    def assumption_for(self, key: str) -> dict[str, Any] | None:
        for item in reversed(self.assumptions):
            if item.get("key") == key:
                return item
        return None


# ------------------------------------------------------------------ хранилище


def default_sessions_path() -> Path:
    """var/sessions.jsonl в корне репозитория (переопределяется env)."""
    env = os.environ.get("LEX_SESSIONS_PATH")
    if env:
        return Path(env)
    var_dir = os.environ.get("LEX_VAR_DIR")
    root = Path(__file__).resolve().parents[2]
    base = Path(var_dir) if var_dir else (root / "var")
    return base / "sessions.jsonl"


@dataclass
class Decision:
    """Что делать после неудачного поиска (§15): ещё итерация или эскалация."""

    questions: list[Question] | None = None
    escalate: dict[str, Any] | None = None
    iterations: int = 0
    out_of_base: bool = False
    reason: str = ""


class SessionStore:
    """Сессии в памяти + журнал var/sessions.jsonl (для перезапуска).

    Файл — append-only: одна строка = снимок состояния сессии. При загрузке
    побеждает последняя строка по session_id (то есть актуальное состояние).
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        clock=time.time,
    ) -> None:
        self.path = Path(path) if path else default_sessions_path()
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}
        self._loaded = False

    # ---- внутреннее ----

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path.exists():
            return
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = obj.get("session_id")
            state = obj.get("state")
            if not sid or not isinstance(state, dict):
                continue
            self._sessions[str(sid)] = Session.from_dict(state)
        self.purge()

    def _append(self, session: Session) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "session_id": session.session_id,
                "state": session.to_dict(),
            }
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            # Хранилище диалога не должно ронять ответ агента.
            pass

    # ---- публичное ----

    def purge(self) -> int:
        """Убирает просроченные сессии. Возвращает число удалённых."""
        now = self._clock()
        dead = [sid for sid, s in self._sessions.items()
                if now - s.last_active > self.ttl_seconds]
        for sid in dead:
            self._sessions.pop(sid, None)
        return len(dead)

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            self._load()
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if self._clock() - session.last_active > self.ttl_seconds:
                self._sessions.pop(session_id, None)
                return None
            return session

    def save(self, session: Session) -> None:
        with self._lock:
            self._load()
            session.last_active = self._clock()
            if not session.created_at:
                session.created_at = session.last_active
            self._sessions[session.session_id] = session
            self._append(session)

    def create(self, session_id: str | None = None, **fields: Any) -> Session:
        now = self._clock()
        session = Session(
            session_id=session_id or f"s-{uuid.uuid4().hex[:8]}",
            created_at=now,
            last_active=now,
            **fields,
        )
        self.save(session)
        return session

    def all(self) -> list[Session]:
        with self._lock:
            self._load()
            return list(self._sessions.values())


# ------------------------------------------------------------------ знание о корпусе


@dataclass
class RuleInfo:
    """Правило корпуса с редакциями — из манифеста, не из кода."""

    key: str
    short: str = ""
    topic: str = ""
    rule_field: str = ""
    revisions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def effective_dates(self) -> list[str]:
        return [str(r.get("effective") or "") for r in self.revisions
                if r.get("effective")]

    @property
    def departments(self) -> list[str]:
        seen: list[str] = []
        for rev in self.revisions:
            dep = str(rev.get("department") or "")
            if dep and dep not in seen:
                seen.append(dep)
        return seen

    @property
    def is_amount_based(self) -> bool:
        for rev in self.revisions:
            value = str(rev.get("value") or "")
            if "руб" in value.lower() or "\u20bd" in value:
                return True
        return False

    @property
    def thresholds(self) -> list[int]:
        out: list[int] = []
        for rev in self.revisions:
            digits = re.sub(r"[^0-9]", "", str(rev.get("value") or ""))
            if digits and str(rev.get("value") or "").lower().find("руб") >= 0:
                with contextlib.suppress(ValueError):
                    out.append(int(digits))
        return sorted(set(out))


def _manifest_path(corpus_dir: str | Path | None) -> Path:
    if corpus_dir:
        return Path(corpus_dir) / "manifest.json"
    root = Path(__file__).resolve().parents[2]
    # Реальный корпус, не синтетика: см. orchestrator.DEFAULT_CORPUS.
    env = os.environ.get("LEX_CORPUS_DIR")
    return (Path(env) if env else root / "corpus-real") / "manifest.json"


class CorpusKnowledge:
    """Что модуль знает о корпусе: правила, отделы, темы документов.

    Источник — manifest.json (те же данные, что читает corpus.py). Ничего
    из этого модуля в корпус не пишется.
    """

    def __init__(self, manifest: dict[str, Any] | None = None,
                 corpus_dir: str | Path | None = None) -> None:
        self.manifest = manifest if manifest is not None else self._load(corpus_dir)
        self.rules: dict[str, RuleInfo] = {}
        self.departments: list[str] = []
        self.topics: list[str] = []
        self.doc_ids: list[str] = []
        self.docs_by_topic: dict[str, list[str]] = {}
        self.vocabulary: set[str] = set()
        self._build()

    @staticmethod
    def _load(corpus_dir: str | Path | None) -> dict[str, Any]:
        path = _manifest_path(corpus_dir)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _build(self) -> None:
        docs = self.manifest.get("documents") or []
        dept_by_doc: dict[str, str] = {}
        topics: list[str] = []
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            doc_id = str(doc.get("doc_id") or "")
            dept = str(doc.get("department") or "")
            if doc_id and dept:
                dept_by_doc[doc_id] = dept
            topic = self._topic_of(doc.get("title"))
            if topic and topic not in topics:
                topics.append(topic)
            if topic and doc_id:
                self.docs_by_topic.setdefault(topic, []).append(doc_id)
        self.topics = topics
        self.doc_ids = [str(d.get("doc_id") or "") for d in docs
                        if isinstance(d, dict) and d.get("doc_id")]

        depts: list[str] = []
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            dep = str(doc.get("department") or "")
            if dep and dep not in depts:
                depts.append(dep)
        self.departments = depts

        for raw in self.manifest.get("conflict_rules") or []:
            if not isinstance(raw, dict):
                continue
            key = str(raw.get("key") or "")
            if not key:
                continue
            revisions = []
            for rev in raw.get("revisions") or []:
                if not isinstance(rev, dict):
                    continue
                doc_id = str(rev.get("doc_id") or "")
                revisions.append({
                    "revision": rev.get("revision"),
                    "effective": str(rev.get("effective") or ""),
                    "value": str(rev.get("value") or ""),
                    "doc_id": doc_id,
                    "department": dept_by_doc.get(doc_id, ""),
                })
            self.rules[key] = RuleInfo(
                key=key,
                short=str(raw.get("short") or ""),
                topic=str(raw.get("topic") or ""),
                rule_field=str(raw.get("field") or ""),
                revisions=revisions,
            )

        # Словарь корпуса: слова, по которым вопрос хоть как-то связан с базой.
        # Если пересечения нет — вопрос «вне базы» (§15.1): уточнять нечего.
        vocab: set[str] = set()
        for topic in self.topics:
            vocab |= _stems_of(topic)
        for rule in self.rules.values():
            for phrase in (rule.short, rule.topic, rule.rule_field):
                vocab |= _stems_of(phrase)
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            for value in (doc.get("kind"), doc.get("subtype"), doc.get("system")):
                vocab |= _stems_of(value)
        self.vocabulary = vocab

    @staticmethod
    def _topic_of(title: str | None) -> str:
        """\u0422\u0435\u043c\u0430 \u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442\u0430 = \u0442\u0435\u043a\u0441\u0442 \u0432 \u043a\u0430\u0432\u044b\u0447\u043a\u0430\u0445, \u0438\u043d\u0430\u0447\u0435 \u2014 \u0441\u0430\u043c \u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043e\u043a.

        \u0423 \u0441\u0438\u043d\u0442\u0435\u0442\u0438\u0447\u0435\u0441\u043a\u043e\u0433\u043e \u043a\u043e\u0440\u043f\u0443\u0441\u0430 \u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043a\u0438 \u0432\u0438\u0434\u0430 \u00ab\u0420\u0435\u0433\u043b\u0430\u043c\u0435\u043d\u0442 \u00ab\u0441\u0440\u043e\u043a \u2026\u00bb\u00bb, \u0438 \u0442\u0435\u043c\u043e\u0439
        \u0434\u043e\u043b\u0436\u043d\u0430 \u0431\u044b\u0442\u044c \u043a\u0430\u0432\u044b\u0447\u043d\u0430\u044f \u0447\u0430\u0441\u0442\u044c. \u0423 \u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442\u043e\u0432 \u0437\u0430\u043a\u0430\u0437\u0447\u0438\u043a\u0430 \u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043e\u043a \u2014 \u0443\u0436\u0435
        \u0442\u0435\u043c\u0430 (\u00ab\u041f\u0440\u043e\u0435\u043a\u0442\u043d\u0430\u044f \u0434\u0435\u044f\u0442\u0435\u043b\u044c\u043d\u043e\u0441\u0442\u044c (\u0414\u0438\u0440\u0435\u043a\u0446\u0438\u044f \u0418\u0411)\u00bb), \u043a\u0430\u0432\u044b\u0447\u0435\u043a \u043d\u0435\u0442; \u0431\u0440\u0430\u0442\u044c
        \u043e\u0431\u0449\u0443\u044e \u0447\u0430\u0441\u0442\u044c \u0434\u043e \u0441\u043a\u043e\u0431\u043a\u0438 \u0437\u043d\u0430\u0447\u0438\u0442 \u043f\u043e\u0442\u0435\u0440\u044f\u0442\u044c \u0440\u0430\u0437\u043b\u0438\u0447\u0438\u0435 \u043c\u0435\u0436\u0434\u0443 \u0447\u0435\u0442\u044b\u0440\u044c\u043c\u044f
        \u0440\u0435\u0433\u043b\u0430\u043c\u0435\u043d\u0442\u0430\u043c\u0438 \u0438 \u043f\u0440\u0435\u0432\u0440\u0430\u0442\u0438\u0442\u044c \u0438\u0445 \u0432 \u043e\u0434\u043d\u0443 \u0442\u0435\u043c\u0443.
        """
        if not title:
            return ""
        text = str(title).strip()
        if "\u00ab" in text and "\u00bb" in text:
            return text.split("\u00ab")[1].split("\u00bb")[0].strip()
        return text.strip()

    # ---- поиск ----

    def _stem_index(self) -> None:
        """Основания правил -> уникальные для правила основы слов.

        Основа — первые 4 буквы слова. Слово считается «своим» для правила,
        если не встречается ни в одном другом правиле: так «срок» и «заявка»
        (общие для нескольких правил) не тянут лишние совпадения, а
        «автоматического» / «одобрения» тянут.
        """
        if getattr(self, "_stems_by_rule", None) is not None:
            return
        stems_by_rule: dict[str, set[str]] = {}
        freq: dict[str, int] = {}
        for key, rule in self.rules.items():
            stems: set[str] = set()
            for phrase in (rule.short, rule.topic, rule.rule_field):
                for word in _norm(phrase).split():
                    if len(word) >= 4:
                        stems.add(word[:4])
            stems_by_rule[key] = stems
            for stem in stems:
                freq[stem] = freq.get(stem, 0) + 1
        self._stems_by_rule = stems_by_rule
        self._stem_freq = freq

    def match_rules(self, question: str) -> list[str]:
        """Ключи правил, тема которых упомянута в вопросе.

        Сначала точное вхождение фразы правила (short/topic/field), затем —
        словоформенный запас: не менее двух основ, уникальных для правила.
        Возвращает все правила с лучшим счётом (несколько = неоднозначно).
        """
        hay = _norm(question)
        if not hay:
            return []
        self._stem_index()
        q_words = [word for word in hay.split() if len(word) >= 4]
        scores: dict[str, int] = {}
        for key, rule in self.rules.items():
            best = 0
            for phrase in (rule.short, rule.topic, rule.rule_field, key):
                norm = _norm(phrase)
                if norm and norm in hay:
                    best = max(best, 100 + len(norm))
            if best < 100:
                unique = {stem for stem in self._stems_by_rule.get(key, set())
                          if self._stem_freq.get(stem, 0) == 1}
                hits = 0
                for stem in unique:
                    if any(stem in word for word in q_words):
                        hits += 1
                if hits >= 2:
                    best = max(best, hits)
            if best:
                scores[key] = best
        if not scores:
            return []
        top = max(scores.values())
        return sorted(key for key, score in scores.items() if score == top)

    def in_corpus(self, question: str) -> bool:
        """Связан ли вопрос с корпусом хоть как-нибудь (§15.1).

        Пустое пересечение со словарём корпуса = вопрос вне базы: про НДС,
        ставку, командировку в этой организации регламентов нет, и уточнять
        бесполезно — сразу эскалация.
        """
        hay = _norm(question)
        if not hay:
            return False
        for doc_id in self.doc_ids:
            if doc_id.lower() in hay:
                return True
        return bool(_stems_of(question) & self.vocabulary)

    def match_topic(self, question: str) -> str | None:
        """Тема документа (вид регламента), если она названа в вопросе."""
        hay = _norm(question)
        if not hay:
            return None
        for topic in sorted(self.topics, key=len, reverse=True):
            norm = _norm(topic)
            if norm and norm in hay:
                return topic
        return None


# ------------------------------------------------------------------ роли


def _roles_from_access() -> dict[str, list[str]]:
    """Отделы роли из lex.access (модуль ACE). Если его нет — пусто."""
    try:
        from . import access  # type: ignore
    except Exception:
        return {}
    try:
        roles = access.roles()  # type: ignore[attr-defined]
    except Exception:
        return {}
    out: dict[str, list[str]] = {}
    for role in roles or []:
        if isinstance(role, dict) and role.get("id"):
            out[str(role["id"])] = [str(d) for d in (role.get("departments") or [])]
    return out


def _fallback_roles(departments: Sequence[str]) -> dict[str, list[str]]:
    """Роли по умолчанию — те же, что в api/core.py (если access.py ещё нет)."""
    def pick(pred) -> list[str]:
        return [d for d in departments if pred(d.lower())]

    return {
        "lawyer": pick(lambda s: "\u044e\u0440\u0438\u0434" in s),
        "quality": pick(lambda s: "\u043a\u0430\u0447\u0435\u0441\u0442\u0432" in s),
        "it": pick(lambda s: s.startswith("\u0438\u0442")),
        "director": ["*"],
    }


# ------------------------------------------------------------------ планировщик


#: Слова, по которым видно, что спрашивается не «какой порог», а «подходит ли».
_APPLY_INTENT = ("одобр", "подойд", "подход", "подпада", "попад", "пройд",
                 "можно", "применим", "автомат")
#: А слова, по которым видно, что вопрос САМ про порог — сумма не нужна.
_THRESHOLD_INTENT = ("\u043f\u043e\u0440\u043e\u0433", "\u043a\u0430\u043a\u0430\u044f \u0441\u0443\u043c\u043c\u0430",
                      "\u0434\u043e \u043a\u0430\u043a\u043e\u0439 \u0441\u0443\u043c\u043c\u044b",
                      "\u043a\u0430\u043a\u043e\u0439 \u0441\u0443\u043c\u043c\u044b")


class Dialogue:
    """Диалог с уточнением параметров задачи (контракт §10).

    Параметры, которые модуль умеет уточнять: as_of, department, amount,
    doc_type, rule_key. Спрашиваются только те, без которых ответ был бы
    догадкой; за один круг — не больше трёх вопросов.
    """

    def __init__(
        self,
        *,
        store: SessionStore | None = None,
        corpus_dir: str | Path | None = None,
        manifest: dict[str, Any] | None = None,
        role_departments: dict[str, list[str]] | None = None,
        ttl_minutes: int = 30,
        max_rounds: int = MAX_CLARIFY_ITERATIONS,
        max_questions: int = MAX_QUESTIONS_PER_ROUND,
        today: str | None = None,
    ) -> None:
        self.store = store or SessionStore(
            path=default_sessions_path(),
            ttl_seconds=ttl_minutes * 60,
        )
        self.knowledge = CorpusKnowledge(manifest=manifest, corpus_dir=corpus_dir)
        self.max_rounds = max_rounds
        self.max_questions = max_questions
        self._today = today
        self._role_departments = role_departments
        self._roles_cache: dict[str, list[str]] | None = None

    # ---- роли ----

    @property
    def role_departments(self) -> dict[str, list[str]]:
        if self._roles_cache is None:
            roles = _roles_from_access()
            if not roles:
                roles = _fallback_roles(self.knowledge.departments)
            self._roles_cache = roles
        return self._roles_cache

    def departments_of_role(self, role: str | None) -> list[str]:
        if not role:
            return []
        return list(self.role_departments.get(role) or [])

    def role_pins_department(self, role: str | None) -> bool:
        """Ограничивает ли роль ровно одним подразделением."""
        deps = [d for d in self.departments_of_role(role) if d and d != "*"]
        return len(deps) == 1

    def today(self) -> str:
        return self._today or date.today().isoformat()

    # ---- главный интерфейс ----

    def needs_clarification(
        self,
        question: str | dict[str, Any] | None = None,
        *,
        role: str | None = None,
        role_id: str | None = None,
        as_of: str | None = None,
        session_id: str | None = None,
        answers: dict[str, Any] | None = None,
        ctx: dict[str, Any] | None = None,
        need_direction: bool = False,
        lines: Sequence[str] | None = None,
    ) -> list[Question] | None:
        """Нужны ли уточнения.

        Аргументы принимаются и как отдельные параметры, и как ctx-словарь
        (ключи: question, role_id/role, as_of, session_id, answers, params,
        need_direction, lines).

        need_direction=True — направление определить не удалось (§17):
        первым идёт вопрос о направлении, потому что без него непонятно,
        какие документы вообще смотреть.

        Возвращает список вопросов (1..3) либо None, если уточнять нечего
        (или итерации исчерпаны — тогда параметры закрываются умолчаниями).
        """
        ctx_data: dict[str, Any] = {}
        if isinstance(question, dict):
            ctx_data.update(question)
        if ctx:
            ctx_data.update(ctx)
        need_direction = bool(need_direction or ctx_data.get("need_direction"))
        lines = list(lines or ctx_data.get("lines") or [])
        q, role, as_of, session_id, answers, params_in = self._normalize_args(
            question, role, role_id, as_of, session_id, answers, ctx
        )

        session = self.store.get(session_id) if session_id else None
        if session is None and session_id:
            session = Session(
                session_id=session_id,
                role_id=role or "",
                question=q or "",
                created_at=time.time(),
                last_active=time.time(),
            )
        if session is not None:
            if q:
                session.question = q
            if role:
                session.role_id = role
            for key, value in (params_in or {}).items():
                if value:
                    session.params[str(key)] = str(value)
            if answers:
                self._apply_answers(session, answers)

        params, matched_rules = self._collect(session, q, role, as_of, params_in)

        if self.out_of_base(q):
            # §15.1: вопрос вне базы — уточнять нечего. Идём в поиск, а если
            # и там пусто — эскалация (after_search).
            if session is not None:
                self.store.save(session)
            return None

        missing = self._missing(params, role, q, matched_rules, need_direction)

        if not missing:
            if session is not None:
                self._close_missing(session, params, role)
                self.store.save(session)
            return None

        if session is not None and session.clarification_iterations >= MAX_CLARIFY_ITERATIONS:
            # §15: итерации исчерпаны — дальше не уточняем, а эскалируем.
            session.exhausted = True
            self._close_missing(session, params, role)
            self.store.save(session)
            return None

        questions = self._build_questions(missing, params, role, q, matched_rules,
                                         lines=lines)
        if session is not None:
            self._record_clarification(session, questions)
            self.store.save(session)
        return questions

    # ---------------------------------------------------------- §15 (v1.7)

    def after_search(
        self,
        session_id: str,
        question: str | None = None,
        role: str | None = None,
        *,
        searched: dict[str, Any] | None = None,
        why_unresolved: str | None = None,
        kind: str | None = None,
        business_line: str | None = None,
    ) -> Decision:
        """§15: поиск не дал ответа — уточнить ещё или эскалировать.

        Правила (в порядке проверки):
          * вопрос вне базы (уточнять нечего) — эскалация сразу;
          * итераций уже MAX (3) — эскалация;
          * параметры собраны, но итераций меньше MIN (2) — ещё одна итерация
            (уточняем предмет), иначе эскалация;
          * иначе — ещё одна итерация уточнения по недостающим параметрам.
        """
        session = self.store.get(session_id)
        if session is None:
            session = Session(session_id=session_id, role_id=role or "",
                              question=question or "", created_at=time.time(),
                              last_active=time.time())
        if question:
            session.question = question
        if role:
            session.role_id = role
        if searched:
            session.searches.append(dict(searched))
        self.store.save(session)

        iterations = session.clarification_iterations
        kind = kind or "not_in_corpus"
        why = why_unresolved or self._why_unresolved(
            session.question, kind, searched)

        def _escalate() -> Decision:
            ticket = self.escalate(session_id, why, kind, searched=searched,
                                   business_line=business_line)
            return Decision(escalate=ticket, iterations=iterations, reason=why)

        if searched and searched.get("no_norm"):
            # §15 / критерий 1 заказчика: причина не «вопрос непонятен», а
            # «нормы нет». Сотрудник уже назвал предмет (срок хранения
            # бухгалтерских документов), и никакой ответ на «о каком заказе
            # речь» не создаст норму в регламенте. Такой вопрос уходит в
            # службу качества сразу, а не крутит круги уточнений.
            return _escalate()

        if self.out_of_base(session.question):
            decision = _escalate()
            decision.out_of_base = True
            return decision

        if iterations >= MAX_CLARIFY_ITERATIONS:
            return _escalate()

        params, matched_rules = self._collect(
            session, session.question, role, None, None)
        missing = self._missing(params, role, session.question, matched_rules)

        if not missing:
            # Параметры собраны, а ответа нет. Уточняем ещё: сначала предмет,
            # потом — вид регламента. Это и есть 2-я и 3-я итерации (§15).
            if iterations < MIN_CLARIFY_ITERATIONS:
                fallback = self._fallback_question(session.question)
            elif (iterations < MAX_CLARIFY_ITERATIONS
                  and not (params.get("doc_type")
                           and "doc_type" not in session.defaulted)):
                fallback = self._fallback_doc_type()
            else:
                fallback = None
            if fallback is None:
                return _escalate()
            self._record_clarification(session, [fallback])
            self.store.save(session)
            return Decision(questions=[fallback],
                            iterations=session.clarification_iterations,
                            reason=fallback.reason)

        questions = self._build_questions(
            missing, params, role, session.question, matched_rules)
        self._record_clarification(session, questions)
        self.store.save(session)
        return Decision(questions=questions,
                        iterations=session.clarification_iterations,
                        reason=questions[0].reason if questions else "")

    def escalate(
        self,
        session_id: str,
        why_unresolved: str,
        kind: str = "not_in_corpus",
        *,
        searched: dict[str, Any] | None = None,
        business_line: str | None = None,
    ) -> dict[str, Any]:
        """§15.2: заявка в службу качества.

        Заявка без «почему» бесполезна службе качества — поэтому why_unresolved
        обязателен и попадает в заявку как есть.
        """
        session = self.store.get(session_id)
        if session is None:
            session = Session(session_id=session_id, created_at=time.time(),
                              last_active=time.time())
        last_search = searched or (session.searches[-1] if session.searches else {})
        ticket: dict[str, Any] = {
            "id": self._next_ticket_id(),
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "session_id": session_id,
            "role_id": session.role_id,
            "business_line": business_line or session.params.get("business_line") or "",
            "question": session.question,
            "clarifications": list(session.clarifications),
            "searched": dict(last_search),
            "why_unresolved": why_unresolved,
            "kind": kind,
            "status": "open",
            "iterations": session.clarification_iterations,
        }
        session.escalation = ticket
        self.store.save(session)
        self._append_ticket(ticket)
        return ticket

    def escalate_event(self, ticket: dict[str, Any]) -> dict[str, Any]:
        """Данные события escalate (контракт §15.3)."""
        clarifications = ticket.get("clarifications") or []
        return {
            "ticket": str(ticket.get("id") or ""),
            "iterations": int(ticket.get("iterations") or len(clarifications)),
            "reason": str(ticket.get("why_unresolved") or ""),
            "searched": dict(ticket.get("searched") or {}),
            "next": ("Заявка передана в службу качества. "
                     "Ответ появится в базе как кейс."),
            "eta": "до 2 рабочих дней",
        }

    def out_of_base(self, question: str | None) -> bool:
        """§15.1: вопрос не про базу — уточнять бессмысленно, эскалируем сразу.

        Вне базы — это когда в вопросе нет ни слова из корпуса. Общие
        процессные слова («срок», «заявка») вопрос в базу возвращают: там
        уточнение полезно, и §15.1 велит сомневаться в сторону уточнения.
        """
        if not self.knowledge.in_corpus(question or ""):
            words = {w[:5] for w in _norm(question).split()}
            return not words & GENERIC_QUESTION_STEMS
        return False

    def _fallback_question(self, question: str | None) -> Question:
        """Последний вопрос, когда параметры собраны, а ответа всё нет."""
        return Question(
            key="subject",
            text="О каком заказе, договоре или поставщике идёт речь? (номер или предмет)",
            reason=("все параметры есть, но нужного документа не нашлось — "
                    "уточню предмет, чтобы искать не кота в темноте"),
            kind="text",
        )

    def _fallback_doc_type(self) -> Question:
        """Третья итерация: какой регламент имеется в виду."""
        return Question(
            key="doc_type",
            text="О каком регламенте идёт речь?",
            reason=("дата и предмет названы, нужного документа нет — "
                    "уточню, какой регламент имеется в виду"),
            kind="choice",
            options=list(self.knowledge.topics[:10]),
        )

    def _subject(self, question: str | None) -> str:
        """Предмет вопроса без вопросительного слова — для «почему не решилось»."""
        q = (question or "").strip().rstrip("?").strip()
        low = _norm(q)
        for word in ("какой", "какая", "какие", "каким", "как", "что", "сколько",
                     "где", "кто", "почему"):
            if low.startswith(word + " "):
                return q[len(word):].strip() or q
        return q

    def _why_unresolved(
        self,
        question: str | None,
        kind: str = "not_in_corpus",
        searched: dict[str, Any] | None = None,
    ) -> str:
        """§15.2: почему не решилось — конкретно, а не «не найдено»."""
        subject = self._subject(question)
        if kind == "out_of_scope":
            return (f"вопрос «{subject}» вне компетенции базы: регламенты по теме "
                    f"не ведутся, поиск не выполнялся")
        if kind == "no_edition_in_force":
            return (f"на запрошенную дату не действует ни одна редакция "
                    f"правила по теме «{subject}»")
        top = (searched or {}).get("top") or []
        if top:
            nearest = ", ".join(str(item) for item in top[:3])
            return (f"в корпусе нет документа по теме «{subject}» "
                    f"(ближайшее по теме: {nearest}, ответа по теме в них нет)")
        return f"в корпусе нет документа по теме «{subject}»"

    # ---- заявки в службу качества ----

    def _tickets_path(self) -> Path:
        env = os.environ.get("LEX_ESCALATIONS_PATH")
        if env:
            return Path(env)
        return self.store.path.parent / "escalations.jsonl"

    def _next_ticket_id(self) -> str:
        path = self._tickets_path()
        count = 0
        if path.exists():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        count += 1
            except OSError:
                count = 0
        return f"esc-{count + 1:04d}"

    def _append_ticket(self, ticket: dict[str, Any]) -> None:
        try:
            path = self._tickets_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(ticket, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def apply_answers(
        self,
        session_id: str,
        answers: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Применяет ответы сотрудника и возвращает обновлённый контекст."""
        session = self.store.get(session_id)
        if session is None:
            session = Session(session_id=session_id, created_at=time.time(),
                              last_active=time.time())
        if answers:
            self._apply_answers(session, answers)
        self.store.save(session)
        return self.context(session)

    def context(self, session: Session | str) -> dict[str, Any]:
        """Контекст диалога: параметры + явные умолчания."""
        if isinstance(session, str):
            found = self.store.get(session)
            if found is None:
                return {
                    "session_id": session,
                    "params": {},
                    "assumptions": [],
                    "rounds": 0,
                    "clarification_iterations": 0,
                    "clarifications": [],
                    "searches": [],
                    "escalation": None,
                    "exhausted": False,
                }
            session = found
        return {
            "session_id": session.session_id,
            "question": session.question,
            "role_id": session.role_id,
            "params": dict(session.params),
            "assumptions": list(session.assumptions),
            "rounds": session.rounds,
            "clarification_iterations": session.clarification_iterations,
            "clarifications": list(session.clarifications),
            "searches": list(session.searches),
            "escalation": session.escalation,
            "exhausted": session.exhausted,
        }

    # ---- сборка ответа для API ----

    def clarify_payload(
        self,
        session_id: str,
        questions: Sequence[Question],
        partial: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Данные события clarify (контракт §10)."""
        payload: dict[str, Any] = {
            "session_id": session_id,
            "questions": [q.to_dict() for q in questions],
            "can_skip": True,
        }
        if partial is not None:
            payload["partial"] = partial
        return payload

    def clarify_step(
        self,
        questions: Sequence[Question],
        *,
        n: int = 2,
        total: int = 9,
        ms: int = 0,
    ) -> dict[str, Any]:
        """Шаг SSE «Уточнение» — цвет sky, идёт вместо decompose."""
        if not questions:
            detail = "уточнения не требуются"
        else:
            first = questions[0]
            label = PARAM_LABELS.get(first.key, first.key)
            detail = f"Нужно уточнить: {label} — {first.reason}"
            extra = [PARAM_LABELS.get(q.key, q.key) for q in questions[1:]]
            if extra:
                detail += " · ещё: " + ", ".join(extra)
        return {
            "n": n,
            "total": max(total, n),
            "key": "clarify",
            "title": "Уточнение",
            "detail": detail,
            "ms": ms,
            "color": "sky",
        }

    # ---- внутреннее ----

    def _collect(
        self,
        session: Session | None,
        q: str,
        role: str | None,
        as_of: str | None,
        params_in: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], list[str]]:
        """Параметры задачи: из сессии + из ответа + из текста вопроса.

        То, что сказано прямо в вопросе, — самая сильная подсказка.
        """
        params = dict(session.params) if session else {}
        params.update({k: v for k, v in (params_in or {}).items() if v})
        if as_of:
            params["as_of"] = as_of
        if role:
            params.setdefault("role_id", role)
        found_date = extract_date(q)
        if found_date:
            params["as_of"] = found_date
        found_amount = extract_amount(q)
        if found_amount:
            params["amount"] = found_amount
        found_department = match_department(q, self.knowledge.departments)
        if found_department:
            params["department"] = found_department
        matched_rules = self.knowledge.match_rules(q)
        if len(matched_rules) == 1:
            params.setdefault("rule_key", matched_rules[0])
        topic = self.knowledge.match_topic(q)
        if topic:
            params.setdefault("doc_type", topic)
        line = self._match_line(q)
        if line:
            params.setdefault("business_line", line)
        return params, matched_rules

    def _match_line(self, question: str | None) -> str | None:
        """Направление, названное в вопросе («по закупкам» → «Закупки»)."""
        hay = _norm(question)
        if not hay:
            return None
        for title in self.available_lines():
            norm = _norm(title)
            if norm and norm in hay:
                return title
        return None

    def _record_clarification(self, session: Session,
                              questions: Sequence[Question]) -> None:
        """§15: считаем итерацию и запоминаем, что спросили."""
        session.clarification_iterations += 1
        session.rounds = session.clarification_iterations
        session.clarifications.append({
            "iteration": session.clarification_iterations,
            "asked": [q.text for q in questions],
            "keys": [q.key for q in questions],
            "answers": [],
        })
        session.history.append({
            "role": "agent",
            "kind": "clarify",
            "round": session.clarification_iterations,
            "iteration": session.clarification_iterations,
            "questions": [item.to_dict() for item in questions],
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

    def _normalize_args(
        self,
        question: str | dict[str, Any] | None,
        role: str | None,
        role_id: str | None,
        as_of: str | None,
        session_id: str | None,
        answers: dict[str, Any] | None,
        ctx: dict[str, Any] | None,
    ) -> tuple[str, str | None, str | None, str | None, dict[str, Any] | None,
               dict[str, Any] | None]:
        data: dict[str, Any] = {}
        if isinstance(question, dict):
            data = dict(question)
        elif isinstance(question, str):
            data["question"] = question
        if ctx:
            for key, value in ctx.items():
                data.setdefault(key, value)

        q = str(data.get("question") or data.get("text") or "")
        role = role or role_id or data.get("role") or data.get("role_id")
        role = str(role) if role else None
        as_of = as_of or data.get("as_of")
        as_of = str(as_of) if as_of else None
        session_id = session_id or data.get("session_id")
        session_id = str(session_id) if session_id else None
        answers = answers if answers is not None else data.get("answers")
        params_in = data.get("params") or {}
        return q, role, as_of, session_id, answers, params_in

    def _apply_answers(self, session: Session, answers: dict[str, Any]) -> None:
        for key, raw in answers.items():
            value = "" if raw is None else str(raw).strip()
            skipped = _norm(value) in SKIP_ANSWERS
            if skipped:
                self._add_assumption(session, key, self._default_for(key, session))
                session.history.append({
                    "role": "user",
                    "key": key,
                    "value": value or "не знаю",
                    "skipped": True,
                    "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
                self._attach_answer(session, value or "не знаю")
                continue
            if key == "as_of":
                parsed = extract_date(value) or value
                session.params["as_of"] = parsed
            elif key == "amount":
                session.params["amount"] = extract_amount(value) or value
            else:
                session.params[key] = value
            session.history.append({
                "role": "user",
                "key": key,
                "value": value,
                "skipped": False,
                "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
            self._attach_answer(session, value)

    def _attach_answer(self, session: Session, value: str) -> None:
        """Ответ сотрудника — в заявку §15.2 (что спрашивали, что ответили)."""
        for entry in reversed(session.clarifications):
            if not entry.get("answers"):
                entry["answers"].append(value)
                return

    def _add_assumption(self, session: Session, key: str, value: str) -> None:
        note = PARAM_DEFAULTS.get(key, ("", "параметр не указан — применяю умолчание"))[1]
        session.assumptions = [a for a in session.assumptions if a.get("key") != key]
        session.assumptions.append(
            {"key": key, "value": value, "note": note}
        )

    def _default_for(self, key: str, session: Session) -> str:
        if key == "as_of":
            return self.today()
        if key == "department":
            deps = [d for d in self.departments_of_role(session.role_id)
                    if d and d != "*"]
            return deps[0] if len(deps) == 1 else "не определено"
        if key == "rule_key":
            rule = self.knowledge.rules.get(session.params.get("rule_key", ""))
            return rule.key if rule else "не определено"
        return PARAM_DEFAULTS.get(key, ("", ""))[0]

    def _close_missing(
        self,
        session: Session,
        params: dict[str, Any],
        role: str | None,
    ) -> None:
        """Закрывает недостающие параметры явными умолчаниями."""
        for key in ("as_of", "department", "amount", "doc_type", "rule_key",
                    "business_line"):
            if params.get(key):
                continue
            if session.assumption_for(key):
                continue
            value = self._default_for(key, session)
            if key == "department" and not role:
                value = "не определено"
            self._add_assumption(session, key, value)
            if key not in session.defaulted:
                session.defaulted.append(key)
            if key == "as_of":
                session.params["as_of"] = value
            elif key in ("department", "amount", "doc_type", "rule_key",
                         "business_line"):
                session.params[key] = value

    def _missing(
        self,
        params: dict[str, Any],
        role: str | None,
        question: str,
        matched_rules: Sequence[str],
        need_direction: bool = False,
    ) -> list[str]:
        """Каких параметров не хватает для честного ответа."""
        missing: list[str] = []

        # §17: направление определить не удалось — спросить его надо первым,
        # от него зависит, какие документы вообще смотреть.
        if need_direction and not params.get("business_line"):
            missing.append("business_line")

        rule_key = params.get("rule_key") or ""
        if not rule_key and len(matched_rules) > 1:
            missing.append("rule_key")
        rule = self.knowledge.rules.get(rule_key)

        if rule is not None and len(rule.revisions) > 1 and not params.get("as_of"):
            missing.append("as_of")

        if rule is not None and rule.is_amount_based and not params.get("amount"):
            if self._needs_amount(question):
                missing.append("amount")

        if rule is not None and len(rule.departments) > 1 and not params.get("department"):
            if not self.role_pins_department(role):
                missing.append("department")

        # Вид документа спрашиваем, только если тема не сузилась сама. У вопроса
        # про «кто назначает РП» документ один, и требовать от сотрудника
        # выбрать из десяти видов — издевательство, а не уточнение.
        if not rule_key and not params.get("doc_type") and self._topic_too_broad(question):
            missing.append("doc_type")
        order = ["business_line", "as_of", "amount", "department", "rule_key",
                 "doc_type"]
        return [key for key in order if key in missing]

    def _topic_too_broad(self, question: str | None) -> bool:
        """Есть ли смысл спрашивать вид документа.

        Спрашивать незачем, если вид назван в вопросе или документов по теме в
        базе не больше двух — поиск сам разберётся. В маленьком каталоге (до
        семи тем) спрашивать тем более нечего: у заказчика четыре регламента,
        и требовать «выберите вид из десяти» — не уточнение, а барьер.
        """
        hay = _norm(question)
        if not hay:
            return False
        if self.knowledge.match_topic(hay):
            return False
        for topic, doc_ids in self.knowledge.docs_by_topic.items():
            norm = _norm(topic)
            if norm and norm in hay and len(doc_ids) <= 2:
                return False
        return len(self.knowledge.topics) > 6

    def _needs_amount(self, question: str) -> bool:
        hay = _norm(question)
        if not hay:
            return False
        if any(word in hay for word in _THRESHOLD_INTENT):
            return False
        return any(word in hay for word in _APPLY_INTENT)

    def _build_questions(
        self,
        missing: Sequence[str],
        params: dict[str, Any],
        role: str | None,
        question: str,
        matched_rules: Sequence[str],
        lines: Sequence[str] | None = None,
    ) -> list[Question]:
        questions: list[Question] = []
        rule_key = params.get("rule_key") or ""
        rule = self.knowledge.rules.get(rule_key)

        for key in missing:
            if key == "business_line":
                options = list(lines or self.available_lines())
                questions.append(Question(
                    key="business_line",
                    text="О каком направлении вопрос?",
                    reason=("без направления неясно, какие документы смотреть — "
                            "направление задаёт и права роли"),
                    kind="choice",
                    options=options,
                ))
            elif key == "as_of" and rule is not None:
                dates = ", ".join(d for d in rule.effective_dates if d)
                # §21: реквизиты (ключ правила, даты редакций) — дословно,
                # а формулировка — проще.
                questions.append(Question(
                    key="as_of",
                    text="На какую дату считаем?",
                    reason=(f"правило {rule.key} в корпусе в "
                            f"{_num_word(len(rule.revisions))} редакциях"
                            + (f" ({dates})" if dates else "")
                            + " — без даты нельзя понять, какая действует"),
                    kind="date",
                ))
            elif key == "amount" and rule is not None:
                options = self._amount_options(rule)
                questions.append(Question(
                    key="amount",
                    text="Какая сумма заявки?",
                    reason=("от суммы зависит ответ — порог автоматического "
                            f"одобрения {self._threshold_text(rule)}"),
                    kind="choice",
                    options=options,
                ))
            elif key == "department":
                options = self._department_options(rule)
                title = (f"роль «{role}» видит все отделы — чей запрос?"
                         if role else "отдел не назван — чей запрос?")
                questions.append(Question(
                    key="department",
                    text="Какое подразделение?",
                    reason=(title + "; от этого зависит, какая редакция правила "
                            "подойдёт"),
                    kind="choice",
                    options=options,
                ))
            elif key == "rule_key":
                options = list(matched_rules)
                questions.append(Question(
                    key="rule_key",
                    text="Какое правило имеется в виду?",
                    reason="под вопрос подходят несколько правил корпуса",
                    kind="choice",
                    options=options,
                ))
            elif key == "doc_type":
                options = list(self.knowledge.topics[:10])
                questions.append(Question(
                    key="doc_type",
                    text="О каком регламенте вопрос?",
                    reason=(f"в корпусе {len(self.knowledge.topics)} видов "
                            "документов — уточните, какой подходит"),
                    kind="choice",
                    options=options,
                ))
            if len(questions) >= self.max_questions:
                break
        return questions[: self.max_questions]

    def available_lines(self) -> list[str]:
        """Направления базы (§11/§17) — из policy, иначе из корпуса.

        Данные, а не код: policy держит карту направлений, корпус — системы
        документов. Пустой список — уточнять нечего.
        """
        try:
            from .policy import BUSINESS_LINES
        except Exception:
            BUSINESS_LINES = {}
        titles = [str(v.get("title") or k)
                  for k, v in (BUSINESS_LINES or {}).items() if k]
        if titles:
            return titles
        systems: list[str] = []
        for doc in self.knowledge.manifest.get("documents") or []:
            if isinstance(doc, dict):
                system = str(doc.get("system") or "").strip()
                if system and system not in systems:
                    systems.append(system)
        return systems[:10]

    def direction_question(self, lines: Sequence[str] | None = None) -> Question:
        """§17: направление определить не удалось — спросим его."""
        return Question(
            key="business_line",
            text="О каком направлении вопрос?",
            reason=("без направления неясно, какие документы смотреть — "
                    "направление задаёт и права роли"),
            kind="choice",
            options=list(lines or self.available_lines()),
        )

    def _threshold_text(self, rule: RuleInfo) -> str:
        thresholds = rule.thresholds
        if thresholds:
            return f"{_fmt_num(thresholds[-1])} рублей"
        for rev in reversed(rule.revisions):
            if rev.get("value"):
                return str(rev["value"])
        return "не задан"

    def _amount_options(self, rule: RuleInfo) -> list[str]:
        thresholds = rule.thresholds
        options: list[str] = []
        if len(thresholds) >= 2:
            options.append(f"до {_fmt_num(thresholds[0])} руб.")
            options.append(f"от {_fmt_num(thresholds[0])} до {_fmt_num(thresholds[-1])} руб.")
            options.append(f"более {_fmt_num(thresholds[-1])} руб.")
        elif thresholds:
            options.append(f"до {_fmt_num(thresholds[0])} руб.")
            options.append(f"более {_fmt_num(thresholds[0])} руб.")
        options.append("не знаю")
        return options

    def _department_options(self, rule: RuleInfo | None) -> list[str]:
        options: list[str] = []
        if rule is not None:
            for dep in rule.departments:
                if dep and dep not in options:
                    options.append(dep)
        for dep in self.knowledge.departments:
            if dep not in options:
                options.append(dep)
        options.append("не знаю")
        return options[:8]


__all__ = [
    "MAX_CLARIFY_ITERATIONS",
    "MAX_CLARIFY_ROUNDS",
    "MAX_QUESTIONS_PER_ROUND",
    "MIN_CLARIFY_ITERATIONS",
    "Assumption",
    "CorpusKnowledge",
    "Decision",
    "Dialogue",
    "Question",
    "RuleInfo",
    "Session",
    "SessionStore",
    "extract_amount",
    "extract_date",
    "match_department",
]
