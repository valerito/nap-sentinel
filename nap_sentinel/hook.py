"""Manager hook: called from the end of NAP's system/manager/process_config.py.

It only *extends* the conditions under which sensord / camerad / encoderd run
(car off + sentineld asked for them) and registers the two sentinel
processes. When the car is on (`started`), every original condition is left
untouched, so driving behaves exactly like stock NAP.
"""
from __future__ import annotations

import time

_OFF = {"sensors": False, "cameras": False, "stream": False}
_cache = {"t": 0.0, "d": dict(_OFF)}


def _procs() -> dict:
  now = time.monotonic()
  if now - _cache["t"] > 0.25:
    try:
      from nap_sentinel.config import read_procs
      _cache["d"] = read_procs()
    except Exception:
      _cache["d"] = dict(_OFF)
    _cache["t"] = now
  return _cache["d"]


def _extend(proc, key: str) -> None:
  if getattr(proc, "_sentinel_wrapped", False):
    return
  original = proc.should_run

  def should_run(started, params, CP, _orig=original, _key=key):
    if _orig(started, params, CP):
      return True
    return (not started) and bool(_procs().get(_key))

  proc.should_run = should_run
  proc._sentinel_wrapped = True


def _always(started, params, CP) -> bool:
  return True


def install(procs: list, managed_processes: dict) -> None:
  from openpilot.system.manager.process import PythonProcess

  for name, key in (("sensord", "sensors"), ("camerad", "cameras"), ("encoderd", "cameras"),
                    ("stream_encoderd", "stream")):
    p = managed_processes.get(name)
    if p is not None:
      _extend(p, key)

  for name, module in (("nap_sentineld", "nap_sentinel.sentineld"),
                       ("nap_sentinel_webd", "nap_sentinel.webd")):
    if name not in managed_processes:
      p = PythonProcess(name, module, _always, restart_if_crash=True)
      procs.append(p)
      managed_processes[name] = p
