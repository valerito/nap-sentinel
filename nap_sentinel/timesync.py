"""Keeps the comma's clock right and formats times in the user's time zone.

The comma normally sets its clock from GPS (timed) or NTP. Parked in a garage
with no GPS fix, and without NTP, it can be months off, which breaks event
names, the web panel and Telegram times. Sentinel corrects it from:
  * the Date header of Telegram's servers (whenever a bot request succeeds),
  * the phone/PC that opens the web panel ("Poner la hora de este dispositivo").
Only corrects when the difference is larger than MIN_CORRECTION_S.
"""
from __future__ import annotations

import datetime
import email.utils
import subprocess
import time

MIN_CORRECTION_S = 60
MIN_VALID_EPOCH = 1735689600      # 2025-01-01: anything earlier is not a real clock
MAX_VALID_EPOCH = 4102444800      # 2100-01-01
_last_http_sync = 0.0
_log = print


def set_logger(fn) -> None:
  global _log
  _log = fn


def clock_offset(true_epoch: float) -> float:
  return true_epoch - time.time()  # noqa: TID251


def set_system_time(true_epoch: float, source: str) -> bool:
  """Returns True if the clock was changed."""
  if not (MIN_VALID_EPOCH < true_epoch < MAX_VALID_EPOCH):
    return False
  off = clock_offset(true_epoch)
  if abs(off) < MIN_CORRECTION_S:
    return False
  stamp = datetime.datetime.fromtimestamp(true_epoch, datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")
  # same command openpilot's timed uses; sudo as a fallback
  for cmd in (["date", "-u", "-s", stamp], ["sudo", "date", "-u", "-s", stamp]):
    try:
      r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
      if r.returncode == 0:
        _log(f"hora corregida {off:+.0f} s desde {source}")
        return True
    except Exception:
      continue
  _log(f"no se pudo corregir la hora ({off:+.0f} s, {source})")
  return False


def from_http_date(header: str | None, source: str = "Telegram") -> None:
  """Use an HTTP Date header (1 s resolution) at most every 10 minutes."""
  global _last_http_sync
  if not header or time.monotonic() - _last_http_sync < 600:
    return
  try:
    dt = email.utils.parsedate_to_datetime(header)
  except (TypeError, ValueError):
    return
  _last_http_sync = time.monotonic()
  set_system_time(dt.timestamp(), source)


def fmt(wall: float, tz: str, pattern: str = "%d/%m %H:%M:%S") -> str:
  try:
    from zoneinfo import ZoneInfo
    return datetime.datetime.fromtimestamp(wall, ZoneInfo(tz)).strftime(pattern)
  except Exception:
    return time.strftime(pattern, time.localtime(wall))
