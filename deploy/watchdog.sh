#!/usr/bin/env bash
# systemd oneshot: restart only lex-stand when its local health endpoint fails.
set -u

URL="${LEX_WATCHDOG_HEALTH_URL:-http://127.0.0.1:8090/api/health}"
LOG="${LEX_WATCHDOG_LOG:-/var/log/lex-watchdog.log}"
LOCK="${LEX_WATCHDOG_LOCK:-/run/lock/lex-watchdog.lock}"
UNIT="lex-stand.service"

log() { printf '%s %s\n' "$(date --iso-8601=seconds)" "$*" >>"$LOG"; }
mkdir -p "$(dirname "$LOCK")" "$(dirname "$LOG")"
exec 9>"$LOCK"
flock -n 9 || exit 0

status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$URL" 2>/dev/null || true)"
if [ "$status" = 200 ]; then
  log "healthy status=200"
  exit 0
fi

log "health_failed status=${status:-no-response}; restarting ${UNIT}"
if ! systemctl restart "$UNIT"; then
  log "restart_failed unit=${UNIT}"
  exit 1
fi

for attempt in $(seq 1 30); do
  status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$URL" 2>/dev/null || true)"
  if [ "$status" = 200 ]; then
    log "recovered status=200 attempts=$attempt"
    exit 0
  fi
  sleep 2
done
log "recovery_failed status=${status:-no-response} after=60s"
exit 1
