"""Реранкер bge-reranker-base (:8082).

Контракт §5: ``POST http://127.0.0.1:8082/rerank`` с ``{query, documents}``.
Возвращает ``results[].index`` + ``score``; порядок в ответе сервер не
гарантирует, поэтому сортируем по ``score`` сами.

Реранкер — второе мнение после гибридного поиска: он читает пару (вопрос,
фрагмент) целиком и потому ловит «рядом по смыслу, но не про это». Без него
цитата в ответе была бы из соседней секции — самый частый провал RAG на
регламентах.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

# Корень сервиса, путь дописываем сами — см. комментарий в index.py про EMBED_URL.
RERANK_URL = os.environ.get("LEX_RERANK_URL", "http://127.0.0.1:8082").rstrip("/")
RERANK_MODEL = os.environ.get("LEX_RERANK_MODEL", "bge-reranker-base")


class RerankError(RuntimeError):
    pass


def rerank(query: str, documents: list[str], *, top: int | None = None, timeout: float = 120.0) -> list[tuple[int, float]]:
    """Ранжирует документы под вопрос. Возвращает [(index, score)] по убыванию."""
    if not documents:
        return []
    body = json.dumps(
        {"model": RERANK_MODEL, "query": query, "documents": documents}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{RERANK_URL}/rerank", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except urllib.error.URLError as exc:
        raise RerankError(f"реранкер недоступен ({RERANK_URL}): {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RerankError(f"реранкер вернул не JSON: {exc}") from exc

    results = payload.get("results")
    if not isinstance(results, list):
        raise RerankError(f"реранкер вернул без results: {str(payload)[:200]}")

    pairs: list[tuple[int, float]] = []
    for item in results:
        try:
            pairs.append((int(item["index"]), float(item["score"])))
        except (KeyError, TypeError, ValueError) as exc:
            raise RerankError(f"непонятная запись реранкера: {item}") from exc
    pairs.sort(key=lambda p: p[1], reverse=True)
    return pairs[:top] if top else pairs


def reachable(timeout: float = 5.0) -> bool:
    """Живая проверка для /api/health."""
    try:
        return bool(rerank("проверка", ["проверочный текст"], timeout=timeout))
    except Exception:
        return False
