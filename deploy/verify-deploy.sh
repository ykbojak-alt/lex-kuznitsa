#!/usr/bin/env bash
# Compare MD5 manifests for Python/JavaScript source under src/ and scripts/.
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
  || { echo 'ERROR: find, sort, and md5sum are required' >&2; exit 2; }
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

(cd "$SOURCE_ROOT" && find src scripts -type f \( -name '*.py' -o -name '*.js' \) -print0 \
  | LC_ALL=C sort -z | xargs -0 -r md5sum) >"$TMP/local.md5"

ssh -i "$SSH_KEY" -o UserKnownHostsFile="$SSH_KNOWN_HOSTS" -c aes128-gcm@openssh.com -o BatchMode=yes -o ConnectTimeout=10 \
  "$REMOTE_HOST" "cd '$REMOTE_DIR' && find src scripts -type f \\( -name '*.py' -o -name '*.js' \\) -print0 | LC_ALL=C sort -z | xargs -0 -r md5sum" \
  >"$TMP/remote.md5"

local_count="$(wc -l <"$TMP/local.md5" | tr -d ' ')"
remote_count="$(wc -l <"$TMP/remote.md5" | tr -d ' ')"
printf 'Python/JS files: local=%s remote=%s\n' "$local_count" "$remote_count"
if diff -u "$TMP/local.md5" "$TMP/remote.md5"; then
  echo 'MATCH: all Python/JavaScript files are byte-identical.'
  exit 0
fi
echo 'MISMATCH: diff above lists missing, extra, or changed files.' >&2
exit 1
