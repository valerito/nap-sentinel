"""Remux the raw streams of an event into browser-playable MP4 (no re-encode).

  qcamera.h264 -> road.mp4     H.264 526x330, plays in any browser
  fcamera.hevc -> fcamera.mp4  HEVC road camera
  ecamera.hevc -> ecamera.mp4  HEVC wide road camera
  dcamera.hevc -> dcamera.mp4  HEVC cabin (IR) camera
  wide_lq.h264 -> wide_lq.mp4  H.264 ~1 Mbps wide camera (sent to Telegram)

Each raw file has a "<file>.ts" sidecar with the encoder timestamp of every
frame, used to rebuild the real timing. PyAV is an openpilot dependency
(webrtcd); the ffmpeg CLI is a fallback if present.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

CAMERA_FPS = 20

# raw file -> (mp4 name, demuxer format)
OUTPUTS = {
  "qcamera.h264": ("road.mp4", "h264"),
  "fcamera.hevc": ("fcamera.mp4", "hevc"),
  "ecamera.hevc": ("ecamera.mp4", "hevc"),
  "dcamera.hevc": ("dcamera.mp4", "hevc"),
  "wide_lq.h264": ("wide_lq.mp4", "h264"),
}


def _timestamps(raw: Path) -> list[int]:
  try:
    with open(str(raw) + ".ts") as f:
      return [int(x) for x in f.read().split()]
  except (OSError, ValueError):
    return []


def _remux_pyav(raw: Path, out_path: Path, fmt: str) -> None:
  import av

  ts = _timestamps(raw)
  with av.open(str(raw), format=fmt) as inp:
    packets = [p for p in inp.demux(inp.streams.video[0]) if p.size > 0]
    use_ts = len(ts) == len(packets) and len(ts) > 1 and all(b > a for a, b in zip(ts, ts[1:], strict=False))
    out = av.open(str(out_path), "w", format="mp4", options={"movflags": "+faststart"})
    try:
      ost = out.add_stream_from_template(inp.streams.video[0])
      if fmt == "hevc":
        ost.codec_tag = "hvc1"  # Safari / iOS
      tb = Fraction(1, 1000)
      for i, pkt in enumerate(packets):
        pts = (ts[i] - ts[0]) // 1_000_000 if use_ts else i * (1000 // CAMERA_FPS)
        dur = ((ts[i + 1] - ts[i]) // 1_000_000 if use_ts and i + 1 < len(ts) else 1000 // CAMERA_FPS)
        pkt.time_base = tb
        pkt.pts = pkt.dts = pts
        pkt.duration = max(1, dur)
        pkt.stream = ost
        out.mux(pkt)
    finally:
      out.close()
  if not packets:
    raise ValueError("empty stream")


def _remux_ffmpeg(raw: Path, out_path: Path, fmt: str) -> None:
  ffmpeg = shutil.which("ffmpeg")
  if ffmpeg is None:
    raise RuntimeError("ffmpeg not available")
  cmd = [ffmpeg, "-y", "-loglevel", "error", "-r", str(CAMERA_FPS), "-f", fmt, "-i", str(raw), "-c", "copy"]
  if fmt == "hevc":
    cmd += ["-tag:v", "hvc1"]
  cmd += ["-movflags", "+faststart", str(out_path)]
  subprocess.run(cmd, check=True, timeout=600)


def remux(raw: Path, out_path: Path, fmt: str) -> None:
  tmp = out_path.with_name(".tmp-" + out_path.name)
  try:
    try:
      _remux_pyav(raw, tmp, fmt)
    except Exception:
      tmp.unlink(missing_ok=True)
      _remux_ffmpeg(raw, tmp, fmt)
    os.replace(tmp, out_path)
  finally:
    tmp.unlink(missing_ok=True)


def make_thumbnail(video: Path, out_jpg: Path, at_s: float = 0.0) -> bool:
  try:
    import av
    with av.open(str(video)) as c:
      s = c.streams.video[0]
      for frame in c.decode(s):
        if frame.time is not None and frame.time + 1e-3 < at_s:
          continue
        frame.to_image().save(str(out_jpg), quality=80)  # needs pillow
        return True
  except Exception:
    return False
  return False


def export_event(event_dir: Path, thumb_at_s: float = 0.0, keep_raw: bool = False, log=print,
                 errors: dict | None = None) -> dict[str, int]:
  """Returns {mp4 name: size}. Raw files are removed once converted.
  Per-stream problems are stored in `errors` ({raw name: reason})."""
  produced: dict[str, int] = {}
  errors = errors if errors is not None else {}
  for raw_name, (mp4, fmt) in OUTPUTS.items():
    raw = event_dir / raw_name
    if not raw.is_file():
      continue
    if raw.stat().st_size == 0:  # stream never delivered a keyframe
      errors[raw_name] = "sin fotogramas (el codificador no envió ningún fotograma clave)"
      raw.unlink(missing_ok=True)
      Path(str(raw) + ".ts").unlink(missing_ok=True)
      continue
    try:
      remux(raw, event_dir / mp4, fmt)
      produced[mp4] = (event_dir / mp4).stat().st_size
      if not keep_raw:
        raw.unlink(missing_ok=True)
        Path(str(raw) + ".ts").unlink(missing_ok=True)
    except Exception as e:  # keep going with the other cameras
      errors[raw_name] = f"{type(e).__name__}: {e}"[:300]
      log(f"sentinel export {raw_name} failed: {e}")
  src = "road.mp4" if "road.mp4" in produced else next(iter(produced), None)
  if src:
    make_thumbnail(event_dir / src, event_dir / "thumb.jpg", thumb_at_s)
  return produced
