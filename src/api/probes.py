"""Живые пробы зависимостей для GET /api/health.

Контракт требует `reachable` как результат реальной проверки, не константу.
Пробы параллельные и с коротким таймаутом: health не должен висеть, если
какой-то сервис лежит.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from . import config


async def _get_json(client: httpx.AsyncClient, url: str) -> tuple[bool, Any]:
    try:
        r = await client.get(url)
        if r.status_code != 200:
            return False, None
        return True, r.json()
    except Exception:
        return False, None


async def probe_embed(client: httpx.AsyncClient) -> dict:
    ok, body = await _get_json(client, f"{config.EMBED_URL}/health")
    body = body if isinstance(body, dict) else {}
    return {
        "model": body.get("model", "bge-m3"),
        "dim": body.get("dim", 0),
        "reachable": bool(ok and body.get("ready", True)),
    }


async def probe_rerank(client: httpx.AsyncClient) -> dict:
    ok, body = await _get_json(client, f"{config.RERANK_URL}/health")
    body = body if isinstance(body, dict) else {}
    return {
        "model": body.get("model", "bge-reranker-base"),
        "reachable": bool(ok and body.get("ready", True)),
    }


async def probe_llm(client: httpx.AsyncClient) -> dict:
    base = config.NEXUS_LINK_BASE_URL
    result = {
        "slot": config.NEXUS_LINK_MODEL,
        "base": base,
        "reachable": False,
    }
    if not base:
        return result
    headers = {}
    if config.NEXUSLINK_KEY:
        headers["Authorization"] = f"Bearer {config.NEXUSLINK_KEY}"
    try:
        r = await client.get(f"{base}/models", headers=headers)
        result["reachable"] = r.status_code in (200, 401, 403)
        if r.status_code == 200:
            try:
                ids = [m.get("id") for m in r.json().get("data", [])]
                if config.NEXUS_LINK_MODEL:
                    result["slot_present"] = config.NEXUS_LINK_MODEL in ids
            except Exception:
                pass
    except Exception:
        pass
    return result


async def probe_all() -> dict:
    """Все три пробы параллельно; corpus-часть добавляет вызывающий."""
    timeout = httpx.Timeout(config.PROBE_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout) as client:
        llm, embed, rerank = await asyncio.gather(
            probe_llm(client), probe_embed(client), probe_rerank(client)
        )
    return {"llm": llm, "embed": embed, "rerank": rerank}
