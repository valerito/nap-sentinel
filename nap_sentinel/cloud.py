"""Acceso remoto: keeps the comma in touch with the relay web (server/api.php).

The relay runs on plain PHP hosting, so nothing stays connected: this thread
polls it (every couple of seconds while someone has the remote page open, once a
minute otherwise), uploads the state and event list, and runs the orders queued
from the web against the local panel API (127.0.0.1:8090). Only a fixed list of
panel calls is allowed, here and on the server; secrets never leave the comma
because only what the local panel publishes is sent. Videos are uploaded only
when someone asks for them from the web.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time

import requests

from nap_sentinel import config, storage

STATE_FILE = config.SHM / "nap_sentinel_cloud.json"
CHUNK = 1024 * 1024
FULL_EVERY_S = 300          # status/config refresh while nobody is watching
EID = r"[0-9]{8}-[0-9]{6}(?:-[0-9]+)?"
ALLOWED = [
  ("GET", re.compile(r"^/api/(status|events|night|update|telegram/log)$")),
  ("POST", re.compile(r"^/api/(config|record|reboot|telegram/test|update/check|update/run|tesla/flash|tesla/vehicles|tesla/vehicle)$")),
  ("POST", re.compile(rf"^/api/events/{EID}/(delete|lock|telegram)$")),
]
UPLOAD_RE = re.compile(rf"^/media/({EID})/((?:road|fcamera|ecamera|dcamera|wide_lq)\.mp4)$")


def allowed(method: str, path: str) -> bool:
  return any(m == method and rx.match(path) for m, rx in ALLOWED)


def _save_state(**kw) -> dict:
  st = {**(config.read_json(STATE_FILE, default={}) or {}), **kw}
  config.atomic_write_json(STATE_FILE, st)
  return st


def state() -> dict:
  return config.read_json(STATE_FILE, default={}) or {}


def _url(cfg: dict, action: str) -> str:
  return cfg["cloud_url"].rstrip("/") + "/api.php?a=" + action


def _short(e: Exception) -> str:
  if isinstance(e, requests.ConnectionError):
    return "sin conexión con el servidor"
  if isinstance(e, requests.Timeout):
    return "el servidor no responde"
  return str(e)[:160]


class CloudError(Exception):
  pass


def _check(r: requests.Response) -> dict:
  try:
    j = r.json()
  except ValueError:
    raise CloudError(f"respuesta no válida del servidor (HTTP {r.status_code}). ¿Es correcta la dirección?") from None
  if r.status_code >= 400:
    raise CloudError(j.get("error") or f"HTTP {r.status_code}")
  return j


def _dev_headers(cfg: dict) -> dict:
  return {"X-Device-Id": cfg["cloud_device_id"], "X-Device-Key": cfg["cloud_device_key"]}


# ── pairing (called from the local panel) ────────────────────────────────
def pair(version: str) -> dict:
  """Registers this comma if needed and returns a fresh 6-letter code for the web."""
  cfg = config.load()
  try:
    if not cfg["cloud_device_id"] or not cfg["cloud_device_key"]:
      j = _check(requests.post(_url(cfg, "d_register"), json={"version": version}, timeout=20))
      config.update({"cloud_device_id": j["device_id"], "cloud_device_key": j["device_key"]})
    else:
      r = requests.post(_url(cfg, "d_pair"), headers=_dev_headers(cfg), json={}, timeout=20)
      if r.status_code == 401:     # the server forgot us (database reset): register again
        config.update({"cloud_device_id": "", "cloud_device_key": ""})
        return pair(version)
      j = _check(r)
  except requests.RequestException as e:
    raise CloudError(_short(e)) from None
  config.update({"cloud_enabled": True, "cloud_pair_code": j["pair_code"],
                 "cloud_pair_expires": time.time() + float(j.get("pair_expires_s", 900))})  # noqa: TID251
  return {"code": j["pair_code"], "expires_s": j.get("pair_expires_s", 900)}


def unlink() -> None:
  cfg = config.load()
  if cfg["cloud_device_id"]:
    try:
      requests.post(_url(cfg, "d_unlink"), headers=_dev_headers(cfg), json={}, timeout=20)
    except requests.RequestException:
      pass
  config.update({"cloud_user": "", "cloud_pair_code": "", "cloud_pair_expires": 0.0})
  _save_state(linked=False, user="")


# ── the polling loop ─────────────────────────────────────────────────────
class CloudClient:
  def __init__(self, local_base: str, version: str):
    self.local = local_base.rstrip("/")
    self.version = version
    self.results: list[dict] = []
    self.lock = threading.Lock()
    self.events_sig_sent = ""
    self.last_full = 0.0
    self.poll_s = 10.0
    self.uploading: set[str] = set()
    self.http = requests.Session()      # keep-alive: no new TLS handshake on every poll (saves mobile data)

  # local panel API (same code paths as the panel buttons)
  def _local(self, method: str, path: str, body=None, timeout: float = 120):
    cfg = config.load()
    auth = ("sentinel", cfg["web_password"]) if cfg["web_password"] else None
    r = requests.request(method, self.local + path, json=body if method == "POST" else None, auth=auth, timeout=timeout)
    try:
      data = r.json()
    except ValueError:
      data = {"error": f"HTTP {r.status_code}"}
    return r.status_code < 400, data

  def run_command(self, c: dict) -> dict:
    method, path, body = str(c.get("method", "")), str(c.get("path", "")), c.get("body")
    if not allowed(method, path):
      return {"id": c.get("id"), "ok": False, "result": {"error": "orden no permitida en el comma"}}
    if path == "/api/config" and isinstance(body, dict):
      body = {k: v for k, v in body.items() if k != "password" and not k.startswith("cloud_")}
    try:
      ok, data = self._local(method, path, body)
    except requests.RequestException as e:
      ok, data = False, {"error": f"el panel local no responde ({type(e).__name__})"}
    return {"id": c.get("id"), "ok": ok, "result": data}

  def _add_result(self, res: dict) -> None:
    with self.lock:
      self.results.append(res)

  # uploads run in their own thread so the panel keeps answering meanwhile
  def upload(self, cfg: dict, eid: str, name: str) -> tuple[bool, str]:
    path = storage.sentinel_root() / eid / name
    if not storage.valid_event_id(eid) or not path.is_file():
      return False, "ese vídeo no existe en el comma"
    total = path.stat().st_size
    if total == 0:
      return False, "el vídeo está vacío"
    offset = 0
    with open(path, "rb") as f:
      while offset < total:
        f.seek(offset)
        chunk = f.read(CHUNK)
        for attempt in range(4):
          try:
            r = requests.post(_url(cfg, "d_upload"), params={"eid": eid, "name": name, "offset": offset, "total": total},
                              data=chunk, headers={**_dev_headers(cfg), "Content-Type": "application/octet-stream"}, timeout=120)
            if r.status_code == 409 and "received" in (r.json() or {}):
              offset = int(r.json()["received"])   # the server already has part of it
              break
            j = _check(r)
            offset = int(j["received"])
            break
          except (requests.RequestException, CloudError) as e:
            if attempt == 3:
              return False, _short(e) if isinstance(e, requests.RequestException) else str(e)
            time.sleep(2 + attempt * 3)
        _save_state(upload={"eid": eid, "name": name, "sent": offset, "total": total})
    _save_state(upload=None)
    return True, "subido"

  def _upload_job(self, cfg: dict, cid, eid: str, name: str) -> None:
    key = f"{eid}/{name}"
    try:
      ok, msg = self.upload(cfg, eid, name)
      if cid:
        self._add_result({"id": cid, "ok": ok, "result": {"ok": ok, "message": msg} if ok else {"error": msg}})
    finally:
      self.uploading.discard(key)

  def _start_upload(self, cfg: dict, cid, eid: str, name: str) -> None:
    key = f"{eid}/{name}"
    if key in self.uploading:
      if cid:
        self._add_result({"id": cid, "ok": True, "result": {"ok": True, "message": "ya se está subiendo"}})
      return
    self.uploading.add(key)
    threading.Thread(target=self._upload_job, args=(cfg, cid, eid, name), daemon=True, name="sentinel-cloud-upload").start()

  def sync_once(self) -> float:
    cfg = config.load()
    if not cfg["cloud_enabled"] or not cfg["cloud_device_id"]:
      _save_state(state="off")
      return 5.0
    now = time.time()  # noqa: TID251
    payload: dict = {"version": self.version}
    with self.lock:
      results, self.results = self.results, []
    payload["results"] = results
    try:
      ok, events = self._local("GET", "/api/events", timeout=20)
      events = events if ok and isinstance(events, list) else []
    except requests.RequestException:
      events = []
    sig = hashlib.sha1(json.dumps(events, sort_keys=True).encode()).hexdigest()
    payload["events_sig"] = sig
    if sig != self.events_sig_sent:
      payload["events"] = events
    if self.poll_s <= 5 or now - self.last_full > FULL_EVERY_S or results:
      try:
        ok, st = self._local("GET", "/api/status", timeout=20)
        if ok:
          payload["status"] = st.get("status") or {}
          payload["config"] = {k: v for k, v in (st.get("config") or {}).items() if not k.startswith("cloud_")}
          payload["extra"] = {"update": {k: v for k, v in (st.get("update") or {}).items() if k != "log"},
                              "status_age_s": st.get("status_age_s"), "device_time": st.get("server_time")}
      except requests.RequestException:
        pass
    try:
      r = self.http.post(_url(cfg, "d_sync"), json=payload, headers=_dev_headers(cfg), timeout=30)
      if r.status_code == 401:
        config.update({"cloud_device_id": "", "cloud_device_key": "", "cloud_user": ""})
        _save_state(state="error", error="el servidor ya no reconoce este comma: vuelve a vincularlo", linked=False)
        return 10.0
      j = _check(r)
    except (requests.RequestException, CloudError) as e:
      with self.lock:                       # keep the results for the next try
        self.results = results + self.results
      _save_state(state="error", error=_short(e) if isinstance(e, requests.RequestException) else str(e))
      return min(60.0, max(10.0, self.poll_s * 2))
    if "events" in payload:
      self.events_sig_sent = sig
    if j.get("want_events"):
      self.events_sig_sent = ""
    if "status" in payload:
      self.last_full = now
    linked = bool(j.get("linked"))
    if linked != bool(cfg["cloud_user"]) or (linked and j.get("user") != cfg["cloud_user"]):
      config.update({"cloud_user": j.get("user", "") if linked else "", **({"cloud_pair_code": ""} if linked else {})})
    _save_state(state="ok", error="", linked=linked, user=j.get("user", ""), last_ok=now, poll_s=j.get("poll_s"))

    for c in j.get("commands") or []:
      if not isinstance(c, dict):
        continue
      if c.get("method") == "THUMBS":
        for w in (c.get("body") or [])[:4]:
          if isinstance(w, dict) and w.get("name") == "thumb.jpg" and storage.valid_event_id(str(w.get("eid", ""))):
            self._start_upload(cfg, None, str(w["eid"]), "thumb.jpg")
        continue
      if c.get("method") == "UPLOAD":
        m = UPLOAD_RE.match(str(c.get("path", "")))
        if not m:
          self._add_result({"id": c.get("id"), "ok": False, "result": {"error": "archivo no permitido"}})
        else:
          self._start_upload(cfg, c.get("id"), m.group(1), m.group(2))
        continue
      self._add_result(self.run_command(c))
    self.poll_s = float(min(120, max(1, int(j.get("poll_s") or 60))))
    with self.lock:
      if self.results:
        return 0.2                           # answer right away
    return self.poll_s

  def loop(self, stop: threading.Event) -> None:
    stop.wait(5)
    while not stop.is_set():
      try:
        wait = self.sync_once()
      except Exception as e:  # never die: the panel must keep working
        _save_state(state="error", error=f"error interno: {e}"[:200])
        wait = 30.0
      stop.wait(wait)


def start(local_base: str, version: str) -> threading.Event:
  stop = threading.Event()
  if os.environ.get("SENTINEL_CLOUD", "1") != "0":
    threading.Thread(target=CloudClient(local_base, version).loop, args=(stop,), daemon=True, name="sentinel-cloud").start()
  return stop
