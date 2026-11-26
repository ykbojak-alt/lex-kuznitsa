#!/usr/bin/env sh
# preflight — проверка окружения перед стартом спринта ИИкузницы.
#
# Проверяет то, что обычно ломает старт: интерпретатор и версия Python,
# наличие зависимостей, доступность модели (по переменным окружения, без
# печати секретов), свободное место на диске и состояние рабочей копии.
#
# Использование:
#   sh scripts/preflight.sh              # базовая проверка
#   sh scripts/preflight.sh --strict     # WARN тоже считается ошибкой
#   sh scripts/preflight.sh --probe-model # реально дёрнуть /models у провайдера
#   sh scripts/preflight.sh --json       # машинный вывод
#
# Коды возврата: 0 — всё в порядке (или только WARN без --strict),
#                1 — есть FAIL, 2 — неверные аргументы.
#
# Секреты никогда не печатаются: выводится только факт наличия переменной.
# Файл создан на этапе подготовки к спринту; продуктовый код не трогает.

set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
MIN_DISK_GB=${MIN_DISK_GB:-10}
MIN_PYTHON=${MIN_PYTHON:-3.11}
STRICT=0
PROBE_MODEL=0
AS_JSON=0

while [ $# -gt 0 ]; do
  case "$1" in
    --strict) STRICT=1 ;;
    --probe-model) PROBE_MODEL=1 ;;
    --json) AS_JSON=1 ;;
    --min-disk) shift; MIN_DISK_GB=${1:-10} ;;
    --min-python) shift; MIN_PYTHON=${1:-3.11} ;;
    -h|--help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "preflight: неизвестный аргумент: $1" >&2; exit 2 ;;
  esac
  shift
done

PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0
ROWS=""

# POSIX sh не экранирует \n в двойных кавычках, поэтому перевод строки
# добавляется реальным символом новой строки в конце присваивания.
emit() {
  ROWS="${ROWS}${1}|${2}|${3}
"
}

ok()   { PASS_COUNT=$((PASS_COUNT + 1)); emit "PASS" "$1" "$2"; }
warn() { WARN_COUNT=$((WARN_COUNT + 1)); emit "WARN" "$1" "$2"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); emit "FAIL" "$1" "$2"; }

# --- 1. Интерпретатор Python -------------------------------------------------
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 9) else 1)' >/dev/null 2>&1; then
      PY="$cand"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  fail "python" "интерпретатор Python 3.9+ не найден в PATH"
else
  PY_VERSION=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "?")
  if "$PY" -c "import sys; sys.exit(0 if sys.version_info[:2] >= tuple(int(x) for x in '${MIN_PYTHON}'.split('.')) else 1)" >/dev/null 2>&1; then
    ok "python" "$PY $PY_VERSION (>= $MIN_PYTHON)"
  else
    fail "python" "$PY $PY_VERSION ниже требуемой версии $MIN_PYTHON"
  fi
fi

# --- 2. pip и окружение -------------------------------------------------------
if [ -n "$PY" ] && "$PY" -m pip --version >/dev/null 2>&1; then
  PIP_VERSION=$("$PY" -m pip --version 2>/dev/null | awk '{print $2}')
  ok "pip" "$PIP_VERSION"
else
  warn "pip" "pip недоступен — зависимости нельзя поставить (python -m ensurepip)"
fi

if [ -d "$ROOT/.venv" ]; then
  ok "venv" ".venv присутствует"
else
  warn "venv" "нет каталога .venv — работа пойдёт в системном окружении"
fi

# --- 3. Зависимости проекта --------------------------------------------------
if [ -f "$ROOT/requirements.txt" ]; then
  REQ_COUNT=$(grep -c '[^[:space:]]' "$ROOT/requirements.txt" 2>/dev/null || echo 0)
  if [ -n "$PY" ] && "$PY" -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('fastapi') else 1)" >/dev/null 2>&1; then
    ok "requirements" "requirements.txt есть ($REQ_COUNT строк), fastapi импортируется"
  else
    warn "requirements" "requirements.txt есть ($REQ_COUNT строк), но ключевые пакеты не импортируются"
  fi
else
  warn "requirements" "нет requirements.txt — состав зависимостей ещё не зафиксирован"
fi

# --- 4. Модель ---------------------------------------------------------------
# MODEL_KEY_NAME — имя переменной (его и печатаем), MODEL_KEY — значение
# (только в заголовок, никогда в вывод). Раньше они жили в одной переменной,
# и проверка «ключ задан (переменная $k)» печатала сам токен в консоль.
MODEL_KEY_NAME=""
MODEL_KEY=""
MODEL_BASE=""
MODEL_NAME=""
# Порядок = приоритет. NEXUSLINK_* идёт первым: это наш основной путь
# (sage-pl), и demo/run-local.sh читает ровно эти имена. Пока списки
# расходились, preflight ругался «ключ не задан» на рабочем окружении.
for k in NEXUSLINK_KEY MODEL_API_KEY LLM_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY; do
  eval "v=\${$k:-}"
  if [ -n "$v" ] && [ -z "$MODEL_KEY_NAME" ]; then
    MODEL_KEY_NAME="$k"
    MODEL_KEY="$v"
  fi
done
for k in NEXUS_LINK_BASE_URL MODEL_BASE_URL LLM_BASE_URL OPENAI_BASE_URL ANTHROPIC_BASE_URL; do
  eval "v=\${$k:-}"
  if [ -n "$v" ] && [ -z "$MODEL_BASE" ]; then
    eval "MODEL_BASE=\${$k}"
  fi
done
for k in NEXUS_LINK_MODEL MODEL_NAME LLM_MODEL OPENAI_MODEL; do
  eval "v=\${$k:-}"
  if [ -n "$v" ] && [ -z "$MODEL_NAME" ]; then
    eval "MODEL_NAME=\${$k}"
  fi
done

if [ -z "$MODEL_KEY_NAME" ]; then
  warn "model" "не задана переменная с ключом модели (NEXUSLINK_KEY/MODEL_API_KEY/...) — без неё RAG не поедет"
else
  ok "model" "ключ модели задан (переменная $MODEL_KEY_NAME, значение не выводится)"
fi

if [ -z "$MODEL_BASE" ]; then
  warn "model-base" "не задан адрес провайдера модели (NEXUS_LINK_BASE_URL/MODEL_BASE_URL/...)"
else
  ok "model-base" "адрес провайдера задан: $MODEL_BASE"
fi

if [ -z "$MODEL_NAME" ]; then
  warn "model-name" "не задано имя модели (NEXUS_LINK_MODEL/MODEL_NAME/...)"
else
  ok "model-name" "модель: $MODEL_NAME"
fi

if [ "$PROBE_MODEL" = 1 ]; then
  if [ -z "$MODEL_BASE" ]; then
    warn "model-probe" "проба пропущена: нет адреса провайдера"
  elif ! command -v curl >/dev/null 2>&1; then
    warn "model-probe" "проба пропущена: нет curl"
  else
    # Ключ уходит заголовком через stdin (--config -), а не в argv: иначе он
    # виден в списке процессов.
    if [ -n "$MODEL_KEY" ]; then
      CODE=$(printf 'header = "Authorization: Bearer %s"\n' "$MODEL_KEY" \
        | curl -s -o /dev/null -w '%{http_code}' --max-time 8 --config - \
          "${MODEL_BASE%/}/models" 2>/dev/null || echo "000")
    else
      CODE=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 \
        "${MODEL_BASE%/}/models" 2>/dev/null || echo "000")
    fi
    case "$CODE" in
      200) ok "model-probe" "провайдер отвечает 200 на /models (все слоты live)" ;;
      401|403) fail "model-probe" "провайдер ответил $CODE — ключ отклонён" ;;
      000) warn "model-probe" "провайдер не ответил за 8 с (сеть или адрес)" ;;
      *) warn "model-probe" "провайдер ответил $CODE" ;;
    esac
    # /models отвечает и без рабочего слота: на NexusLink 200 на /models
    # не значит, что генерация пойдёт. Дёргаем настоящий чат — 1 токен,
    # модель из окружения. Ловим ровно тот случай, который иначе всплыл бы
    # на первом шаге RAG уже в спринте.
    if [ -n "$MODEL_NAME" ] && [ "$CODE" = "200" ]; then
      CHAT_BODY=$(printf '{"model":"%s","max_tokens":1,"messages":[{"role":"user","content":"ok"}]}' "$MODEL_NAME")
      CHAT_CODE=$(printf 'header = "Authorization: Bearer %s"\nheader = "content-type: application/json"\n' "$MODEL_KEY" \
        | curl -s -o /dev/null -w '%{http_code}' --max-time 25 --config - \
          --data-binary "$CHAT_BODY" "${MODEL_BASE%/}/chat/completions" 2>/dev/null || echo "000")
      case "$CHAT_CODE" in
        200) ok "model-chat" "слот $MODEL_NAME генерирует (200 на /chat/completions)" ;;
        000) warn "model-chat" "слот $MODEL_NAME не ответил за 25 с" ;;
        *) warn "model-chat" "слот $MODEL_NAME ответил $CHAT_CODE на /chat/completions" ;;
      esac
    fi
  fi
fi

# --- 5. Диск ------------------------------------------------------------------
DISK_KB=$(df -Pk "$ROOT" 2>/dev/null | awk 'NR==2 {print $4}')
if [ -z "${DISK_KB:-}" ]; then
  warn "disk" "не удалось определить свободное место на диске"
else
  DISK_GB=$((DISK_KB / 1024 / 1024))
  if [ "$DISK_GB" -ge "$MIN_DISK_GB" ]; then
    ok "disk" "свободно ${DISK_GB} ГБ (нужно минимум ${MIN_DISK_GB} ГБ)"
  else
    fail "disk" "свободно ${DISK_GB} ГБ, нужно минимум ${MIN_DISK_GB} ГБ — освободите место до старта"
  fi
fi

# --- 6. VRAM / GPU -----------------------------------------------------------
MIN_VRAM_GB=${MIN_VRAM_GB:-8}
if command -v nvidia-smi >/dev/null 2>&1; then
  GPU_LINE=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>/dev/null | head -n 1)
  if [ -n "$GPU_LINE" ]; then
    GPU_NAME=$(printf '%s' "$GPU_LINE" | cut -d, -f1 | sed 's/^ *//;s/ *$//')
    VRAM_MB=$(printf '%s' "$GPU_LINE" | cut -d, -f2 | tr -d ' ' | sed 's/MiB//')
    case "$VRAM_MB" in
      ''|*[!0-9]*) warn "vram" "не удалось разобрать объём VRAM: $GPU_LINE" ;;
      *)
        VRAM_GB=$((VRAM_MB / 1024))
        if [ "$VRAM_GB" -ge "$MIN_VRAM_GB" ]; then
          ok "vram" "$GPU_NAME: ${VRAM_GB} ГБ VRAM (нужно минимум ${MIN_VRAM_GB} ГБ)"
        else
          warn "vram" "$GPU_NAME: ${VRAM_GB} ГБ VRAM, нужно минимум ${MIN_VRAM_GB} ГБ — локальная модель может не поместиться"
        fi
        ;;
    esac
  else
    warn "vram" "nvidia-smi есть, но не отдал данные о GPU"
  fi
elif command -v rocm-smi >/dev/null 2>&1; then
  VRAM_RAW=$(rocm-smi --showmeminfo vram --csv 2>/dev/null | head -n 2 | tail -n 1 | cut -d, -f2 | tr -d ' ')
  if [ -n "$VRAM_RAW" ]; then
    ok "vram" "AMD GPU, VRAM: $VRAM_RAW"
  else
    warn "vram" "rocm-smi есть, но не отдал объём VRAM"
  fi
else
  warn "vram" "GPU не обнаружен (нет nvidia-smi/rocm-smi) — для удалённой модели не нужно, для локальной понадобится"
fi

# --- 7. Инструменты -----------------------------------------------------------
for tool in git curl; do
  if command -v "$tool" >/dev/null 2>&1; then
    ok "tool:$tool" "установлен"
  else
    warn "tool:$tool" "не найден в PATH"
  fi
done

# --- 7. Состояние рабочей копии ---------------------------------------------
if command -v git >/dev/null 2>&1 && git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  BRANCH=$(git -C "$ROOT" branch --show-current 2>/dev/null || echo "?")
  DIRTY=$(git -C "$ROOT" status --porcelain 2>/dev/null | head -n 1)
  if [ -n "$DIRTY" ]; then
    warn "git" "ветка $BRANCH: есть незакоммиченные изменения"
  else
    ok "git" "ветка $BRANCH, рабочая копия чистая"
  fi
else
  warn "git" "каталог не под контролем git"
fi

# --- 8. Порт демо-стенда -----------------------------------------------------
PORT=${DEMO_PORT:-8874}
if [ -n "$PY" ]; then
  if "$PY" - "$PORT" <<'PYEOF' >/dev/null 2>&1
import socket, sys
s = socket.socket()
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
finally:
    s.close()
PYEOF
  then
    ok "port" "порт $PORT свободен"
  else
    warn "port" "порт $PORT занят — стенд не сможет подняться, выберите другой"
  fi
else
  warn "port" "не удалось проверить порт $PORT без Python"
fi

# --- 9. Корпус и вопросы -----------------------------------------------------
if [ -f "$ROOT/sample-corpus/manifest.json" ]; then
  if [ -f "$ROOT/sample-corpus/questions.jsonl" ]; then
    Q_COUNT=$(grep -c '[^[:space:]]' "$ROOT/sample-corpus/questions.jsonl" 2>/dev/null || echo 0)
    if [ "$Q_COUNT" -ge 1 ] 2>/dev/null; then
      ok "corpus" "sample-corpus: manifest.json + $Q_COUNT вопросов"
    else
      warn "corpus" "questions.jsonl пуст — оценку не на чем строить"
    fi
  else
    warn "corpus" "есть manifest.json, нет questions.jsonl"
  fi
else
  warn "corpus" "нет sample-corpus/manifest.json — сгенерируйте: demo/gen-sample-corpus.py"
fi

# --- Итог ---------------------------------------------------------------------
EXIT_CODE=0
if [ "$FAIL_COUNT" -gt 0 ]; then
  EXIT_CODE=1
elif [ "$STRICT" = 1 ] && [ "$WARN_COUNT" -gt 0 ]; then
  EXIT_CODE=1
fi

if [ "$AS_JSON" = 1 ]; then
  printf '{"ok": %s, "pass": %d, "warn": %d, "fail": %d, "root": "%s", "checks": [\n' \
    "$([ "$EXIT_CODE" -eq 0 ] && echo true || echo false)" \
    "$PASS_COUNT" "$WARN_COUNT" "$FAIL_COUNT" "$ROOT"
  printf '%s\n' "$ROWS" | while IFS='|' read -r level name detail; do
    [ -z "${level:-}" ] && continue
    printf '    {"level": "%s", "check": "%s", "detail": "%s"},\n' "$level" "$name" "$detail"
  done
  printf '    {"level": "SUMMARY", "check": "total", "detail": "pass=%d warn=%d fail=%d"}\n' \
    "$PASS_COUNT" "$WARN_COUNT" "$FAIL_COUNT"
  printf '  ]}\n'
else
  printf '%s\n' "$ROWS" | while IFS='|' read -r level name detail; do
    [ -z "${level:-}" ] && continue
    case "$level" in
      PASS) mark="[PASS]" ;;
      WARN) mark="[WARN]" ;;
      *)    mark="[FAIL]" ;;
    esac
    printf '%s %-16s %s\n' "$mark" "$name" "$detail"
  done
  printf '\nИтог: PASS=%d WARN=%d FAIL=%d\n' "$PASS_COUNT" "$WARN_COUNT" "$FAIL_COUNT"
  if [ "$EXIT_CODE" = 0 ]; then
    printf 'Окружение готово к старту спринта.\n'
  else
    printf 'Есть блокирующие замечания — исправьте FAIL (и WARN при --strict) до старта.\n'
  fi
fi

exit "$EXIT_CODE"
