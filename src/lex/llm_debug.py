"""Журнал вызовов модели — чтобы «иногда падает» стало числом.

Владелец: нужен числой ответ, а не «иногда». Поэтому каждый вызов
``/chat/completions`` пишет одну строку в ``var/llm-debug.jsonl``: модель,
лимит, ``finish_reason``, длины ``content`` и ``reasoning``, время.

Лог пишется **на диск, не в stdout**: демо идёт в терминале, и отладочные
строки там дороже, чем польза от них. Файл — JSONL, чтобы его можно было
разобрать скриптом и посчитать распределение.

Ключ сюда не попадает никогда: пишем только то, что нужно для диагностики.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEBUG_PATH = Path(os.environ.get("LEX_LLM_DEBUG_PATH") or (ROOT / "var" / "llm-debug.jsonl"))

_LOCK = threading.Lock()
# Флаг: писать ли вообще. По умолчанию — да: цена одной строки на вызов
# ничтожна, а без этого не бывает ответа на «сколько пустых».
ENABLED = os.environ.get("LEX_LLM_DEBUG", "1").lower() not in ("0", "false", "no", "off")


def record(*, model: str, max_tokens: int, finish_reason: str, content: str,
           reasoning: str, elapsed_ms: int, attempt: int = 0,
           retried: bool = False, error: str = "", path: str | Path | None = None) -> None:
    if not ENABLED:
        return
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": model,
        "max_tokens": int(max_tokens),
        "finish_reason": finish_reason or "",
        "content_chars": len(content or ""),
        "reasoning_chars": len(reasoning or ""),
        "empty": not (content or "").strip(),
        "attempt": int(attempt),
        "retried": bool(retried),
        "elapsed_ms": int(elapsed_ms),
    }
    if error:
        row["error"] = error[:300]
    target = Path(path) if path else DEBUG_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with _LOCK, target.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        # Журнал диагностики не имеет права ломать ответ пользователю.
        pass
