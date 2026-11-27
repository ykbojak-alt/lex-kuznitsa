"""Правка нижней таблицы калькулятора экономики LEX.

Дефект (владелец, 28.11.2026): «в нижней таблице калькулятора почему такая чушь».
Три конкретные вещи:

1. В одной строке стояли слот шлюза (``claude-opus-4-7``) и фактическая модель
   (``deepseek-4.1-flash``) без единого слова о том, что это разные вещи —
   читатель видел противоречие, а не механику маски.
2. Цена DeepSeek в нижней таблице (0.225) не совпадала с ценой в строке 9
   (0.23) — два разных числа для одной и той же модели в одном файле.
3. «Средняя стоимость по трём слотам 0.2139» подана как стоимость запроса,
   хотя запрос уходит в ОДИН слот. Среднее по трём гипотетическим тарифам —
   не стоимость, а ориентир.

Плюс: слот на стенде — ``claude-opus-4-6``, а не ``-4-7``; и ни одна формула
не имела кэша значений, поэтому в файле, открытом без пересчёта, все суммы
пустые.

Запуск:  python tools/fix-calculator-sheet.py
"""

from __future__ import annotations

from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.workbook.properties import CalcProperties

BOOK = Path(r"D:\projects\lex-kuznitsa\docs\ИИкузница — калькулятор экономики решения.xlsx")
SHEET = "Расчёт"

# Единая цена DeepSeek для всего файла — та же, что в src/api/costs.py:
# среднее между peak и off-peak тарифами. Раньше в строке 9 стояло 0.23,
# а в нижней таблице 0.225 — два разных числа для одной модели.
DEEPSEEK_IN = 0.225
DEEPSEEK_OUT = 0.90

GREY = PatternFill("solid", fgColor="F2F2F2")
BOLD = Font(bold=True)


def main() -> None:
    wb = openpyxl.load_workbook(BOOK)
    ws = wb[SHEET]

    # --- 1. Строка 9: цена к общему значению ---------------------------------
    ws["F9"] = DEEPSEEK_IN
    ws["B16"] = (
        "Объём токенов — живой замер 27.11.2026 (журнал q-b2a82c2131, стенд :4437): "
        "4180 вх + 793 вых, 36.3 с, 1 вызов модели (только сборка ответа; разбор вопроса — "
        "локальная эвристика, модель не зовёт). Цена строки 9 — DeepSeek 4.1 Flash: "
        "$0.225/млн вход, $0.90/млн выход — среднее между peak и off-peak тарифами "
        "(та же цифра, что в нижней таблице и в src/api/costs.py). Курс $ = 102.5 — "
        "заданное допущение книги, не курс ЦБ РФ (ЦБ на 26.11.2026 — 82.3130 ₽/$)."
    )

    # --- 2. Заголовок таблицы: что она показывает ----------------------------
    ws["A17"] = (
        "Сравнение: во сколько обойдётся ТОТ ЖЕ запрос, если его отправить в другой слот. "
        "На стенде запрос идёт в один слот — средняя по трём ниже это ориентир, а не счёт."
    )
    ws["A17"].font = BOLD
    ws["A17"].alignment = Alignment(wrap_text=False, vertical="center")

    # --- 3. Шапка: развести слот и модель словами ----------------------------
    ws["A21"] = "Слот NexusLink (его называет клиент)"
    ws["B21"] = "Фактически отвечает модель"
    ws["I21"] = "₽ за 1 запрос"
    for cell in ("A21", "B21", "C21", "D21", "E21", "F21", "G21", "H21", "I21", "J21", "K21"):
        ws[cell].font = BOLD
        ws[cell].alignment = Alignment(wrap_text=True, vertical="bottom")

    # --- 4. Строки слотов ----------------------------------------------------
    slots = (
        22, "claude-opus-4-6", "deepseek-4.1-flash", DEEPSEEK_IN, DEEPSEEK_OUT, "$",
        "DeepSeek: среднее peak/off-peak",
        "https://api-docs.deepseek.com/quick_start/pricing/",
    ), (
        23, "claude-sonnet-4-7", "qwen-3.8-27b", 3.323, 16.9375, "CNY",
        "Qwen: среднее Beijing/Singapore",
        "https://help.aliyun.com/en/model-studio/qwen3-8-27b",
    ), (
        24, "claude-sonnet-5", "ernie-5.5-flash", 1.5, 6.0, "CNY",
        "ERNIE: среднее peak/off-peak",
        "https://cloud.baidu.com/article/10178908",
    )

    for row, slot, model, price_in, price_out, cur, method, link in slots:
        ws[f"A{row}"] = slot
        ws[f"B{row}"] = model
        ws[f"C{row}"] = "=$C$9"
        ws[f"D{row}"] = "=$D$9"
        ws[f"E{row}"] = "=$E$9"
        ws[f"F{row}"] = price_in
        ws[f"G{row}"] = price_out
        ws[f"H{row}"] = cur
        # Формула считает ИЗ СВОЕЙ строки (F/G/H), а не из строки 9: иначе при
        # правке цены в нижней таблице сумма не менялась бы вовсе.
        ws[f"I{row}"] = (
            f'=C{row}*(D{row}*F{row}+E{row}*G{row})/1000000'
            f'*IF(H{row}="$",$I$3,$K$28)'
        )
        ws[f"I{row}"].number_format = "0.0000"
        ws[f"J{row}"] = method
        ws[f"K{row}"] = link

    # Работающая строка помечена: это ответ на вопрос «сколько стоит запрос».
    ws["A22"] = "claude-opus-4-6 ← слот стенда"
    ws["A22"].font = BOLD
    for col in "ABCDEFGHIJK":
        ws[f"{col}22"].fill = GREY

    # --- 5. Средняя — честно названа ориентиром ------------------------------
    ws["A25"] = "Ориентир: средняя по трём слотам, если слоты равновероятны"
    ws["I25"] = "=AVERAGE(I22:I24)"
    ws["I25"].number_format = "0.0000"

    # --- 6. Курсы: не прятать в $K вне видимой части -------------------------
    ws["J26"] = "Курсы"
    ws["K26"] = "Значение"
    ws["J26"].font = BOLD
    ws["K26"].font = BOLD
    ws["J27"] = "USD/RUB · задано в I3"
    ws["K27"] = "=$I$3"
    ws["J28"] = "CNY/RUB · ЦБ РФ, 26.11.2026"
    ws["K28"] = 12.3154
    ws["J29"] = "USD/RUB · ЦБ РФ, 26.11.2026 (справочно, в расчёте не участвует)"
    ws["K29"] = 82.313

    # --- 7. Пересчёт при открытии -------------------------------------------
    # openpyxl не считает формулы: в файле не было кэша значений, и Excel
    # показывал пустоту до первого пересчёта. Флаг заставляет Excel посчитать
    # книгу сразу при открытии.
    wb.calculation = CalcProperties(fullCalcOnLoad=True)

    wb.save(BOOK)
    print(f"сохранено: {BOOK}")


if __name__ == "__main__":
    main()
