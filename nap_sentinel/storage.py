"""Event storage for sentinel mode.

Layout (outside loggerd's realdata dir, so the deleter never touches it):

  <SENTINEL_ROOT>/<event_id>/event.json
  <SENTINEL_ROOT>/<event_id>/*.h264|*.hevc  raw streams while recording
  <SENTINEL_ROOT>/<event_id>/road.mp4     (qcamera, small, plays everywhere)
  <SENTINEL_ROOT>/<event_id>/fcamera.mp4  (full res road, HEVC)
  <SENTINEL_ROOT>/<event_id>/ecamera.mp4  (wide road, HEVC)
  <SENTINEL_ROOT>/<event_id>/dcamera.mp4  (cabin / IR, HEVC)
  <SENTINEL_ROOT>/<event_id>/thumb.jpg
"""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

from nap_sentinel.config import atomic_write_json, read_json


def sentinel_root() -> Path:
  if os.environ.get("SENTINEL_ROOT"):
    return Path(os.environ["SENTINEL_ROOT"])
  return Path("/data/media/0/sentinel")


EVENT_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}(-[0-9]+)?$")


def new_event_id(wall_time: float | None = None) -> str:
  root = sentinel_root()
  try:
    from nap_sentinel import config, timesync
    base = timesync.fmt(wall_time or time.time(), config.load()["timezone"], "%Y%m%d-%H%M%S")  # noqa: TID251
  except Exception:
    base = time.strftime("%Y%m%d-%H%M%S", time.localtime(wall_time or time.time()))  # noqa: TID251
  eid, n = base, 1
  while (root / eid).exists():
    eid = f"{base}-{n}"
    n += 1
  return eid


def valid_event_id(eid: str) -> bool:
  return bool(EVENT_ID_RE.match(eid))


def save_event(event: dict) -> None:
  atomic_write_json(sentinel_root() / event["id"] / "event.json", event)


def load_event(eid: str) -> dict | None:
  if not valid_event_id(eid):
    return None
  return read_json(sentinel_root() / eid / "event.json")


def list_events() -> list[dict]:
  root = sentinel_root()
  if not root.is_dir():
    return []
  events = []
  for d in root.iterdir():
    if d.is_dir() and valid_event_id(d.name):
      ev = read_json(d / "event.json")
      if ev:
        ev["size_bytes"] = dir_size(d)
        events.append(ev)
  events.sort(key=lambda e: e.get("wall_time", 0), reverse=True)
  return events


def delete_event(eid: str) -> bool:
  if not valid_event_id(eid):
    return False
  d = sentinel_root() / eid
  if d.is_dir():
    shutil.rmtree(d, ignore_errors=True)
    return True
  return False


def dir_size(d: Path) -> int:
  total = 0
  for p in d.rglob("*"):
    try:
      if p.is_file():
        total += p.stat().st_size
    except OSError:
      pass
  return total


def enforce_storage_cap(max_bytes: int, keep: set[str] | None = None) -> list[str]:
  """Delete the oldest events until the sentinel dir is under max_bytes.
  Events marked "locked" by the user are never deleted."""
  keep = keep or set()
  events = sorted(list_events(), key=lambda e: e.get("wall_time", 0))
  total = sum(e.get("size_bytes", 0) for e in events)
  removed = []
  for ev in events:
    if total <= max_bytes:
      break
    if ev["id"] in keep or ev.get("locked") or ev.get("status") in ("recording", "exporting"):
      continue
    if delete_event(ev["id"]):
      total -= ev.get("size_bytes", 0)
      removed.append(ev["id"])
  return removed
