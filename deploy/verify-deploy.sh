#!/usr/bin/env bash
# Compare MD5 and mtime manifests for Python/JavaScript under src/ and scripts/.
set -euo pipefail

REMOTE_HOST="${1:-${LEX_REMOTE_HOST:-root@2.27.5.234}}"
REMOTE_DIR="${2:-${LEX_REMOTE_DIR:-/opt/lex-kuznitsa}}"
SOURCE_ROOT="${3:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"
DEFAULT_KEY="$HOME/.ssh/id_ed25519_ai2_sage_diagnostic"
if [ ! -r "$DEFAULT_KEY" ] && [ -r /mnt/c/Users/BoJaK/.ssh/id_ed25519_ai2_sage_diagnostic ]; then
  DEFAULT_KEY=/mnt/c/Users/BoJaK/.ssh/id_ed25519_ai2_sage_diagnostic
fi
SSH_KEY="${LEX_SSH_KEY:-$DEFAULT_KEY}"
DEFAULT_KNOWN_HOSTS="$HOME/.ssh/known_hosts"
if [ ! -r "$DEFAULT_KNOWN_HOSTS" ] && [ -r /mnt/c/Users/BoJaK/.ssh/known_hosts ]; then
  DEFAULT_KNOWN_HOSTS=/mnt/c/Users/BoJaK/.ssh/known_hosts
fi
SSH_KNOWN_HOSTS="${LEX_SSH_KNOWN_HOSTS:-$DEFAULT_KNOWN_HOSTS}"
command -v ssh >/dev/null || { echo 'ERROR: ssh is required' >&2; exit 2; }
command -v find >/dev/null && command -v md5sum >/dev/null && command -v sort >/dev/null \
  && command -v stat >/dev/null && command -v python3 >/dev/null \
  || { echo 'ERROR: find, md5sum, sort, stat, and python3 are required' >&2; exit 2; }
[ -r "$SSH_KEY" ] || { echo "ERROR: SSH key not readable: $SSH_KEY" >&2; exit 2; }
[ -r "$SSH_KNOWN_HOSTS" ] || { echo "ERROR: SSH known_hosts not readable: $SSH_KNOWN_HOSTS" >&2; exit 2; }
[ -d "$SOURCE_ROOT/src" ] && [ -d "$SOURCE_ROOT/scripts" ] \
  || { echo "ERROR: expected src/ and scripts/ under $SOURCE_ROOT" >&2; exit 2; }

TMP="$(mktemp -d)"
KEY_COPY=""
cleanup() {
  rm -rf -- "$TMP"
  [ -z "$KEY_COPY" ] || rm -f -- "$KEY_COPY"
}
trap cleanup EXIT
if [ -n "${WSL_DISTRO_NAME:-}" ] && [[ "$SSH_KEY" == /mnt/* ]]; then
  KEY_COPY="$(mktemp)"
  chmod 600 "$KEY_COPY"
  cat "$SSH_KEY" >"$KEY_COPY"
  SSH_KEY="$KEY_COPY"
fi

manifest() {
  local root="$1"
  (
    cd "$root"
    # Исключение ровно то же, что у rsync в sync-and-restart.sh: '/api/static/'
    # — собранная копия src/web, в git её нет, и деплой её не передаёт.
    # Без исключения сравнение берёт объединение ключей, и на чистом клоне
    # (до build-web.sh) четыре файла static оказываются server-only —
    # скрипт вечно отвечает MISMATCH при одинаковом коде. Ответ зависел бы
    # от того, собирали ли локально, то есть от источника, а не от сути
    # (нашла FORGE).
    find src scripts -type f \( -name '*.py' -o -name '*.js' \) \
      -not -path 'src/api/static/*' -print0 \
      | LC_ALL=C sort -z \
      | while IFS= read -r -d '' path; do
          digest="$(md5sum <"$path" | cut -d ' ' -f1)"
          mtime="$(stat -c '%Y' -- "$path")"
          printf '%s\t%s\t%s\n' "$path" "$digest" "$mtime"
        done
  ) | LC_ALL=C sort -t "$(printf '\t')" -k1,1
}

manifest "$SOURCE_ROOT" >"$TMP/local.tsv"

ssh -i "$SSH_KEY" -o UserKnownHostsFile="$SSH_KNOWN_HOSTS" -c aes128-gcm@openssh.com -o BatchMode=yes -o ConnectTimeout=10 \
  "$REMOTE_HOST" bash -s -- "$REMOTE_DIR" >"$TMP/remote.tsv" <<'REMOTE_MANIFEST'
set -euo pipefail
ROOT="$1"
cd "$ROOT"
# То же исключение, что локально и у rsync: '/api/static/' на стенде — тоже
# собранная копия, и её не передают. Без него сравнение видит четыре
# server-only файла на любой машине, где статику собирали (нашла FORGE).
find src scripts -type f \( -name '*.py' -o -name '*.js' \) \
  -not -path 'src/api/static/*' -print0 \
  | LC_ALL=C sort -z \
  | while IFS= read -r -d '' path; do
      digest="$(md5sum <"$path" | cut -d ' ' -f1)"
      mtime="$(stat -c '%Y' -- "$path")"
      printf '%s\t%s\t%s\n' "$path" "$digest" "$mtime"
    done \
  | LC_ALL=C sort -t "$(printf '\t')" -k1,1
REMOTE_MANIFEST

local_count="$(wc -l <"$TMP/local.tsv" | tr -d ' ')"
remote_count="$(wc -l <"$TMP/remote.tsv" | tr -d ' ')"
printf 'Python/JS files: local=%s remote=%s\n' "$local_count" "$remote_count"

python3 - "$TMP/local.tsv" "$TMP/remote.tsv" <<'PY'
from datetime import datetime, timezone
from pathlib import Path
import sys

def load(path):
    result = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        name, digest, mtime = line.split("\t")
        result[name] = (digest, int(mtime))
    return result

local, remote = map(load, sys.argv[1:])
paths = sorted(local.keys() | remote.keys())
diffs = []
touched = 0
def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")

for name in paths:
    left, right = local.get(name), remote.get(name)
    if left is None:
        diffs.append((name, "missing-local", "—", right[0], "—", stamp(right[1]), "server-only"))
    elif right is None:
        diffs.append((name, "missing-server", left[0], "—", stamp(left[1]), "—", "local-only"))
    elif left[0] != right[0]:
        newer = "local" if left[1] > right[1] else "server" if right[1] > left[1] else "same-mtime"
        diffs.append((name, "md5-diff", left[0], right[0], stamp(left[1]), stamp(right[1]), newer))
    elif left[1] != right[1]:
        touched += 1

if diffs:
    print("MD5/mtime differences:")
    print("status\tfile\tlocal_md5\tserver_md5\tlocal_mtime_utc\tserver_mtime_utc\tnewer_by_mtime")
    for row in diffs:
        print("\t".join(row))
    print(f"MISMATCH: {len(diffs)} differing/missing path(s); {touched} matching-content path(s) have different mtimes.")
    sys.exit(1)
print(f"MATCH: all {len(paths)} Python/JavaScript files have identical MD5.")
if touched:
    print(f"NOTE: {touched} identical file(s) have different mtimes.")
PY
