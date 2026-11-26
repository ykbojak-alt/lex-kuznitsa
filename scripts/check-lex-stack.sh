#!/usr/bin/env bash
# check-lex-stack.sh — проверка GO/NO-GO №8 ровно так, как повторит ALICE.
#
#   sh scripts/check-lex-stack.sh
#
# Проверяет: /health обоих серверов -> 200; POST /v1/embeddings с русской фразой
# -> вектор нужной длины; POST /rerank -> осмысленные оценки двух документов.
# Exit 0 = GO, 1 = NO-GO (печатает, что именно не так).
#
# Тела запросов пишутся в UTF-8-файлы и шлются через --data-binary: в Git Bash на
# Windows кириллица в inline-аргументе `curl -d` разрушается (83 байта вместо 129),
# сервер получает битый JSON. С файлом кодировка сохраняется.
set -u

EMBED=${LEX_EMBED_URL:-http://127.0.0.1:8081}
RERANK=${LEX_RERANK_URL:-http://127.0.0.1:8082}
fail=0
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

say() { printf '%s\n' "$*"; }
ok()  { printf '  OK   %s\n' "$*"; }
bad() { printf '  FAIL %s\n' "$*"; fail=1; }
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$1"; }

say "== health =="
for pair in "embed:$EMBED" "rerank:$RERANK"; do
  name=${pair%%:*}; base=${pair#*:}
  c=$(code "$base/health")
  if [ "$c" = "200" ]; then ok "$name /health -> 200  $(curl -s --max-time 5 "$base/health")"
  else bad "$name /health -> $c (ожидали 200)"; fi
done

say "== embeddings =="
cat > "$tmp/emb.json" <<'JSON'
{"model":"bge-m3","input":"Договор поставки вступает в силу с момента подписания."}
JSON
emb_json=$(curl -s --max-time 120 -X POST "$EMBED/v1/embeddings" \
  -H 'Content-Type: application/json' --data-binary @"$tmp/emb.json")
say "$(printf '%s' "$emb_json" | cut -c1-120)…"
dim=$(printf '%s' "$emb_json" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(len(d["data"][0]["embedding"]))' 2>/dev/null)
if [ -n "${dim:-}" ] && [ "$dim" -gt 0 ] 2>/dev/null; then
  ok "вектор получен, размерность $dim"
  printf '%s' "$emb_json" | python3 -c '
import sys,json,math
d=json.load(sys.stdin); v=d["data"][0]["embedding"]
n=math.sqrt(sum(x*x for x in v))
print("  OK   норма вектора %.4f, первые 3 значения: %s" % (n, [round(x,4) for x in v[:3]]))'
else
  bad "вектор не получен / пустой ответ"
fi

say "== rerank =="
cat > "$tmp/rr.json" <<'JSON'
{"query":"срок действия договора поставки","documents":["Договор поставки действует до 31 декабря 2026 года.","Курс доллара вырос на два процента за неделю."]}
JSON
rr_json=$(curl -s --max-time 120 -X POST "$RERANK/rerank" \
  -H 'Content-Type: application/json' --data-binary @"$tmp/rr.json")
say "$(printf '%s' "$rr_json" | cut -c1-160)…"
printf '%s' "$rr_json" | python3 -c '
import sys,json
d=json.load(sys.stdin); r=d.get("results") or []
if len(r)<2: raise SystemExit("мало результатов: %d"%len(r))
top,bottom=r[0],r[1]
print("  top1 index=%d score=%.4f  %r" % (top["index"], top["score"], top.get("document","")[:50]))
print("  top2 index=%d score=%.4f  %r" % (bottom["index"], bottom["score"], bottom.get("document","")[:50]))
if top["index"]==0 and top["score"]>bottom["score"]:
    print("  OK   релевантный документ (index 0) выше нерелевантного")
else:
    print("  FAIL порядок/оценки неверны"); raise SystemExit(1)
' || fail=1

say ""
if [ "$fail" -eq 0 ]; then say "ИТОГ: GO — embedding :8081 и reranker :8082 работают"; exit 0
else say "ИТОГ: NO-GO — см. FAIL выше"; exit 1; fi
