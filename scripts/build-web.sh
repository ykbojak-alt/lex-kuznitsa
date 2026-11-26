#!/usr/bin/env sh
# build-web — собрать UI в src/api/static/.
#
# Фронтенд (§1 контракта) — классические скрипты без сборщика: index.html
# подключает lex.css, mock-lex.js, progress-view.js, console-view.js, app.js
# через <script src>. Поэтому «сборка» — это копирование src/web -> src/api/static.
#
# Запуск из корня репозитория:  sh scripts/build-web.sh
# Сервер после этого отдаёт UI на / (см. src/api/app.py).

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SRC="$ROOT/src/web"
DST="$ROOT/src/api/static"

if [ ! -f "$SRC/index.html" ]; then
    echo "нет $SRC/index.html — фронтенд не найден" >&2
    exit 1
fi

rm -rf "$DST"
mkdir -p "$DST"
# -R: каталоги, -p: права; точка в конце — копируем содержимое, не саму папку.
cp -R "$SRC"/. "$DST"/

test -f "$DST/index.html" || { echo "index.html не скопирован" >&2; exit 1; }
n=$(find "$DST" -type f | wc -l | tr -d ' ')
echo "UI собран: $DST ($n файлов)"
