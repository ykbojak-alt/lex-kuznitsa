"""Конфигурация HTTP-слоя: пути, адреса сервисов, .env.

Никаких секретов в коде — ключ NexusLink берётся только из .env.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# src/api/config.py -> src/api -> src -> корень репозитория
ROOT = Path(__file__).resolve().parents[2]

load_dotenv(ROOT / ".env")

VERSION = "1.0.0"

CORPUS_DIR = Path(os.environ.get("LEX_CORPUS_DIR") or (ROOT / "sample-corpus"))
VAR_DIR = Path(os.environ.get("LEX_VAR_DIR") or (ROOT / "var"))
STATIC_DIR = Path(__file__).resolve().parent / "static"
JOURNAL_PATH = Path(os.environ.get("LEX_JOURNAL_PATH") or (VAR_DIR / "journal.jsonl"))

EMBED_URL = os.environ.get("LEX_EMBED_URL", "http://127.0.0.1:8081").rstrip("/")
RERANK_URL = os.environ.get("LEX_RERANK_URL", "http://127.0.0.1:8082").rstrip("/")

NEXUS_LINK_BASE_URL = os.environ.get("NEXUS_LINK_BASE_URL", "").rstrip("/")
NEXUSLINK_KEY = os.environ.get("NEXUSLINK_KEY", "")
NEXUS_LINK_MODEL = os.environ.get("NEXUS_LINK_MODEL", "")

PROBE_TIMEOUT = float(os.environ.get("LEX_PROBE_TIMEOUT", "4"))


def ensure_dirs() -> None:
    VAR_DIR.mkdir(parents=True, exist_ok=True)
