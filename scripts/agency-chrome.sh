#!/usr/bin/env bash
# Run the always-open agency Chrome windows for payout --source extension
# (Ubuntu desktop; the mekoo-op bot itself keeps running in Docker).
#
# Usage:
#   scripts/agency-chrome.sh             # start ID and PH windows (skips running ones)
#   scripts/agency-chrome.sh ph          # one market only
#   scripts/agency-chrome.sh autostart   # also start both at every desktop login
#
# Each market gets its own profile in ~/.config/mekoo-chrome/<market>, logged in
# as that agency and with chrome-extension/ loaded (see chrome-extension/README.md).
set -euo pipefail
cd "$(dirname "$0")/.."

CHROME="$(command -v google-chrome || command -v google-chrome-stable || true)"
[[ -n "$CHROME" ]] || { echo "Google Chrome not found (install the .deb from google.com/chrome)" >&2; exit 1; }
PROFILES="$HOME/.config/mekoo-chrome"

start() {
  local market="$1" dir="$PROFILES/$1"
  mkdir -p "$dir"
  if pgrep -f -- "--user-data-dir=$dir" >/dev/null; then
    echo "[$market] already running"
    return
  fi
  nohup "$CHROME" --user-data-dir="$dir" --no-first-run --no-default-browser-check \
    --class="mekoo-$market" https://business.tevi.com/ >/dev/null 2>&1 &
  echo "[$market] started (profile $dir)"
}

autostart() {
  mkdir -p "$HOME/.config/autostart"
  cat > "$HOME/.config/autostart/mekoo-agency-chrome.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=mekoo-op agency Chrome
Exec=$PWD/scripts/agency-chrome.sh
X-GNOME-Autostart-enabled=true
DESKTOP
  echo "autostart installed: ~/.config/autostart/mekoo-agency-chrome.desktop"
}

case "${1:-both}" in
  id|ph)     start "$1" ;;
  both)      start id; start ph ;;
  autostart) autostart; start id; start ph ;;
  *)         echo "usage: $0 [id|ph|both|autostart]" >&2; exit 1 ;;
esac
