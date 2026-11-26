"""Конфигурация HTTP-слоя: пути, адреса сервисов, .env.

Никаких секретов в коде — ключ NexusLink берётся только из .env.
Пути, которые ядро тоже использует (корпус, кейсы, журнал), живут здесь
одной точкой: иначе HTTP-слой и ядро читали бы разные файлы.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# src/api/config.py -> src/api -> src -> корень репозитория
ROOT = Path(__file__).resolve().parents[2]

load_dotenv(ROOT / ".env")

VERSION = "1.14.0"

CORPUS_DIR = Path(os.environ.get("LEX_CORPUS_DIR") or (ROOT / "corpus-real"))
VAR_DIR = Path(os.environ.get("LEX_VAR_DIR") or (ROOT / "var"))
STATIC_DIR = Path(os.environ.get("LEX_STATIC_DIR")
                  or (Path(__file__).resolve().parent / "static"))

JOURNAL_PATH = Path(os.environ.get("LEX_JOURNAL_PATH") or (VAR_DIR / "journal.jsonl"))
CASES_PATH = Path(os.environ.get("LEX_CASES_PATH") or (VAR_DIR / "cases.jsonl"))
TICKETS_PATH = Path(os.environ.get("LEX_TICKETS_PATH") or (VAR_DIR / "tickets.jsonl"))
ESCALATIONS_PATH = Path(os.environ.get("LEX_ESCALATIONS_PATH") or (VAR_DIR / "escalations.jsonl"))
CHANGE_REQUESTS_PATH = Path(os.environ.get("LEX_CHANGE_REQUESTS_PATH")
                            or (VAR_DIR / "change_requests.jsonl"))

EMBED_URL = os.environ.get("LEX_EMBED_URL", "http://127.0.0.1:8081").rstrip("/")
RERANK_URL = os.environ.get("LEX_RERANK_URL", "http://127.0.0.1:8082").rstrip("/")

NEXUS_LINK_BASE_URL = os.environ.get("NEXUS_LINK_BASE_URL", "").rstrip("/")
NEXUSLINK_KEY = os.environ.get("NEXUSLINK_KEY", "")
NEXUS_LINK_MODEL = os.environ.get("NEXUS_LINK_MODEL", "")

PROBE_TIMEOUT = float(os.environ.get("LEX_PROBE_TIMEOUT", "4"))

# §12: ответ без модели (цитаты как есть). Нужен, когда слот отвечает пустым
# content из-за reasoning-бюджета: демо не должно падать на шаге compose.
NO_LLM = os.environ.get("LEX_NO_LLM", "").lower() in ("1", "true", "yes", "on")

# §18.2: SLA тикета. Значения в конфиге, не в коде.
SLA_HOURS = {"normal": float(os.environ.get("LEX_SLA_HOURS", "48")),
             "high": float(os.environ.get("LEX_SLA_HOURS_HIGH", "8"))}
# §19.6: один кейс — наблюдение, три — сигнал.
# Каноническое имя — LEX_CR_MIN_CASES. Имя без префикса LEX_ читается как
# запасное (легаси), чтобы старые .env продолжали задавать тот же порог.
CR_MIN_CASES = int(
    os.environ.get("LEX_CR_MIN_CASES") or os.environ.get("CR_MIN_CASES", "3")
)

# §22.4: обращение в службу качества в демо — имитация письма.
# Домен example.invalid зарезервирован RFC 2606: письмо физически некуда отправить,
# и это намеренно — демо публичное, реальные адреса подставлять нельзя.
QUALITY_EMAIL = os.environ.get("QUALITY_EMAIL", "quality@example.invalid")
EMAIL_MODE = os.environ.get("EMAIL_MODE", "simulated")
EMPLOYEE_EMAIL_DOMAIN = os.environ.get("EMPLOYEE_EMAIL_DOMAIN", "example.invalid")


def employee_email(role_id: str) -> str:
    """Адрес задающего вопрос. В демо — из роли, на зарезервированном домене."""
    local = "".join(ch if ch.isalnum() or ch in "-._" else "-"
                    for ch in (role_id or "employee").lower()).strip("-") or "employee"
    return f"{local}@{EMPLOYEE_EMAIL_DOMAIN}"


def ensure_dirs() -> None:
    VAR_DIR.mkdir(parents=True, exist_ok=True)
