"""Голосовой ввод-вывод для стенда: STT и TTS как два аддитивных эндпоинта.

Зачем отдельный модуль: ``/api/ask`` не должен знать про голос — распознавание
и озвучка живут рядом, а не внутри RAG. Оба сервиса внешние, поэтому их
недоступность не валит стенд: STT отдаёт 503 ``stt_unavailable``, TTS —
503 ``tts_unavailable``. Стенд без голоса обязан работать.

Ключ Fish берётся только из окружения (``FISH_API_KEY``); на стенде он лежит в
``/etc/lex-voice.env`` и в лог/ответ не попадает никогда.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

router = APIRouter()

# Первый вызов STT грузит модель — таймаут с запасом, иначе стенд отвалится
# на «холодном» старте. Fish отвечает быстрее, но 60 с — тоже с запасом.
STT_TIMEOUT = float(os.environ.get("LEX_STT_TIMEOUT", "120"))
TTS_TIMEOUT = float(os.environ.get("LEX_TTS_TIMEOUT", "60"))

# Длина реплики, которую озвучиваем за один вызов. Длиннее — режем по границе
# предложения: Fish иначе обрывает фразу посередине.
TTS_MAX_CHARS = 1500


def _stt_url() -> str:
    return os.environ.get("LEX_STT_URL", "http://127.0.0.1:19786").rstrip("/")


def _fish_key() -> str:
    return os.environ.get("FISH_API_KEY", "")


def _fish_model() -> str:
    return os.environ.get("FISH_MODEL", "s2.1-pro-free")


def _fish_reference() -> str:
    return os.environ.get("FISH_REFERENCE_ID", "5d8e4d17f1174094aebb5d77bf05de74")


def _truncate_at_sentence(text: str, limit: int) -> tuple[str, bool]:
    """Обрезает по последней границе предложения; флаг — была ли обрезка."""
    if len(text) <= limit:
        return text, False
    cut = max(text.rfind(". ", 0, limit), text.rfind("! ", 0, limit),
              text.rfind("? ", 0, limit), text.rfind("\n", 0, limit))
    if cut == -1:
        cut = limit
    else:
        cut += 1
    return text[:cut], True


class TTSRequest(BaseModel):
    text: str = ""


@router.post("/api/stt")
async def stt(audio: UploadFile = File(...)) -> dict[str, Any]:
    data = await audio.read()
    if not data:
        raise HTTPException(status_code=400, detail="пустой файл")
    body = b""
    boundary = "----lex-stt-boundary"
    body += f"--{boundary}\r\n".encode()
    body += (
        f'Content-Disposition: form-data; name="audio"; filename="{audio.filename or "audio"}"'
        "\r\nContent-Type: application/octet-stream\r\n\r\n"
    ).encode()
    body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{_stt_url()}/stt", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=STT_TIMEOUT) as resp:
            import json
            payload = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError):
        raise HTTPException(status_code=503, detail="stt_unavailable") from None
    return {
        "text": payload.get("text", ""),
        "language": payload.get("language"),
        "duration": payload.get("duration"),
    }


@router.post("/api/tts")
def tts(req: TTSRequest) -> Response:
    key = _fish_key()
    if not key:
        raise HTTPException(status_code=503, detail="tts_unavailable")
    text, truncated = _truncate_at_sentence(req.text.strip(), TTS_MAX_CHARS)
    if not text:
        raise HTTPException(status_code=400, detail="пустой текст")
    import json
    body = json.dumps({
        "text": text,
        "reference_id": _fish_reference(),
        "format": "mp3",
        "normalize": True,
        "language": "auto",
    }).encode("utf-8")
    http_req = urllib.request.Request(
        "https://api.fish.audio/v1/tts", data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(http_req, timeout=TTS_TIMEOUT) as resp:
            audio = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        raise HTTPException(status_code=503, detail="tts_unavailable") from None
    headers = {"X-TTS-Truncated": "1"} if truncated else None
    return Response(content=audio, media_type="audio/mpeg", headers=headers)


__all__ = ["router", "stt", "tts", "TTSRequest"]