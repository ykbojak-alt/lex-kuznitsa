"""Страж: числа в доке о блоке build сверяются с ответом API, а не с памятью.

Три раза за ночь число в этом абзаце не выводилось из того, что считало:
24 модуля, «Шесть полей» при восьми, «Восемь полей» при девяти. Правило
«проверять предъявленное» тут не работает — предъявленное и есть число.
Работает только одно: посчитать за автора.

Что делает:
  1. берёт блок §8 (таблица полей) и считает, сколько ключей она перечисляет;
  2. сравнивает с len(_build_fingerprint()) — фактом;
  3. сверяет словесное число («Девять полей») с обоими;
  4. сверяет упомянутые числа модулей/файлов с _build_fingerprint();
  5. КРАСНЕЕТ, если расходятся.

Запуск:  python tools/check-doc-counts.py [--doc docs/DEMO-SCRIPT.md]
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

WORDS = {
    1: "Один", 2: "Два", 3: "Три", 4: "Четыре", 5: "Пять", 6: "Шесть",
    7: "Семь", 8: "Восемь", 9: "Девять", 10: "Десять",
}

fails: list[str] = []
oks: list[str] = []


def chk(label: str, cond: bool, detail: str = "") -> None:
    (oks if cond else fails).append(f"{label}{(' — ' + detail) if detail else ''}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", default=str(ROOT / "docs" / "DEMO-SCRIPT.md"))
    args = ap.parse_args()

    from api.app import _build_fingerprint

    fp = _build_fingerprint()
    keys = set(fp)
    doc = Path(args.doc).read_text(encoding="utf-8")

    # --- блок таблицы ---
    if "Отпечаток сборки записан" not in doc:
        print("SKIP: в документе нет блока «Отпечаток сборки записан»")
        return 0
    block = doc.split("Отпечаток сборки записан")[1].split("**Чего НЕ следует")[0]

    table = [line for line in block.split("\n")
             if line.strip().startswith("|") and "---" not in line and "| поле |" not in line]
    listed: set[str] = set()
    for row in table:
        listed |= set(re.findall(r"`([a-z0-9_]+)`", row))
    # ключи внутри пути build.* не считаются отдельными именами верхнего уровня
    top = {n for n in listed if n in keys}

    chk("таблица перечисляет все ключи ответа",
        top == keys, f"таблица {len(top)} / ответ {len(keys)}: нет {sorted(keys - top)}" if top != keys else f"{len(top)}")

    # --- словесное число ---
    m = re.search(r"([А-Яа-я]+) полей", block)
    if m:
        word = m.group(1).capitalize()
        claim = next((n for n, w in WORDS.items() if w == word), None)
        chk("словесное число полей совпадает с ответом",
            claim == len(keys), f"«{word}» = {claim}, в ответе {len(keys)}")
        chk("словесное число совпадает со строкой таблицы",
            claim == len(top), f"«{word}» = {claim}, в таблице {len(top)}")
    else:
        chk("словесное число полей найдено", False, "нет фразы «N полей»")

    # --- арифметика строк, если заявлена ---
    m = re.search(r"(\d+(?:\s*\+\s*\d+)+)\s*=\s*(\d+)", block)
    if m:
        parts = [int(x) for x in re.findall(r"\d+", m.group(1))]
        total = int(m.group(2))
        chk("арифметика в тексте сходится", sum(parts) == total,
            f"{' + '.join(map(str, parts))} = {sum(parts)}, заявлено {total}")
        chk("заявленная сумма равна ответу", total == len(keys), f"{total} против {len(keys)}")

    # --- прочие счётные утверждения против API ---
    for n, unit in re.findall(r"(\d+)\s*(модул\w*)", block):
        chk(f"«{n} {unit}» против answer.modules",
            int(n) == fp["modules"], f"в ответе {fp['modules']}")
    for n, unit in re.findall(r"(\d+)\s*(файл\w*)\s+клиента", block):
        chk(f"«{n} {unit} клиента» против answer.static_files",
            int(n) == fp["static_files"], f"в ответе {fp['static_files']}")

    # --- утверждения об ОТСУТСТВИИ ключа, ПО ВСЕМУ ДОКУМЕНТУ ---
    #
    # §8 — не единственное место, где пишут про поля. §16 («Разрыв
    # наблюдаемости») сказал про них раньше. Первая версия стража читала
    # только §8, и ложь, подложенная в §16, давала 9 passed / EXIT=0 —
    # проверено. Счётные утверждения привязаны к таблице §8, но отрицания
    # («ключа X нет») проверяются в любом абзаце документа.
    #
    # Осторожно с примером: фраза «ключа `build.sha256` нет» в репозитории
    # НЕ встречалась (git show f4044ce/651912e: там «блок `build` (НЕ
    # `build.sha256` — поля лежат внутри `build`)», и это верно). Моё
    # утверждение о такой фразе было домыслом о предъявленном.
    def _exists(name: str) -> bool:
        top_name = name.split(".")[-1]
        return (name in keys
                or name in fp
                or (name.startswith("build.") and top_name in keys))

    for name in re.findall(r"[Кк]люча\s+`([a-z0-9_.]+)`\s+(?:тоже\s+)?нет", doc):
        chk(f"утверждение «ключа `{name}` нет» истинно", not _exists(name),
            "но он ЕСТЬ в ответе" if _exists(name) else "")

    # «НЕ `X` … нет» — форма из скобки. Найдена ALICE: первый вариант регулярки
    # её пропускал. _exists допускает и путь (`build.sha256`), и ключ верхнего
    # уровня, потому что «нет такого ключа» может относиться к обоим.
    for neg in re.findall(r"НЕ\s+`([a-z0-9_.]+)`[^.\n]{0,40}?нет", doc):
        chk(f"утверждение в скобке «НЕ `{neg}` … нет» истинно", not _exists(neg),
            "но он ЕСТЬ в ответе" if _exists(neg) else "")

    # --- упомянутые пути build.X обязаны существовать (весь документ) ---
    for path in sorted(set(re.findall(r"`(build\.[a-z0-9_]+)`", doc))):
        sub = path.split(".", 1)[1]
        chk(f"путь `{path}` существует в ответе", sub in keys,
            f"в ответе нет ключа `{sub}`" if sub not in keys else "")

    for line in oks:
        print(f"OK   {line}")
    for line in fails:
        print(f"провал: {line}")
    # Формат итога — контракт check-all.py (регулярка _RE_TOTALS): без него
    # успешный прогон читается как «не дошёл до итога», то есть как провал.
    # Проверено вызовом через check-all._run: с прежней строкой «ИТОГ: …»
    # здоровый страж давал failed=['… не дошёл до итога (код 0)'].
    print(f"{len(oks)} passed, {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
