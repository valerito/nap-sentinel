import math
import random

from nap_sentinel.detector import G, MotionDetector

HZ = 104.0


def _run(det, seconds, accel_fn, t0=0.0, gyro_fn=None):
  trig = []
  n = int(seconds * HZ)
  for i in range(n):
    t = t0 + i / HZ
    r = det.update_accel(accel_fn(t), t)
    if r:
      trig.append(r)
    if gyro_fn is not None:
      r = det.update_gyro(gyro_fn(t), t)
      if r:
        trig.append(r)
  return trig, t0 + n / HZ


def _still(noise_g=0.003, gravity=(0.3, 0.2, G)):
  rng = random.Random(0)
  return lambda t: [gravity[i] + rng.gauss(0, noise_g * G) for i in range(3)]


def test_quiet_car_never_triggers():
  for level in range(1, 6):
    det = MotionDetector(sensitivity=level)
    trig, _ = _run(det, 120, _still(), gyro_fn=lambda t: [0.001, -0.002, 0.0015])
    assert trig == [], level


def test_bump_triggers_impact():
  det = MotionDetector(sensitivity=3)
  still = _still()
  _, t = _run(det, 10, still)
  assert det.ready

  def bump(tt):
    a = still(tt)
    dt = tt - t
    if 0 <= dt < 0.3:  # damped oscillation ~0.25 g peak
      a[0] += 0.25 * G * math.exp(-dt * 12) * math.sin(2 * math.pi * 8 * dt)
    return a
  trig, _ = _run(det, 2, bump, t0=t)
  assert trig and trig[0].reason == "impact"
  assert trig[0].magnitude > 0.1


def test_small_bump_ignored_at_low_sensitivity():
  det = MotionDetector(sensitivity=1)
  still = _still()
  _, t = _run(det, 10, still)

  def bump(tt):
    a = still(tt)
    dt = tt - t
    if 0 <= dt < 0.3:
      a[2] += 0.12 * G * math.exp(-dt * 12) * math.sin(2 * math.pi * 8 * dt)
    return a
  trig, _ = _run(det, 2, bump, t0=t)
  assert trig == []


def test_jacking_up_triggers_tilt():
  det = MotionDetector(sensitivity=1, tilt_deg=1.5)
  still = _still(noise_g=0.001)
  _, t = _run(det, 10, still)

  def jack(tt):
    ang = math.radians(min(3.0, (tt - t) * 0.1))  # 0.1 deg/s, too slow for impact
    return [G * math.sin(ang), 0.0, G * math.cos(ang)]
  trig, _ = _run(det, 40, jack, t0=t)
  assert any(tr.reason == "tilt" for tr in trig)
  assert not any(tr.reason == "impact" for tr in trig)


def test_gyro_bias_is_ignored_but_rocking_triggers():
  det = MotionDetector(sensitivity=3)
  bias = [0.02, -0.015, 0.01]  # ~1.6 deg/s uncalibrated bias
  trig, t = _run(det, 60, _still(), gyro_fn=lambda tt: bias)
  assert trig == []

  def rock(tt):
    w = math.radians(10) * math.sin(2 * math.pi * 1.5 * (tt - t))
    return [bias[0] + w, bias[1], bias[2]]
  trig, _ = _run(det, 2, _still(), t0=t, gyro_fn=rock)
  assert any(tr.reason == "rotate" for tr in trig)


def test_retrigger_holdoff():
  det = MotionDetector(sensitivity=5, retrigger_s=2.0)
  still = _still(noise_g=0.001)
  _, t = _run(det, 10, still)

  def shaking(tt):
    a = still(tt)
    a[0] += 0.3 * G * math.sin(2 * math.pi * 10 * (tt - t))
    return a
  trig, _ = _run(det, 5, shaking, t0=t)
  assert 2 <= len(trig) <= 3
