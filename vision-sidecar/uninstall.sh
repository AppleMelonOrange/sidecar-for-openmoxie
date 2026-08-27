#!/usr/bin/env bash
#
# uninstall.sh — remove the vision-sidecar add-on. Reverses install.sh.
#
# ─── WHAT IT DOES ────────────────────────────────────────────────────────────
#   [1] Stops and removes the user LaunchAgent (com.openmoxie.vision-sidecar).
#       The sidecar stops running, so the camera gate is no longer armed.
#   [2] Leaves the database config (data_sharing / image-captioning props) in
#       place by default — it is INERT without the sidecar running (nothing arms
#       the gate, so the robot does not caption). Pass --show-config-revert to
#       print how to clear it too.
#
# It never uses sudo and never touches OpenMoxie source.
#
# Usage:   ./uninstall.sh [--show-config-revert]
#
set -euo pipefail

LABEL="com.openmoxie.vision-sidecar"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "== vision-sidecar uninstaller =="

# [1] remove the service ------------------------------------------------------
echo "[1/2] stopping + removing the LaunchAgent ..."
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [ -f "$PLIST_DST" ]; then
  rm -f "$PLIST_DST"
  echo "      removed $PLIST_DST"
else
  echo "      (no LaunchAgent found — already removed, or you ran the sidecar by hand)"
fi

# [2] config note -------------------------------------------------------------
echo "[2/2] database config left in place (it is inert without the sidecar)."
if [ "${1:-}" = "--show-config-revert" ]; then
  cat <<'EOF'

      To also clear the config, in your OpenMoxie you can set the props back:
        image_captioning="0"   (stops the robot attempting captions)
        data_sharing           (remove it, or leave it — harmless without the rest)
      via the OpenMoxie admin/DB, then re-push config. This is optional; with the
      sidecar gone, nothing arms the camera gate regardless.
EOF
fi

echo
echo "Done. The sidecar is no longer running; the camera gate will not be armed."
