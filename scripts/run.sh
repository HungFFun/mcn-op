#!/usr/bin/env bash
# mekoo-op task runner (macOS / Linux).
#
# Usage:
#   scripts/run.sh <task> [extra args...]
#
# Tasks:
#   mcn-report    MCN Mekoo daily health report      (tasks.mcn_daily_report)
#   leave-watch   MCN leave-request alert (deduped)  (tasks.mcn_leave_watch)
#   daily-recap   Yesterday's payout recap ID+PH     (tasks.daily_recap)
#   payout        Payout batch ID+PH (PDF+Telegram)  (payout.run --market both)
#   check         Config diagnostic                  (common.check)
#   schedule      Run the in-process daily scheduler (scheduler; blocks)
#
# Examples:
#   scripts/run.sh daily-recap
#   scripts/run.sh mcn-report --no-send
#   scripts/run.sh check --live
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

PY="${PYTHON:-python3}"
task="${1:-}"; shift || true

case "$task" in
  mcn-report)   exec "$PY" -m tasks.mcn_daily_report "$@" ;;
  leave-watch)  exec "$PY" -m tasks.mcn_leave_watch "$@" ;;
  daily-recap)  exec "$PY" -m tasks.daily_recap "$@" ;;
  payout)       exec "$PY" -m payout.run --market both "$@" ;;
  check)        exec "$PY" -m common.check "$@" ;;
  schedule)     exec "$PY" -m scheduler "$@" ;;
  webui)        exec "$PY" -m webui "$@" ;;
  bot)          exec "$PY" -m bot "$@" ;;
  *)
    echo "Usage: scripts/run.sh {mcn-report|leave-watch|daily-recap|payout|check|schedule|webui|bot} [args]" >&2
    exit 2 ;;
esac
