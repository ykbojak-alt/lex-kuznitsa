"""Transparent market-price estimate for one representative LEX request.

Token volume is the measured request in row 9 of the economics workbook.
Published prices are averaged across the listed public price variants, then
converted to rubles with the workbook's USD rate and the CBR CNY rate.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

USD_RUB = Decimal("102.5")  # Explicit workbook assumption; not a live FX quote.
CNY_RUB = Decimal("12.3154")  # Bank of Russia, 2026-11-26.
REFERENCE_CALLS = 1
REFERENCE_INPUT_TOKENS = 4_180
REFERENCE_OUTPUT_TOKENS = 793

_SLOTS = (
    {
        "slot": "claude-opus-4-7",
        "model": "deepseek-4.1-flash",
        "label": "DeepSeek 4.1 Flash",
        "input_price": Decimal("0.225"),
        "output_price": Decimal("0.90"),
        "currency": "USD",
        "method": "Среднее между off-peak и peak тарифами DeepSeek.",
        "source": "https://api-docs.deepseek.com/quick_start/pricing/",
    },
    {
        "slot": "claude-sonnet-4-7",
        "model": "qwen-3.8-27b",
        "label": "Qwen 3.8 27B",
        "input_price": Decimal("3.323"),
        "output_price": Decimal("16.9375"),
        "currency": "CNY",
        "method": "Среднее между тарифами Beijing и Singapore.",
        "source": "https://help.aliyun.com/en/model-studio/qwen3-8-27b",
    },
    {
        "slot": "claude-sonnet-5",
        "model": "ernie-5.5-flash",
        "label": "ERNIE 5.5 Flash",
        "input_price": Decimal("1.5"),
        "output_price": Decimal("6"),
        "currency": "CNY",
        "method": "Среднее между off-peak и peak тарифами Qianfan.",
        "source": "https://cloud.baidu.com/article/10178908",
    },
)


def estimate_request_costs() -> dict[str, Any]:
    """Return a reproducible per-slot estimate for the workbook's token volume."""
    models = []
    exact_costs = []
    for item in _SLOTS:
        fx = USD_RUB if item["currency"] == "USD" else CNY_RUB
        input_cost = (
            Decimal(REFERENCE_CALLS * REFERENCE_INPUT_TOKENS) * item["input_price"]
            / Decimal(1_000_000) * fx
        )
        output_cost = (
            Decimal(REFERENCE_CALLS * REFERENCE_OUTPUT_TOKENS) * item["output_price"]
            / Decimal(1_000_000) * fx
        )
        exact_cost = input_cost + output_cost
        exact_costs.append(exact_cost)
        models.append({
            "slot": item["slot"],
            "model": item["model"],
            "label": item["label"],
            "calls": REFERENCE_CALLS,
            "input_price_per_million": float(item["input_price"]),
            "output_price_per_million": float(item["output_price"]),
            "currency": item["currency"],
            "input_cost_rub": float(input_cost),
            "output_cost_rub": float(output_cost),
            "cost_rub": float(exact_cost),
            "method": item["method"],
            "source": item["source"],
        })

    average = sum(exact_costs) / Decimal(len(exact_costs))
    return {
        "basis": "market-estimate",
        "calls_per_request": REFERENCE_CALLS,
        "input_tokens": REFERENCE_INPUT_TOKENS,
        "output_tokens": REFERENCE_OUTPUT_TOKENS,
        "average_cost_rub": float(average),
        "usd_rub": float(USD_RUB),
        "cny_rub": float(CNY_RUB),
        "fx_as_of": "2026-11-26",
        "models": models,
        "note": (
            "Средняя оценка по трём слотам на измеренном объёме запроса "
            "из таблицы экономики; тарифы усреднены по официальным прайсам. "
            "USD/RUB 102.5 оставлен как значение книги; ЦБ РФ на 26.11.2026 "
            "указывает 82.3130 ₽/$. Это оценка, не фактический счёт NexusLink."
        ),
    }


__all__ = ["estimate_request_costs"]
