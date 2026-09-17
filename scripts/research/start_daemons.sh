#!/usr/bin/env bash
# Idempotent daemon starter for the 24/7 legs (MR loop + signal web).
# setsid detaches them from any session; PID files enable clean stops.
# Usage: scripts/research/start_daemons.sh [start|stop|status]
set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PY="$REPO/.venv/bin/python"
STATE="${STAMMTISCH_HOME:-$HOME/.local/share/stammtisch}"
INTEL="$STATE/intel"
export HTTPS_PROXY="${HTTPS_PROXY:-http://127.0.0.1:17877}"
export HTTP_PROXY="${HTTP_PROXY:-http://127.0.0.1:17877}"
export ASTOCK_CAPITAL="${ASTOCK_CAPITAL:-1000000}"

alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

ensure() {  # ensure <name> <pidfile> <logfile> <argv...>
  local name="$1" pidfile="$2" logfile="$3"; shift 3
  if alive "$pidfile"; then
    echo "$name: already running (pid $(cat "$pidfile"))"
  else
    mkdir -p "$(dirname "$pidfile")" "$(dirname "$logfile")"
    setsid "$@" >>"$logfile" 2>&1 < /dev/null &
    sleep 2
    pgrep -f "$(basename "$2")" | head -1 > "$pidfile"
    alive "$pidfile" && echo "$name: started (pid $(cat "$pidfile"))" \
                       || { echo "$name: FAILED — see $logfile"; return 1; }
  fi
}

case "${1:-start}" in
start)
  ensure "mr-loop" "$INTEL/mr-loop.pid" "$INTEL/mr-loop.out" \
    "$PY" "$REPO/scripts/research/mr_loop.py" --loop --interval 300
  ensure "astock-web" "$INTEL/astock-web.pid" "$INTEL/astock-web.out" \
    "$PY" "$REPO/scripts/research/astock_web.py"
  ;;
stop)
  for pidfile in "$INTEL/mr-loop.pid" "$INTEL/astock-web.pid"; do
    if alive "$pidfile"; then
      pid="$(cat "$pidfile")"; kill -TERM "$pid" && echo "stopped $pid"
    fi; rm -f "$pidfile"
  done
  ;;
status)
  for pair in "mr-loop:$INTEL/mr-loop.pid" "astock-web:$INTEL/astock-web.pid"; do
    name="${pair%%:*}"; pidfile="${pair#*:}"
    alive "$pidfile" && echo "$name: running (pid $(cat "$pidfile"))" \
                      || echo "$name: down"
  done
  ;;
esac
