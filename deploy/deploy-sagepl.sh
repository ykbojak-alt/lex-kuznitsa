#!/usr/bin/env bash
# deploy-sagepl.sh — выкладка стенда LEX на удалённый хост.
#
# Копируется только код. Данные заказчика (dataset/, corpus-real/), секреты
# (.env) и рабочие файлы (var/, .git/) остаются на месте: корпус — данные
# третьей стороны, он не уходит на чужую машину ни в какую сторону.
#
# Использование:
#   ./deploy/deploy-sagepl.sh <хост> [путь]
#   ./deploy/deploy-sagepl.sh root@2.27.5.234.nip.io /opt/lex-kuznitsa
#
# Требования на удалённом хосте: python3 >= 3.11, systemd, rsync. uv
# желателен — без него venv создаётся через python3 -m venv.
#
# Секретов в этом файле нет и быть не может: ключи приходят на стенд только
# через .env, который создаётся на месте и не копируется.

set -euo pipefail

REMOTE_HOST="${1:-}"
REMOTE_DIR="${2:-/opt/lex-kuznitsa}"

UNIT_NAME="lex-stand.service"
UNIT_SRC="deploy/lex-stand.service"
LOCAL_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
HEALTH_URL="http://127.0.0.1:8090/api/health"

# Зависимости стенда. pymupdf, pillow, pytesseract, markdown-it-py и httpx
# импортируются лениво, но без pymupdf не открываются PDF, а без httpx не
# работает проба модели — ставим весь набор сразу, кроме pytesseract (нужен
# ещё и системный бинарник tesseract, его ставит хостер).
DEPS=(
  "fastapi"
  "uvicorn"
  "pydantic"
  "numpy"
  "httpx"
  "python-dotenv"
  "markdown-it-py"
  "pymupdf"
  "pillow"
)

usage() {
  echo "использование: $0 <хост> [путь-на-хосте]" >&2
  echo "пример:        $0 root@2.27.5.234.nip.io /opt/lex-kuznitsa" >&2
  exit 2
}

die() {
  echo "ОШИБКА: $*" >&2
  exit 1
}

step() {
  printf '\n== %s\n' "$*"
}

if [ -z "$REMOTE_HOST" ]; then
  usage
fi
if [ "$REMOTE_HOST" = "-h" ] || [ "$REMOTE_HOST" = "--help" ]; then
  usage
fi

cd -- "$LOCAL_ROOT"
[ -f "$UNIT_SRC" ] || die "не найден $UNIT_SRC — запускать из репозитория lex-kuznitsa"
[ -f pyproject.toml ] || die "не найден pyproject.toml — запускать из корня репозитория"

# ---------------------------------------------------------------- копирование
#
# Исключения — первым делом. Каждое с причиной:
#   dataset/, corpus-real/, sample-corpus-real/ — документы заказчика;
#   .env                       — секреты, на стенде свой;
#   var/                       — рабочие записи стенда, не переносим;
#   .git/                      — история нужна локально, не на стенде;
#   .venv/, __pycache__, .ruff_cache — пересобираются на месте.
step "rsync кода на $REMOTE_HOST:$REMOTE_DIR"
ssh "$REMOTE_HOST" "mkdir -p '$REMOTE_DIR'"
rsync -az --delete \
  --exclude 'dataset/' \
  --exclude 'corpus-real/' \
  --exclude 'sample-corpus-real/' \
  --exclude '.env' \
  --exclude 'var/' \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '.ruff_cache/' \
  --exclude '*.pyc' \
  ./ "$REMOTE_HOST:$REMOTE_DIR/"

# ------------------------------------------------------------------- окружение
step "виртуальное окружение и зависимости"
# Один ssh-скрипт, а не пять: среда (uv или venv) на удалённой стороне
# выбирается там, где мы точно знаем, что она есть.
# shellcheck disable=SC2087
ssh "$REMOTE_HOST" bash -s -- "$REMOTE_DIR" "${DEPS[@]}" <<'REMOTE_SETUP'
set -euo pipefail
DIR="$1"
shift
DEPS=("$@")

cd -- "$DIR"
PY="${DIR}/.venv/bin/python"

if [ ! -x "$PY" ]; then
  if command -v uv >/dev/null 2>&1; then
    echo "venv: uv"
    uv venv --python python3 "${DIR}/.venv"
  else
    echo "venv: python3 -m venv (uv не найден)"
    python3 -m venv "${DIR}/.venv"
  fi
fi

if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$PY" "${DEPS[@]}"
else
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install "${DEPS[@]}"
fi

"$PY" -c 'import fastapi, uvicorn, numpy, pydantic, httpx, dotenv; print("зависимости ок")'
REMOTE_SETUP

# ----------------------------------------------------------------------- юнит
step "systemd: установка $UNIT_NAME"
scp "$UNIT_SRC" "$REMOTE_HOST:/etc/systemd/system/$UNIT_NAME"
# shellcheck disable=SC2086
ssh "$REMOTE_HOST" "set -e
  systemctl daemon-reload
  systemctl enable --now $UNIT_NAME
  systemctl --no-pager --lines=0 status $UNIT_NAME || true"

# ------------------------------------------------------------------ проверка
# .env копией не приезжает — без него сервер не стартует (config.py читает
# ключ NexusLink). Предупреждаем до, а не после падения.
step "проверка .env на стенде"
if ssh "$REMOTE_HOST" "test -f '$REMOTE_DIR/.env'"; then
  echo ".env на месте"
else
  echo "ВНИМАНИЕ: нет $REMOTE_DIR/.env — сервер поднимется, но модель будет недоступна." >&2
  echo "          создать из .env.example и вписать ключ на самой машине, затем перезапустить юнит." >&2
fi

step "проверка здоровья: $HEALTH_URL"
# Даём процессу подняться: uvicorn стартует за доли секунды, но загрузка
# корпуса и индекса идёт при первом запросе.
ATTEMPTS=20
for i in $(seq 1 "$ATTEMPTS"); do
  if BODY=$(ssh "$REMOTE_HOST" "curl -sf --max-time 5 '$HEALTH_URL'"); then
    echo "health: $BODY"
    echo
    # Ключевая строка деплоя: работает полный гибрид или только BM25. Поле —
    # embed.reachable из src/api/probes.py. Без векторов стенд ЖИВОЙ (поиск на
    # BM25, ответы с оговоркой no_similarity), но это надо видеть сразу, а не
    # выяснять на демо. Разбор — на удалённой стороне: там точно есть python3.
    ssh "$REMOTE_HOST" bash -s -- "$HEALTH_URL" <<'REMOTE_MODE' || true
set -u
URL="$1"
JSON=$(curl -sf --max-time 5 "$URL") || exit 0
printf '%s' "$JSON" | python3 -c '
import json, sys
h = json.load(sys.stdin)
e = h.get("embed") or {}
r = h.get("rerank") or {}
hybrid = bool(e.get("reachable"))
print("режим поиска: " + ("полный гибрид" if hybrid else "BM25 (без векторов)")
      + " · embed.reachable=" + str(hybrid).lower()
      + " · rerank.reachable=" + str(bool(r.get("reachable"))).lower())
if not hybrid:
    print("  штатный режим стенда: векторного сервиса нет.")
    print("  ответы приходят с оговоркой no_similarity; шаги retrieve и verify")
    print("  помечают, что близость не проверялась.")
'
REMOTE_MODE
    echo
    echo "стенд поднят: $REMOTE_HOST:$REMOTE_DIR, порт 8090 (локально на хосте)"
    exit 0
  fi
  sleep 1
done

echo "health не ответил за ${ATTEMPTS} с. Что смотреть:" >&2
echo "  ssh $REMOTE_HOST 'journalctl -u $UNIT_NAME -n 50 --no-pager'" >&2
echo "  ssh $REMOTE_HOST 'test -f $REMOTE_DIR/.env || echo \"нет .env на стенде\"'" >&2
exit 1
