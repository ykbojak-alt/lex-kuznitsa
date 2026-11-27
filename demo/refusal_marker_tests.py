"""Regression checks for model refusal markers and the API journal contract."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

MARKERS = (
    "ОТКАЗ: нет данных",
    "**ОТКАЗ:** нет данных",
    "## ОТКАЗ: нет данных",
    "ОТКАЗ：нет данных",
)


def _stage_refusal(marker: str):
    from lex.orchestrator import Frame, Orchestrator, RunOptions

    orch = Orchestrator(RunOptions(question="Тестовый вопрос", no_llm=False))
    frame = Frame(index=1, chunk_id="c1", doc_id="D-1", title="Тест",
                  text="Тестовый нормативный фрагмент")
    orch._build_frames = lambda _docs, _cases: [frame]
    orch._ask_model_with_retry = lambda _frames, _state: (marker, "")
    orch._open_ticket = lambda _state, kind: "LEX-TEST-0001"
    orch._searched = lambda _state: {"documents": 1}
    orch._refusal_hint = lambda _ticket_id: "ticket created"
    state: dict = {"allowed": [object()], "case_hits": []}
    asyncio.run(orch._stage_compose(10, state))
    return state["final"][1]


def _api_journal(refusal_data) -> dict:
    from api import core
    from lex.orchestrator import RunOptions, Signal

    captured: list[dict] = []

    class FakeOrchestrator:
        def __init__(self, _opts):
            pass

        async def run(self):
            yield Signal(kind="refusal", data=refusal_data)

    fake_module = SimpleNamespace(Orchestrator=FakeOrchestrator,
                                  RunOptions=RunOptions)

    async def consume() -> None:
        async for _event in core.stream_answer("Тестовый вопрос", "director", None):
            pass

    with patch.object(core, "core_modules", return_value={"orchestrator": fake_module}), \
            patch.object(core, "core_ready", return_value=True), \
            patch.object(core, "journal_append", side_effect=captured.append):
        asyncio.run(consume())
    assert len(captured) == 1, f"ожидалась одна запись журнала, получено {len(captured)}"
    return captured[0]


def run_refusal_marker_tests() -> tuple[int, list[str]]:
    failures: list[str] = []
    for marker in MARKERS:
        try:
            data = _stage_refusal(marker)
            if data.kind != "model_refusal":
                raise AssertionError(f"refusal_kind={data.kind!r}")
            row = _api_journal(data)
            if row.get("refused") is not True:
                raise AssertionError(f"refused={row.get('refused')!r}")
            if row.get("refusal_kind") != "model_refusal":
                raise AssertionError(f"refusal_kind={row.get('refusal_kind')!r}")
        except Exception as exc:
            failures.append(f"{marker!r}: {type(exc).__name__}: {exc}")
    return len(MARKERS) - len(failures), failures


if __name__ == "__main__":
    count, failures = run_refusal_marker_tests()
    for failure in failures:
        print(f"FAIL {failure}")
    print(f"{count}/{len(MARKERS)} refusal-marker cases passed")
    raise SystemExit(1 if failures else 0)
