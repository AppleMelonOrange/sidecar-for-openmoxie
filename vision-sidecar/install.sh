#!/usr/bin/env bash
#
# install.sh — one-command installer for the vision-sidecar add-on.
#
# This automates EXACTLY the manual steps in README.md — nothing hidden, nothing
# with sudo, and no OpenMoxie source file is touched. Read it before running; it
# prints each step as it goes.
#
# ─── WHAT IT DOES (step by step) ────────────────────────────────────────────
#   [0] Finds your OpenMoxie install (first argument, or $HOME/openmoxie) and
#       checks it has a site/ folder and a Python venv.
#   [1] Confirms that venv has paho-mqtt + protobuf; installs them INTO THAT VENV
#       ONLY if missing (nothing global).
#   [2] Applies the one-time database config — data_sharing="full" plus the
#       image-captioning props — by running apply_vision_config.py against your
#       OpenMoxie DB. This is idempotent (re-running says "Unchanged").
#   [3] Installs a *user* LaunchAgent (com.openmoxie.vision-sidecar) pointed at
#       this package, so the sidecar runs now and restarts on login. This is a
#       gui/<uid> user agent — NOT a root/sudo daemon.
#   [4] Starts it and checks it loaded.
#
# ─── WHAT IT DOES NOT DO ─────────────────────────────────────────────────────
#   • It does not edit any OpenMoxie source file (add-on, not a fork).
#   • It does not set up the DNS redirect or the caption server. Those are the
#     other half of "seeing" (README → Prerequisites). Without them the camera
#     gate opens but you get no descriptions.
#   • It never uses sudo.
#
# ─── PREFER TO DO IT BY HAND? ────────────────────────────────────────────────
#   See README.md → "Install / run (manual)". This script is just those steps.
#
# ─── UNINSTALL / ROLLBACK ────────────────────────────────────────────────────
#   Run ./uninstall.sh  (stops + removes the LaunchAgent). See README → Uninstall.
#
# Usage:   ./install.sh [/path/to/openmoxie]
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MOXIE="${1:-$HOME/openmoxie}"
PY="$MOXIE/venv/bin/python"
SITE="$MOXIE/site"
LABEL="com.openmoxie.vision-sidecar"
PLIST_SRC="$HERE/com.openmoxie.vision-sidecar.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "== vision-sidecar installer =="
echo "  OpenMoxie : $MOXIE"
echo "  venv py   : $PY"
echo "  package   : $HERE"
echo

# [0] sanity ------------------------------------------------------------------
[ -d "$SITE" ] || { echo "ERROR: no OpenMoxie site/ at $SITE"; echo "Pass your OpenMoxie path:  ./install.sh /path/to/openmoxie"; exit 1; }
[ -x "$PY" ]   || { echo "ERROR: no venv python at $PY (is OpenMoxie installed with its venv?)"; exit 1; }

# [1] deps (venv only) --------------------------------------------------------
echo "[1/4] checking paho-mqtt + protobuf in the venv ..."
if ! "$PY" - <<'PYCHK'
import importlib.util as u, sys
sys.exit(0 if (u.find_spec("paho") and u.find_spec("google.protobuf")) else 1)
PYCHK
then
  echo "      missing — installing into $PY ..."
  "$PY" -m pip install --quiet paho-mqtt protobuf
fi

# [2] DB config (idempotent) --------------------------------------------------
echo "[2/4] applying vision config to the OpenMoxie DB (data_sharing=full + props) ..."
DJANGO_SETTINGS_MODULE=openmoxie.settings "$PY" "$HERE/apply_vision_config.py" --site-dir "$SITE"

# [3] LaunchAgent -------------------------------------------------------------
echo "[3/4] installing the user LaunchAgent -> $PLIST_DST ..."
mkdir -p "$HOME/Library/LaunchAgents"
# Rewrite the @HOME@ template to THIS package's real paths (most→least specific).
sed -e "s|@HOME@/openmoxie/venv/bin/python|$PY|g" \
    -e "s|@HOME@/openmoxie/vision-sidecar/vision_sidecar.py|$HERE/vision_sidecar.py|g" \
    -e "s|@HOME@/openmoxie/vision-sidecar|$HERE|g" \
    -e "s|@HOME@|$HOME|g" \
    "$PLIST_SRC" > "$PLIST_DST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true   # unload if already present
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"

# [4] verify ------------------------------------------------------------------
echo "[4/4] verifying ..."
sleep 2
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  echo "      service loaded ✓"
else
  echo "      WARNING: service not loaded — check $HERE/vision-sidecar.log"
fi

echo
echo "Done. The camera gate is armed on the robot's next connect."
echo "NOTE: to actually SEE descriptions you still need the DNS redirect + caption"
echo "      server (README → Prerequisites). This installed only the gate-opener."
echo "Uninstall any time:  ./uninstall.sh"
