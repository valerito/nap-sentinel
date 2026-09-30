import asyncio
import base64

import pytest
from aiohttp.test_utils import TestClient, TestServer

from nap_sentinel import config, storage, webd


@pytest.fixture
def env(tmp_path, monkeypatch):
  monkeypatch.setenv("SENTINEL_ROOT", str(tmp_path / "events"))
  monkeypatch.setenv("SENTINEL_CONFIG", str(tmp_path / "config.json"))
  monkeypatch.setattr(config, "TRIGGER_FILE", tmp_path / "trigger")
  monkeypatch.setattr(config, "STATUS_FILE", tmp_path / "status.json")
  return tmp_path


def _run(coro):
  asyncio.run(coro)


def test_config_record_and_auth(env):
  async def go():
    async with TestClient(TestServer(webd.make_app())) as c:
      assert (await c.post("/api/record")).status == 409            # disabled
      r = await c.post("/api/config", json={"enabled": True, "prerecord": True, "prerecord_s": 99})
      cfg = await r.json()
      assert cfg["enabled"] and cfg["prerecord"] and cfg["prerecord_s"] == 30
      assert (await c.post("/api/record")).status == 200
      assert config.TRIGGER_FILE.exists()
      await c.post("/api/config", json={"password": "pw"})
      assert (await c.get("/api/status")).status == 401
      hdr = {"Authorization": "Basic " + base64.b64encode(b"x:pw").decode()}
      r = await c.get("/api/status", headers=hdr)
      assert r.status == 200 and "web_password" not in (await r.json())["config"]
  _run(go())


def test_media_whitelist_and_delete(env):
  storage.save_event({"id": "20260101-000000", "wall_time": 1, "status": "ready", "files": {"road.mp4": 3}})
  d = storage.sentinel_root() / "20260101-000000"
  (d / "road.mp4").write_bytes(b"abc")

  async def go():
    async with TestClient(TestServer(webd.make_app())) as c:
      assert (await c.get("/media/20260101-000000/road.mp4")).status == 200
      assert (await c.get("/media/20260101-000000/event.json")).status == 404
      assert (await c.get("/media/..%2F..%2Fetc/passwd")).status == 404
      assert (await c.post("/api/events/20260101-000000/delete")).status == 200
      assert storage.list_events() == []
  _run(go())


def test_storage_cap_keeps_locked(env):
  for i, locked in enumerate([True, False, False]):
    eid = f"2026010{i + 1}-000000"
    storage.save_event({"id": eid, "wall_time": i, "status": "ready", "locked": locked})
    (storage.sentinel_root() / eid / "road.mp4").write_bytes(b"x" * 1000)
  assert storage.enforce_storage_cap(2500) == ["20260102-000000"]
