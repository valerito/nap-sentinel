import asyncio
import time

import pytest

from nap_sentinel import config, storage, telegram


class FakeAPI:
  def __init__(self):
    self.calls = []
    self.fail = None
    self.next_id = 100

  def __call__(self, token, method, data=None, files=None, timeout=20):
    if self.fail:
      raise self.fail
    self.calls.append((method, dict(data or {}), sorted((files or {}).keys())))
    self.next_id += 1
    return {"message_id": self.next_id, "username": "mi_coche_bot"} if method in ("sendMessage", "sendVideo", "getMe") else True

  def methods(self):
    return [c[0] for c in self.calls]


@pytest.fixture
def env(tmp_path, monkeypatch):
  monkeypatch.setenv("SENTINEL_ROOT", str(tmp_path / "events"))
  monkeypatch.setenv("SENTINEL_CONFIG", str(tmp_path / "config.json"))
  monkeypatch.setattr(config, "TRIGGER_FILE", tmp_path / "trigger")
  monkeypatch.setattr(config, "STATUS_FILE", tmp_path / "status.json")
  monkeypatch.setattr(config, "INSTALL_DIR", tmp_path)
  fake = FakeAPI()
  monkeypatch.setattr(telegram, "api", fake)
  config.atomic_write_json(config.STATUS_FILE, {"t": time.time(), "state": "armed", "voltage": 12.61, "network": "wifi"})
  return fake


def _linked():
  config.update({"enabled": True, "telegram_token": "1:abc", "telegram_bot": "mi_coche_bot", "telegram_chat_id": "42"})


def _event(eid="20260930-120000", with_video=True, size=30_000):
  storage.save_event({"id": eid, "wall_time": time.time(), "status": "ready", "reason": "impact", "peak": 0.23,
                      "triggers": [{"reason": "impact", "magnitude": 0.23}], "duration_s": 55, "prerecord_s": 10})
  if with_video:
    (storage.sentinel_root() / eid / "wide_lq.mp4").write_bytes(b"x" * size)
  return eid


def _msg(chat_id, text):
  return {"update_id": 1, "message": {"chat": {"id": int(chat_id), "first_name": "Samuel"}, "text": text}}


def test_link_with_valid_code_only(env):
  config.update({"telegram_token": "1:abc", "telegram_link_code": "CODE123", "telegram_link_expires": time.time() + 60})
  svc = telegram.TelegramService()
  svc.handle_update(config.load(), _msg(7, "/start WRONG"))
  assert config.load()["telegram_chat_id"] == ""
  svc.handle_update(config.load(), _msg(7, "/start CODE123"))
  c = config.load()
  assert c["telegram_chat_id"] == "7" and c["telegram_chat_name"] == "Samuel" and c["telegram_link_code"] == ""
  assert "sendMessage" in env.methods() and "setMyCommands" in env.methods()


def test_expired_code_rejected(env):
  config.update({"telegram_token": "1:abc", "telegram_link_code": "C", "telegram_link_expires": time.time() - 1})
  telegram.TelegramService().handle_update(config.load(), _msg(7, "/start C"))
  assert config.load()["telegram_chat_id"] == ""


def test_commands_only_from_linked_chat(env):
  _linked()
  svc = telegram.TelegramService()
  svc.handle_update(config.load(), _msg(99, "/desactivar"))
  assert config.load()["enabled"] is True and env.calls == []
  svc.handle_update(config.load(), _msg(42, "/grabar"))
  assert config.TRIGGER_FILE.exists()
  svc.handle_update(config.load(), _msg(42, "/desactivar"))
  assert config.load()["enabled"] is False
  svc.handle_update(config.load(), _msg(42, "/estado"))
  assert "Sentinel" in env.calls[-1][1]["text"]


def test_alert_then_video_as_reply(env):
  _linked()
  eid = _event()
  svc = telegram.TelegramService()
  svc.handle("alert", eid)
  alert_id = telegram.load_tg_state(eid)["alert_msg_id"]
  assert "Golpe" in env.calls[0][1]["text"] and "0.23 g" in env.calls[0][1]["text"]
  svc.handle("video", eid)
  method, data, files = env.calls[-1]
  assert method == "sendVideo" and data["reply_to_message_id"] == alert_id and "video" in files
  assert "gran angular" in data["caption"] and "10 s antes" in data["caption"]
  svc.handle("video", eid)                       # not sent twice
  assert env.methods().count("sendVideo") == 1


def test_video_too_big_sends_note(env, monkeypatch):
  _linked()
  monkeypatch.setattr(telegram, "MAX_VIDEO_BYTES", 25_000)
  eid = _event(size=30_000)
  telegram.TelegramService().handle("video", eid)
  assert env.calls[-1][0] == "sendMessage" and "50 MB" in env.calls[-1][1]["text"]


def test_video_retry_and_wifi_only(env):
  _linked()
  eid = _event()
  svc = telegram.TelegramService()
  env.fail = OSError("no network")
  svc.handle("video", eid)
  st = telegram.load_tg_state(eid)
  assert not st.get("video_sent") and st["video_tries"] == 1 and eid in svc._retry_at
  env.fail = None
  config.update({"telegram_video_wifi_only": True})
  config.atomic_write_json(config.STATUS_FILE, {"network": "cell"})
  svc.handle("video", eid)
  assert telegram.load_tg_state(eid).get("video_waiting_wifi") and "sendVideo" not in env.methods()
  config.atomic_write_json(config.STATUS_FILE, {"network": "wifi"})
  svc._retry_at.clear()
  svc._last_scan = -1e9
  svc._scan_pending()
  assert telegram.load_tg_state(eid).get("video_sent")


def test_disabled_or_unlinked_sends_nothing(env):
  eid = _event()
  svc = telegram.TelegramService()
  svc.handle("alert", eid)
  config.update({"telegram_token": "1:abc", "telegram_chat_id": "42", "telegram_alerts": False, "telegram_video": False})
  svc.handle("alert", eid)
  svc.handle("video", eid)
  assert env.calls == []


def test_web_telegram_flow(env, monkeypatch):
  from aiohttp.test_utils import TestClient, TestServer
  from nap_sentinel import webd
  monkeypatch.setattr(telegram, "get_me", lambda token: {"username": "mi_coche_bot"})

  async def go():
    async with TestClient(TestServer(webd.make_app())) as c:
      assert (await c.post("/api/telegram/token", json={"token": "bad"})).status == 400
      r = await (await c.post("/api/telegram/token", json={"token": "123:ABC"})).json()
      assert r["telegram_token_set"] and r["telegram_bot"] == "mi_coche_bot" and "telegram_token" not in r
      link = await (await c.post("/api/telegram/link")).json()
      assert link["url"] == f"https://t.me/mi_coche_bot?start={link['code']}"
      # identity can't be forced through the generic config endpoint
      await c.post("/api/config", json={"telegram_chat_id": "666", "telegram_video": False})
      cfg = config.load()
      assert cfg["telegram_chat_id"] == "" and cfg["telegram_video"] is False
      assert (await c.post("/api/telegram/test")).status == 409
      config.update({"telegram_chat_id": "42"})
      assert (await c.post("/api/telegram/test")).status == 200
      r = await (await c.post("/api/telegram/unlink", json={})).json()
      assert not r["telegram_linked"] and r["telegram_token_set"]
  asyncio.run(go())


class Reject400(FakeAPI):
  """Rejects the first N sendVideo attempts with a 400, like Telegram does for a bad thumbnail."""
  def __init__(self, reject=1, method="sendVideo"):
    super().__init__()
    self.reject, self.method = reject, method

  def __call__(self, token, method, data=None, files=None, timeout=20):
    if method == self.method and self.reject > 0:
      self.reject -= 1
      self.calls.append((method + "!rejected", dict(data or {}), sorted((files or {}).keys())))
      raise telegram.TelegramError("Bad Request: wrong thumbnail", code=400)
    return super().__call__(token, method, data, files, timeout)


def _big_event(eid="20260930-130000"):
  eid = _event(eid, size=30_000)
  return eid


def test_video_falls_back_when_telegram_rejects(env, monkeypatch):
  _linked()
  fake = Reject400(reject=2)
  monkeypatch.setattr(telegram, "api", fake)
  eid = _big_event()
  out = telegram.TelegramService()._send_video(config.load(), eid)
  assert out == "enviado"
  assert [c[0] for c in fake.calls] == ["sendVideo!rejected", "sendVideo!rejected", "sendDocument"]
  assert telegram.load_tg_state(eid)["video_sent"]


def test_video_final_rejection_is_reported_not_retried(env, monkeypatch):
  _linked()
  fake = Reject400(reject=99, method="sendVideo")
  monkeypatch.setattr(telegram, "api", fake)
  real = fake.__call__

  def api(token, method, data=None, files=None, timeout=20):
    if method == "sendDocument":
      raise telegram.TelegramError("Bad Request: file is empty", code=400)
    return real(token, method, data, files, timeout)
  monkeypatch.setattr(telegram, "api", api)
  eid = _big_event()
  svc = telegram.TelegramService()
  out = svc._send_video(config.load(), eid)
  assert out.startswith("Telegram lo rechazó")
  st = telegram.load_tg_state(eid)
  assert st["video_failed"] and "file is empty" in st["video_error"]
  assert any(c[0] == "sendMessage" and "No pude enviar" in c[1]["text"] for c in fake.calls)
  assert eid not in svc._retry_at
  assert any("rechazado" in line for line in telegram.read_log())


def test_ultimo_always_answers(env, monkeypatch):
  _linked()
  svc = telegram.TelegramService()
  svc.handle_update(config.load(), _msg(42, "/ultimo"))
  assert env.calls[-1][1]["text"] == "No hay vídeos todavía."
  eid = _event(size=10)   # export too small/broken -> no usable video
  svc.handle_update(config.load(), _msg(42, "/ultimo"))
  assert "Enviando" in env.calls[-2][1]["text"] and "No se pudo enviar" in env.calls[-1][1]["text"]
  (storage.sentinel_root() / eid / "wide_lq.mp4").write_bytes(b"x" * 30_000)
  svc.handle_update(config.load(), _msg(42, "/ultimo"))
  assert env.calls[-1][0] == "sendVideo"


def test_thumbnail_is_resized_for_telegram(tmp_path):
  from PIL import Image
  Image.new("RGB", (1928, 1208), (200, 10, 10)).save(tmp_path / "t.jpg", quality=95)
  b = telegram.small_thumbnail(tmp_path / "t.jpg")
  import io
  im = Image.open(io.BytesIO(b))
  assert max(im.size) <= 320 and len(b) < 200_000
