import json
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
import requests

from nap_sentinel import cloud, config, storage

SERVER = Path(__file__).resolve().parents[2] / "server"


def test_whitelist():
  assert cloud.allowed("GET", "/api/status") and cloud.allowed("POST", "/api/config")
  assert cloud.allowed("POST", "/api/events/20261009-101500/delete") and cloud.allowed("POST", "/api/events/20261009-101500-2/lock")
  for m, p in [("POST", "/api/cloud/pair"), ("POST", "/api/tesla/login/start"), ("POST", "/api/telegram/token"),
               ("GET", "/media/20261009-101500/road.mp4"), ("POST", "/api/events/../../x/delete"), ("DELETE", "/api/status"),
               ("POST", "/api/config/../cloud/unlink")]:
    assert not cloud.allowed(m, p), (m, p)


def test_run_command_filters(monkeypatch):
  seen = []
  c = cloud.CloudClient("http://127.0.0.1:1", "t")
  monkeypatch.setattr(c, "_local", lambda m, p, b=None, timeout=0: seen.append((m, p, b)) or (True, {"ok": 1}))
  body = {"password": "x", "cloud_url": "http://evil", "sensitivity": 5}
  r = c.run_command({"id": 7, "method": "POST", "path": "/api/config", "body": body})
  assert r == {"id": 7, "ok": True, "result": {"ok": 1}} and seen[-1][2] == {"sensitivity": 5}
  r = c.run_command({"id": 8, "method": "POST", "path": "/api/telegram/token", "body": {"token": "1:2"}})
  assert not r["ok"] and len(seen) == 1


def _free_port():
  s = socket.socket()
  s.bind(("127.0.0.1", 0))
  port = s.getsockname()[1]
  s.close()
  return port


@pytest.fixture
def relay(tmp_path):
  if not shutil.which("php"):
    pytest.skip("php not installed")
  root = tmp_path / "www"
  shutil.copytree(SERVER, root)
  port = _free_port()
  p = subprocess.Popen(["php", "-S", f"127.0.0.1:{port}", "-t", str(root)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  base = f"http://127.0.0.1:{port}"
  for _ in range(50):
    try:
      requests.get(base + "/api.php?a=me", timeout=1, proxies={"http": None})
      break
    except requests.RequestException:
      time.sleep(0.1)
  yield base, root
  p.terminate()


class Web:
  def __init__(self, base):
    self.base, self.s = base, requests.Session()
    self.s.trust_env = False
    self.csrf = self.get("me")["csrf"]

  def get(self, a, **params):
    r = self.s.get(f"{self.base}/api.php", params={"a": a, **params}, timeout=10)
    return r.json() if r.headers.get("Content-Type", "").startswith("application/json") else r

  def post(self, a, body=None, status=200):
    r = self.s.post(f"{self.base}/api.php?a={a}", json=body or {}, headers={"X-CSRF": self.csrf}, timeout=10)
    assert r.status_code == status, (a, r.status_code, r.text)
    return r.json()


def test_relay_end_to_end(relay, tmp_path, monkeypatch):
  base, root = relay
  monkeypatch.setenv("SENTINEL_CONFIG", str(tmp_path / "config.json"))
  monkeypatch.setenv("SENTINEL_ROOT", str(tmp_path / "events"))
  monkeypatch.setenv("NO_PROXY", "127.0.0.1")
  monkeypatch.setattr(cloud, "STATE_FILE", tmp_path / "cloud.json")
  monkeypatch.setattr(cloud, "CHUNK", 1000)
  config.update({"cloud_url": base, "telegram_token": "123:SECRET", "sensitivity": 3})

  eid = "20261009-101500"
  storage.save_event({"id": eid, "wall_time": 1, "status": "ready", "files": {"road.mp4": 3500}})
  video = bytes(range(256)) * 14
  (storage.sentinel_root() / eid / "road.mp4").write_bytes(video[:3500])

  # the "local panel" the client talks to
  calls = []

  def fake_local(method, path, body=None, timeout=0):
    calls.append((method, path, body))
    if path == "/api/events":
      return True, storage.list_events()
    if path == "/api/status":
      return True, {"status": {"state": "armed", "voltage": 12.5}, "config": config.public(config.load()), "update": {}}
    if path == "/api/config":
      config.update(body)
      return True, config.public(config.load())
    return True, {"ok": True}

  c = cloud.CloudClient("http://127.0.0.1:1", "1.5.0")
  monkeypatch.setattr(c, "_local", fake_local)

  code = cloud.pair("1.5.0")["code"]
  assert len(code) == 6 and config.load()["cloud_device_key"]
  c.sync_once()                                    # not linked yet: no orders

  web = Web(base)
  web.post("link", {"code": code}, status=401)     # must log in first
  web.post("register", {"email": "a@test.es", "password": "contraseña1"})
  web.post("link", {"code": "ZZZZZZ"}, status=404)
  dev = web.post("link", {"code": code, "name": "Model S"})["id"]
  web.post("link", {"code": code}, status=404)     # single use

  c.sync_once()
  assert config.load()["cloud_user"] == "a@test.es"
  st = web.get("state", device=dev)
  assert st["status"]["voltage"] == 12.5 and st["events"][0]["id"] == eid
  assert '"telegram_token":' not in json.dumps(st) and "SECRET" not in json.dumps(st) and "cloud_device_key" not in json.dumps(st)

  # an order from the web runs on the comma and the result comes back
  cid = web.post("cmd", {"device": dev, "method": "POST", "path": "/api/config", "body": {"sensitivity": 5, "password": "pw"}})["id"]
  web.post("cmd", {"device": dev, "method": "POST", "path": "/api/telegram/token", "body": {}}, status=403)
  for _ in range(3):
    c.sync_once()
  assert config.load()["sensitivity"] == 5 and config.load()["web_password"] == ""
  assert web.get("cmd_result", id=cid)["state"] == "done"

  # video upload in chunks, then served with Range
  web.post("cmd", {"device": dev, "method": "UPLOAD", "path": f"/media/{eid}/road.mp4"})
  c.sync_once()
  for _ in range(100):
    if not c.uploading:
      break
    time.sleep(0.05)
  r = web.s.get(f"{base}/api.php", params={"a": "media", "device": dev, "eid": eid, "name": "road.mp4"}, headers={"Range": "bytes=10-19"})
  assert r.status_code == 206 and r.content == video[10:20]

  # another account sees nothing
  other = Web(base)
  other.post("register", {"email": "b@test.es", "password": "contraseña2"})
  other.post("cmd", {"device": dev, "method": "POST", "path": "/api/record"}, status=404)
  assert other.get("devices") == []
  r = other.s.get(f"{base}/api.php", params={"a": "media", "device": dev, "eid": eid, "name": "road.mp4"})
  assert r.status_code == 404

  # CSRF and wrong password
  assert web.s.post(f"{base}/api.php?a=cmd", json={"device": dev, "method": "POST", "path": "/api/record"}).status_code == 403
  bad = Web(base)
  bad.post("login", {"email": "a@test.es", "password": "nope"}, status=401)

  # unlink from the comma
  cloud.unlink()
  assert web.get("devices") == [] and not config.load()["cloud_user"]
