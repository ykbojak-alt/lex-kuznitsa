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

CORPUS_DIR = Path(os.environ.get("LEX_CORPUS_DIR") or (ROOT / "sample-corpus"))
VAR_DIR = Path(os.environ.get("LEX_VAR_DIR") or (ROOT / "var"))
STATIC_DIR = Path(__file__).resolve().parent / "static"

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

# §18.2: SLA тикета. Значения в конфиге, не в коде.
SLA_HOURS = {"normal": float(os.environ.get("LEX_SLA_HOURS", "48")),
             "high": float(os.environ.get("LEX_SLA_HOURS_HIGH", "8"))}
# §19.6: один кейс — наблюдение, три — сигнал.
CR_MIN_CASES = int(os.environ.get("LEX_CR_MIN_CASES", "3"))


def ensure_dirs() -> None:
    VAR_DIR.mkdir(parents=True, exist_ok=True)
