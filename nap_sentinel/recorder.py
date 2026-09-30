"""Records the camera streams that encoderd already publishes.

encoderd publishes every encoded frame on msgq (roadEncodeData, ...). Instead
of starting loggerd, sentinel subscribes to those messages and writes the raw
H.264/HEVC bitstreams itself. That allows a RAM ring buffer ("pre-recording"):
with the cameras kept on, the last N seconds are always in memory and get
written out when an event fires, so the clip starts *before* the impact.

The pure-python parts (RingBuffer, StreamWriter) have no openpilot imports so
they can be unit tested on a PC.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

V4L2_BUF_FLAG_KEYFRAME = 0x8

# service -> output raw file
STREAMS = {
  "qRoadEncodeData": "qcamera.h264",
  "roadEncodeData": "fcamera.hevc",
  "wideRoadEncodeData": "ecamera.hevc",
  "driverEncodeData": "dcamera.hevc",
  # low-bitrate H.264 of the wide camera from stream_encoderd, for Telegram
  "livestreamWideRoadEncodeData": "wide_lq.h264",
}


@dataclass
class Packet:
  ts_ns: int          # encoder timestampEof
  recv: float         # time.monotonic() when received
  keyframe: bool
  header: bytes       # codec config (SPS/PPS/VPS), usually only on keyframes
  data: bytes


class RingBuffer:
  """Keeps at least `seconds` of packets, always starting at a keyframe."""
  def __init__(self, seconds: float):
    self.seconds = seconds
    self.q: deque[Packet] = deque()

  def push(self, p: Packet) -> None:
    if not self.q and not p.keyframe:
      return  # a buffer must start at a keyframe
    self.q.append(p)
    self._trim()

  def _trim(self) -> None:
    newest = self.q[-1].ts_ns
    limit = self.seconds * 1e9
    while True:
      # find the next keyframe after the first one; drop the first GOP if the
      # remaining buffer still covers the requested duration
      nxt = next((i for i, p in enumerate(self.q) if i > 0 and p.keyframe), None)
      if nxt is None or newest - self.q[nxt].ts_ns < limit:
        return
      for _ in range(nxt):
        self.q.popleft()

  def duration(self) -> float:
    return (self.q[-1].ts_ns - self.q[0].ts_ns) / 1e9 if len(self.q) > 1 else 0.0

  def drain(self) -> list[Packet]:
    out = list(self.q)
    self.q.clear()
    return out


class StreamWriter:
  """Writes an Annex-B raw stream plus a sidecar with one timestamp per frame
  (so the exporter can rebuild real timing, including dropped frames)."""
  def __init__(self, path: Path):
    self.path = path
    self.f = open(path, "wb")
    self.ts = open(str(path) + ".ts", "w")
    self.started = False
    self.header = b""
    self.frames = 0
    self.first_recv: float | None = None

  def write(self, p: Packet) -> None:
    if p.header:
      self.header = p.header
    if not self.started:
      if not p.keyframe:
        return
      self.started = True
      self.first_recv = p.recv
    if p.keyframe and self.header:
      self.f.write(self.header)
    self.f.write(p.data)
    self.ts.write(f"{p.ts_ns}\n")
    self.frames += 1

  def close(self) -> None:
    self.f.close()
    self.ts.close()


class Recorder:
  """Owns the msgq subscriptions (only while cameras are on)."""
  def __init__(self, services: list[str], prerecord_s: float):
    import cereal.messaging as messaging
    self._messaging = messaging
    self.services = services
    self.socks = {s: messaging.sub_sock(s, conflate=False) for s in services}
    self.buffers = {s: RingBuffer(prerecord_s) for s in services}
    self.headers: dict[str, bytes] = {}
    self.writers: dict[str, StreamWriter] = {}
    self.last_packet = time.monotonic()

  def set_prerecord(self, seconds: float) -> None:
    for b in self.buffers.values():
      b.seconds = seconds

  def buffered_s(self) -> float:
    b = self.buffers.get("qRoadEncodeData") or next(iter(self.buffers.values()), None)
    return round(b.duration(), 1) if b else 0.0

  def poll(self) -> None:
    now = time.monotonic()
    for s, sock in self.socks.items():
      for m in self._messaging.drain_sock(sock):
        e = getattr(m, s)
        hdr = bytes(e.header)
        if hdr:
          self.headers[s] = hdr
        p = Packet(ts_ns=e.idx.timestampEof, recv=now,
                   keyframe=bool(e.idx.flags & V4L2_BUF_FLAG_KEYFRAME),
                   header=hdr or (self.headers.get(s, b"") if e.idx.flags & V4L2_BUF_FLAG_KEYFRAME else b""),
                   data=bytes(e.data))
        self.last_packet = now
        w = self.writers.get(s)
        if w is not None:
          w.write(p)
        else:
          self.buffers[s].push(p)

  def start(self, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for s in self.services:
      w = StreamWriter(out_dir / STREAMS[s])
      for p in self.buffers[s].drain():   # pre-recorded seconds first
        w.write(p)
      self.writers[s] = w

  def recording(self) -> bool:
    return bool(self.writers)

  def first_recv(self) -> float | None:
    w = self.writers.get("qRoadEncodeData") or next(iter(self.writers.values()), None)
    return w.first_recv if w else None

  def stop(self) -> dict[str, int]:
    frames = {}
    for s, w in self.writers.items():
      w.close()
      frames[STREAMS[s]] = w.frames
    self.writers = {}
    return frames

  def close(self) -> None:
    if self.writers:
      self.stop()
    self.socks = {}
