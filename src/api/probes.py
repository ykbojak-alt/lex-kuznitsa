"""Пробы живости сервисов для /api/health.

Три независимые проверки: LLM-шлюз NexusLink, сервис эмбеддингов и сервис
переранжирования. Каждая возвращает словарь с полем reachable — вызывающий
собирает из них общую картину, не полагаясь на один источник.
"""

from __future__ import annotations

import asyncio

import httpx

from . import config


async def _get_json(client: httpx.AsyncClient, url: str) -> tuple[bool, dict]:
    try:
        r = await client.get(url)
        if r.status_code != 200:
            return False, {}
        return True, r.json()
    except Exception:
        return False, {}


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


# Внутренний слот NexusLink -> масочное имя, которое видит пользователь
# (владелец, 27.11.2026: в аналитике показывать DeepSeek/Qwen/Ernie,
# а не внутренний claude-opus-4-6). Обратный словарь к MODELS_PUBLIC.
# Обновлять при смене NEXUS_LINK_MODEL или добавлении моделей.
_MODEL_PUBLIC_MAP = {
    "claude-opus-4-7": "deepseek-4.1-flash",
    "claude-sonnet-4-7": "qwen-3.8-27b",
    "claude-sonnet-5": "ernie-5.5-flash",
    "claude-opus-4-6": "deepseek-4.0-flash",
}


async def probe_llm(client: httpx.AsyncClient) -> dict:
    base = config.NEXUS_LINK_BASE_URL
    slot = config.NEXUS_LINK_MODEL
    result = {
        "slot": slot,
        "slot_label": _MODEL_PUBLIC_MAP.get(slot, slot),
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
