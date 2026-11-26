"""Полный регрессионный набор LEX: одна команда — состояние целиком.

    python demo/check-all.py              # ядро + стабильность (коротко)
    python demo/check-all.py --fast       # без стабильности: только тесты
    python demo/check-all.py --runs 5 --questions 8

Что запускается:

1. ``demo/check-core.py`` — самопроверка ядра: каскад, policy, барьеры,
   тикеты, кейсы, гейт §12.8, критерий 3, журнал, алиасы ролей.
2. ``demo/check-stability.py`` — стабильность ответов на повторных прогонах.

Итог разделён на три корзины, и это главное:

* **PASS** — зелено;
* **FAIL** — покраснело, и это надо чинить сейчас;
* **EXPECTED-FAIL** — помечено ``xfail`` в самом тесте: баг известен, его
  чинят (FORGE/C-ось), и прогон не должен из-за него быть «сломанным».
  Выход с кодом 1 — только по настоящему FAIL.

Стабильность по умолчанию не считается провалом: она показывает картину
(сколько вопросов отвечают одинаково), а не гейтит сдачу. Флаг ``--strict``
делает нестабильность провалом — для того, кто уже взялся за фикс.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable or "python"

# Строки, которыми check-core.py сообщает итог.
_RE_TOTALS = re.compile(r"^(\d+) passed, (\d+) failed(?:, (\d+) expected-fail, (\d+) xpass)?$")
_RE_FAIL = re.compile(r"^провал: (.+)$")
_RE_XFAIL = re.compile(r"^ожидаемо красное \(чинится не здесь\): (.+)$")
_RE_XPASS = re.compile(r"^помечено xfail, но уже зелено.*: (.+)$")
# Авто-xfail среды: прогон сам пометил проверки красными, потому что модель
# не отвечает. Это НЕ провал кода, и в сводке их надо отделить от «баг
# чинится кем-то».
_RE_ENV_DOWN = re.compile(r"^внимание: модель не отвечает .*?Причина: (.+)$")


class Result:
    def __init__(self, name: str) -> None:
        self.name = name
        self.passed = 0
        self.failed: list[str] = []
        self.xfail: list[str] = []
        self.xpass: list[str] = []
        self.returncode = 0
        self.output = ""
        self.env_down = ""

    @property
    def ok(self) -> bool:
        return not self.failed


def _run(script: str, args: list[str], *, timeout: int) -> Result:
    r = Result(Path(script).stem)
    cmd = [PY, str(ROOT / script), *args]
    # Дочерний процесс печатает в pipe консольной кодировкой Windows (cp1251),
    # а разбор здесь читает utf-8. Без этой строки кириллица в именах тестов
    # приходит кашей, и регулярки «провал:»/«ожидаемо» не срабатывают — сводка
    # показывает «0 passed, 0 failed» на УСПЕШНОМ прогоне. PYTHONIOENCODING
    # переключает stdout/stderr ребёнка на utf-8 до того, как он что-то напишет.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8:replace",
           "PYTHONUTF8": "1"}
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              env=env)
    except subprocess.TimeoutExpired:
        r.returncode = 124
        r.output = f"таймаут {timeout} с"
        r.failed = [f"{script}: не уложился в {timeout} с"]
        return r
    r.returncode = proc.returncode
    r.output = (proc.stdout or "") + (proc.stderr or "")
    for line in r.output.splitlines():
        line = line.strip()
        m = _RE_TOTALS.match(line)
        if m:
            r.passed = int(m.group(1))
            continue
        m = _RE_FAIL.match(line)
        if m:
            r.failed = [x.strip() for x in m.group(1).split(",") if x.strip()]
            continue
        m = _RE_XFAIL.match(line)
        if m:
            r.xfail = [x.strip() for x in m.group(1).split(",") if x.strip()]
            continue
        m = _RE_XPASS.match(line)
        if m:
            r.xpass = [x.strip() for x in m.group(1).split(",") if x.strip()]
            continue
        m = _RE_ENV_DOWN.match(line)
        if m:
            r.env_down = m.group(1).strip()
    if r.returncode not in (0, 1):
        r.failed = r.failed or [f"{script}: код {r.returncode}"]
    return r


def _print_block(r: Result) -> None:
    status = "OK" if r.ok else "ПРОВАЛ"
    print(f"\n=== {r.name}: {status} — {r.passed} passed, {len(r.failed)} failed, "
          f"{len(r.xfail)} expected-fail ===")
    if r.env_down:
        print(f"   среда: модель недоступна — {r.env_down}")
        print("   (сквозные проверки помечены XFAIL как среда, не как баг кода)")
    if r.failed:
        for name in r.failed:
            print(f"   FAIL  {name}")
    if r.xfail:
        for name in r.xfail:
            print(f"   XFAIL {name}")
    if r.xpass:
        for name in r.xpass:
            print(f"   XPASS {name} · баг починен — снимите xfail")
    if not r.ok and not r.failed:
        tail = "\n".join(r.output.splitlines()[-12:])
        print("   вывод:")
        for line in tail.splitlines():
            print("   " + line)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Полный регрессионный набор LEX.")
    ap.add_argument("--fast", action="store_true", help="без стабильности (только тесты)")
    ap.add_argument("--questions", type=int, default=3, help="вопросов в прогоне стабильности")
    ap.add_argument("--runs", type=int, default=3, help="прогонов на вопрос")
    ap.add_argument("--strict", action="store_true",
                    help="нестабильность считать провалом (по умолчанию — только показать)")
    ap.add_argument("--no-llm", action="store_true", help="стабильность без модели")
    ap.add_argument("--timeout", type=int, default=900, help="таймаут на один набор, с")
    args = ap.parse_args(argv)
    # Сводка печатается русскими именами тестов; на cp1251-консоли Windows
    # это падало на «→»/«·». Мягкая замена — как в check-core и harness.
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    results: list[Result] = []

    core = _run("demo/check-core.py", [], timeout=args.timeout)
    results.append(core)
    _print_block(core)

    unstable = 0
    if not args.fast:
        stab = _run("demo/check-stability.py",
                    ["--questions", str(args.questions), "--runs", str(args.runs),
                     *(["--no-llm"] if args.no_llm else [])],
                    timeout=args.timeout)
        _print_block(stab)
        for line in stab.output.splitlines():
            if "нестабильных" in line:
                m = re.search(r"нестабильных (\d+)", line)
                if m:
                    unstable = int(m.group(1))
                print("   " + line.strip())

    total_pass = sum(r.passed for r in results)
    total_fail = sum(len(r.failed) for r in results)
    total_xfail = sum(len(r.xfail) for r in results)
    total_xpass = sum(len(r.xpass) for r in results)

    print("\n" + "=" * 60)
    print(f"ИТОГО: {total_pass} PASS · {total_fail} FAIL · {total_xfail} EXPECTED-FAIL"
          + (f" · {total_xpass} XPASS" if total_xpass else ""))
    if not args.fast:
        print(f"стабильность: нестабильных вопросов {unstable}"
              + ("" if args.strict else " (не влияет на итог, см. --strict)"))
    if total_xpass:
        print("XPASS — пометка xfail устарела: баг починен, снимите её.")
    if total_fail:
        print("Есть настоящие провалы — см. блоки выше.")
        return 1
    if args.strict and unstable:
        print("--strict: нестабильность считается провалом.")
        return 1
    print("Всё, что должно быть зелёным, зелено.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
