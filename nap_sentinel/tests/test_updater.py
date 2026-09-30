import asyncio
import json
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from nap_sentinel import config, updater, webd


class FakeResp:
  def __init__(self, text):
    self.text = text


RED, OFF = "\\033[1;31m", "\\033[0m"


def fake_installer(version, rc=0, msg=""):
  body = (f'echo -e "{RED}[sentinel] {msg}{OFF}" >&2; exit {rc}' if rc
          else 'echo "$VERSION" > "$SENTINEL_HOME/VERSION"')
  return (f'#!/usr/bin/env bash\nVERSION="{version}"\n'
          f'echo -e "\\033[1;32m[sentinel]\\033[0m NAP Sentinel $VERSION"\n{body}\n'
          ": <<'__NAP_SENTINEL_PAYLOAD__'\n__NAP_SENTINEL_PAYLOAD__\n")


@pytest.fixture
def env(tmp_path, monkeypatch):
  home = tmp_path / "sentinel"
  home.mkdir()
  (home / "VERSION").write_text("1.3.1\n")
  monkeypatch.setattr(config, "INSTALL_DIR", home)
  monkeypatch.setattr(config, "STATUS_FILE", tmp_path / "status.json")
  monkeypatch.setattr(updater, "STATE_FILE", tmp_path / "update.json")
  monkeypatch.setattr(updater, "RUNNING_VERSION", "1.3.1")
  monkeypatch.setenv("SENTINEL_CONFIG", str(tmp_path / "config.json"))
  monkeypatch.setenv("SENTINEL_ROOT", str(tmp_path / "events"))
  files = {"VERSION": "1.4.0\n", "dist/nap-sentinel-install.sh": fake_installer("1.4.0")}
  monkeypatch.setattr(updater, "_get", lambda path, timeout: FakeResp(files[path]))
  return home, files


def test_version_compare():
  assert updater.newer("1.4.0", "1.3.1")
  assert updater.newer("1.10.0", "1.9.9")
  assert updater.newer("1.3.1.1", "1.3.1")
  assert not updater.newer("1.3.1", "1.3.1")
  assert not updater.newer("1.3.0", "1.3.1")
  assert not updater.newer("<html>", "1.3.1")
  assert updater.newer("1.0.0", "?")


def test_check_and_update_ok(env):
  home, _ = env
  st = updater.status()
  assert not st["available"] and st["state"] == "idle"
  updater.check()
  st = updater.status()
  assert st["available"] and st["latest"] == "1.4.0" and not st["needs_reboot"]
  ok, msg, v = updater.run()
  assert ok, msg
  assert v == "1.4.0" and (home / "VERSION").read_text().strip() == "1.4.0"
  st = updater.status()
  assert st["needs_reboot"] and not st["available"] and st["installed_version"] == "1.4.0"
  assert not (home / ".update-install.sh").exists()


def test_update_failure_reports_installer_error(env):
  home, files = env
  files["dist/nap-sentinel-install.sh"] = fake_installer("1.4.0", rc=1, msg="No encuentro openpilot en /x")
  ok, msg, _ = updater.run()
  assert not ok and "No encuentro openpilot" in msg
  assert (home / "VERSION").read_text().strip() == "1.3.1"
  files["dist/nap-sentinel-install.sh"] = "<html>not found</html>"
  ok, msg, _ = updater.run()
  assert not ok and "no es el instalador" in msg


def test_background_start_and_stale_run(env):
  updater.check()
  ok, _ = updater.start()
  assert ok
  for _ in range(100):
    if updater.status()["state"] != "running":
      break
    time.sleep(0.05)
  st = updater.status()
  assert st["state"] == "done" and st["needs_reboot"]
  ok, msg = updater.start()          # nothing newer anymore
  assert not ok and "no hay" in msg
  # a run that never finished (webd killed) is not shown as running forever
  updater._save(state="running", started_at=time.time() - 3600)
  assert updater.status()["state"] == "failed"


def test_web_endpoints(env, monkeypatch):
  calls = []
  monkeypatch.setattr(updater, "reboot", lambda: calls.append(1) or "manager")
  monkeypatch.setenv("SENTINEL_UPDATE_CHECK", "0")

  async def go():
    async with TestClient(TestServer(webd.make_app())) as c:
      assert (await c.post("/api/update/run")).status == 409          # not checked yet
      u = await (await c.post("/api/update/check")).json()
      assert u["available"] and u["latest"] == "1.4.0"
      assert (await (await c.get("/api/status")).json())["update"]["latest"] == "1.4.0"
      assert (await c.post("/api/update/run")).status == 200
      for _ in range(100):
        u = await (await c.get("/api/update")).json()
        if u["state"] != "running":
          break
        await asyncio.sleep(0.05)
      assert u["state"] == "done" and u["needs_reboot"]
      config.STATUS_FILE.write_text(json.dumps({"t": time.time(), "onroad": True}))
      assert (await c.post("/api/reboot")).status == 409 and not calls
      config.STATUS_FILE.write_text(json.dumps({"t": time.time(), "onroad": False}))
      assert (await c.post("/api/reboot")).status == 200 and calls == [1]
  asyncio.run(go())
