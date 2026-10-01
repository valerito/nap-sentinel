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


def test_login_start_is_valid_pkce():
  import base64
  import hashlib
  import urllib.parse
  st = tesla.login_start()
  q = urllib.parse.parse_qs(urllib.parse.urlparse(st["url"]).query)
  assert st["url"].startswith(tesla.AUTHORIZE_URL)
  assert q["client_id"] == ["ownerapi"] and q["redirect_uri"] == [tesla.REDIRECT_URI]
  assert q["code_challenge_method"] == ["S256"] and q["state"] == [st["state"]]
  expect = base64.urlsafe_b64encode(hashlib.sha256(st["verifier"].encode()).digest()).rstrip(b"=").decode()
  assert q["code_challenge"] == [expect]
  assert 43 <= len(st["verifier"]) <= 128


def test_parse_callback():
  cb = tesla.parse_callback("tesla://auth/callback?code=abc123&state=xyz&issuer=https%3A%2F%2Fauth.tesla.com%2Foauth2%2Fv3")
  assert cb == {"code": "abc123", "state": "xyz", "token_url": "https://auth.tesla.com/oauth2/v3/token"}
  assert tesla.parse_callback("tesla://auth/callback?code=c&issuer=https://auth.tesla.cn/oauth2/v3")["token_url"].startswith(
    "https://auth.tesla.cn")
  with pytest.raises(tesla.TeslaError, match="código"):
    tesla.parse_callback("tesla://auth/callback?error=login_cancelled")
  # Chrome's console line, pasted whole
  line = ("Failed to launch 'tesla://auth/callback?code=EU_abc-123.x&state=s1&issuer=https%3A%2F%2Fauth.tesla.com%2Foauth2%2Fv3' "
          "because the scheme does not have a registered handler.")
  assert tesla.parse_callback(line)["code"] == "EU_abc-123.x" and tesla.parse_callback(line)["state"] == "s1"
  assert tesla.parse_callback("  EU_0123456789abcdefghij  ")["code"] == "EU_0123456789abcdefghij"
  assert "phone" in tesla.login_start()["url"]
  with pytest.raises(tesla.TeslaError, match="recortada"):
    tesla.parse_callback("Failed to launch 'tesla://auth/callback?code=EU_g2n6iuuw2ruy…er=https%3A%2F%2Fauth.tesla.com&state=x'")


class LoginSession(FakeSession):
  def post(self, url, data=None, json=None, timeout=None):
    if json is not None:
      self.calls.append(("CODE", url, dict(json)))
      if json["code"] != "good-code" or json["code_verifier"] != self.expect_verifier:
        return Resp(400, {"error": "invalid_grant"})
      return Resp(200, {"access_token": "at-login", "refresh_token": "rt-good", "expires_in": 28800})
    return super().post(url, data=data, timeout=timeout)


def test_login_finish_exchanges_code(cfg):
  s = LoginSession()
  s.expect_verifier = "ver"
  tok = tesla.login_finish("tesla://auth/callback?code=good-code&state=st", "ver", "st", session=s)
  assert tok["refresh_token"] == "rt-good"
  assert s.calls[0][2]["grant_type"] == "authorization_code" and s.calls[0][2]["redirect_uri"] == tesla.REDIRECT_URI
  with pytest.raises(tesla.TeslaError, match="otro inicio"):
    tesla.login_finish("tesla://auth/callback?code=good-code&state=OTHER", "ver", "st", session=s)
  with pytest.raises(tesla.TeslaError, match="no aceptó"):
    tesla.login_finish("tesla://auth/callback?code=bad&state=st", "ver", "st", session=s)


def test_web_login_flow(cfg, monkeypatch):
  import asyncio
  from aiohttp.test_utils import TestClient, TestServer
  from nap_sentinel import webd
  config.update({"tesla_refresh_token": "", "tesla_vehicle_id": ""})
  holder = {}

  def fake_session():
    s = LoginSession()
    s.expect_verifier = config.load()["tesla_login_verifier"]
    holder["s"] = s
    return s
  monkeypatch.setattr(tesla, "_session", fake_session)

  async def go():
    async with TestClient(TestServer(webd.make_app())) as c:
      assert (await c.post("/api/tesla/login/finish", json={"url": "x"})).status == 409   # not started
      r = await (await c.post("/api/tesla/login/start")).json()
      assert r["url"].startswith(tesla.AUTHORIZE_URL)
      state = config.load()["tesla_login_state"]
      assert "tesla_login_verifier" not in (await (await c.get("/api/config")).json())
      r = await c.post("/api/tesla/login/finish", json={"url": f"tesla://auth/callback?code=good-code&state={state}"})
      body = await r.json()
      assert r.status == 200 and body["config"]["tesla_connected"] and body["vehicles"][0]["name"] == "Sam"
      c2 = config.load()
      assert c2["tesla_backend"] == "owner" and c2["tesla_refresh_token"].startswith("rt-") and c2["tesla_login_verifier"] == ""
  asyncio.run(go())
