#!/usr/bin/env bash
# NAP Sentinel uninstaller. Leaves NAP exactly as it was.
#   bash /data/sentinel/uninstall.sh            (asks about recordings)
#   bash /data/sentinel/uninstall.sh --purge    (also deletes recordings)
set -u
SENTINEL_HOME="${SENTINEL_HOME:-/data/sentinel}"
OP="${NAP_OPENPILOT:-/data/openpilot}"
EVENTS="${SENTINEL_ROOT:-/data/media/0/sentinel}"
CONT="${NAP_CONTINUE:-/data/continue.sh}"
say() { echo -e "\033[1;32m[sentinel]\033[0m $*"; }
PY=/usr/local/venv/bin/python3; [ -x "$PY" ] || PY=python3

for f in "$OP/system/manager/process_config.py" /data/safe_staging/finalized/system/manager/process_config.py; do
  if [ -f "$f" ] && grep -q "nap-sentinel hook >>>" "$f"; then
    sed -i '/^# >>> nap-sentinel hook >>>$/,/^# <<< nap-sentinel hook <<<$/d' "$f"
    # drop the blank line the installer added before the block
    sed -i -e :a -e '/^\n*$/{$d;N;ba' -e '}' "$f"
    say "hook quitado de $f"
  fi
done

if [ -f "$CONT" ] && grep -q "nap-sentinel" "$CONT"; then
  sed -i '/nap-sentinel/d' "$CONT"
  say "/data/continue.sh restaurado"
fi

# give back the power-down setting sentinel changed
if [ -f "$SENTINEL_HOME/state.json" ]; then
  (cd "$OP" && PYTHONPATH="$OP" "$PY" - <<PYEOF
import json
from openpilot.common.params import Params
st = json.load(open("$SENTINEL_HOME/state.json"))
if "saved_disable_powerdown" in st:
  Params().put_bool("DisablePowerDown", bool(st["saved_disable_powerdown"]))
  print("[sentinel] DisablePowerDown restaurado a", bool(st["saved_disable_powerdown"]))
PYEOF
  ) || true
fi

rm -f /dev/shm/nap_sentinel_*.json /dev/shm/nap_sentinel_trigger

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1
if [ $PURGE -eq 0 ] && [ -d "$EVENTS" ] && [ -r /dev/tty ]; then
  { read -r -p "¿Borrar también las grabaciones de $EVENTS? [s/N] " a < /dev/tty; } 2>/dev/null || a=n
  case "$a" in s|S|y|Y) PURGE=1;; esac
fi
[ $PURGE -eq 1 ] && rm -rf "$EVENTS" && say "grabaciones borradas"

cd / && rm -rf "$SENTINEL_HOME"
say "Sentinel desinstalado. Reinicia el comma para terminar: sudo reboot"
