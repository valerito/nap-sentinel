import time

import pytest

from nap_sentinel import config, tesla


class Resp:
  def __init__(self, status, body):
    self.status_code, self._b = status, body

  def json(self):
    return self._b


class FakeSession:
  """Owner API fake: rotates refresh tokens, car asleep until woken."""
  def __init__(self):
    self.calls = []
    self.awake = False
    self.valid_access = set()
    self.n = 0

  def post(self, url, data=None, timeout=None):
    self.calls.append(("AUTH", url, dict(data)))
    if data["refresh_token"] != "rt-good" and not data["refresh_token"].startswith("rt-rot"):
      return Resp(401, {"error": "invalid"})
    self.n += 1
    tok = f"at-{self.n}"
    self.valid_access.add(tok)
    return Resp(200, {"access_token": tok, "refresh_token": f"rt-rot{self.n}", "expires_in": 28800})

  def request(self, method, url, json=None, timeout=None, headers=None):
    self.calls.append((method, url))
    tok = headers["Authorization"].split()[1]
    if tok not in self.valid_access:
      return Resp(401, {})
    path = url.split(".com", 1)[1]
    if path == "/api/1/vehicles":
      return Resp(200, {"response": [{"id_s": "123", "display_name": "Sam", "vin": "5YJS", "state": "asleep"}]})
    if path.endswith("/wake_up"):
      self.awake = True
      return Resp(200, {"response": {"state": "online"}})
    if path == "/api/1/vehicles/123":
      return Resp(200, {"response": {"state": "online" if self.awake else "asleep"}})
    if path.endswith("/command/flash_lights"):
      if not self.awake:
        return Resp(408, {"error": "vehicle unavailable"})
      return Resp(200, {"response": {"result": True, "reason": ""}})
    return Resp(404, {})


@pytest.fixture
def cfg(tmp_path, monkeypatch):
  monkeypatch.setenv("SENTINEL_CONFIG", str(tmp_path / "c.json"))
  monkeypatch.setattr(config, "INSTALL_DIR", tmp_path)
  config.update({"tesla_refresh_token": "rt-good", "tesla_vehicle_id": "123", "tesla_flash": True})
  return config.load()


def test_vehicles_and_token_rotation(cfg):
  s = FakeSession()
  c = tesla.TeslaClient(cfg, session=s)
  cars = c.vehicles()
  assert cars == [{"id": "123", "name": "Sam", "vin": "5YJS", "state": "asleep"}]
  assert s.calls[0][1] == tesla.OWNER_AUTH and s.calls[0][2]["client_id"] == "ownerapi"
  assert config.load()["tesla_refresh_token"] == "rt-rot1"   # rotated token persisted


def test_flash_wakes_sleeping_car(cfg, monkeypatch):
  monkeypatch.setattr(tesla.time, "sleep", lambda s: None)
  s = FakeSession()
  tesla.TeslaClient(cfg, session=s).flash("123")
  paths = [c[1].split(".com", 1)[1] for c in s.calls if c[0] != "AUTH"]
  assert paths == ["/api/1/vehicles/123/command/flash_lights", "/api/1/vehicles/123",
                   "/api/1/vehicles/123/wake_up", "/api/1/vehicles/123/command/flash_lights"]


def test_expired_access_token_is_refreshed(cfg):
  s = FakeSession()
  c = tesla.TeslaClient(cfg, session=s)
  c.vehicles()
  s.valid_access.clear()            # server revoked/expired it
  c.vehicles()
  assert sum(1 for x in s.calls if x[0] == "AUTH") == 2


def test_bad_refresh_token(cfg):
  cfg["tesla_refresh_token"] = "nope"
  with pytest.raises(tesla.TeslaError, match="rechazó el token"):
    tesla.TeslaClient(cfg, session=FakeSession()).vehicles()


def test_fleet_uses_client_id_and_base(cfg):
  cfg.update({"tesla_backend": "fleet", "tesla_client_id": "my-app", "tesla_base_url": "https://proxy.example.com/"})
  s = FakeSession()
  c = tesla.TeslaClient(cfg, session=s)
  assert c.base == "https://proxy.example.com"
  c._refresh()
  assert s.calls[0][1] == tesla.FLEET_AUTH and s.calls[0][2]["client_id"] == "my-app"


def test_sun_elevation_madrid():
  # 2026-06-21 12:14 UTC ~ solar noon in Madrid: sun ~73 deg; 2026-06-21 23:00 UTC: night
  assert 70 < tesla.sun_elevation(40.4168, -3.7038, 1782044040) < 75
  assert tesla.sun_elevation(40.4168, -3.7038, 1782082800) < -15


def test_should_flash_rules(cfg, monkeypatch):
  lights = tesla.LightsService()
  monkeypatch.setattr(tesla, "is_night", lambda c, t=None: (False, 30.0))
  ok, why = lights.should_flash(cfg, "impact")
  assert not ok and "día" in why
  monkeypatch.setattr(tesla, "is_night", lambda c, t=None: (True, -20.0))
  assert lights.should_flash(cfg, "impact")[0]
  assert not lights.should_flash(cfg, "manual")[0]          # manual needs its own toggle
  lights.recent = [time.monotonic()] * tesla.MAX_FLASHES_PER_HOUR
  assert "límite" in lights.should_flash(cfg, "impact")[1]
  cfg["tesla_flash"] = False
  assert lights.should_flash(cfg, "impact") == (False, "desactivado")


def test_secrets_not_public(cfg):
  pub = config.public(config.load())
  assert "tesla_refresh_token" not in pub and pub["tesla_connected"] and pub["tesla_token_set"]


def test_web_connect_flow(cfg, monkeypatch):
  import asyncio
  from aiohttp.test_utils import TestClient, TestServer
  from nap_sentinel import webd
  config.update({"tesla_refresh_token": "", "tesla_vehicle_id": ""})
  monkeypatch.setattr(tesla, "_session", FakeSession)
  monkeypatch.setattr(config, "STATUS_FILE", cfg and (config.INSTALL_DIR / "status.json"))

  async def go():
    async with TestClient(TestServer(webd.make_app())) as c:
      assert (await c.post("/api/tesla/connect", json={"refresh_token": ""})).status == 400
      assert (await c.post("/api/tesla/connect", json={"refresh_token": "bad"})).status == 400
      r = await (await c.post("/api/tesla/connect", json={"refresh_token": "rt-good"})).json()
      assert r["vehicles"][0]["name"] == "Sam" and r["config"]["tesla_connected"]
      assert config.load()["tesla_refresh_token"] == "rt-rot1"
      # identity can't be changed through the generic endpoint, toggles can
      await c.post("/api/config", json={"tesla_vehicle_id": "999", "tesla_flash_when": "always", "tesla_flash": True})
      cfg2 = config.load()
      assert cfg2["tesla_vehicle_id"] == "123" and cfg2["tesla_flash_when"] == "always"
      n = await (await c.get("/api/night")).json()
      assert "sun_elevation" in n
      r = await (await c.post("/api/tesla/disconnect")).json()
      assert not r["tesla_connected"] and config.load()["tesla_refresh_token"] == ""
  asyncio.run(go())
