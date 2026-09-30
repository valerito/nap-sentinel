from fractions import Fraction

import pytest

from nap_sentinel.recorder import Packet, RingBuffer, StreamWriter

av = pytest.importorskip("av")
np = pytest.importorskip("numpy")

FPS = 20
GOP = 20


def _encode(codec: str, n: int, w=160, h=96) -> list[tuple[bool, bytes]]:
  enc = av.CodecContext.create(codec, "w")
  enc.width, enc.height, enc.pix_fmt = w, h, "yuv420p"
  enc.time_base = Fraction(1, FPS)
  enc.gop_size = GOP
  enc.max_b_frames = 0
  if codec == "libx265":
    enc.options = {"x265-params": "log-level=none:bframes=0:keyint=20:min-keyint=20:scenecut=0"}
  else:
    enc.options = {"bf": "0", "g": str(GOP), "keyint_min": str(GOP), "sc_threshold": "0"}
  out = []
  for i in range(n):
    img = np.full((h, w, 3), (i * 7) % 255, dtype=np.uint8)
    fr = av.VideoFrame.from_ndarray(img, format="rgb24").reformat(format="yuv420p")
    fr.pts = i
    for p in enc.encode(fr):
      out.append((bool(p.is_keyframe), bytes(p)))
  for p in enc.encode(None):
    out.append((bool(p.is_keyframe), bytes(p)))
  return out


def _packets(codec, n, t0_ns=1_000_000_000, gap_after=None):
  pk, t = [], t0_ns
  for i, (kf, data) in enumerate(_encode(codec, n)):
    pk.append(Packet(ts_ns=t, recv=t / 1e9, keyframe=kf, header=b"", data=data))
    t += 50_000_000 if gap_after is None or i != gap_after else 1_000_000_000
  return pk


def test_ring_buffer_starts_at_keyframe_and_keeps_duration():
  rb = RingBuffer(3.0)
  for p in _packets("libx264", 200):
    rb.push(p)
  q = list(rb.q)
  assert q[0].keyframe
  assert 3.0 <= rb.duration() < 3.0 + GOP / FPS + 0.1


def test_ring_buffer_zero_keeps_current_gop():
  rb = RingBuffer(0)
  for p in _packets("libx264", 45):
    rb.push(p)
  assert rb.q[0].keyframe and len(rb.q) <= GOP


@pytest.mark.parametrize("codec,raw,mp4", [("libx264", "qcamera.h264", "road.mp4"), ("libx265", "fcamera.hevc", "fcamera.mp4")])
def test_prerecord_then_live_export(tmp_path, monkeypatch, codec, raw, mp4):
  from nap_sentinel import exporter
  from nap_sentinel.exporter import export_event
  monkeypatch.setattr(exporter, "_ffmpeg_bin", lambda: None)   # PyAV path only, like on a comma without h264 in ffmpeg
  pk = _packets(codec, 200, gap_after=150)   # 1 s dropout in the live part
  rb = RingBuffer(2.0)
  for p in pk[:100]:
    rb.push(p)
  w = StreamWriter(tmp_path / raw)
  pre = rb.drain()
  for p in pre + pk[100:]:
    w.write(p)
  w.close()
  files = export_event(tmp_path)
  assert mp4 in files and (tmp_path / "thumb.jpg").exists()
  assert not (tmp_path / raw).exists()
  with av.open(str(tmp_path / mp4)) as c:
    s = c.streams.video[0]
    n = sum(1 for _ in c.demux(s) if _.size)
    assert n == w.frames == len(pre) + 100
    dur = float(s.duration * s.time_base)
    expected = (len(pre) + 100 - 1) * 0.05 + 0.95 + 0.05
    assert abs(dur - expected) < 0.15
