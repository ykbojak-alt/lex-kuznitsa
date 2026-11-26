"""Самопроверка HTTP-слоя LEX по docs/API-CONTRACT.md v1.14.

Запуск:  python3 -I src/api/selfcheck.py [--port 8874] [--no-sse]
Печатает таблицу «эндпоинт → код → что вернул» и итог. Ненулевой код выхода,
если хоть один обязательный пункт не выполнен — «готово» без прогона не бывает.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8874"

OK, FAIL, SKIP = "OK", "FAIL", "SKIP"
rows: list[tuple[str, str, str]] = []


def call(method: str, path: str, body: dict | None = None,
         timeout: float = 30.0) -> tuple[int, str, dict | list | str]:
    # doc_id и статусы бывают кириллицей — urlopen не кодирует их сам
    url = BASE + urllib.parse.quote(path, safe="/?=&:%")
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            ctype = r.headers.get("Content-Type", "")
            text = raw.decode("utf-8", "replace")
            if "json" in ctype:
                try:
                    return r.status, ctype, json.loads(text)
                except Exception:
                    return r.status, ctype, text
            return r.status, ctype, text
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        try:
            return e.code, e.headers.get("Content-Type", ""), json.loads(text)
        except Exception:
            return e.code, e.headers.get("Content-Type", ""), text
    except Exception as e:
        return 0, "", f"{type(e).__name__}: {e}"


def check(name: str, method: str, path: str, body: dict | None = None,
          expect: int | tuple[int, ...] = 200, note: str = "") -> dict | list | str:
    code, _ctype, payload = call(method, path, body)
    want = (expect,) if isinstance(expect, int) else expect
    if code in want:
        rows.append((OK, f"{method} {path}", note or str(code)))
    else:
        brief = ""
        if isinstance(payload, (dict, list)):
            brief = json.dumps(payload, ensure_ascii=False)[:200]
        else:
            brief = str(payload)[:200]
        rows.append((FAIL, f"{method} {path}", f"{code} (ждали {want}) {brief}"))
    return payload


def sse(path: str, body: dict) -> list[tuple[str, dict]]:
    """Читаем поток целиком и разбираем кадры."""
    data = json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "text/event-stream"})
    events: list[tuple[str, dict]] = []
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            event = ""
            for raw in r:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    try:
                        events.append((event, json.loads(line[6:])))
                    except Exception:
                        events.append((event, {"_raw": line[6:]}))
    except Exception as e:
        events.append(("_transport_error", {"error": f"{type(e).__name__}: {e}"}))
    return events


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8874)
    ap.add_argument("--no-sse", action="store_true")
    ap.add_argument("--no-llm", action="store_true",
                    help="SSE-прогон без модели (быстро, проверяет только каркас)")
    args = ap.parse_args()
    BASE = f"http://127.0.0.1:{args.port}"

    # --- §2 базовое
    health = check("health", "GET", "/api/health", note="пробы :8081/:8082/NexusLink")
    roles = check("roles", "GET", "/api/roles")
    check("docs", "GET", "/api/docs", expect=200, note="swagger")
    check("openapi", "GET", "/api/openapi.json")

    # --- §11 направления
    lines = check("business-lines", "GET", "/api/business-lines")

    # --- §8 кейсы
    # §23: роль обязательна — без неё /api/cases отдавал весь журнал.
    check("cases list", "GET", "/api/cases?role_id=lawyer")
    check("cases без роли -> 400", "GET", "/api/cases", expect=400,
          note="§23: пустая роль — не «покажи все»")
    case = check("case create", "POST", "/api/cases", {
        "question": "Можно ли заключить контракт с единственным поставщиком до 600 тыс.?",
        "answer_in_practice": "Да, при соблюдении годового лимита по 44-ФЗ.",
        "outcome": "resolved", "author_role": "lawyer",
        "author_name": "юрист", "content_type": "text",
    })
    case_id = case.get("id") if isinstance(case, dict) else None

    # --- §13.2 документы (нужен реальный doc_id)
    doc_id = _first_doc_id()
    foreign = getattr(_first_doc_id, "foreign", None)
    if doc_id:
        check("doc html", "GET", f"/api/doc/{doc_id}?role_id=lawyer", note="HTML + оглавление")
        check("doc download", "GET", f"/api/doc/{doc_id}/download?role_id=lawyer")
        check("doc preview", "GET", f"/api/doc/{doc_id}/preview?page=1&role_id=lawyer",
              expect=(200, 404))
        # §13.2: документ вне области роли — 403, не 404
        if foreign:
            denied = call("GET", f"/api/doc/{foreign}?role_id=lawyer")
            if denied[0] == 403:
                reason = str((denied[2] or {}).get("detail", "")) if isinstance(denied[2], dict) else ""
                rows.append((OK, "doc чужого отдела -> 403",
                             f"403 с причиной: {reason[:60]}"))
            else:
                rows.append((FAIL, "doc чужого отдела -> 403",
                             f"{denied[0]} (ждали 403) — §13.2 требует 403, не 404"))
        else:
            rows.append((SKIP, "doc чужого отдела -> 403", "все документы одному отделу"))
        check("doc 404", "GET", "/api/doc/нет-такого?role_id=lawyer", expect=404)
    else:
        rows.append((FAIL, "doc html", "в корпусе нет документов"))

    # --- §13.3 формы
    forms = check("forms", "GET", "/api/forms")
    form_id = forms[0]["form_id"] if isinstance(forms, list) and forms else None
    if form_id:
        check("form detail", "GET", f"/api/forms/{form_id}")
        check("form download", "GET", f"/api/forms/{form_id}/download")
        check("form fill", "POST", f"/api/forms/{form_id}/fill",
              {"session_id": "selfcheck", "values": {}})
    else:
        rows.append((SKIP, "form detail", "форм в корпусе нет"))

    # --- §13.1 expand (detail_id живёт в памяти ядра — без SSE его нет)
    # §13.1: роль обязательна и здесь — иначе деталь чужого ответа читается
    # по одному идентификатору.
    check("expand 404", "POST", "/api/expand",
          {"detail_id": "q-нет", "section": "all", "role_id": "lawyer"},
          expect=404, note="404 без живого detail_id — ожидаемо")
    check("expand без роли -> 400", "POST", "/api/expand",
          {"detail_id": "q-нет", "section": "all"}, expect=400,
          note="§13.1: деталь принадлежит роли ответа")

    # --- §18 тикеты + §22 почта
    ticket = check("ticket create", "POST", "/api/tickets", {
        "question": "Как оформить приёмку по контракту, если поставщик просрочил на 3 дня?",
        "kind": "not_in_corpus", "priority": "normal",
        "session_id": "selfcheck-ticket-1", "role_id": "lawyer",
        "business_line": "закупки", "why_unresolved": "в корпусе нет порядка приёмки",
    })
    tid = ticket.get("id") if isinstance(ticket, dict) else None
    if isinstance(ticket, dict):
        _expect_field("ticket.channel", ticket.get("channel"), "email")
        _expect_field("ticket.email.delivery", (ticket.get("email") or {}).get("delivery"),
                      "simulated")
        _expect_field("ticket.email.to", (ticket.get("email") or {}).get("to"),
                      "quality@example.invalid")
        _expect_field("ticket.email.subject", (ticket.get("email") or {}).get("subject"),
                      "[LEX] Вопрос без ответа в базе: ")

    check("tickets list", "GET", "/api/tickets")
    check("tickets by status", "GET", "/api/tickets?status=open")
    # §18.7: out_of_scope тикет не создаёт
    check("ticket out_of_scope -> 400", "POST", "/api/tickets", {
        "question": "q", "kind": "out_of_scope", "session_id": "selfcheck-oos",
    }, expect=400, note="§18.7")
    # дедуп по session_id
    again = call("POST", "/api/tickets", {
        "question": "Как оформить приёмку по контракту, если поставщик просрочил на 3 дня?",
        "kind": "not_in_corpus", "session_id": "selfcheck-ticket-1", "role_id": "lawyer",
    })
    if again[0] == 200 and isinstance(again[2], dict) and again[2].get("id") == tid:
        rows.append((OK, "ticket dedupe по session_id", f"вернулся тот же {tid}"))
    else:
        rows.append((FAIL, "ticket dedupe по session_id", f"{again[0]} {str(again[2])[:120]}"))

    if tid:
        check("ticket get", "GET", f"/api/tickets/{tid}")
        check("ticket assign", "POST", f"/api/tickets/{tid}/assign",
              {"assignee": "quality-1"})
        # §18.3: второй человек не может взять без force
        conflict = call("POST", f"/api/tickets/{tid}/assign",
                        {"assignee": "quality-2"})
        if conflict[0] == 409:
            rows.append((OK, "ticket assign чужой -> 409", "занят другим"))
        else:
            rows.append((FAIL, "ticket assign чужой -> 409", str(conflict[0])))
        check("ticket assign force", "POST", f"/api/tickets/{tid}/assign",
              {"assignee": "quality-2", "force": True})
        # §22.3 входящие
        inbox = check("inbox list", "GET", "/api/inbox")
        if isinstance(inbox, dict):
            _expect_field("inbox.delivery", inbox.get("delivery"), "simulated")
            _expect_field("inbox.mailbox", inbox.get("mailbox"), "quality@example.invalid")
        check("inbox new", "GET", "/api/inbox?status=new")
        letter = check("inbox open", "GET", f"/api/inbox/{tid}")
        if isinstance(letter, dict):
            _expect_field("letter.body", bool(str(letter.get("body") or "").strip()), True)
            # назначение уже произошло выше — письмо в работе, не «новое»
            _expect_field("letter.inbox_status", letter.get("inbox_status"), "working")
        check("inbox bad status -> 400", "GET", "/api/inbox?status=мусор", expect=400)
        reply = check("inbox reply", "POST", f"/api/inbox/{tid}/reply",
                      {"body": "Приёмку оформить актом по форме, срок — 3 рабочих дня."})
        if isinstance(reply, dict):
            _expect_field("reply.status", reply.get("status"), "answered")
            _expect_field("reply.draft", bool(reply.get("draft")), True)
        check("inbox answered", "GET", "/api/inbox?status=answered")
        draft = check("ticket draft", "GET", f"/api/tickets/{tid}/draft")
        if isinstance(draft, dict):
            _expect_field("draft.email.delivery", (draft.get("email") or {}).get("delivery"),
                          "simulated")
        # §16.3: подтверждение без editor_role — 400
        check("confirm без editor_role -> 400", "POST", f"/api/tickets/{tid}/confirm",
              {"case": {"question": "q", "answer_in_practice": "a"}},
              expect=400, note="§16.3")
        # §16.3: ссылка на несуществующий документ — 400
        check("confirm битый doc_id -> 400", "POST", f"/api/tickets/{tid}/confirm",
              {"editor_role": "lawyer",
               "case": {"question": "q", "answer_in_practice": "a",
                        "linked_docs": ["нет-такого"]}},
              expect=400, note="§16.3")
        ok_confirm = check("ticket confirm", "POST", f"/api/tickets/{tid}/confirm",
                           {"editor_role": "lawyer", "acknowledge_warnings": True,
                            "case": {"question": "Как оформить приёмку при просрочке?",
                                     "answer_in_practice":
                                         "Акт приёмки с отметкой о просрочке, претензия.",
                                     "outcome": "resolved", "content_type": "text"}})
        if isinstance(ok_confirm, dict):
            _expect_field("confirm.in_index", bool(ok_confirm.get("in_index")), True)

    # --- §15 эскалации
    esc = check("escalate create", "POST", "/api/escalate", {
        "question": "Вопрос для самопроверки", "kind": "not_in_corpus",
        "session_id": "selfcheck-esc", "why_unresolved": "проверка",
    })
    eid = esc.get("id") if isinstance(esc, dict) else None
    check("escalate list", "GET", "/api/escalate")
    if eid:
        check("escalate get", "GET", f"/api/escalate/{eid}")
        check("escalate answer", "POST", f"/api/escalate/{eid}/answer",
              {"answer": "Ответ службы качества"})
        check("escalate draft", "GET", f"/api/escalate/{eid}/draft")
        check("escalate confirm", "POST", f"/api/escalate/{eid}/confirm",
              {"editor_role": "lawyer",
               "case": {"question": "Вопрос для самопроверки",
                        "answer_in_practice": "Ответ службы качества",
                        "outcome": "resolved", "content_type": "text"}})
    check("escalate без why -> 400", "POST", "/api/escalate",
          {"question": "q"}, expect=400, note="§15.2")

    # --- §19 предложения об изменении
    crs = check("change-requests", "GET", "/api/change-requests")
    if isinstance(crs, list) and crs:
        crid = crs[0].get("id")
        check("change-request get", "GET", f"/api/change-requests/{crid}")
        check("change-request status", "POST", f"/api/change-requests/{crid}/status",
              {"status": "accepted", "by": "selfcheck"})
        check("change-request bad status -> 400", "POST",
              f"/api/change-requests/{crid}/status", {"status": "мусор", "by": "x"},
              expect=400)
    else:
        rows.append((SKIP, "change-request get", "предложений пока нет"))

    # --- аналитика §20.5/§18.6/§19.5
    an = check("analytics", "GET", "/api/analytics")
    if isinstance(an, dict):
        for key in ("caveats", "answer_rate", "tickets", "similar_questions",
                    "knowledge_loop", "change_requests", "by_business_line"):
            _expect_field(f"analytics.{key}", key in an, True)
        cav = an.get("caveats") or {}
        for key in ("total", "by_level", "by_reason", "answer_rate"):
            _expect_field(f"analytics.caveats.{key}", key in cav, True)

    # --- §2 ingest
    check("ingest", "POST", "/api/ingest")

    # --- §3/§17/§21 SSE
    if not args.no_sse:
        _run_sse(args)

    _print()
    failed = [r for r in rows if r[0] == FAIL]
    return 1 if failed else 0


def _expect_field(name: str, got, want) -> None:
    if isinstance(want, str) and want.endswith(": "):
        ok = isinstance(got, str) and got.startswith(want)
    elif isinstance(want, str):
        ok = got == want
    else:
        ok = got == want
    if ok:
        rows.append((OK, name, str(got)[:70]))
    else:
        rows.append((FAIL, name, f"получили {str(got)[:70]!r}, ждали {want!r}"))


def _first_doc_id() -> str | None:
    """Документ, который роли `lawyer` положен, и документ чужого отдела.

    Права — часть контракта (§13.2): нельзя проверять 200 на документе, который
    роли не положен. Отдел берём из манифеста, роли — из /api/roles.
    """
    code, _, payload = call("GET", "/api/roles")
    allowed: set[str] = set()
    if code == 200 and isinstance(payload, list):
        for r in payload:
            if isinstance(r, dict) and r.get("id") == "lawyer":
                allowed = {str(d) for d in (r.get("departments") or [])}

    try:
        import os
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2]
        corpus = pathlib.Path(os.environ.get("LEX_CORPUS_DIR") or (root / "corpus-real"))
        manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
        docs = [d for d in (manifest.get("documents") or []) if isinstance(d, dict)]
    except Exception:
        return None

    mine = next((d for d in docs
                 if d.get("doc_id") and (not allowed or "*" in allowed
                                         or str(d.get("department") or "") in allowed)), None)
    foreign = next((d for d in docs
                    if d.get("doc_id") and allowed and "*" not in allowed
                    and str(d.get("department") or "") not in allowed), None)
    if mine is None:
        return None
    _first_doc_id.foreign = str(foreign["doc_id"]) if foreign else None  # type: ignore[attr-defined]
    return str(mine["doc_id"])


def _run_sse(args) -> None:
    """Прогон потока: короткий вопрос (clarify) + полный каскад с уточнениями.

    Ядро по §10 спрашивает уточнение, пока не получит ответы; поэтому
    «полный каскад» — это несколько запросов с накопленными `answers`
    в одной сессии. Транспорт обязан отдать каждый поток целиком.
    """
    question = ("Сколько экземпляров договора нужно оформить при закупке "
                "у единственного поставщика?")
    session = "selfcheck-sse"
    answers: dict = {}
    events: list[tuple[str, dict]] = []

    for _ in range(4):
        body = {"question": question, "role_id": "lawyer",
                "as_of": "2026-07-01", "tone": "plain",
                "session_id": session, "answers": answers}
        events = sse("/api/ask", body)
        kinds = [k for k, _ in events]
        if any(k in ("answer", "refusal", "escalate") for k in kinds):
            break
        clar = next((d for k, d in events if k == "clarify"), None)
        if clar is None:
            break
        questions = clar.get("questions") or []
        if not questions:
            break
        q = questions[0]
        options = q.get("options") or []
        answers[q.get("key") or "answer"] = options[0] if options else "да"

    kinds = [k for k, _ in events]
    steps = [d for k, d in events if k == "step"]

    if not steps:
        rows.append((FAIL, "SSE: шаги", f"шагов нет, события: {kinds[:8]}"))
        return
    rows.append((OK, "SSE: шаги", f"{len(steps)} шагов"))

    keys = [d.get("key") for d in steps]
    # §17.1: policy — первый СОДЕРЖАТЕЛЬНЫЙ шаг; перед ним идёт accept (§3).
    first_after_accept = next((k for k in keys if k != "accept"), None)
    if first_after_accept == "policy":
        rows.append((OK, "SSE: policy первым после accept (§17.1)",
                     f"{keys[0]} -> {first_after_accept}"))
    else:
        rows.append((FAIL, "SSE: policy первым после accept (§17.1)",
                     f"после accept: {first_after_accept}; шаги {keys}"))

    totals = {d.get("total") for d in steps}
    if len(totals) == 1:
        rows.append((OK, "SSE: total одинаков везде", f"total={totals.pop()}"))
    else:
        rows.append((FAIL, "SSE: total одинаков везде", f"разные: {totals}"))

    n = [d.get("n") for d in steps]
    if n == list(range(1, len(n) + 1)):
        rows.append((OK, "SSE: нумерация подряд", f"1..{len(n)}"))
    else:
        rows.append((FAIL, "SSE: нумерация подряд", str(n)))

    terminal = [k for k in kinds if k in ("answer", "refusal", "clarify", "escalate")]
    if len(terminal) == 1:
        rows.append((OK, "SSE: ровно один исход (§3)", terminal[0]))
    elif not terminal:
        rows.append((FAIL, "SSE: ровно один исход (§3)", f"нет исхода; события {kinds[-4:]}"))
    else:
        rows.append((FAIL, "SSE: ровно один исход (§3)", str(terminal)))

    if "clarify" in kinds:
        if "done" in kinds:
            rows.append((FAIL, "SSE: clarify закрывает без done (§10)", "done пришёл"))
        else:
            rows.append((OK, "SSE: clarify закрывает без done (§10)", "done нет — верно"))
    elif "done" in kinds:
        rows.append((OK, "SSE: done в конце", "есть"))
    else:
        rows.append((FAIL, "SSE: done в конце", f"нет; события {kinds[-4:]}"))

    if "error" in kinds:
        err = next(d for k, d in events if k == "error")
        rows.append((FAIL, "SSE: без ошибок", str(err)[:160]))

    for kind in ("answer", "escalate"):
        payload = next((d for k, d in events if k == kind), None)
        if payload is None:
            continue
        if kind == "answer":
            for key in ("short", "has_details", "citations"):
                _expect_field(f"SSE answer.{key}", key in payload, True)
            cav = payload.get("caveat")
            if cav is None:
                rows.append((SKIP, "SSE answer.caveat", "оговорки нет — допустимо (§20.6)"))
            else:
                for key in ("level", "reasons", "advice"):
                    _expect_field(f"SSE answer.caveat.{key}", key in cav, True)
                if cav.get("level") in ("low", "medium", "high"):
                    rows.append((OK, "SSE answer.caveat.level", cav["level"]))
                else:
                    rows.append((FAIL, "SSE answer.caveat.level", str(cav.get("level"))))
        else:
            _expect_field("SSE escalate.channel", payload.get("channel"), "email")
            _expect_field("SSE escalate.email.delivery",
                          (payload.get("email") or {}).get("delivery"), "simulated")
            for key in ("ticket", "email"):
                _expect_field(f"SSE escalate.{key}", key in payload, True)


def _print() -> None:
    width = max((len(r[1]) for r in rows), default=10)
    for status, name, detail in rows:
        mark = {"OK": "[ok]  ", "FAIL": "[FAIL]", "SKIP": "[skip]"}[status]
        print(f"{mark} {name.ljust(width)}  {detail}")
    ok = sum(1 for r in rows if r[0] == OK)
    fail = sum(1 for r in rows if r[0] == FAIL)
    skip = sum(1 for r in rows if r[0] == SKIP)
    print()
    print(f"ИТОГО: ok={ok} fail={fail} skip={skip} всего={len(rows)}")
    if fail:
        print("ПРОВАЛЕННЫЕ:")
        for status, name, detail in rows:
            if status == FAIL:
                print(f"  - {name}: {detail}")


if __name__ == "__main__":
    sys.exit(main())
