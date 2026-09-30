import time
import types

from nap_sentinel import config, storage, timesync


def test_fmt_uses_timezone():
  # 2026-09-30 10:00:00 UTC
  t = 1790762400
  assert timesync.fmt(t, "Europe/Madrid") == "30/09 12:00:00"
  assert timesync.fmt(t, "UTC") == "30/09 10:00:00"


def test_set_time_only_when_off_and_sane(monkeypatch):
  calls = []
  monkeypatch.setattr(timesync.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or types.SimpleNamespace(returncode=0))
  assert not timesync.set_system_time(time.time() + 5, "t")          # small drift: ignore  # noqa: TID251
  assert not timesync.set_system_time(1_000_000_000, "t")           # 2001: not a real clock
  assert timesync.set_system_time(time.time() + 3600 * 24 * 300, "t")  # noqa: TID251
  assert calls and calls[0][:3] == ["date", "-u", "-s"]


def test_http_date_rate_limited(monkeypatch):
  seen = []
  monkeypatch.setattr(timesync, "set_system_time", lambda e, s: seen.append(e))
  monkeypatch.setattr(timesync, "_last_http_sync", 0.0)
  monkeypatch.setattr(timesync.time, "monotonic", lambda: 10_000.0)
  timesync.from_http_date("Wed, 30 Sep 2026 15:04:05 GMT")
  timesync.from_http_date("Wed, 30 Sep 2026 15:04:06 GMT")
  assert len(seen) == 1 and abs(seen[0] - 1790780645) < 1


def test_event_id_uses_configured_timezone(tmp_path, monkeypatch):
  monkeypatch.setenv("SENTINEL_ROOT", str(tmp_path))
  monkeypatch.setenv("SENTINEL_CONFIG", str(tmp_path / "c.json"))
  config.update({"timezone": "Europe/Madrid"})
  assert storage.new_event_id(1790762400) == "20260930-120000"


def test_power_fallbacks():
  import pytest
  try:
    from nap_sentinel.sentineld import Sentinel
  except ImportError:
    pytest.skip("needs openpilot's built msgq (runs on the device / openpilot dev env)")
  s = Sentinel.__new__(Sentinel)
  ps = types.SimpleNamespace(voltage=12600, current=250)
  s.sm = {"peripheralState": ps}
  ds = types.SimpleNamespace(powerDrawW=0.0, somPowerDrawW=0.0)
  assert s._power(ds) == {"power_draw_w": 3.15, "power_source": "entrada"}      # comma 4
  ds.powerDrawW = 1.84
  assert s._power(ds)["power_source"] == "comma"                                 # comma 3X
  ds.powerDrawW, ps.current, ds.somPowerDrawW = 0.0, 0, 2.2
  assert s._power(ds) == {"power_draw_w": 2.2, "power_source": "SoM"}
  ds.somPowerDrawW = 0.0
  assert s._power(ds)["power_draw_w"] is None
