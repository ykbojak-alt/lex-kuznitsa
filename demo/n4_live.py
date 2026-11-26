"""Живой путь N-4: done-кадр несёт tokens, журнал — business_line.

Зовётся из demo/check-core.py (check_tokens_and_line). Отдельным модулем —
потому что тест подменяет HTTP-вызов провайдера и должен вернуть его назад
даже при падении: иначе следующий тест пойдёт по зафиксированному ответу.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path


def _fake_payload(text: str, prompt: int = 120, completion: int = 40) -> dict:
    """Ответ провайдера в форме, которую ждёт lex.llm._post_chat."""
    return {
        "choices": [{
            "message": {"content": text, "reasoning": ""},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }


def run(check_fn) -> None:
    """Прогон с подменённым провайдером; ``check_fn`` — проверка из check-core."""
    from api import config as api_config
    from api import core as api_core
    from lex import llm

    old = {
        "JOURNAL": api_config.JOURNAL_PATH,
        "TICKETS": api_config.TICKETS_PATH,
        "CASES": api_config.CASES_PATH,
        "ESCAL": api_config.ESCALATIONS_PATH,
        "CHR": api_config.CHANGE_REQUESTS_PATH,
        "CORPUS": api_config.CORPUS_DIR,
    }
    tmp = Path(tempfile.mkdtemp(prefix="lex-n4-"))
    api_config.JOURNAL_PATH = tmp / "journal.jsonl"
    api_config.TICKETS_PATH = tmp / "tickets.jsonl"
    api_config.CASES_PATH = tmp / "cases.jsonl"
    api_config.ESCALATIONS_PATH = tmp / "escalations.jsonl"
    api_config.CHANGE_REQUESTS_PATH = tmp / "change_requests.jsonl"
    # Корпус — рабочий по умолчанию (corpus-real): вопрос ниже покрыт его
    # документами, поэтому каскад доходит до compose, а не уходит в заявку.

    real_post = llm._post_chat

    def fake_post(*args, **kwargs):
        # Сигнатура настоящего _post_chat меняется (compose зовёт иначе, чем
        # chat) — подмена всеядная, иначе тест падает на собственной лямбде.
        messages = kwargs.get("messages") or (args[0] if args else [])
        max_tokens = kwargs.get("max_tokens") or (args[1] if len(args) > 1 else 0)
        temperature = kwargs.get("temperature") or (args[2] if len(args) > 2 else 0)
        # Один и тот же ответ на любой шаг: тест про доставку полей, не про
        # качество текста. Ссылка [1] держит утверждение покрытым, иначе
        # verify уведёт ответ в отказ и до done дело не дойдёт.
        return _fake_payload(
            "Согласно регламенту, команда проекта формируется приказом [1]."), {
            "model": llm.model_name(), "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature,
        }

    llm._post_chat = fake_post
    try:
        events: list[str] = []

        async def _go():
            async for chunk in api_core.stream_answer(
                    "Как формируется команда проекта?", "director", None,
                    session_id="s-n4"):
                events.append(chunk)

        asyncio.run(_go())

        # --- done-кадр ---
        done = None
        answer = None
        for ev in events:
            if ev.startswith("event: done"):
                done = json.loads(ev.split("data: ", 1)[1].strip())
            elif ev.startswith("event: answer"):
                answer = json.loads(ev.split("data: ", 1)[1].strip())
        check_fn("N-4: вопрос дошёл до done-кадра", done is not None,
                 f"кадров {len(events)}")
        if done is not None:
            tok = done.get("tokens")
            ok = (isinstance(tok, dict) and tok.get("prompt", 0) > 0
                  and tok.get("total", 0) >= tok.get("prompt", 0)
                  and tok.get("calls", 0) >= 1)
            check_fn("N-4: done несёт tokens с prompt>0 (живой каскад)", ok,
                     str(tok))
        else:
            check_fn("N-4: done несёт tokens с prompt>0 (живой каскад)", False,
                     "done не пришёл")

        # --- журнал ---
        rows = []
        if api_config.JOURNAL_PATH.exists():
            rows = [json.loads(x) for x in
                    api_config.JOURNAL_PATH.read_text(encoding="utf-8").splitlines()
                    if x.strip()]
        row = rows[0] if rows else {}
        check_fn("N-4: журнал записан после ответа", bool(row),
                 f"записей {len(rows)}")
        line = (row.get("business_line") or "")
        # Направление зависит от корпуса: у sample-corpus это «проектная
        # деятельность», у corpus-real вопрос может направления не иметь.
        # Проверяем не конкретную строку, а согласие журнала с policy.
        from lex.corpus import load_corpus
        from lex.policy import evaluate
        verdict = evaluate(load_corpus(api_config.CORPUS_DIR),
                           "Как формируется команда проекта?")
        expected = getattr(verdict, "business_line", None) or ""
        check_fn("N-4: журнал пишет business_line шага policy",
                 line == expected, f"журнал {line!r} vs policy {expected!r}")
        jtok = row.get("tokens") or {}
        check_fn("N-4: журнал пишет tokens того же вопроса",
                 jtok.get("prompt", 0) > 0, str(jtok))
        check_fn("N-4: ответ не пустой (тест прошёл до сборки ответа)",
                 bool(answer and str(answer.get("text") or "").strip()),
                 "answer есть" if answer else "answer нет")
    finally:
        llm._post_chat = real_post
        api_config.JOURNAL_PATH = old["JOURNAL"]
        api_config.TICKETS_PATH = old["TICKETS"]
        api_config.CASES_PATH = old["CASES"]
        api_config.ESCALATIONS_PATH = old["ESCAL"]
        api_config.CHANGE_REQUESTS_PATH = old["CHR"]
        api_config.CORPUS_DIR = old["CORPUS"]
