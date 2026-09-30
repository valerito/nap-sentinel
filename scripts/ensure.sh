#!/usr/bin/env bash
# NAP Sentinel: re-adds the manager hook if a NAP update removed it.
# Called from /data/continue.sh on every boot (before openpilot starts) and
# also patches an update waiting in /data/safe_staging/finalized, so it is
# already hooked when it gets swapped in. Fast, idempotent, never fails.
SENTINEL_HOME="${SENTINEL_HOME:-/data/sentinel}"
BLOCK="$SENTINEL_HOME/hook_block.txt"
[ -f "$BLOCK" ] || exit 0
for f in "${NAP_OPENPILOT:-/data/openpilot}/system/manager/process_config.py" \
         /data/safe_staging/finalized/system/manager/process_config.py; do
  [ -f "$f" ] || continue
  grep -q "nap-sentinel hook >>>" "$f" 2>/dev/null && continue
  grep -q "managed_processes" "$f" 2>/dev/null || continue   # unknown layout: leave it alone
  cat "$BLOCK" >> "$f" 2>/dev/null && echo "[sentinel] hook added to $f"
done
exit 0
