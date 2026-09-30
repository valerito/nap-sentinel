"""Sentinel configuration and paths.

Everything lives outside /data/openpilot so NAP updates never touch it:
  /data/sentinel/config.json      user settings (this module)
  /data/media/0/sentinel/<event>  recorded events
  /dev/shm/nap_sentinel_*.json    live state shared between processes
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

INSTALL_DIR = Path(os.environ.get("SENTINEL_HOME", "/data/sentinel"))
SHM = Path("/dev/shm") if os.path.isdir("/dev/shm") else Path(tempfile.gettempdir())

# name -> (default, type, min, max)
SCHEMA: dict[str, tuple] = {
  "enabled": (False, bool, None, None),
  "sensitivity": (3, int, 1, 5),
  "clip_s": (45, int, 15, 180),
  "arm_delay_s": (90, int, 10, 900),
  "tilt_deg": (1.5, float, 0.5, 10.0),
  "record_cabin": (True, bool, None, None),
  "record_wide": (True, bool, None, None),
  "record_hd": (True, bool, None, None),
  "prerecord": (False, bool, None, None),
  "prerecord_s": (10, int, 5, 30),
  "discard_on_drive": (True, bool, None, None),
  "max_storage_gb": (10.0, float, 1.0, 50.0),
  "max_parked_hours": (0.0, float, 0.0, 720.0),   # 0 = never power down on time
  "low_voltage": (11.8, float, 11.0, 12.4),       # hard floor, always active
  "web_password": ("", str, None, None),
  "timezone": ("Europe/Madrid", str, None, None),  # for Telegram times and event names
  # Telegram (your own bot, created with @BotFather)
  "telegram_token": ("", str, None, None),
  "telegram_bot": ("", str, None, None),          # bot username, from getMe
  "telegram_chat_id": ("", str, None, None),      # set by /start <code>
  "telegram_chat_name": ("", str, None, None),
  "telegram_link_code": ("", str, None, None),
  "telegram_link_expires": (0.0, float, None, None),
  "telegram_alerts": (True, bool, None, None),
  "telegram_alert_delay_s": (30, int, 0, 300),    # wait, so starting the car cancels it
  "telegram_video": (True, bool, None, None),     # low-quality wide camera clip
  "telegram_video_wifi_only": (False, bool, None, None),
  # Tesla API: flash the lights on an event (at night)
  "tesla_backend": ("owner", str, None, None),     # "owner" | "fleet"
  "tesla_refresh_token": ("", str, None, None),
  "tesla_access_token": ("", str, None, None),     # fleet proxies that hand out a static token
  "tesla_client_id": ("", str, None, None),        # fleet only
  "tesla_base_url": ("", str, None, None),         # fleet only (region URL or proxy)
  "tesla_auth_url": ("", str, None, None),         # fleet only, optional
  "tesla_login_verifier": ("", str, None, None),   # PKCE, only during "Iniciar sesión con Tesla"
  "tesla_login_state": ("", str, None, None),
  "tesla_login_expires": (0.0, float, None, None),
  "tesla_vehicle_id": ("", str, None, None),
  "tesla_vehicle_name": ("", str, None, None),
  "tesla_flash": (False, bool, None, None),
  "tesla_flash_when": ("night", str, None, None),  # "night" | "always"
  "tesla_flash_manual": (False, bool, None, None),
  "location_source": ("gps", str, None, None),     # "gps" (last comma fix) | "manual"
  "latitude": (40.4168, float, -90.0, 90.0),
  "longitude": (-3.7038, float, -180.0, 180.0),
  "night_sun_elevation": (-4.0, float, -18.0, 5.0),  # sun below this = night
}

CHOICES = {"tesla_backend": ("owner", "fleet"), "tesla_flash_when": ("night", "always"),
           "location_source": ("gps", "manual")}

SECRETS = ("web_password", "telegram_token", "telegram_link_code", "tesla_refresh_token", "tesla_access_token",
           "tesla_login_verifier", "tesla_login_state")


def config_path() -> Path:
  return Path(os.environ.get("SENTINEL_CONFIG", str(INSTALL_DIR / "config.json")))


def atomic_write_json(path: Path, data) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
  with os.fdopen(fd, "w") as f:
    json.dump(data, f, indent=1)
  os.replace(tmp, path)


def read_json(path: Path, default=None):
  try:
    with open(path) as f:
      return json.load(f)
  except (OSError, ValueError):
    return default


def _coerce(name: str, value):
  default, typ, lo, hi = SCHEMA[name]
  try:
    v = typ(value) if typ is not bool else bool(value)
  except (TypeError, ValueError):
    return default
  if lo is not None:
    v = max(lo, min(hi, v))
  if name in CHOICES and v not in CHOICES[name]:
    return default
  return v


def load() -> dict:
  raw = read_json(config_path(), default={}) or {}
  return {k: _coerce(k, raw[k]) if k in raw else d for k, (d, *_rest) in SCHEMA.items()}


def update(changes: dict) -> dict:
  cfg = load()
  for k, v in changes.items():
    if k in SCHEMA:
      cfg[k] = _coerce(k, v)
  atomic_write_json(config_path(), cfg)
  return cfg


def public(cfg: dict) -> dict:
  out = {k: v for k, v in cfg.items() if k not in SECRETS}
  out["password_set"] = bool(cfg.get("web_password"))
  out["telegram_token_set"] = bool(cfg.get("telegram_token"))
  out["telegram_linked"] = bool(cfg.get("telegram_token") and cfg.get("telegram_chat_id"))
  out["tesla_connected"] = bool((cfg.get("tesla_refresh_token") or cfg.get("tesla_access_token")) and cfg.get("tesla_vehicle_id"))
  out["tesla_token_set"] = bool(cfg.get("tesla_refresh_token") or cfg.get("tesla_access_token"))
  return out


def telegram_ready(cfg: dict) -> bool:
  return bool(cfg.get("telegram_token") and cfg.get("telegram_chat_id"))


# ── live state shared with the manager hook and the web UI ─────────────────
PROCS_FILE = SHM / "nap_sentinel_procs.json"      # what the manager must run
STATUS_FILE = SHM / "nap_sentinel_status.json"    # for the web UI
TRIGGER_FILE = SHM / "nap_sentinel_trigger"       # "record now" from the web
STALE_S = 10.0


def write_procs(sensors: bool, cameras: bool, stream: bool = False) -> None:
  atomic_write_json(PROCS_FILE, {"t": time.monotonic(), "sensors": sensors, "cameras": cameras, "stream": stream})


def read_procs() -> dict:
  d = read_json(PROCS_FILE, default=None) or {}
  # if sentineld dies, its requests expire and the manager stops the extra processes
  if time.monotonic() - float(d.get("t", -1e9)) > STALE_S:
    return {"sensors": False, "cameras": False, "stream": False}
  return d
