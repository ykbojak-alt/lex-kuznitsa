#!/usr/bin/env bash
# deploy-models-sagepl.sh — перенос моделей LEX на удалённый хост.
#
# Стенд должен быть 1:1 с разработкой: те же bge-m3 и bge-reranker-base, а не
# урезанная замена. Модели весят 3.3 ГБ и в git не ходят (бинарники, которых
# не должно быть в публичном репозитории), поэтому едут отдельным rsync.
#
# Порядок: диск -> python-стек -> модели -> сервисы -> проверка. На каждом
# шаге проверяем, что влезает: падение на середине переноса 3 ГБ дороже, чем
# отказ на старте.
#
#   ./deploy/deploy-models-sagepl.sh root@host [/opt/lex-kuznitsa]
#
# Требования на хосте: python3 >= 3.11, rsync, systemd. Модели кладутся в
# /opt/lex-models (не внутрь репозитория), чтобы деплой кода их не затирал.

set -euo pipefail

REMOTE_HOST="${1:-}"
REMOTE_DIR="${2:-/opt/lex-kuznitsa}"
MODELS_DIR="/opt/lex-models"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ -z "$REMOTE_HOST" ]]; then
    echo "использование: $0 <хост> [путь]" >&2
    exit 2
fi

LOCAL_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
EMBED_LOCAL="${EMBED_LOCAL:-D:/models/bge-m3}"
RERANK_LOCAL="${RERANK_LOCAL:-D:/models/bge-reranker-base}"

say() { printf '\n=== %s\n' "$*"; }

say "1. Диск на хосте"
# Даже после разгрузки нужно ~4.1 ГБ: модели 3.3 + python-стек 0.75.
AVAIL_KB=$(ssh "$REMOTE_HOST" "df -Pk /opt | awk 'NR==2{print \$4}'")
AVAIL_GB=$((AVAIL_KB / 1024 / 1024))
echo "доступно на /opt: ${AVAIL_GB} ГБ"
if (( AVAIL_GB < 5 )); then
    echo "ОТКАЗ: нужно минимум 5 ГБ (модели 3.3 + стек 0.75 + запас), есть ${AVAIL_GB}" >&2
    exit 3
fi

say "2. Python и venv"
ssh "$REMOTE_HOST" bash -s <<REMOTE
set -euo pipefail
$PYTHON_BIN --version
mkdir -p "$REMOTE_DIR" "$MODELS_DIR"
if [[ ! -d "$REMOTE_DIR/.venv" ]]; then
    $PYTHON_BIN -m venv "$REMOTE_DIR/.venv" || { echo "нет venv-модуля"; exit 4; }
fi
"$REMOTE_DIR/.venv/bin/pip" install -q --upgrade pip
"$REMOTE_DIR/.venv/bin/pip" install -q \
    torch --index-url https://download.pytorch.org/whl/cpu
"$REMOTE_DIR/.venv/bin/pip" install -q \
    sentence-transformers transformers "fastapi" "uvicorn[standard]" \
    "numpy" "pydantic" "httpx" "python-dotenv" "markdown-it-py" \
    "pymupdf" "pillow" "safetensors"
"$REMOTE_DIR/.venv/bin/python" -c "import torch, sentence_transformers, transformers; print('torch', torch.__version__)"
REMOTE

say "3. Модели (rsync, докачка с продолжением)"
# --partial: оборванный перенос продолжается, а не начинается заново.
ssh "$REMOTE_HOST" "mkdir -p '$MODELS_DIR'"
rsync -a --partial --info=progress2 \
    "$EMBED_LOCAL/" "$REMOTE_HOST:$MODELS_DIR/bge-m3/"
rsync -a --partial --info=progress2 \
    "$RERANK_LOCAL/" "$REMOTE_HOST:$MODELS_DIR/bge-reranker-base/"

say "4. Юниты модельных сервисов"
# Пути к моделям — параметром, а не константой внутри скрипта: на roy1 это
# D:/models, на sage-pl /opt/lex-models, и хардкод разъехался бы молча.
ssh "$REMOTE_HOST" bash -s <<REMOTE
set -euo pipefail
cat > /etc/systemd/system/lex-embed.service <<'UNIT'
[Unit]
Description=LEX embeddings bge-m3 (:8081)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$REMOTE_DIR
Environment=PYTHONUNBUFFERED=1
Environment=OMP_NUM_THREADS=2
ExecStart=$REMOTE_DIR/.venv/bin/python scripts/embed_server.py --port 8081 --model $MODELS_DIR/bge-m3
Restart=on-failure
RestartSec=5
MemoryMax=1600M

[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/lex-rerank.service <<'UNIT'
[Unit]
Description=LEX reranker bge-reranker-base (:8082)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$REMOTE_DIR
Environment=PYTHONUNBUFFERED=1
Environment=OMP_NUM_THREADS=2
ExecStart=$REMOTE_DIR/.venv/bin/python scripts/rerank_server.py --port 8082 --model $MODELS_DIR/bge-reranker-base
Restart=on-failure
RestartSec=5
MemoryMax=800M

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now lex-embed.service lex-rerank.service
REMOTE

say "5. Проверка: модели отвечают"
ssh "$REMOTE_HOST" bash -s <<'REMOTE'
set -uo pipefail
# Модель грузится секунды-десятки секунд; ждём до 3 минут, а не сразу падаем.
for i in $(seq 1 36); do
    e=$(curl -s -o /dev/null -w '%{http_code}' -m 2 http://127.0.0.1:8081/health || true)
    r=$(curl -s -o /dev/null -w '%{http_code}' -m 2 http://127.0.0.1:8082/health || true)
    [[ "$e" == "200" && "$r" == "200" ]] && break
    sleep 5
done
echo "embed :8081 -> $e, rerank :8082 -> $r"
curl -s -m 60 -X POST http://127.0.0.1:8081/v1/embeddings \
    -H 'Content-Type: application/json' \
    -d '{"model":"bge-m3","input":["проверка"]}' | head -c 120; echo
free -m | grep -E 'Mem|Swap'
REMOTE

say "готово"
echo "дальше: ./deploy/deploy-sagepl.sh $REMOTE_HOST $REMOTE_DIR"
echo "в .env на хосте: LEX_EMBED_URL=http://127.0.0.1:8081"
echo "                 LEX_RERANK_URL=http://127.0.0.1:8082"
