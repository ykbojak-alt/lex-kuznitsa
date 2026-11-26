"""Клиент NexusLink (контракт §5).

Переменные — только из окружения (``.env`` в корне, не в коде):
``NEXUSLINK_KEY``, ``NEXUS_LINK_BASE_URL``, ``NEXUS_LINK_MODEL``.

``max_tokens ≥ 1024`` — не прихоть: модель на этом слоте сначала пишет блок
``reasoning`` (видно в ``usage.reasoning_tokens``), и при меньшем лимите весь
бюджет уходит туда, а ``content`` приходит пустым. Проверено на живой слот
26.11.2026: 104 токена рассуждения на вопрос «работает?».
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import urllib.error
import urllib.request

MIN_MAX_TOKENS = 1024
DEFAULT_TIMEOUT = 180.0

# ------------------------------------------------------------ §21: два тона
#
# Владелец: «Агент может ответить сотруднику менее формальным языком, НО БЕЗ
# ИЗМЕНЕНИЯ СМЫСЛА». Тон меняет только формулировку; реквизиты нормы (числа,
# сроки, суммы, коды) остаются дословными — и это проверяется программно в
# grounding.py, а не на слово модели.

Tone = str  # "plain" | "formal"

TONE_RULES: dict[str, str] = {
    "plain": (
        "Пиши простым человеческим языком, как объяснил бы коллеге. Убирай "
        "канцелярит, длинные обороты разбивай на короткие, начинай с сути. "
        "Но смысл не меняй: условия, исключения и модальность («обязан» "
        "остаётся «обязан», а не «принято») сохраняй полностью."
    ),
    "formal": (
        "Пиши канцелярским языком документа: «согласно», «в соответствии с», "
        "полные формулировки. Ответ может уйти в письмо или в документ, "
        "поэтому недопустимы разговорные сокращения и вольный пересказ."
    ),
}

# Запрещены при ЛЮБОМ тоне, если их нет в цитате: они меняют модальность
# нормы. Список дублирует grounding.AMPLIFIERS намеренно — промпт и проверка
# должны говорить одно и то же, иначе модель честно выполнит промпт и
# провалит проверку.
BANNED_IN_TONE = (
    "обычно, как правило, в большинстве случаев, зачастую, как показывает "
    "практика, примерно, около, порядка"
)

RIGID_RULE = (
    "Числа, сроки, суммы, названия, коды и номера пунктов вставляй ДОСЛОВНО из "
    "фрагмента, не пересказывай и не округляй: «пяти рабочих дней» можно "
    "написать «пять рабочих дней», но не «около недели»; «не менее трёх лет» — "
    "«не меньше трёх лет», но не «несколько лет». Если в фрагменте нет числа — "
    "не подставляй своё."
)


def tone_rules(tone: Tone = "plain") -> str:
    """Правила тона для системного промпта."""
    base = TONE_RULES.get((tone or "plain").lower(), TONE_RULES["plain"])
    return (
        f"{base} Запрещены слова «{BANNED_IN_TONE}», если их нет в цитате: они "
        f"смягчают норму. {RIGID_RULE}"
    )


class LLMError(RuntimeError):
    pass


def base_url() -> str:
    return (os.environ.get("NEXUS_LINK_BASE_URL") or "").rstrip("/")


def model_name() -> str:
    return os.environ.get("NEXUS_LINK_MODEL") or "claude-sonnet-5"


def api_key() -> str:
    return os.environ.get("NEXUSLINK_KEY") or ""


def configured() -> bool:
    return bool(base_url() and api_key())


def chat(
    messages: list[dict],
    *,
    max_tokens: int = MIN_MAX_TOKENS,
    temperature: float = 0.2,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Один вызов ``/chat/completions``. Возвращает текст ответа."""
    if not configured():
        raise LLMError(
            "NexusLink не настроен: нужны NEXUS_LINK_BASE_URL и NEXUSLINK_KEY "
            "(см. .env и docs/DEPLOY-NEXUSLINK.md)"
        )
    url = f"{base_url()}/chat/completions"
    body = {
        "model": model_name(),
        "messages": messages,
        "max_tokens": max(MIN_MAX_TOKENS, int(max_tokens)),
        "temperature": temperature,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key()}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        with contextlib.suppress(Exception):
            detail = exc.read().decode("utf-8", "replace")[:400]
        raise LLMError(f"NexusLink HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"NexusLink недоступен ({url}): {exc}") from exc
    except json.JSONDecodeError as exc:
        raise LLMError(f"NexusLink вернул не JSON: {exc}") from exc

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMError(f"NexusLink без choices: {str(payload)[:300]}")
    message = choices[0].get("message") or {}
    content = (message.get("content") or "").strip()
    if not content:
        (message.get("reasoning") or "").strip()
        usage = payload.get("usage") or {}
        hint = (
            f" (reasoning_tokens={usage.get('reasoning_tokens')}, "
            f"max_tokens={body['max_tokens']})"
        )
        raise LLMError(
            "NexusLink вернул пустой content — обычно ответ ушёл целиком в блок "
            f"reasoning; поднимите max_tokens{hint}"
        )
    return content


def reachable(timeout: float = 20.0) -> bool:
    """Живая проверка для /api/health — не константа."""
    if not configured():
        return False
    try:
        chat([{"role": "user", "content": "ответь одним словом: ok"}], timeout=timeout)
        return True
    except Exception:
        return False


def timed_chat(messages: list[dict], **kw) -> tuple[str, int]:
    """``chat`` + длительность в миллисекундах — для поля ``ms`` в событиях."""
    t0 = time.monotonic()
    text = chat(messages, **kw)
    return text, int((time.monotonic() - t0) * 1000)
