"""Self-update from GitHub, driven from the web panel.

  check()      reads VERSION from the repo (every few hours, or on demand)
  start()      downloads dist/nap-sentinel-install.sh and runs it with
               --yes --no-reboot in the background (same installer as the
               one-line install, so config and events are kept)
  status()     what the panel shows: current / latest / running / done / failed
  reboot()     asks the openpilot manager to reboot (DoReboot), like the
               comma's own "Reboot" button; falls back to `sudo reboot`

The new code is on disk right after the installer finishes, but the running
processes keep the old one until the comma reboots, so "needs_reboot" is simply
installed VERSION != VERSION this process started with.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path

import requests

from nap_sentinel import config

BASE_URL = os.environ.get("SENTINEL_UPDATE_BASE", "https://raw.githubusercontent.com/valerito/nap-sentinel/main")
REPO_URL = "https://github.com/valerito/nap-sentinel"
CHECK_EVERY_S = 6 * 3600
INSTALL_TIMEOUT_S = 300
STALE_RUN_S = INSTALL_TIMEOUT_S + 120
STATE_FILE = config.SHM / "nap_sentinel_update.json"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_lock = threading.Lock()


def _home() -> Path:
  return config.INSTALL_DIR


def installed_version() -> str:
  for p in (_home() / "VERSION", Path(__file__).resolve().parent.parent / "VERSION"):
    try:
      v = p.read_text().strip()
      if v:
        return v
    except OSError:
      pass
  return "?"


RUNNING_VERSION = installed_version()


def parse_version(v: str) -> tuple[int, ...] | None:
  m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", (v or "").strip())
  return tuple(int(x) for x in m.group(1).split(".")) if m else None


def newer(latest: str, current: str) -> bool:
  a, b = parse_version(latest), parse_version(current)
  if a is None:
    return False
  if b is None:
    return True
  n = max(len(a), len(b))
  return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def _state() -> dict:
  return config.read_json(STATE_FILE, default={}) or {}


def _save(**changes) -> dict:
  st = {**_state(), **changes}
  config.atomic_write_json(STATE_FILE, st)
  return st


def log_path() -> Path:
  return _home() / "update.log"


def log_tail(n: int = 25) -> list[str]:
  try:
    lines = log_path().read_text(encoding="utf-8", errors="replace").splitlines()
  except OSError:
    return []
  return [_ANSI.sub("", ln) for ln in lines[-n:]]


def _get(path: str, timeout: float) -> requests.Response:
  # the query string skips GitHub's raw CDN cache (a few minutes) so a fresh push shows up
  r = requests.get(f"{BASE_URL}/{path}", params={"t": int(time.time())}, timeout=timeout)  # noqa: TID251
  r.raise_for_status()
  return r


def check() -> dict:
  """Fetch the latest VERSION from GitHub; never raises."""
  try:
    latest = _get("VERSION", 15).text.strip()
    if parse_version(latest) is None:
      raise ValueError(f"VERSION inesperado: {latest[:20]!r}")
    return _save(latest=latest, checked_at=time.time(), check_error="")  # noqa: TID251
  except Exception as e:
    return _save(checked_at=time.time(), check_error=_short(e))  # noqa: TID251


def check_due(st: dict | None = None) -> bool:
  st = _state() if st is None else st
  return time.time() - float(st.get("checked_at", 0)) > CHECK_EVERY_S  # noqa: TID251


def _short(e: Exception) -> str:
  if isinstance(e, requests.ConnectionError):
    return "sin conexión a internet"
  if isinstance(e, requests.Timeout):
    return "GitHub no responde"
  if isinstance(e, requests.HTTPError) and e.response is not None:
    return f"GitHub respondió {e.response.status_code}"
  return str(e)[:200]


def status() -> dict:
  st = _state()
  installed = installed_version()
  latest = st.get("latest", "")
  state = st.get("state", "idle")
  if state == "running" and time.time() - float(st.get("started_at", 0)) > STALE_RUN_S:  # noqa: TID251
    state, st["error"] = "failed", "la actualización se interrumpió"
  out = {
    "running_version": RUNNING_VERSION,
    "installed_version": installed,
    "latest": latest,
    "available": bool(latest) and newer(latest, installed),
    "needs_reboot": installed != RUNNING_VERSION,
    "state": state,
    "target": st.get("target", ""),
    "error": st.get("error", ""),
    "checked_at": st.get("checked_at"),
    "check_error": st.get("check_error", ""),
    "finished_at": st.get("finished_at"),
    "repo": REPO_URL,
  }
  if state in ("running", "failed"):
    out["log"] = log_tail()
  return out


def start(force: bool = False) -> tuple[bool, str]:
  """Start the update in a background thread. Returns (started, message)."""
  if not _lock.acquire(blocking=False):
    return False, "ya se está actualizando"
  try:
    st = status()
    if st["state"] == "running":
      _lock.release()
      return False, "ya se está actualizando"
    if not force and not st["available"]:
      _lock.release()
      return False, "no hay ninguna versión nueva"
    _save(state="running", started_at=time.time(), target=st["latest"], error="")  # noqa: TID251
  except Exception:
    _lock.release()
    raise
  threading.Thread(target=_run_locked, name="sentinel-update", daemon=True).start()
  return True, "actualizando"


def _run_locked() -> None:
  try:
    ok, msg, version = run()
    _save(state="done" if ok else "failed", error="" if ok else msg, finished_at=time.time(),  # noqa: TID251
          **({"target": version} if version else {}))
  except Exception as e:  # pragma: no cover - last resort, the panel must not stay "running"
    _save(state="failed", error=_short(e), finished_at=time.time())  # noqa: TID251
  finally:
    _lock.release()


def run() -> tuple[bool, str, str]:
  """Download and run the installer. Blocking. Returns (ok, message, version)."""
  home = _home()
  home.mkdir(parents=True, exist_ok=True)
  log = log_path()
  script = home / ".update-install.sh"
  with open(log, "w", encoding="utf-8", errors="replace") as lf:
    lf.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] actualizando desde {BASE_URL}\n")
    try:
      body = _get("dist/nap-sentinel-install.sh", 120).text
    except Exception as e:
      lf.write(f"descarga fallida: {e}\n")
      return False, f"no se pudo descargar el instalador ({_short(e)})", ""
    m = re.search(r'^VERSION="([^"]+)"', body, re.M)
    if not body.startswith("#!") or not m or "__NAP_SENTINEL_PAYLOAD__" not in body:
      lf.write("el archivo descargado no es el instalador\n")
      return False, "el archivo descargado no es el instalador", ""
    version = m.group(1)
    script.write_text(body, encoding="utf-8")
    lf.write(f"instalador {version} descargado ({len(body) // 1024} KB)\n")
    lf.flush()
    try:
      p = subprocess.run(["bash", str(script), "--yes", "--no-reboot"], stdin=subprocess.DEVNULL, stdout=lf,
                         stderr=subprocess.STDOUT, timeout=INSTALL_TIMEOUT_S, start_new_session=True,
                         env={**os.environ, "SENTINEL_HOME": str(home)})
      rc = p.returncode
    except subprocess.TimeoutExpired:
      lf.write("el instalador tardó demasiado\n")
      return False, "el instalador tardó demasiado", version
    finally:
      try:
        script.unlink()
      except OSError:
        pass
    lf.write(f"código de salida: {rc}\n")
  if rc != 0:
    return False, _installer_error() or f"el instalador terminó con código {rc}", version
  now = installed_version()
  if now != version:
    return False, f"tras instalar, la versión es {now} (esperaba {version})", version
  return True, "ok", version


def _installer_error() -> str:
  """The installer's die() message (printed in red), if any."""
  try:
    lines = log_path().read_text(encoding="utf-8", errors="replace").splitlines()
  except OSError:
    return ""
  for ln in reversed(lines):
    if "\x1b[1;31m" in ln:
      return _ANSI.sub("", ln).replace("[sentinel]", "", 1).strip()
  return ""


def reboot() -> str:
  """Reboot the comma the same way its own settings button does."""
  try:
    from openpilot.common.params import Params
    Params().put_bool("DoReboot", True)
    return "manager"
  except Exception:
    subprocess.Popen(["sudo", "reboot"], start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "sudo"
