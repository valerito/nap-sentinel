"""Adds / removes the manager hook in NAP's process_config.py.

Used by install.sh, uninstall.sh, /data/continue.sh at every boot, and by
sentineld every 10 minutes. It also patches a pending NAP update waiting in
/data/safe_staging/finalized, so the hook is already there after the update
is swapped in on the next boot.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BEGIN = "# >>> nap-sentinel hook >>>"
END = "# <<< nap-sentinel hook <<<"
BLOCK = f"""
{BEGIN}
# Added by /data/sentinel (NAP Sentinel). Safe to delete; if this fails,
# openpilot starts exactly as without it.
try:
  import sys as _nap_s
  if "/data/sentinel" not in _nap_s.path:
    _nap_s.path.insert(0, "/data/sentinel")
  from nap_sentinel.hook import install as _nap_sentinel_install
  _nap_sentinel_install(procs, managed_processes)
except Exception as _nap_e:  # never break openpilot
  print("nap-sentinel hook disabled:", _nap_e)
{END}
"""

REL = "system/manager/process_config.py"
ROOTS = [os.environ.get("NAP_OPENPILOT", "/data/openpilot"), "/data/safe_staging/finalized"]


def has_hook(path: Path) -> bool:
  try:
    return BEGIN in path.read_text()
  except OSError:
    return False


def add_hook(path: Path) -> bool:
  """Returns True if the file was changed."""
  if not path.is_file() or has_hook(path):
    return False
  text = path.read_text()
  if "managed_processes" not in text:
    return False  # unknown layout: do nothing rather than break it
  with open(path, "a") as f:
    f.write(("" if text.endswith("\n") else "\n") + BLOCK)
  return True


def remove_hook(path: Path) -> bool:
  if not has_hook(path):
    return False
  text = path.read_text()
  start = text.index(BEGIN)
  end = text.index(END, start) + len(END)
  new = (text[:start].rstrip("\n") + "\n" + text[end:].lstrip("\n")).rstrip("\n") + "\n"
  path.write_text(new)
  return True


def ensure_all() -> list[str]:
  changed = []
  for root in ROOTS:
    p = Path(root) / REL
    if add_hook(p):
      changed.append(str(p))
  return changed


def remove_all() -> list[str]:
  changed = []
  for root in ROOTS:
    p = Path(root) / REL
    if remove_hook(p):
      changed.append(str(p))
  return changed


if __name__ == "__main__":
  cmd = sys.argv[1] if len(sys.argv) > 1 else "ensure"
  res = remove_all() if cmd == "remove" else ensure_all()
  for r in res:
    print(("removed hook from " if cmd == "remove" else "hook added to ") + r)
