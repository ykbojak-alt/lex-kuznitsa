#!/usr/bin/env bash
# Deploy only the committed src/ and scripts/ tree, then restart lex-stand.
# Runtime data, .env, corpus, indexes, and generated static assets stay remote.
set -euo pipefail

REMOTE_HOST="${LEX_REMOTE_HOST:-root@2.27.5.234}"
REMOTE_DIR="${LEX_REMOTE_DIR:-/opt/lex-kuznitsa}"
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
HEALTH_URL="http://127.0.0.1:8090/api/health"
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
VERIFY="$ROOT/deploy/verify-deploy.sh"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
command -v git >/dev/null || die "git is required"
command -v ssh >/dev/null || die "ssh is required"
command -v rsync >/dev/null || die "rsync is required (run from Git Bash/WSL with rsync installed)"
[ -f "$VERIFY" ] || die "verify script not found: $VERIFY"
[ -r "$SSH_KEY" ] || die "SSH key not readable: $SSH_KEY"
[ -r "$SSH_KNOWN_HOSTS" ] || die "SSH known_hosts not readable: $SSH_KNOWN_HOSTS"

KEY_COPY=""
STAGE=""
cleanup() {
  [ -z "$STAGE" ] || rm -rf -- "$STAGE"
  [ -z "$KEY_COPY" ] || rm -f -- "$KEY_COPY"
}
trap cleanup EXIT
# WSL sees Windows-mounted private keys as world-readable (0444), which OpenSSH
# rejects. Use a short-lived 0600 copy in WSL /tmp; never print or retain it.
if [ -n "${WSL_DISTRO_NAME:-}" ] && [[ "$SSH_KEY" == /mnt/* ]]; then
  KEY_COPY="$(mktemp)"
  chmod 600 "$KEY_COPY"
  cat "$SSH_KEY" >"$KEY_COPY"
  SSH_KEY="$KEY_COPY"
fi

cd "$ROOT"
# Do not silently ship unrelated or half-finished local edits. The archive below
# is HEAD; relevant dirty source must be committed before this deploy command.
if [ -n "$(git status --porcelain --untracked-files=all -- src scripts)" ]; then
  die "src/ or scripts/ has uncommitted or untracked files; commit the intended payload first"
fi

STAGE="$(mktemp -d)"
git archive --format=tar HEAD src scripts | tar -xf - -C "$STAGE"
[ -d "$STAGE/src" ] || die "HEAD archive has no src/"
[ -d "$STAGE/scripts" ] || die "HEAD archive has no scripts/"

SSH_CMD=(ssh -i "$SSH_KEY" -o UserKnownHostsFile="$SSH_KNOWN_HOSTS" -c aes128-gcm@openssh.com -o BatchMode=yes -o ConnectTimeout=10)
RSYNC_SSH="ssh -i '$SSH_KEY' -o UserKnownHostsFile='$SSH_KNOWN_HOSTS' -c aes128-gcm@openssh.com -o BatchMode=yes -o ConnectTimeout=10"

printf 'Pre-deploy comparison (%s -> %s)\n' "$ROOT" "$REMOTE_HOST:$REMOTE_DIR"
if bash "$VERIFY" "$REMOTE_HOST" "$REMOTE_DIR" "$STAGE"; then
  echo 'Pre-deploy comparison: identical.'
else
  echo 'Pre-deploy differences found; syncing the committed HEAD snapshot now.'
fi

"${SSH_CMD[@]}" "$REMOTE_HOST" "mkdir -p '$REMOTE_DIR/src' '$REMOTE_DIR/scripts'"
rsync -az --delete --exclude='/api/static/' -e "$RSYNC_SSH" \
  "$STAGE/src/" "$REMOTE_HOST:$REMOTE_DIR/src/"
rsync -az --delete -e "$RSYNC_SSH" \
  "$STAGE/scripts/" "$REMOTE_HOST:$REMOTE_DIR/scripts/"

"${SSH_CMD[@]}" "$REMOTE_HOST" 'systemctl restart lex-stand.service'
printf 'Waiting for %s to return HTTP 200' "$HEALTH_URL"
for attempt in $(seq 1 45); do
  code="$("${SSH_CMD[@]}" "$REMOTE_HOST" \
    "curl -sS -o /dev/null -w '%{http_code}' --max-time 8 '$HEALTH_URL'" 2>/dev/null || true)"
  if [ "$code" = 200 ]; then
    echo
    "${SSH_CMD[@]}" "$REMOTE_HOST" bash -s -- "$HEALTH_URL" <<'REMOTE_HEALTH'
set -euo pipefail
body="$(curl -fsS --max-time 8 "$1")"
python3 -c 'import json,sys; d=json.load(sys.stdin); llm=d.get("llm") or {}; c=d.get("corpus") or {}; print("health=200"); print("slot=" + str(llm.get("slot") or "—")); print("corpus=" + str(c.get("path") or c.get("dir") or "—")); print("documents=" + str(c.get("documents") if c.get("documents") is not None else "—"))' <<<"$body"
REMOTE_HEALTH
    exit 0
  fi
  printf '.'
  sleep 2
done
echo
die "health did not return 200 within 90 seconds; inspect journalctl -u lex-stand"
