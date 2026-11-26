"""Клиент NexusLink (контракт §5).

Переменные — только из окружения (``.env`` в корне, не в коде):
``NEXUSLINK_KEY``, ``NEXUS_LINK_BASE_URL``, ``NEXUS_LINK_MODEL``.

``max_tokens ≥ 1024`` — не прихоть: модель на этом слоте сначала пишет блок
``reasoning`` (видно в ``usage.reasoning_tokens``), и при меньшем лимите весь
бюджет уходит туда, а ``content`` приходит пустым.

**Замер 26.11.2026 на corpus-real, 20 вызовов тяжёлого промпта:** при
``max_tokens=1024`` пять ответов пришли пустыми, все пять — с
``finish_reason=length`` (рассуждение съело бюджет). При ``max_tokens=4096``
пустых ноль. Поэтому базовый лимит — 4096, а не 1024: 1024 хватало на
короткие вопросы и падало на длинном контексте, а демо гоняет именно длинный.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
import urllib.error
import urllib.request

from . import llm_debug

MIN_MAX_TOKENS = 1024
# Базовый лимит ответа. Замер 26.11.2026 на claude-sonnet-4-7: рассуждение
# занимало 31 010 символов и съедало весь потолок 8192 — finish_reason=length,
# content пуст, ответа нет. Поэтому база 16384: рассуждение + ответ должны
# уместиться оба, иначе слот стабильно возвращает пустоту на «сложных»
# вопросах (а на простых проходит — отсюда была нестабильность 2/3).
DEFAULT_MAX_TOKENS = int(os.environ.get("LEX_LLM_BASE_TOKENS", "16384"))
DEFAULT_TIMEOUT = 240.0

# «Пустой content» — закономерность слота: модель пишет блок рассуждения, и
# при малом лимите ответ не начинается. Повтор с ЕЩЁ большим потолком имеет
# смысл только если первая попытка упёрлась в length; при 16384 чаще всего
# уже хватает, поэтому один повтор.
EMPTY_RETRIES = int(os.environ.get("LEX_LLM_EMPTY_RETRIES", "1"))
# Бюджеты попыток растут от базы. Потолок 32768 — верхняя граница, которую
# слот отдаёт без ошибки; выше начинается уже отказ провайдера.
EMPTY_MAX_TOKENS = int(os.environ.get("LEX_LLM_MAX_TOKENS", "32768"))

# Троттлинг апстрима (OpenRouter 429 внутри NexusLink-502) — вторая причина
# провалов, и она НЕ лечится размером лимита. Диагностика 26.11.2026: при
# 20 вызовах подряд с 16-го сыплется «all candidates exhausted … HTTP 429»,
# а фактических пустых content — 0. Значит нужна пауза, а не бюджет.
RATE_RETRIES = int(os.environ.get("LEX_LLM_RATE_RETRIES", "3"))
RATE_BASE_PAUSE = float(os.environ.get("LEX_LLM_RATE_PAUSE", "3.0"))
# Опознаём троттлинг по тексту ошибки: NexusLink заворачивает 429 апстрима в
# 502 и теряет код, поэтому кода мало — нужна подпись.
_RATE_MARKERS = ("429", "ratelimit", "rate limit", "too many requests",
                 "candidates exhausted", "quota")
RETRY_BUDGETS = tuple(
    int(x) for x in (os.environ.get("LEX_LLM_RETRY_BUDGETS") or "16384,32768").split(",")
    if x.strip()
)

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
    # Дефолт — живой слот: 27.11.2026 claude-sonnet-4-7 попал в чёрный список
    # апстрима целиком, и стенд без переменной окружения отвечал ошибкой.
    return os.environ.get("NEXUS_LINK_MODEL") or "claude-sonnet-4-6"


def model_chain() -> list[str]:
    """Основной слот и запасные, в порядке приоритета.

    Слоты NexusLink флапают: замер 27.11.2026 на стенде — 88 ошибок из 244
    вызовов (36%), все вида «all candidates exhausted … candidates are
    currently blacklisted». Отказал кандидат у апстрима — и слот целиком
    недоступен, хотя остальные шесть работают. Падать сотруднику в ответ на
    это нельзя: у нас есть другие слоты, и подменять модель допустимо, потому
    что смысл ответа от модели не зависит — он проверяется цитатами.

    Порядок задаётся ``LEX_LLM_FALLBACK_MODELS`` (через запятую). По умолчанию
    ``claude-sonnet-4-6, claude-opus-5``: в стресс-тесте 6/6 каждый, среднее
    0.8 с и 1.4 с — быстрее основного opus-4-6 (18.8 с).
    """
    primary = model_name()
    raw = os.environ.get("LEX_LLM_FALLBACK_MODELS")
    if raw is None:
        raw = "claude-sonnet-4-6,claude-opus-5"
    chain = [primary]
    for item in raw.split(","):
        item = item.strip()
        if item and item not in chain:
            chain.append(item)
    return chain


def api_key() -> str:
    return os.environ.get("NEXUSLINK_KEY") or ""


def configured() -> bool:
    return bool(base_url() and api_key())


def is_slot_dead(exc: Exception) -> bool:
    """Слот недоступен целиком — все его кандидаты в чёрном списке.

    Это не троттлинг (пауза не поможет: кандидаты заблокированы на стороне
    апстрима) и не обрыв сети. Лечится только сменой слота, поэтому опознаётся
    отдельно — чтобы ``chat`` переключился на следующий, а не ждал впустую.
    """
    text = str(exc).lower()
    return "blacklisted" in text or "candidates exhausted" in text


def chat(
    messages: list[dict],
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 0.2,
    timeout: float = DEFAULT_TIMEOUT,
    retries_on_empty: int | None = None,
    model: str | None = None,
) -> str:
    """Вызов ``/chat/completions`` с лестницей бюджетов. Возвращает текст.

    **Цепочка слотов.** Если слот отдал «all candidates exhausted … blacklisted»,
    повторять его бессмысленно: кандидаты заблокированы на апстриме. Вместо
    этого переходим к следующему слоту из ``model_chain()``. Смысл ответа от
    модели не зависит — он проверяется цитатами в ``verify``, поэтому подмена
    слота не ослабляет гарантию «только из регламента».

    **Лестница бюджетов.** Провайдер на этом слоте пишет блок ``reasoning`` до
    ``content``. При малом ``max_tokens`` рассуждение съедает весь лимит:
    ``finish_reason = length``, ``content`` пуст. Это не сбой сети — это
    исчерпанный бюджет, и лечится он бОльшим лимитом, а не отказом сотруднику.

    Поэтому пустой ответ — не ошибка сразу, а повод повторить с лимитом из
    ``RETRY_BUDGETS``. Каждая попытка пишется в ``var/llm-debug.jsonl`` (если
    журнал включён, ``LEX_LLM_DEBUG=1``) — по этому журналу считается
    распределение, а не «иногда падает».
    """
    if not configured():
        raise LLMError(
            "NexusLink не настроен: нужны NEXUS_LINK_BASE_URL и NEXUSLINK_KEY "
            "(см. .env и docs/DEPLOY-NEXUSLINK.md)"
        )
    retries = EMPTY_RETRIES if retries_on_empty is None else retries_on_empty
    budgets_chain = [max(MIN_MAX_TOKENS, int(max_tokens))]
    for b in RETRY_BUDGETS[:retries]:
        if b > budgets_chain[-1]:
            budgets_chain.append(b)

    models = [model] if model else model_chain()
    last_error: LLMError | None = None
    last: tuple[dict, dict] | None = None
    dead: list[str] = []

    for slot in models:
        if slot in dead:
            continue
        slot_failed = False
        for attempt, budget in enumerate(budgets_chain):
            t0 = time.monotonic()
            try:
                payload, body = _post_chat(messages, budget, temperature, timeout, slot)
            except LLMError as exc:
                llm_debug.record(
                    model=slot, max_tokens=budget, finish_reason="",
                    content="", reasoning="",
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    attempt=attempt, retried=attempt > 0, error=str(exc),
                )
                last_error = exc
                if is_slot_dead(exc):
                    dead.append(slot)
                    slot_failed = True
                    break
                if not is_transient(exc):
                    raise
                if _throttle_wait(attempt + 1):
                    continue
                slot_failed = True
                break
            content = _content_of(payload, body)
            reasoning = _reasoning_of(payload)
            _tokens_add(payload)
            llm_debug.record(
                model=slot, max_tokens=budget,
                finish_reason=_finish_reason(payload), content=content,
                reasoning=reasoning,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                attempt=attempt, retried=attempt > 0,
            )
            if content:
                return content
            last = (payload, body)
        if slot_failed:
            continue

    if last is None:
        raise LLMError(
            "NexusLink не отдал ответ ни на одной попытке: "
            f"{last_error or 'апстрим недоступен'} "
            f"(слотов опрошено {len(models)}, мертвы: {', '.join(dead) or 'нет'})"
        )
    _raise_empty(*last)


def is_throttled(exc: Exception) -> bool:
    """429 апстрима, завёрнутый NexusLink в 502 — по подписи, а не по коду."""
    text = str(exc).lower()
    return any(marker in text for marker in _RATE_MARKERS)


# Разрыв установленного соединения. В стрессе 26.11.2026 первый из десяти
# прогонов дал `[WinError 10054] Удалённый хост принудительно разорвал
# соединение` — соединение с апстримом оборвалось само, запрос не дошёл.
# Повтор здесь законен: это не отказ провайдера, а сеть.
_TRANSPORT_MARKERS = ("10054", "10053", "connection reset", "connection aborted",
                      "connection refused", "timed out", "время ожидания")


def is_transient(exc: Exception) -> bool:
    """Повторяемое ли это: троттлинг или обрыв связи."""
    text = str(exc).lower()
    return is_throttled(exc) or any(m in text for m in _TRANSPORT_MARKERS)


def _throttle_wait(attempt: int) -> bool:
    """Пауза перед повтором. False — попытки исчерпаны, ждать нечего."""
    if attempt > RATE_RETRIES:
        return False
    time.sleep(RATE_BASE_PAUSE * attempt)
    return True


def _post_chat(messages: list[dict], max_tokens: int, temperature: float,
               timeout: float, model: str | None = None) -> tuple[dict, dict]:
    url = f"{base_url()}/chat/completions"
    body = {
        "model": model or model_name(),
        "messages": messages,
        "max_tokens": max_tokens,
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
    return payload, body


def _first_choice(payload: dict) -> dict:
    choices = payload.get("choices") or [{}]
    return choices[0] if isinstance(choices[0], dict) else {}


def _content_of(payload: dict, body: dict) -> str:
    message = _first_choice(payload).get("message") or {}
    return (message.get("content") or "").strip()


def _reasoning_of(payload: dict) -> str:
    """Блок рассуждения. Провайдер кладёт его и в message, и в choice."""
    message = _first_choice(payload).get("message") or {}
    return (message.get("reasoning") or _first_choice(payload).get("reasoning") or "").strip()


def _finish_reason(payload: dict) -> str:
    return str(_first_choice(payload).get("finish_reason") or "")


# --- счётчик токенов ---------------------------------------------------------
# Расход считается по ответам провайдера (`usage`), а не по локальной оценке:
# у нас нет токенизатора слота, а `usage` приходит в каждом ответе. Копим за
# весь запрос (все попытки и все вызовы LLM на шагах), потому что человеку
# нужно «сколько стоил этот вопрос», а не «сколько стоил один HTTP-вызов».
#
# Счётчик НЕ thread-local: генератор SSE и вызов LLM могут исполняться в
# разных потоках (anyio-пул в uvicorn), и тогда reset в одном потоке не виден
# в другом — счётчик молча оставался нулевым. Состояние защищено замком, а
# обнуление вызывается в начале запроса.
_TOKEN_LOCK = threading.Lock()
_TOKENS: dict[str, int] = {"prompt": 0, "completion": 0, "calls": 0}


def _tokens_reset() -> None:
    with _TOKEN_LOCK:
        _TOKENS["prompt"] = 0
        _TOKENS["completion"] = 0
        _TOKENS["calls"] = 0


def _tokens_add(payload: dict) -> None:
    usage = payload.get("usage") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    with _TOKEN_LOCK:
        _TOKENS["prompt"] += prompt
        _TOKENS["completion"] += completion
        _TOKENS["calls"] += 1


def tokens_total() -> dict:
    """Накопленный расход текущего вопроса: ``{prompt, completion, total, calls}``."""
    with _TOKEN_LOCK:
        prompt = int(_TOKENS["prompt"])
        completion = int(_TOKENS["completion"])
        calls = int(_TOKENS["calls"])
    return {
        "prompt": prompt,
        "completion": completion,
        "total": prompt + completion,
        "calls": calls,
    }


def _raise_empty(payload: dict, body: dict) -> None:
    usage = payload.get("usage") or {}
    hint = (
        f" (finish_reason={_finish_reason(payload) or '?'}, "
        f"reasoning_tokens={usage.get('reasoning_tokens')}, "
        f"reasoning_chars={len(_reasoning_of(payload))}, "
        f"max_tokens={body.get('max_tokens')})"
    )
    raise LLMError(
        "NexusLink вернул пустой content на всех попытках — рассуждение "
        f"съедает бюджет; нужен другой слот или потолок выше{hint}"
    )


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
