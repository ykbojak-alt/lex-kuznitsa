"""E2E-гейт конвейера LEX через живой HTTP-SSE.

Запуск из корня репозитория:

    python demo/check-pipeline.py
    python demo/check-pipeline.py --no-llm      # без модели, быстрее
    python demo/check-pipeline.py --keep-state  # оставить журнал прогона

Зачем это отдельно от `check-core.py`: тот проверяет ядро вызовами Python,
а здесь проверяется **весь путь** — uvicorn, HTTP, SSE-кадры, журнал и
аналитика. Ядро может быть зелёным, а поток SSE — разъехаться; этот скрипт
ловит именно такие расхождения.

Что проверяется на каждом вопросе:
  * шаги идут в порядке `STEP_ORDER`, без пропусков и повторов;
  * у каждого шага есть `n/total/key/title/detail/ms/color`;
  * ровно одно терминальное событие `answer|refusal|clarify|escalate`;
  * терминальное — последнее содержательное, затем `done`
    (кроме `clarify`: §10 закрывает поток без `done`).

Коды возврата: 0 — прогон чистый или честный SKIP; 1 — расхождение.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _corpus_dir() -> Path:
    """Корпус выбирается так же, как в транспорте: LEX_CORPUS_DIR или sample-corpus."""
    raw = os.environ.get("LEX_CORPUS_DIR") or "sample-corpus"
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


CORPUS = _corpus_dir()
QUESTIONS = CORPUS / "questions.jsonl"

PASS: list[str] = []
FAIL: list[str] = []
SKIPPED: list[str] = []

# Шаги, которые обязаны быть в каждом прогоне. Источник правды — ядро, но
# сверяем с константой намеренно: если ядро и SSE разъедутся, тест должен
# упасть, а не подстроиться под то, что пришло.
EXPECTED_ORDER = (
    "accept", "policy", "decompose", "inspect", "retrieve", "cases",
    "permissions", "resolve", "conflict", "compose", "verify",
)
STEP_FIELDS = ("n", "total", "key", "title", "detail", "ms", "color")
TERMINAL = ("answer", "refusal", "clarify", "escalate")


def ok(name: str, detail: str = "") -> None:
    PASS.append(name)
    print(f"OK   {name}" + (f" — {detail}" if detail else ""))


def bad(name: str, detail: str = "") -> None:
    FAIL.append(name)
    print(f"FAIL {name}" + (f" — {detail}" if detail else ""))


def skip(name: str, detail: str = "") -> None:
    SKIPPED.append(name)
    print(f"SKIP {name}" + (f" — {detail}" if detail else ""))


def check(name: str, cond: bool, detail: str = "") -> bool:
    (ok if cond else bad)(name, detail)
    return bool(cond)


# ------------------------------------------------------------------ .env


def load_env() -> bool:
    """Грузит .env из корня. Истина — если есть ключ NexusLink.

    python-dotenv в этом окружении не установлен, поэтому сначала пробуем его,
    затем — собственный разбор. Второй путь нарочно узкий: только ``KEY=VALUE``,
    без подстановок и экспорта — этого достаточно и неожиданностей меньше.
    """
    env_file = ROOT / ".env"
    if not env_file.exists():
        return False
    try:
        from dotenv import load_dotenv

        load_dotenv(env_file)
    except ImportError:
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
    return bool(os.environ.get("NEXUSLINK_KEY") and os.environ.get("NEXUS_LINK_BASE_URL"))


# ------------------------------------------------------------------ сервер


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_health(base: str, timeout: float = 90.0) -> tuple[bool, str]:
    """Ждёт /api/health. Возвращает (поднялся, последняя ошибка)."""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/api/health", timeout=3) as resp:
                body = json.loads(resp.read())
                return True, str(body.get("status", ""))
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
        except urllib.error.URLError as exc:
            last = str(exc.reason)
        except (json.JSONDecodeError, OSError) as exc:
            last = str(exc)
        time.sleep(0.4)
    return False, last or "таймаут"


def start_server(port: int, state_dir: Path) -> tuple[subprocess.Popen, object]:
    """Поднимает uvicorn из корня репозитория. Возвращает процесс и лог."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    # Вопросы берутся из CORPUS (по умолчанию sample-corpus), а сервер без этих
    # переменных поднялся бы на корпусе заказчика: вопросы одни, документы другие,
    # и шаги resolve/conflict пропали бы — гейт падал бы на ровном месте.
    env["LEX_CORPUS_DIR"] = str(CORPUS)
    env.setdefault("LEX_CORPUS_NORM_DIR", str(ROOT / "no-corpus-norm"))
    # Журнал и кейсы — во временный каталог: гейт не должен дописывать в живой
    # var/. Иначе каждый прогон искажает аналитику демо.
    env["LEX_VAR_DIR"] = str(state_dir)
    env["LEX_JOURNAL_PATH"] = str(state_dir / "journal.jsonl")
    env["LEX_CASES_PATH"] = str(state_dir / "cases.jsonl")
    env["LEX_TICKETS_PATH"] = str(state_dir / "tickets.jsonl")
    env["LEX_ESCALATIONS_PATH"] = str(state_dir / "escalations.jsonl")
    env["LEX_CHANGE_REQUESTS_PATH"] = str(state_dir / "change_requests.jsonl")
    log = (state_dir / "server.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.api.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    return proc, log


def stop_server(proc: subprocess.Popen, log) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    try:
        log.close()
    except Exception:
        pass


# --------------------------------------------------------------------- SSE


def _parse_stream(raw: str) -> list[tuple[str, dict]]:
    """Разбирает SSE-поток на пары (имя события, данные).

    Формат ровно один: ``event: <имя>`` и ``data: <json>``, кадры через пустую
    строку. Комментарии и прочие поля SSE (``id:``, ``retry:``) игнорируются —
    наш сервер их не шлёт, но поток не должен ломаться от чужого кадра.
    """
    events: list[tuple[str, dict]] = []
    name = ""
    data_lines: list[str] = []

    def flush() -> None:
        nonlocal name, data_lines
        if not name and not data_lines:
            return
        payload = "\n".join(data_lines).strip()
        parsed: dict = {}
        if payload:
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                parsed = {"_raw": payload}
        events.append((name or "message", parsed))
        name, data_lines = "", []

    for line in raw.splitlines():
        if not line.strip():
            flush()
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            name = value.strip()
        elif field == "data":
            data_lines.append(value)
    flush()
    return events


def ask(base: str, question: str, role: str = "lawyer",
        as_of: str | None = None) -> tuple[int, str, list[tuple[str, dict]]]:
    body: dict = {"question": question, "role_id": role, "tone": "plain"}
    if as_of:
        body["as_of"] = as_of
    req = urllib.request.Request(
        f"{base}/api/ask",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        raw = resp.read().decode("utf-8", "replace")
        return resp.status, raw, _parse_stream(raw)


def _validate_events(label: str, events: list[tuple[str, dict]]) -> tuple[str, dict] | None:
    """Проверяет поток. Возвращает (вид, данные) терминального события."""
    names = [n for n, _ in events]

    steps = [d for n, d in events if n == "step"]
    terminals = [d for n, d in events if n in TERMINAL]
    errors = [d for n, d in events if n == "error"]

    if errors:
        bad(f"{label}: поток без error", f"ядро вернуло error: {errors[0]}")
        return None

    if not check(f"{label}: события разобраны", bool(names), f"{len(events)} кадров"):
        return None

    # 1. Ровно одно терминальное.
    if not check(f"{label}: одно терминальное событие", len(terminals) == 1,
                 f"терминальных {len(terminals)}: "
                 f"{[n for n in names if n in TERMINAL] or '—'}"):
        return None

    # 2. Поля каждого шага на месте.
    missing = []
    for i, st in enumerate(steps):
        gaps = [f for f in STEP_FIELDS if f not in st or st[f] in ("", None)]
        if gaps:
            missing.append(f"#{i + 1} нет {','.join(gaps)}")
    check(f"{label}: у каждого шага n/total/key/title/detail/ms/color",
          not missing, "; ".join(missing[:3]) or f"{len(steps)} шагов")

    # 3. Порядок шагов = STEP_ORDER (префикс: отказ обрывает каскад).
    # `clarify` и `escalate` — вне STEP_ORDER намеренно: §10 ставит шаг
    # уточнения ВМЕСТО decompose, §15 — эскалацию вместо verify. Они законны,
    # но только последним шагом; всё остальное обязано быть префиксом
    # STEP_ORDER без пропусков.
    keys = [str(st.get("key", "")) for st in steps]
    extra = [k for k in keys if k not in EXPECTED_ORDER]
    if extra:
        check(f"{label}: шаги вне STEP_ORDER только последним",
              keys[-1] in ("clarify", "escalate") and len(extra) == 1,
              f"вне списка: {extra}")
    core_keys = [k for k in keys if k in EXPECTED_ORDER]
    expected = list(EXPECTED_ORDER[:len(core_keys)])
    check(f"{label}: порядок шагов = STEP_ORDER",
          core_keys == expected,
          f"получено {' -> '.join(core_keys[:4])}…" if core_keys != expected
          else f"{len(core_keys)} шагов (+{len(extra)} вне списка)")

    # 4. Нумерация n сквозная 1..len(steps).
    nums = [st.get("n") for st in steps]
    check(f"{label}: n шагов сквозной 1..N", nums == list(range(1, len(steps) + 1)),
          f"{nums}")

    # 5. Терминальное — после шагов, done — после терминального.
    term_idx = next(i for i, (n, _) in enumerate(events) if n in TERMINAL)
    if term_idx != len(steps):
        bad(f"{label}: терминальное событие идёт после всех шагов",
            f"шагов {len(steps)}, терминальное на позиции {term_idx}")

    term_kind = names[term_idx]
    if term_kind == "clarify":
        # §10: поток закрывается без done — это не ошибка, а ожидаемое.
        check(f"{label}: clarify закрывает поток без done",
              "done" not in names, f"кадров после clarify: {len(names) - term_idx - 1}")
    else:
        tail = names[term_idx + 1:]
        check(f"{label}: после терминального ровно один done", tail == ["done"],
              f"хвост {tail}")

    return term_kind, terminals[0]


def _terminal_kind(events: list[tuple[str, dict]]) -> str:
    return next((n for n, _ in events if n in TERMINAL), "")


# ------------------------------------------------------------------ корпус


def _step_keys(events: list[tuple[str, dict]]) -> list[str]:
    return [str(d.get("key", "")) for n, d in events if n == "step"]


def _pick_corpus(n: int) -> list[str]:
    """Первые N вопросов корпуса — по одному на разные документы, если можно."""
    if not QUESTIONS.exists():
        return []
    out: list[str] = []
    seen_docs: set[str] = set()
    for line in QUESTIONS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        doc = str(row.get("expected_doc_id") or "")
        question = str(row.get("question") or "").strip()
        # Вопросы-отказы сюда не берём: у них нет документа, и отказы уже
        # проверяются отдельно (НДС, старая дата).
        if not question or not doc or row.get("refusal_expected") or doc in seen_docs:
            continue
        seen_docs.add(doc)
        out.append(question)
        if len(out) >= n:
            break
    return out


# --------------------------------------------------------------- сценарии


def run_corpus_questions(base: str, questions: list[str]) -> int:
    passed = 0
    for question in questions:
        label = f"корпус: «{question[:52]}…»" if len(question) > 52 else f"корпус: «{question}»"
        status, _raw, events = ask(base, question, as_of="2026-03-01")
        if not check(f"{label}: HTTP 200", status == 200, f"status={status}"):
            continue
        got = _validate_events(label, events)
        if got is None:
            continue
        kind, _data = got
        ok(f"{label} -> {len(_step_keys(events))} шагов, {kind}")
        passed += 1
    return passed


def run_nds(base: str) -> bool:
    """§17: НДС — не наша тема. Отказ на шаге policy, поиска нет."""
    label = "НДС"
    status, _raw, events = ask(base, "Какая ставка НДС в России?", as_of="2026-03-01")
    if not check(f"{label}: HTTP 200", status == 200, f"status={status}"):
        return False
    got = _validate_events(label, events)
    if got is None:
        return False
    kind, data = got
    keys = _step_keys(events)
    good = (
        kind == "refusal"
        and str(data.get("kind")) == "out_of_scope"
        and keys == ["accept", "policy"]
    )
    return check(f"{label}: out_of_scope на policy, без поиска", good,
                 f"{kind}/{data.get('kind')}, шаги {keys}")


def run_old_date(base: str) -> bool:
    """Дата до первой редакции — отказ с указанием, с какой даты правило есть."""
    label = "старая дата"
    status, _raw, events = ask(base, "Какой срок согласования заявки на закупку?",
                               as_of="2025-12-01")
    if not check(f"{label}: HTTP 200", status == 200, f"status={status}"):
        return False
    got = _validate_events(label, events)
    if got is None:
        return False
    kind, data = got
    keys = _step_keys(events)
    good = (
        kind == "refusal"
        and str(data.get("kind")) == "no_edition_in_force"
        and "resolve" in keys
    )
    return check(f"{label}: no_edition_in_force", good,
                 f"{kind}/{data.get('kind')}, шаги {len(keys)}")


def run_journal(base: str, state_dir: Path, before: int, expect_out_of_scope: bool) -> None:
    """Журнал: после прогона записи есть, out_of_scope попал в отказы."""
    path = state_dir / "journal.jsonl"
    rows: list[dict] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    check("журнал: записи добавились", len(rows) > before,
          f"было {before}, стало {len(rows)}")

    kinds = {str(r.get("refusal_kind")) for r in rows if r.get("refused")}
    if expect_out_of_scope:
        check("журнал: out_of_scope записан в refusal_kind", "out_of_scope" in kinds,
              f"виды отказов в журнале: {sorted(k for k in kinds if k and k != 'None')}")

    status, raw, _ = _get_json(base, "/api/analytics")
    if status != 200:
        bad("аналитика: /api/analytics отвечает", f"status={status}")
        return
    by_reason = raw.get("refusals_by_reason") or {}
    check("аналитика: refusals_by_reason содержит out_of_scope",
          "out_of_scope" in by_reason and int(by_reason.get("out_of_scope") or 0) >= 1,
          f"out_of_scope={by_reason.get('out_of_scope')}, ключи {sorted(by_reason)}")


def _get_json(base: str, path: str) -> tuple[int, dict, str]:
    try:
        with urllib.request.urlopen(f"{base}{path}", timeout=30) as resp:
            return resp.status, json.loads(resp.read()), ""
    except urllib.error.HTTPError as exc:
        return exc.code, {}, f"HTTP {exc.code}"
    except Exception as exc:  # диагностика: любой сбой чтения — не повод валить гейт
        return 0, {}, str(exc)


# -------------------------------------------------------------------- main


def run_stress(base: str, runs: int, state_dir: Path) -> bool:
    """Режим --stress: N прогонов подряд, ноль пустых content и error-кадров.

    Проверяется не «ответ хороший», а «ответ вообще пришёл». Пустой content
    на compose — блокер демо: сотрудник видит ошибку вместо нормы. Поэтому
    считаем именно это, а не форму ответа.

    Вопрос берём один и тот же намеренно: если слот троттлит, это проявится
    на повторах, а разные вопросы лишь размыли бы картину.
    """
    question = ("Какой срок согласования заявки на закупку?")
    debug_path = state_dir / "llm-debug.jsonl"
    empties = 0
    errors = 0
    answered = 0

    for i in range(1, runs + 1):
        status, _raw, events = ask(base, question, as_of="2026-03-01")
        kinds = [n for n, _ in events]
        if status != 200:
            errors += 1
            print(f"  {i:02d}/{runs} HTTP {status}")
            continue
        if "error" in kinds:
            errors += 1
            msg = next((d.get("message", "") for n, d in events if n == "error"), "")
            print(f"  {i:02d}/{runs} error: {str(msg)[:80]}")
            continue
        terminal = next((d for n, d in events if n in TERMINAL), None)
        if terminal is None:
            empties += 1
            print(f"  {i:02d}/{runs} нет терминального события")
            continue
        if terminal.get("refused") is False:
            answered += 1
        print(f"  {i:02d}/{runs} {kinds[-1]:<8} "
              f"{len(_step_keys(events))} шагов")

    check(f"stress: {runs} прогонов подряд без error-кадров", errors == 0,
          f"ошибок {errors}")
    check(f"stress: {runs} прогонов подряд без пустого терминального",
          empties == 0, f"пустых {empties}")

    # Подтверждение из журнала самого клиента модели: сколько вызовов пришло
    # пустыми. Если сюда ничего не попало, значит ответы не пустые ни разу.
    rows = []
    if debug_path.exists():
        for line in debug_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    empty_calls = sum(1 for r in rows if r.get("empty"))
    check("stress: llm-debug не содержит пустых content", empty_calls == 0,
          f"вызовов {len(rows)}, пустых {empty_calls}")
    return errors == 0 and empties == 0 and empty_calls == 0


def main() -> int:
    ap = argparse.ArgumentParser(description="E2E-гейт конвейера LEX (HTTP + SSE)")
    ap.add_argument("--no-llm", action="store_true",
                    help="без модели: compose берёт цитаты как есть")
    ap.add_argument("--keep-state", action="store_true",
                    help="оставить временный каталог прогона")
    ap.add_argument("--questions", type=int, default=5, help="сколько вопросов корпуса (5)")
    ap.add_argument("--corpus", default=None,
                    help="каталог корпуса с questions.jsonl (по умолчанию LEX_CORPUS_DIR "
                         "или sample-corpus)")
    ap.add_argument("--stress", type=int, default=0, metavar="N",
                    help="режим стабильности: N прогонов подряд, ноль пустых ответов")
    args = ap.parse_args()

    global CORPUS, QUESTIONS
    if args.corpus:
        CORPUS = Path(args.corpus).resolve()
    QUESTIONS = CORPUS / "questions.jsonl"

    has_key = load_env()
    if not has_key:
        if args.stress:
            print("SKIP: режим --stress требует живой ключ NexusLink в .env")
            return 0
        skip("живой LLM", "NEXUSLINK_KEY/NEXUS_LINK_BASE_URL не заданы в .env")
    if args.no_llm:
        os.environ["LEX_NO_LLM"] = "1"

    state_dir = Path(tempfile.mkdtemp(prefix="lex-e2e-"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    # Журнал вызовов модели — в каталог прогона, чтобы --stress его прочёл.
    # Журнал по умолчанию выключен (в нём текст вопросов сотрудников), а гейт
    # читает его в --stress — включаем явно.
    os.environ["LEX_LLM_DEBUG_PATH"] = str(state_dir / "llm-debug.jsonl")
    os.environ["LEX_LLM_DEBUG"] = "1"
    print(f"сервер: {base} · состояние: {state_dir}\n")

    proc = None
    log = None
    code = 1
    try:
        proc, log = start_server(port, state_dir)
        up, detail = wait_health(base)
        if not check("сервер поднялся, /api/health = ok", up, detail):
            if proc.poll() is not None:
                bad("uvicorn не упал при старте", f"код {proc.returncode}, см. {state_dir}/server.log")
            return 1

        ok("сервер поднялся, /api/health = ok", detail)

        if args.stress:
            print(f"режим стабильности: {args.stress} прогонов подряд\n")
            stable = run_stress(base, args.stress, state_dir)
            print()
            if FAIL:
                print(f"{PASS.__len__()} passed, {len(FAIL)} failed")
                print("провал: " + "; ".join(FAIL[:6]))
                code = 1
            else:
                print(f"{PASS.__len__()} passed, 0 failed — "
                      f"{args.stress} прогонов стабильны")
                code = 0
            return code if stable else 1

        questions = _pick_corpus(args.questions)
        if not questions:
            bad("вопросы корпуса загружены", f"нет {QUESTIONS} (CORPUS={CORPUS})")
            return 1

        corpus_ok = run_corpus_questions(base, questions)
        nds_ok = run_nds(base)
        old_ok = run_old_date(base)
        run_journal(base, state_dir, before=0, expect_out_of_scope=nds_ok)

        if FAIL:
            print(f"\n{PASS.__len__()} passed, {len(FAIL)} failed")
            print("провал: " + "; ".join(FAIL[:6]))
            code = 1
        else:
            print(f"\n{PASS.__len__()} passed, 0 failed")
            print(f"вопросов корпуса: {corpus_ok}/{len(questions)} · "
                  f"НДС {'ok' if nds_ok else 'fail'} · "
                  f"старая дата {'ok' if old_ok else 'fail'}")
            code = 0
    finally:
        if proc is not None:
            stop_server(proc, log)
        if not args.keep_state:
            _cleanup(state_dir)
        else:
            print(f"(состояние сохранено: {state_dir})")
    return code


def _cleanup(state_dir: Path) -> None:
    """Удаляет временный каталог. На Windows uvicorn может держать файлы ещё
    мгновение после terminate — это не повод валить гейт."""
    import shutil

    for attempt in range(5):
        try:
            shutil.rmtree(state_dir)
            return
        except OSError:
            time.sleep(0.3 * (attempt + 1))


if __name__ == "__main__":
    raise SystemExit(main())
