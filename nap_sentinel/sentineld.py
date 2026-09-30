#!/usr/bin/env python3
"""nap_sentineld: parked-car surveillance for NotAutopilot.

  OFF ──(enabled & car off)──▶ ARMING ──(arm delay)──▶ ARMED ──(event)──▶ RECORDING
   ▲                                                     ▲                    │
   └──────────── car on / disabled ◀─────────────────────┴──── clip ends ─────┘

ARMED, normal mode:       only sensord runs (IMU). Cameras start on an event,
                          so the first ~2-3 s after the impact are missed.
ARMED, pre-record mode:   camerad + encoderd stay on and the last N seconds are
                          kept in RAM, so the clip starts before the impact
                          (≈ +2 W while parked).

It asks the manager for processes through /dev/shm/nap_sentinel_procs.json
(read by nap_sentinel.hook). No car-control process is ever started.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import cereal.messaging as messaging
from cereal import log
from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper
from openpilot.common.swaglog import cloudlog

from nap_sentinel import config, storage
from nap_sentinel.detector import MotionDetector, Trigger
from nap_sentinel.exporter import OUTPUTS, export_event
from nap_sentinel.recorder import Recorder
from nap_sentinel.telegram import TelegramService, load_tg_state, save_tg_state

ThermalStatus = log.DeviceState.ThermalStatus

LOOP_HZ = 20
MAX_CLIP_S = 180
POST_RECORD_COOLDOWN_S = 10
NO_VIDEO_TIMEOUT_S = 20
MIN_RECORD_VOLTAGE_MARGIN = 0.1
VOLTAGE_TAU_S = 45.0
HOOK_CHECK_S = 600
STATE_FILE = config.INSTALL_DIR / "state.json"


class State:
  OFF = "off"
  ARMING = "arming"
  ARMED = "armed"
  RECORDING = "recording"


class Sentinel:
  def __init__(self):
    self.params = Params()
    self.sm = messaging.SubMaster(['deviceState', 'pandaStates', 'peripheralState'])
    self.accel_sock = messaging.sub_sock('accelerometer', conflate=False)
    self.gyro_sock = messaging.sub_sock('gyroscope', conflate=False)

    self.cfg = config.load()
    self.cfg_t = 0.0
    self.state = State.OFF
    self.state_t = time.monotonic()
    self.offroad_since: float | None = None
    self.detector = MotionDetector()
    self.recorder: Recorder | None = None
    self.recorder_services: list[str] = []
    self.event: dict | None = None
    self.record_until = 0.0
    self.record_started = 0.0
    self.cooldown_until = 0.0
    self.trigger_mono = 0.0
    self.export_thread: threading.Thread | None = None
    self.last_trigger: dict | None = None
    self.skipped_reason = ""
    self.voltage_lp: float | None = None
    self.last_status_write = 0.0
    self.last_hook_check = 0.0
    self.pwrsave_overridden = False
    self.shutdown_requested = False
    self.telegram = TelegramService(log=cloudlog.warning)
    from nap_sentinel import timesync
    from nap_sentinel.telegram import append_log
    timesync.set_logger(lambda m: (cloudlog.warning(f"sentinel: {m}"), append_log(m)))
    self.pending_alerts: dict[str, float] = {}   # event id -> monotonic time to send
    self.telegram.start()

    config.write_procs(False, False)
    self._recover_interrupted_events()

  # ── helpers ────────────────────────────────────────────────
  def _set_state(self, s: str) -> None:
    if s != self.state:
      cloudlog.event("sentinel state", old=self.state, new=s)
      self.state = s
      self.state_t = time.monotonic()

  def _onroad(self) -> bool:
    ign = any(ps.ignitionLine or ps.ignitionCan for ps in self.sm['pandaStates']
              if ps.pandaType != log.PandaState.PandaType.unknown)
    return bool(self.sm['deviceState'].started or ign)

  def _power(self, ds) -> dict:
    """Device power draw. The comma 3X reports it in deviceState.powerDrawW,
    but on the comma 4 that sensor doesn't exist and it is always 0, so fall
    back to the input voltage x current measured for pandad, then to the SoM."""
    if ds.powerDrawW > 0.05:
      return {"power_draw_w": round(ds.powerDrawW, 2), "power_source": "comma"}
    ps = self.sm['peripheralState']
    if ps.voltage > 0 and ps.current > 0:
      return {"power_draw_w": round(ps.voltage * ps.current / 1e6, 2), "power_source": "entrada"}
    if ds.somPowerDrawW > 0.05:
      return {"power_draw_w": round(ds.somPowerDrawW, 2), "power_source": "SoM"}
    return {"power_draw_w": None, "power_source": None}

  def _voltage(self) -> float | None:
    ps = self.sm['peripheralState']
    if ps.pandaType == log.PandaState.PandaType.unknown or ps.voltage == 0:
      return None
    return ps.voltage / 1000.0

  def _recover_interrupted_events(self) -> None:
    for ev in storage.list_events():
      d = storage.sentinel_root() / ev["id"]
      leftover_raw = any((d / raw).is_file() and (d / raw).stat().st_size > 0 for raw in OUTPUTS)
      if ev.get("status") in ("recording", "exporting") or (leftover_raw and ev.get("status") in ("ready", "failed")):
        # interrupted, or a stream an older version could not convert: try again
        ev["status"] = "pending_export"
        storage.save_event(ev)
        st = load_tg_state(ev["id"])
        if st.pop("video_failed", None) is not None:
          st.pop("video_error", None)
          save_tg_state(ev["id"], st)

  def _wanted_services(self) -> list[str]:
    s = ["qRoadEncodeData"]
    if self.cfg["record_hd"]:
      s.append("roadEncodeData")
    if self.cfg["record_wide"]:
      s.append("wideRoadEncodeData")
    if self.cfg["record_cabin"]:
      s.append("driverEncodeData")
    if self._want_lq_stream():
      s.append("livestreamWideRoadEncodeData")
    return s

  def _want_lq_stream(self) -> bool:
    # low-bitrate H.264 wide camera for Telegram, from stream_encoderd
    return config.telegram_ready(self.cfg) and self.cfg["telegram_video"]

  # ── power: keep the device on while parked, with our own voltage floor ──
  def _manage_power(self, now: float, onroad: bool) -> None:
    enabled = self.cfg["enabled"]
    st = config.read_json(STATE_FILE, default={}) or {}
    if enabled and "saved_disable_powerdown" not in st:
      st["saved_disable_powerdown"] = self.params.get_bool("DisablePowerDown")
      config.atomic_write_json(STATE_FILE, st)
      self.params.put_bool("DisablePowerDown", True)
    elif enabled and not self.params.get_bool("DisablePowerDown"):
      self.params.put_bool("DisablePowerDown", True)
    elif not enabled and "saved_disable_powerdown" in st:
      self.params.put_bool("DisablePowerDown", bool(st.pop("saved_disable_powerdown")))
      config.atomic_write_json(STATE_FILE, st)

    v = self._voltage()
    if v is not None:
      if self.voltage_lp is None:
        self.voltage_lp = v
      else:
        k = (1.0 / LOOP_HZ) / (VOLTAGE_TAU_S + 1.0 / LOOP_HZ)
        self.voltage_lp += k * (v - self.voltage_lp)

    if onroad:
      self.offroad_since = None
      return
    if self.offroad_since is None:
      self.offroad_since = now
    if not enabled:
      return
    parked = now - self.offroad_since
    reason = None
    if self.voltage_lp is not None and self.voltage_lp < self.cfg["low_voltage"] and parked > 60:
      reason = f"low voltage {self.voltage_lp:.2f} V"
    elif self.cfg["max_parked_hours"] > 0 and parked > self.cfg["max_parked_hours"] * 3600:
      reason = "max parked time"
    if reason and self.state != State.RECORDING and not self.shutdown_requested:
      self.shutdown_requested = True
      cloudlog.warning(f"sentinel: shutting down device ({reason})")
      self.params.put_bool("DoShutdown", True)

  def _set_powersave(self, on: bool) -> None:
    try:
      from openpilot.system.hardware import HARDWARE
      HARDWARE.set_power_save(on)
    except Exception:
      cloudlog.exception("sentinel: set_power_save failed")

  # ── cameras / recorder ─────────────────────────────────────
  def _cameras_wanted(self) -> bool:
    if self.state == State.RECORDING:
      return True
    return self.state == State.ARMED and self.cfg["prerecord"] and not self._too_hot()

  def _too_hot(self) -> bool:
    return self.sm['deviceState'].thermalStatus >= ThermalStatus.red

  def _update_recorder(self, cameras: bool) -> None:
    services = self._wanted_services()
    if cameras and (self.recorder is None or (services != self.recorder_services and not self.recorder.recording())):
      if self.recorder is not None:
        self.recorder.close()
      self.recorder = Recorder(services, self.cfg["prerecord_s"] if self.cfg["prerecord"] else 0)
      self.recorder_services = services
    elif not cameras and self.recorder is not None:
      self.recorder.close()
      self.recorder = None
    if self.recorder is not None:
      self.recorder.set_prerecord(self.cfg["prerecord_s"] if self.cfg["prerecord"] else 0)
      self.recorder.poll()

  # ── recording ──────────────────────────────────────────────
  def _start_or_extend(self, trig: Trigger, now: float) -> None:
    clip_s = self.cfg["clip_s"]
    trig_d = {"reason": trig.reason, "magnitude": round(trig.magnitude, 3), "wall_time": time.time()}  # noqa: TID251
    self.last_trigger = trig_d

    if self.state == State.RECORDING and self.event is not None:
      self.event["triggers"].append(trig_d)
      if trig.reason == "impact":
        self.event["peak"] = max(self.event.get("peak", 0.0), trig_d["magnitude"])
      self.record_until = min(max(self.record_until, now + clip_s), self.record_started + MAX_CLIP_S)
      storage.save_event(self.event)
      return

    v = self._voltage()
    if self._too_hot():
      self.skipped_reason = "demasiado caliente"
      return
    if v is not None and v < self.cfg["low_voltage"] + MIN_RECORD_VOLTAGE_MARGIN:
      self.skipped_reason = f"tensión baja {v:.2f} V"
      return
    self.skipped_reason = ""

    self.event = {
      "id": storage.new_event_id(),
      "wall_time": trig_d["wall_time"],
      "status": "recording",
      "reason": trig.reason,
      "peak": round(trig.magnitude, 3) if trig.reason == "impact" else 0.0,
      "triggers": [trig_d],
      "prerecord_s": 0.0,
      "files": {},
      "locked": False,
    }
    storage.save_event(self.event)
    self.trigger_mono = now
    self.record_started = now
    self.record_until = now + clip_s
    self._set_state(State.RECORDING)
    self._set_powersave(False)
    self.pwrsave_overridden = True
    cloudlog.event("sentinel trigger", **trig_d, event_id=self.event["id"])
    delay = self.cfg["telegram_alert_delay_s"] if self.cfg["discard_on_drive"] and trig.reason != "manual" else 0
    if delay > 0:
      # give the owner time to start the car: if it starts, the event is
      # discarded and this alert is never sent
      self.pending_alerts[self.event["id"]] = now + delay
    else:
      self.telegram.notify_trigger(self.event["id"])

  def _event_dir(self) -> Path:
    assert self.event is not None
    return storage.sentinel_root() / self.event["id"]

  def _step_recording(self, now: float) -> None:
    ev = self.event
    if ev is None or self.recorder is None:
      return
    if not self.recorder.recording():
      buffered = self.recorder.buffered_s()
      self.recorder.start(self._event_dir())
      ev["prerecord_s"] = buffered
      ev["services"] = self.recorder_services
      storage.save_event(ev)
    elif now - self.recorder.last_packet > NO_VIDEO_TIMEOUT_S:
      cloudlog.error("sentinel: no video from encoderd")
      ev["error"] = "sin vídeo de las cámaras"
      self.record_until = now

  def _finish_recording(self, discard: bool = False) -> None:
    ev, self.event = self.event, None
    first = self.recorder.first_recv() if self.recorder is not None else None
    frames = self.recorder.stop() if self.recorder is not None and self.recorder.recording() else {}
    if self.pwrsave_overridden:
      self.pwrsave_overridden = False
      if not self._onroad():
        self._set_powersave(True)
    self.cooldown_until = time.monotonic() + POST_RECORD_COOLDOWN_S
    if ev is None:
      return
    if discard:
      self.pending_alerts.pop(ev["id"], None)
      alert_msg_id = load_tg_state(ev["id"]).get("alert_msg_id")
      storage.delete_event(ev["id"])
      if alert_msg_id:
        self.telegram.notify_discard(ev["id"], alert_msg_id)
      cloudlog.event("sentinel: event discarded, car started", event_id=ev["id"])
      return
    ev["duration_s"] = round(time.monotonic() - self.record_started + ev.get("prerecord_s", 0.0), 1)
    if first is not None:
      ev["trigger_at_s"] = round(max(0.0, self.trigger_mono - first), 1)
    ev["frames"] = frames
    if frames.get("qcamera.h264"):
      ev["duration_s"] = round(frames["qcamera.h264"] / 20.0, 1)
    ev["status"] = "pending_export" if any(frames.values()) else "failed"
    storage.save_event(ev)

  # ── export (background) ────────────────────────────────────
  def _maybe_export(self) -> None:
    if self.export_thread is not None and self.export_thread.is_alive():
      return
    if self.state == State.RECORDING or self._onroad():
      return
    pending = [e for e in storage.list_events() if e.get("status") == "pending_export"]
    if pending:
      self.export_thread = threading.Thread(target=self._export, args=(pending[-1],), daemon=True)
      self.export_thread.start()

  def _export(self, ev: dict) -> None:
    try:
      ev["status"] = "exporting"
      storage.save_event(ev)
      d = storage.sentinel_root() / ev["id"]
      errors: dict = {}
      export_event(d, thumb_at_s=ev.get("trigger_at_s", 0.0), log=cloudlog.warning, errors=errors)
      # everything playable in the folder (also cameras converted on an earlier pass)
      ev["files"] = {mp4: (d / mp4).stat().st_size for mp4, _ in OUTPUTS.values() if (d / mp4).is_file()}
      ev["export_errors"] = errors
      ev["status"] = "ready" if ev["files"] else "failed"
    except Exception:
      cloudlog.exception("sentinel export failed")
      ev["status"] = "failed"
    storage.save_event(ev)
    if ev["status"] == "ready":
      self.telegram.notify_ready(ev["id"])
    storage.enforce_storage_cap(int(self.cfg["max_storage_gb"] * 1e9))

  # ── keep the manager hook present across NAP updates ──────
  def _check_hook(self, now: float) -> None:
    if now - self.last_hook_check < HOOK_CHECK_S:
      return
    self.last_hook_check = now
    try:
      from nap_sentinel.install_hook import ensure_all
      ensure_all()
    except Exception:
      cloudlog.exception("sentinel: hook upkeep failed")

  # ── status for the web UI ──────────────────────────────────
  def _write_status(self, now: float, onroad: bool) -> None:
    if now - self.last_status_write < 0.5:
      return
    self.last_status_write = now
    ds = self.sm['deviceState']
    status = {
      "t": time.time(),  # noqa: TID251
      "state": self.state,
      "onroad": onroad,
      "arming_left_s": max(0, round(self.cfg["arm_delay_s"] - (now - self.state_t))) if self.state == State.ARMING else 0,
      "recording_left_s": max(0, round(self.record_until - now)) if self.state == State.RECORDING else 0,
      "event_id": self.event["id"] if self.event else None,
      "detector_ready": self.detector.ready,
      "impact_threshold_g": self.detector.impact_g,
      "live_impact_g": round(self.detector.last_impact_g, 4),
      "live_tilt_deg": round(self.detector.last_tilt_deg, 3),
      "live_gyro_dps": round(self.detector.last_gyro_dps, 3),
      "cameras_on": self.recorder is not None,
      "prerecord_buffer_s": self.recorder.buffered_s() if self.recorder is not None and not self.recorder.recording() else 0,
      "last_trigger": self.last_trigger,
      "skipped_reason": self.skipped_reason,
      "voltage": self._voltage(),
      **self._power(ds),
      "thermal_status": str(ds.thermalStatus),
      "max_temp_c": round(ds.maxTempC, 1),
      "free_space_pct": round(ds.freeSpacePercent, 1),
      "network": str(ds.networkType),
      "parked_h": round((now - self.offroad_since) / 3600, 2) if self.offroad_since else 0,
    }
    try:
      config.atomic_write_json(config.STATUS_FILE, status)
    except Exception:
      cloudlog.exception("sentinel: status write failed")

  # ── main loop ──────────────────────────────────────────────
  def step(self) -> None:
    now = time.monotonic()
    self.sm.update(0)
    if now - self.cfg_t > 1.0:
      self.cfg = config.load()
      self.cfg_t = now
    accel = messaging.drain_sock(self.accel_sock)
    gyro = messaging.drain_sock(self.gyro_sock)
    onroad = self._onroad()
    enabled = self.cfg["enabled"]
    self._manage_power(now, onroad)

    if onroad or not enabled:
      if self.state == State.RECORDING:
        self._finish_recording(discard=onroad and self.cfg["discard_on_drive"])
      self._set_state(State.OFF)
      self.detector.reset()
    elif self.state == State.OFF:
      self._set_state(State.ARMING)
    elif self.state == State.ARMING and now - self.state_t >= self.cfg["arm_delay_s"]:
      self.detector = MotionDetector(sensitivity=self.cfg["sensitivity"], tilt_deg=self.cfg["tilt_deg"])
      self._set_state(State.ARMED)

    if self.state in (State.ARMED, State.RECORDING):
      self.detector.set_sensitivity(self.cfg["sensitivity"])
      self.detector.tilt_deg = self.cfg["tilt_deg"]
      trig: Trigger | None = None
      for m in accel:
        a = m.accelerometer
        if a.which() == "acceleration" and len(a.acceleration.v) == 3:
          trig = self.detector.update_accel(list(a.acceleration.v), a.timestamp * 1e-9) or trig
      for m in gyro:
        g = m.gyroscope
        if g.which() == "gyroUncalibrated" and len(g.gyroUncalibrated.v) == 3:
          trig = self.detector.update_gyro(list(g.gyroUncalibrated.v), g.timestamp * 1e-9) or trig
      if trig is not None and (self.state == State.RECORDING or now >= self.cooldown_until):
        self._start_or_extend(trig, now)

    # "record now" from the web UI
    if config.TRIGGER_FILE.exists():
      config.TRIGGER_FILE.unlink(missing_ok=True)
      if self.state in (State.ARMING, State.ARMED, State.RECORDING):
        self._start_or_extend(Trigger("manual", 0.0, now), now)

    cameras = self._cameras_wanted()
    sensors = self.state in (State.ARMED, State.RECORDING)
    config.write_procs(sensors, cameras, cameras and self._want_lq_stream())
    self._update_recorder(cameras)

    if self.state == State.RECORDING:
      self._step_recording(now)
      if now >= self.record_until:
        self._finish_recording()
        self._set_state(State.ARMED)

    for eid, due in list(self.pending_alerts.items()):
      if now >= due and not self._onroad():
        self.pending_alerts.pop(eid)
        if storage.load_event(eid) is not None:
          self.telegram.notify_trigger(eid)

    self._maybe_export()
    self._check_hook(now)
    self._write_status(now, onroad)


def main() -> None:
  s = Sentinel()
  rk = Ratekeeper(LOOP_HZ, print_delay_threshold=None)
  while True:
    try:
      s.step()
    except Exception:
      cloudlog.exception("sentineld step failed")
      time.sleep(1)
    rk.keep_time()


if __name__ == "__main__":
  main()
