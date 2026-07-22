#!/bin/zsh
# Task Ledger cadence installer: weekday 4:45 PM local-time launchd agent.
#
#   ./install_cadence.sh install     write plist, bootstrap the agent
#   ./install_cadence.sh status      is it loaded, when did it last run
#   ./install_cadence.sh run-now     fire one tick immediately via launchd
#   ./install_cadence.sh uninstall   bootout and remove the plist
#
# Configure by editing the CONFIG block below before install (or reinstall
# after changing it). launchd StartCalendarInterval follows the Mac's local
# timezone, so 16:45 stays 4:45 PM through PST/PDT transitions.

set -e

# ------------------------------------------------------------------ CONFIG
TL_USER_REF="aad-dev"
DB_DSN="host=localhost port=5432 dbname=taskintel user=$(whoami)"
TL_INBOX="$HOME/task-ledger-inbox"
HOUR=16
MINUTE=45
# -------------------------------------------------------------- END CONFIG

LABEL="com.farmingfarmer.taskledger.cadence"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$(command -v python3)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs/task-ledger"

write_plist() {
  mkdir -p "$HOME/Library/LaunchAgents" "$LOGDIR"
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$REPO/cadence/cadence_tick.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>DB_DSN</key><string>$DB_DSN</string>
    <key>TL_USER</key><string>$TL_USER_REF</string>
    <key>TL_REPO</key><string>$REPO</string>
    <key>TL_INBOX</key><string>$TL_INBOX</string>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Weekday</key><integer>1</integer>
          <key>Hour</key><integer>$HOUR</integer>
          <key>Minute</key><integer>$MINUTE</integer></dict>
    <dict><key>Weekday</key><integer>2</integer>
          <key>Hour</key><integer>$HOUR</integer>
          <key>Minute</key><integer>$MINUTE</integer></dict>
    <dict><key>Weekday</key><integer>3</integer>
          <key>Hour</key><integer>$HOUR</integer>
          <key>Minute</key><integer>$MINUTE</integer></dict>
    <dict><key>Weekday</key><integer>4</integer>
          <key>Hour</key><integer>$HOUR</integer>
          <key>Minute</key><integer>$MINUTE</integer></dict>
    <dict><key>Weekday</key><integer>5</integer>
          <key>Hour</key><integer>$HOUR</integer>
          <key>Minute</key><integer>$MINUTE</integer></dict>
  </array>
  <key>StandardOutPath</key><string>$LOGDIR/cadence.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/cadence.log</string>
  <key>RunAtLoad</key><false/>
</dict>
</plist>
PLIST
  plutil -lint "$PLIST"
}

case "${1:-}" in
  install)
    write_plist
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    launchctl enable "gui/$(id -u)/$LABEL"
    echo "installed: weekdays at $HOUR:$MINUTE local time"
    echo "log: $LOGDIR/cadence.log"
    ;;
  status)
    launchctl print "gui/$(id -u)/$LABEL" | \
      grep -E "state|last exit|program|runs" || \
      echo "not loaded"
    echo "--- last log lines:"
    tail -8 "$LOGDIR/cadence.log" 2>/dev/null || echo "(no log yet)"
    ;;
  run-now)
    launchctl kickstart "gui/$(id -u)/$LABEL"
    echo "kicked; tail -f $LOGDIR/cadence.log"
    ;;
  uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed"
    ;;
  *)
    echo "usage: $0 install|status|run-now|uninstall"
    exit 1
    ;;
esac
