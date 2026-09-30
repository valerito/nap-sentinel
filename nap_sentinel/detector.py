"""Motion detector for sentinel mode.

Pure-python, no openpilot imports, so it can be unit tested on a PC and
replayed against logged IMU data.

Three independent triggers:
  * impact: high-pass filtered acceleration magnitude (bumps, door slams,
    someone leaning on / hitting the car, glass breaking).
  * tilt:   slow change of the gravity vector compared to the reference taken
    when the detector armed (jacking the car up, towing, wheel theft).
  * rotate: angular rate from the gyroscope (car rocking).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

G = 9.81

# sensitivity level (1 = least sensitive, 5 = most sensitive)
#   -> (impact threshold [g], gyro threshold [deg/s])
SENSITIVITY_LEVELS: dict[int, tuple[float, float]] = {
  1: (0.30, 12.0),
  2: (0.18, 8.0),
  3: (0.10, 5.0),
  4: (0.06, 3.0),
  5: (0.035, 2.0),
}
DEFAULT_SENSITIVITY = 3


@dataclass
class Trigger:
  reason: str       # "impact" | "tilt" | "rotate" | "manual"
  magnitude: float  # g for impact, degrees for tilt, deg/s for rotate
  t: float          # monotonic seconds


def _norm(v) -> float:
  return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def _angle_deg(a, b) -> float:
  na, nb = _norm(a), _norm(b)
  if na < 1e-6 or nb < 1e-6:
    return 0.0
  c = (a[0] * b[0] + a[1] * b[1] + a[2] * b[2]) / (na * nb)
  return math.degrees(math.acos(max(-1.0, min(1.0, c))))


class _LowPass3:
  def __init__(self, tau: float):
    self.tau = tau
    self.x: list[float] | None = None
    self.t: float | None = None

  def update(self, v, t: float) -> list[float]:
    if self.x is None or self.t is None:
      self.x = [float(v[0]), float(v[1]), float(v[2])]
    else:
      dt = max(0.0, min(t - self.t, 1.0))
      k = dt / (self.tau + dt) if self.tau > 0 else 1.0
      self.x = [self.x[i] + k * (float(v[i]) - self.x[i]) for i in range(3)]
    self.t = t
    return self.x


class MotionDetector:
  def __init__(self, sensitivity: int = DEFAULT_SENSITIVITY, tilt_deg: float = 1.5,
               warmup_s: float = 5.0, retrigger_s: float = 2.0):
    self.set_sensitivity(sensitivity)
    self.tilt_deg = tilt_deg
    self.warmup_s = warmup_s
    self.retrigger_s = retrigger_s
    self.reset()

  def set_sensitivity(self, level: int) -> None:
    level = int(level) if level in SENSITIVITY_LEVELS else DEFAULT_SENSITIVITY
    self.sensitivity = level
    self.impact_g, self.gyro_dps = SENSITIVITY_LEVELS[level]

  def reset(self) -> None:
    self._baseline = _LowPass3(tau=1.0)      # removes gravity -> high pass
    self._gravity = _LowPass3(tau=4.0)       # slow gravity estimate for tilt
    self._gyro_bias = _LowPass3(tau=20.0)    # uncalibrated gyro -> remove bias
    self._tilt_ref: list[float] | None = None
    self._t0: float | None = None
    self._last_trigger_t = -1e9
    # live values for the UI
    self.last_impact_g = 0.0
    self.last_tilt_deg = 0.0
    self.last_gyro_dps = 0.0

  @property
  def ready(self) -> bool:
    return self._tilt_ref is not None

  def _can_trigger(self, t: float) -> bool:
    return self.ready and (t - self._last_trigger_t) >= self.retrigger_s

  def update_accel(self, a, t: float) -> Trigger | None:
    """a: acceleration in m/s^2 (x, y, z). t: monotonic seconds."""
    if self._t0 is None:
      self._t0 = t
    base = self._baseline.update(a, t)
    grav = self._gravity.update(a, t)

    if self._tilt_ref is None:
      if t - self._t0 >= self.warmup_s:
        self._tilt_ref = list(grav)
      return None

    hp = (a[0] - base[0], a[1] - base[1], a[2] - base[2])
    impact_g = _norm(hp) / G
    tilt = _angle_deg(grav, self._tilt_ref)
    self.last_impact_g = impact_g
    self.last_tilt_deg = tilt

    if not self._can_trigger(t):
      return None
    if impact_g >= self.impact_g:
      self._last_trigger_t = t
      return Trigger("impact", impact_g, t)
    if tilt >= self.tilt_deg:
      self._last_trigger_t = t
      # re-reference so a car that stays tilted doesn't fire forever;
      # a car being towed keeps changing attitude and keeps firing.
      self._tilt_ref = list(grav)
      return Trigger("tilt", tilt, t)
    return None

  def update_gyro(self, w, t: float) -> Trigger | None:
    """w: angular rate in rad/s (x, y, z). Uses the uncalibrated gyro, so the
    slowly varying bias is estimated and removed."""
    bias = self._gyro_bias.update(w, t)
    dps = math.degrees(_norm((w[0] - bias[0], w[1] - bias[1], w[2] - bias[2])))
    self.last_gyro_dps = dps
    if self._can_trigger(t) and dps >= self.gyro_dps:
      self._last_trigger_t = t
      return Trigger("rotate", dps, t)
    return None
