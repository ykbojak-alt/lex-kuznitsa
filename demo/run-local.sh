#!/usr/bin/env sh
# run-local — локальный прогон подготовительной обвязки ИИкузницы.
#
# Три шага:
#   1. проверка окружения (scripts/preflight.sh) — без неё дальше не идём;
#   2. генерация синтетического корпуса (demo/gen-sample-corpus.py);
#   3. демонстрация поиска на 5 вопросах: индексация → поиск → сверка
#      с ожидаемым документом.
#
# ВАЖНО: реального инференса модели здесь нет и до старта спринта не будет.
# Шаг 3 проверяет конвейер и качество ЛЕКСИЧЕСКОГО поиска (recall@1/recall@3),
# а не качество ответов LLM. Ответы модели появится после появления стенда;
# методика оценки — docs/EVAL-HARNESS.md.
#
# Использование:
#   sh demo/run-local.sh
#   sh demo/run-local.sh --questions 8
#   sh demo/run-local.sh --skip-preflight --keep
#
# Коды возврата: 0 — шаги выполнены, 1 — проверка окружения или генерация
# корпуса не прошла.

set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
QUESTIONS=5
COUNT=20
SEED=20261126
CORPUS_DIR="$ROOT/sample-corpus"
SKIP_PREFLIGHT=0
KEEP_CORPUS=0
RUN_OUT="${TMPDIR:-/tmp}/iikuznitsa-eval"

while [ $# -gt 0 ]; do
  case "$1" in
    --questions) shift; QUESTIONS=${1:-5} ;;
    --count) shift; COUNT=${1:-20} ;;
    --seed) shift; SEED=${1:-20261126} ;;
    --out) shift; CORPUS_DIR=${1:-$CORPUS_DIR} ;;
    --run-out) shift; RUN_OUT=${1:-$RUN_OUT} ;;
    --skip-preflight) SKIP_PREFLIGHT=1 ;;
    --keep) KEEP_CORPUS=1 ;;
    -h|--help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "run-local: неизвестный аргумент: $1" >&2; exit 2 ;;
  esac
  shift
done

step() { printf '\n=== %s ===\n' "$1"; }

PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  echo "run-local: не найден Python — сначала прогоните scripts/preflight.sh" >&2
  exit 1
fi

# --- Шаг 1: окружение ---------------------------------------------------------
if [ "$SKIP_PREFLIGHT" = 1 ]; then
  step "Шаг 1/3: проверка окружения — пропущена (--skip-preflight)"
else
  step "Шаг 1/3: проверка окружения"
  if ! sh "$ROOT/scripts/preflight.sh"; then
    echo "run-local: проверка окружения не пройдена (есть FAIL). Исправьте и повторите." >&2
    exit 1
  fi
fi

# --- Шаг 2: корпус ------------------------------------------------------------
step "Шаг 2/3: генерация корпуса"
if [ "$KEEP_CORPUS" = 1 ]; then
  echo "используем существующий корпус: $CORPUS_DIR"
else
  if ! "$PY" "$ROOT/demo/gen-sample-corpus.py" --count "$COUNT" --seed "$SEED" --out "$CORPUS_DIR" --clean; then
    echo "run-local: генерация корпуса не удалась" >&2
    exit 1
  fi
fi

# --- Шаг 3: демо поиска на 5 вопросах ----------------------------------------
step "Шаг 3/3: демо поиска (без инференса модели)"
mkdir -p "$RUN_OUT"

"$PY" - "$CORPUS_DIR" "$RUN_OUT" "$QUESTIONS" <<'PYEOF'
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

corpus_dir, run_out, n_questions = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])

STOP = {
    "какой", "какая", "какие", "что", "как", "для", "по", "на", "и", "в", "из", "к",
    "с", "не", "нет", "или", "тот", "это", "при", "за", "до", "по", "у", "же",
    "документ", "документа", "правило", "правила", "значение", "значения",
}


def tokenize(text: str) -> list[str]:
    raw = re.findall(r"[0-9A-Za-zА-Яа-яЁё]+", text.lower())
    return [t for t in raw if len(t) > 2 and t not in STOP]


docs = []
for path in sorted(corpus_dir.glob("*.md")):
    if path.name == "INDEX.md":
        continue
    text = path.read_text(encoding="utf-8")
    docs.append({"doc_id": path.name.split("-")[0] + "-" + path.name.split("-")[1],
                 "file": path.name, "tokens": set(tokenize(text))})
manifest = json.loads((corpus_dir / "manifest.json").read_text(encoding="utf-8"))
by_file = {d["file"]: d["doc_id"] for d in manifest["documents"]}
for d in docs:
    d["doc_id"] = by_file.get(d["file"], d["file"])

df = Counter()
for d in docs:
    df.update(d["tokens"])
n_docs = max(len(docs), 1)


def idf(token: str) -> float:
    return math.log(1 + n_docs / (1 + df.get(token, 0)))


questions = [json.loads(line) for line in (corpus_dir / "questions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]

# Смешанная выборка: обычные вопросы + вопросы по редакциям правил (включая отказ).
generic = [q for q in questions if q.get("kind") == "generic"]
rule_qs = [q for q in questions if q.get("kind") == "rule_asof"]
refusal_qs = [q for q in rule_qs if q.get("refusal_expected")]
picked = []
if generic:
    picked.append(generic[0])
if generic:
    picked.append(generic[len(generic) // 2])
for q in refusal_qs[:1] + rule_qs[:2]:
    if q not in picked:
        picked.append(q)
picked = picked[:n_questions]
if not picked:
    picked = questions[:n_questions]

results = []
hit1 = hit3 = 0
print("Инференс модели не выполняется: проверяется только конвейер (индексация → поиск → сверка).")
print()
for i, q in enumerate(picked, start=1):
    qt = set(tokenize(q["question"]))
    scored = []
    for d in docs:
        overlap = qt & d["tokens"]
        score = sum(idf(t) for t in overlap)
        scored.append((score, d["doc_id"], d["file"], sorted(overlap)[:6]))
    scored.sort(key=lambda x: (-x[0], x[1]))
    top = scored[:3]
    ranks = [rank for rank, (s, doc_id, f, ov) in enumerate(scored, start=1) if doc_id == q.get("expected_doc_id")]
    rank = ranks[0] if ranks else None
    if rank == 1:
        hit1 += 1
    if rank is not None and rank <= 3:
        hit3 += 1
    expected = q.get("expected_doc_id") or "(ожидается отказ)"
    print(f"[{i}] {q['question']}")
    print(f"    тип: {q.get('kind')} | ожидаемый документ: {expected}")
    for s, doc_id, f, ov in top:
        mark = "<- ожидаемый" if doc_id == q.get("expected_doc_id") else ""
        print(f"    {s:6.2f}  {doc_id}  {f} {mark}")
    print(f"    позиция ожидаемого: {rank if rank else 'не найден'}")
    print()
    results.append({
        "question": q["question"],
        "kind": q.get("kind"),
        "expected_doc_id": q.get("expected_doc_id"),
        "retrieved": [doc_id for _, doc_id, _, _ in top],
        "rank_of_expected": rank,
        "refusal_expected": q.get("refusal_expected", False),
    })

total = len(picked)
summary = {
    "questions": total,
    "recall@1": round(hit1 / total, 3) if total else 0.0,
    "recall@3": round(hit3 / total, 3) if total else 0.0,
    "note": "лексический поиск без LLM; качество ответов не измерялось",
}
(run_out / "stub-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
(run_out / "stub-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print("Итог лексического поиска:", json.dumps(summary, ensure_ascii=False))
print(f"Артефакты прогона: {run_out}")
print("Полная методика оценки (recall, faithfulness, refusal accuracy): docs/EVAL-HARNESS.md")
PYEOF

step "Готово"
echo "Корпус: $CORPUS_DIR"
echo "Следующий шаг на старте спринта: подключить стенд и прогнать 92 вопроса по docs/EVAL-HARNESS.md"
