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

import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Sequence

QuestionKind = Literal["date", "choice", "text", "doc_id"]

MAX_QUESTIONS_PER_ROUND = 3
MAX_CLARIFY_ROUNDS = 2
DEFAULT_TTL_SECONDS = 30 * 60

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
                            (_TEXT_DATE_RE, "dmy")):
        m = pattern.search(raw)
        if not m:
            continue
        if order == "ymd":
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
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
            "exhausted": self.exhausted,
            "created_at": self.created_at,
            "last_active": self.last_active,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        return cls(
            session_id=str(data.get("session_id") or ""),
            role_id=str(data.get("role_id") or ""),
            question=str(data.get("question") or ""),
            params={str(k): str(v) for k, v in (data.get("params") or {}).items()},
            history=list(data.get("history") or []),
            assumptions=list(data.get("assumptions") or []),
            rounds=int(data.get("rounds") or 0),
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
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
    field: str = ""
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
                try:
                    out.append(int(digits))
                except ValueError:
                    pass
        return sorted(set(out))


def _manifest_path(corpus_dir: str | Path | None) -> Path:
    if corpus_dir:
        return Path(corpus_dir) / "manifest.json"
    root = Path(__file__).resolve().parents[2]
    return root / "sample-corpus" / "manifest.json"


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
        self.topics = topics

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
                field=str(raw.get("field") or ""),
                revisions=revisions,
            )

    @staticmethod
    def _topic_of(title: str | None) -> str:
        if not title:
            return ""
        text = title.split("(")[0].strip()
        if "\u00ab" in text and "\u00bb" in text:
            text = text.split("\u00ab")[1].split("\u00bb")[0]
        return text.strip()

    # ---- поиск ----

    def match_rules(self, question: str) -> list[str]:
        """Ключи правил, тема которых упомянута в вопросе."""
        hay = _norm(question)
        if not hay:
            return []
        found: list[tuple[int, str]] = []
        for key, rule in self.rules.items():
            best = 0
            for phrase in (rule.short, rule.topic, rule.field, key):
                norm = _norm(phrase)
                if norm and norm in hay:
                    best = max(best, len(norm))
            if best:
                found.append((best, key))
        found.sort(key=lambda pair: (-pair[0], pair[1]))
        return [key for _, key in found]

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
        max_rounds: int = MAX_CLARIFY_ROUNDS,
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
    ) -> list[Question] | None:
        """Нужны ли уточнения.

        Аргументы принимаются и как отдельные параметры, и как ctx-словарь
        (ключи: question, role_id/role, as_of, session_id, answers, params).

        Возвращает список вопросов (1..3) либо None, если уточнять нечего
        (или круги исчерпаны — тогда параметры закрываются умолчаниями).
        """
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

        params = dict(session.params) if session else {}
        params.update({k: v for k, v in (params_in or {}).items() if v})
        if as_of:
            params["as_of"] = as_of
        if role:
            params.setdefault("role_id", role)

        # То, что сказано прямо в вопросе, — самая сильная подсказка.
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

        missing = self._missing(params, role, q, matched_rules)

        if not missing:
            if session is not None:
                self._close_missing(session, params, role)
                self.store.save(session)
            return None

        if session is not None and session.rounds >= self.max_rounds:
            # Круги исчерпаны: дальше не спрашиваем, закрываем умолчаниями.
            session.exhausted = True
            self._close_missing(session, params, role)
            self.store.save(session)
            return None

        questions = self._build_questions(missing, params, role, q, matched_rules)
        if session is not None:
            session.rounds += 1
            session.history.append({
                "role": "agent",
                "kind": "clarify",
                "round": session.rounds,
                "questions": [item.to_dict() for item in questions],
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
            self.store.save(session)
        return questions

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
            detail = f"Нужна {label}: {first.reason}"
            extra = [PARAM_LABELS.get(q.key, q.key) for q in questions[1:]]
            if extra:
                detail += " · ещё: " + ", ".join(extra)
        return {
            "n": n,
            "total": total,
            "key": "clarify",
            "title": "Уточнение",
            "detail": detail,
            "ms": ms,
            "color": "sky",
        }

    # ---- внутреннее ----

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
                    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
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
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            })

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
        for key in ("as_of", "department", "amount", "doc_type", "rule_key"):
            if params.get(key):
                continue
            if session.assumption_for(key):
                continue
            value = self._default_for(key, session)
            if key == "department" and not role:
                value = "не определено"
            self._add_assumption(session, key, value)
            if key == "as_of":
                session.params["as_of"] = value
            elif key in ("department", "amount", "doc_type", "rule_key"):
                session.params[key] = value

    def _missing(
        self,
        params: dict[str, Any],
        role: str | None,
        question: str,
        matched_rules: Sequence[str],
    ) -> list[str]:
        """Каких параметров не хватает для честного ответа."""
        missing: list[str] = []

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

        if not rule_key and not params.get("doc_type"):
            missing.append("doc_type")

        order = ["as_of", "amount", "department", "rule_key", "doc_type"]
        return [key for key in order if key in missing]

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
    ) -> list[Question]:
        questions: list[Question] = []
        rule_key = params.get("rule_key") or ""
        rule = self.knowledge.rules.get(rule_key)

        for key in missing:
            if key == "as_of" and rule is not None:
                dates = ", ".join(d for d in rule.effective_dates if d)
                questions.append(Question(
                    key="as_of",
                    text="На какую дату определить действующую редакцию?",
                    reason=(f"в корпусе {_num_word(len(rule.revisions))} редакции "
                            f"правила {rule.key}"
                            + (f" ({dates})" if dates else "")
                            + ", без даты нельзя выбрать действующую"),
                    kind="date",
                ))
            elif key == "amount" and rule is not None:
                options = self._amount_options(rule)
                questions.append(Question(
                    key="amount",
                    text="Какая сумма заявки?",
                    reason=("от суммы зависит порядок: порог автоматического "
                            f"одобрения — {self._threshold_text(rule)}"),
                    kind="choice",
                    options=options,
                ))
            elif key == "department":
                options = self._department_options(rule)
                title = (f"Отдел «{role}» видит все подразделения — по чьему "
                         "подразделению вопрос?") if role else                         "Подразделение не указано — по чьему подразделению вопрос?"
                questions.append(Question(
                    key="department",
                    text="Подразделение?",
                    reason=(title + "; редакции правила лежат в разных "
                            "подразделениях, от этого зависит, какая редакция "
                            "попадёт в ответ"),
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
                            "документов — без уточнения не выбрать "
                            "применимый"),
                    kind="choice",
                    options=options,
                ))
            if len(questions) >= self.max_questions:
                break
        return questions[: self.max_questions]

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
    "Assumption",
    "CorpusKnowledge",
    "Dialogue",
    "Question",
    "RuleInfo",
    "Session",
    "SessionStore",
    "MAX_CLARIFY_ROUNDS",
    "MAX_QUESTIONS_PER_ROUND",
    "extract_amount",
    "extract_date",
    "match_department",
]
