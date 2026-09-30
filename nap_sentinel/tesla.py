"""Tesla API: flash the headlights when an event happens at night.

Two backends, both used with a *refresh token* (Sentinel never asks for, stores
or sends the Tesla account password):

  owner  Unofficial Owner API (owner-api.teslamotors.com), client_id "ownerapi".
         auth.tesla.com only mints Owner-scoped tokens when the refresh is done
         over TLS 1.3, so every request here forces TLS 1.3.
  fleet  Official Fleet API, or a Fleet API proxy: base URL + client_id
         (+ refresh token, or a static access token for proxies that give one).

Pre-2021 Model S/X accept plain REST commands (no signed vehicle commands), so
flash_lights works on a 2012-2014 Model S. A sleeping car has to be woken up
first (wake_up + wait until "online"), which takes ~10-40 s.
"""
from __future__ import annotations

import math
import ssl
import threading
import time

from nap_sentinel import config

OWNER_BASE = "https://owner-api.teslamotors.com"
OWNER_AUTH = "https://auth.tesla.com/oauth2/v3/token"
OWNER_CLIENT_ID = "ownerapi"
FLEET_AUTH = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
FLEET_BASE_EU = "https://fleet-api.prd.eu.vn.cloud.tesla.com"
WAKE_TIMEOUT_S = 60
MAX_FLASHES_PER_HOUR = 6


class TeslaError(Exception):
  def __init__(self, msg: str, status: int | None = None):
    super().__init__(msg)
    self.status = status


def _session():
  import requests
  from requests.adapters import HTTPAdapter

  class TLS13(HTTPAdapter):
    def init_poolmanager(self, *a, **kw):
      ctx = ssl.create_default_context()
      ctx.minimum_version = ssl.TLSVersion.TLSv1_3
      kw["ssl_context"] = ctx
      return super().init_poolmanager(*a, **kw)

  s = requests.Session()
  s.mount("https://", TLS13())
  s.headers["User-Agent"] = "nap-sentinel"
  return s


class TeslaClient:
  def __init__(self, cfg: dict | None = None, session=None):
    self.cfg = cfg or config.load()
    self.s = session or _session()
    self.access_token = ""
    self.expires_at = 0.0

  # ── auth ─────────────────────────────────────────────────
  @property
  def backend(self) -> str:
    return self.cfg["tesla_backend"]

  @property
  def base(self) -> str:
    if self.backend == "owner":
      return OWNER_BASE
    return (self.cfg["tesla_base_url"] or FLEET_BASE_EU).rstrip("/")

  def _refresh(self) -> None:
    import requests
    rt = self.cfg["tesla_refresh_token"]
    if not rt:
      if self.backend == "fleet" and self.cfg["tesla_access_token"]:
        self.access_token, self.expires_at = self.cfg["tesla_access_token"], time.time() + 3600  # noqa: TID251
        return
      raise TeslaError("falta el token de refresco")
    url = OWNER_AUTH if self.backend == "owner" else (self.cfg["tesla_auth_url"] or FLEET_AUTH)
    client_id = OWNER_CLIENT_ID if self.backend == "owner" else self.cfg["tesla_client_id"]
    data = {"grant_type": "refresh_token", "client_id": client_id, "refresh_token": rt}
    if self.backend == "owner":
      data["scope"] = "openid email offline_access"
    try:
      r = self.s.post(url, data=data, timeout=20)
    except requests.RequestException as e:
      raise TeslaError(f"sin conexión con Tesla ({type(e).__name__})") from None
    if r.status_code != 200:
      raise TeslaError(f"Tesla rechazó el token de refresco (HTTP {r.status_code})", r.status_code)
    j = r.json()
    self.access_token = j["access_token"]
    self.expires_at = time.time() + float(j.get("expires_in", 3600)) - 120  # noqa: TID251
    if j.get("refresh_token") and j["refresh_token"] != rt:
      # Tesla rotates refresh tokens: keep the new one or the next refresh fails
      config.update({"tesla_refresh_token": j["refresh_token"]})
      self.cfg["tesla_refresh_token"] = j["refresh_token"]

  def _token(self) -> str:
    if not self.access_token or time.time() > self.expires_at:  # noqa: TID251
      self._refresh()
    return self.access_token

  def request(self, method: str, path: str, json_body: dict | None = None, timeout: float = 20):
    import requests
    for attempt in range(2):
      try:
        r = self.s.request(method, self.base + path, json=json_body, timeout=timeout,
                           headers={"Authorization": f"Bearer {self._token()}"})
      except requests.RequestException as e:
        raise TeslaError(f"sin conexión con Tesla ({type(e).__name__})") from None
      if r.status_code == 401 and attempt == 0:
        self.access_token = ""   # expired/revoked access token: refresh once and retry
        continue
      if r.status_code == 408:
        raise TeslaError("el coche está dormido o sin conexión", 408)
      if r.status_code == 403:
        raise TeslaError("Tesla denegó el acceso (403). Con Owner API puede que tu cuenta ya no la admita: "
                         "prueba con Fleet API.", 403)
      if r.status_code >= 400:
        raise TeslaError(f"error de la API de Tesla (HTTP {r.status_code})", r.status_code)
      return r.json().get("response")
    raise TeslaError("token no válido", 401)

  # ── vehicle ──────────────────────────────────────────────
  def vehicles(self) -> list[dict]:
    res = self.request("GET", "/api/1/vehicles") or []
    return [{"id": str(v.get("id_s") or v.get("id")), "name": v.get("display_name") or v.get("vin", "Tesla"),
             "vin": v.get("vin", ""), "state": v.get("state", "")} for v in res]

  def state(self, vid: str) -> str:
    return (self.request("GET", f"/api/1/vehicles/{vid}") or {}).get("state", "")

  def wake(self, vid: str, timeout_s: float = WAKE_TIMEOUT_S) -> float:
    """Returns seconds it took to be online."""
    t0 = time.monotonic()
    if self.state(vid) == "online":
      return 0.0
    while time.monotonic() - t0 < timeout_s:
      try:
        st = (self.request("POST", f"/api/1/vehicles/{vid}/wake_up") or {}).get("state", "")
      except TeslaError as e:
        if e.status != 408:
          raise
        st = ""
      if st == "online":
        return time.monotonic() - t0
      time.sleep(3)
    raise TeslaError(f"el coche no despertó en {timeout_s:.0f} s")

  def flash(self, vid: str) -> float:
    """Flash the lights, waking the car if needed. Returns seconds spent."""
    t0 = time.monotonic()
    try:
      res = self.request("POST", f"/api/1/vehicles/{vid}/command/flash_lights")
    except TeslaError as e:
      if e.status != 408:
        raise
      self.wake(vid)
      res = self.request("POST", f"/api/1/vehicles/{vid}/command/flash_lights")
    if res and res.get("result") is False:
      raise TeslaError(f"el coche rechazó el destello: {res.get('reason') or 'sin motivo'}")
    return time.monotonic() - t0


# ── night detection ─────────────────────────────────────────
def sun_elevation(lat: float, lon: float, t: float) -> float:
  """Solar elevation in degrees (NOAA approximation, good to ~0.5 deg)."""
  import datetime
  dt = datetime.datetime.fromtimestamp(t, datetime.UTC)
  doy = dt.timetuple().tm_yday
  hour = dt.hour + dt.minute / 60 + dt.second / 3600
  g = 2 * math.pi / 365 * (doy - 1 + (hour - 12) / 24)
  decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
          + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
  eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                     - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
  tst = hour * 60 + eqtime + 4 * lon
  ha = math.radians(tst / 4 - 180)
  la = math.radians(lat)
  cos_zen = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
  return 90 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zen))))


def location(cfg: dict) -> tuple[float, float, str]:
  st = config.read_json(config.INSTALL_DIR / "state.json", default={}) or {}
  if cfg["location_source"] == "gps" and st.get("gps_lat") is not None:
    return float(st["gps_lat"]), float(st["gps_lon"]), "GPS del comma"
  why = "ubicación manual" if cfg["location_source"] == "manual" else "aún sin GPS: ubicación manual"
  return float(cfg["latitude"]), float(cfg["longitude"]), why


def is_night(cfg: dict, t: float | None = None) -> tuple[bool, float]:
  lat, lon, _ = location(cfg)
  elev = sun_elevation(lat, lon, t or time.time())  # noqa: TID251
  return elev < cfg["night_sun_elevation"], elev


# ── used by sentineld ───────────────────────────────────────
class LightsService:
  def __init__(self, log=print):
    self.log = log
    self.recent: list[float] = []
    self.last_result = ""
    self._busy = threading.Lock()

  def should_flash(self, cfg: dict, reason: str) -> tuple[bool, str]:
    if not (cfg["tesla_flash"] and cfg["tesla_vehicle_id"] and
            (cfg["tesla_refresh_token"] or cfg["tesla_access_token"])):
      return False, "desactivado"
    if reason == "manual" and not cfg["tesla_flash_manual"]:
      return False, "grabación manual"
    if cfg["tesla_flash_when"] == "night":
      night, elev = is_night(cfg)
      if not night:
        return False, f"es de día (sol a {elev:.0f}°)"
    now = time.monotonic()
    self.recent = [t for t in self.recent if now - t < 3600]
    if len(self.recent) >= MAX_FLASHES_PER_HOUR:
      return False, f"límite de {MAX_FLASHES_PER_HOUR} destellos por hora"
    return True, ""

  def flash_async(self, cfg: dict, on_done=None) -> None:
    if not self._busy.acquire(blocking=False):
      return  # one at a time
    self.recent.append(time.monotonic())

    def run():
      try:
        took = TeslaClient(cfg).flash(cfg["tesla_vehicle_id"])
        self.last_result = f"destello enviado ({took:.0f} s)"
      except Exception as e:
        self.last_result = f"destello fallido: {e}"
      finally:
        self._busy.release()
      self.log(f"tesla: {self.last_result}")
      if on_done:
        on_done(self.last_result)
    threading.Thread(target=run, daemon=True).start()
