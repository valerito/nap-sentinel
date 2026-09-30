#!/usr/bin/env python3
"""sentinel_webd: small web UI on the comma device to watch sentinel events.

  http://<device ip>:8090

Reachable from any device on the same network: the comma's own hotspot, your
home Wi-Fi, or a VPN such as Tailscale. Set a password with the
web_password setting (from the page itself) to require HTTP basic auth.
"""
from __future__ import annotations

import base64
import hmac
import os
from pathlib import Path

from aiohttp import web

from nap_sentinel import config, storage

PORT = int(os.environ.get("SENTINEL_WEB_PORT", "8090"))
WEB_DIR = Path(__file__).parent / "web"
MEDIA_FILES = {"road.mp4", "fcamera.mp4", "ecamera.mp4", "dcamera.mp4", "thumb.jpg"}

def make_app() -> web.Application:

  @web.middleware
  async def auth(request: web.Request, handler):
    pw = config.load()["web_password"]
    if pw:
      ok = False
      hdr = request.headers.get("Authorization", "")
      if hdr.startswith("Basic "):
        try:
          _, _, given = base64.b64decode(hdr[6:]).decode().partition(":")
          ok = hmac.compare_digest(given.encode(), pw.encode())
        except Exception:
          ok = False
      if not ok:
        return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="sentinel"'})
    resp = await handler(request)
    if request.path.startswith("/api/"):
      resp.headers["Cache-Control"] = "no-store"
    return resp

  async def index(request):
    return web.FileResponse(WEB_DIR / "index.html")

  async def status(request):
    st = config.read_json(config.STATUS_FILE, default={}) or {}
    return web.json_response({"status": st, "config": config.public(config.load())})

  async def get_config(request):
    return web.json_response(config.public(config.load()))

  async def post_config(request):
    try:
      data = await request.json()
      if "password" in data:
        data["web_password"] = str(data.pop("password") or "")
      return web.json_response(config.public(config.update(data)))
    except (ValueError, TypeError) as e:
      return web.json_response({"error": str(e)}, status=400)

  async def events(request):
    return web.json_response(storage.list_events())

  async def delete_event(request):
    ok = storage.delete_event(request.match_info["eid"])
    return web.json_response({"ok": ok}, status=200 if ok else 404)

  async def lock_event(request):
    ev = storage.load_event(request.match_info["eid"])
    if ev is None:
      return web.json_response({"ok": False}, status=404)
    data = await request.json() if request.can_read_body else {}
    ev["locked"] = bool(data.get("locked", not ev.get("locked")))
    storage.save_event(ev)
    return web.json_response(ev)

  async def record_now(request):
    if not config.load()["enabled"]:
      return web.json_response({"ok": False, "error": "sentinel desactivado"}, status=409)
    config.TRIGGER_FILE.touch()
    return web.json_response({"ok": True})

  async def media(request):
    eid, name = request.match_info["eid"], request.match_info["name"]
    if not storage.valid_event_id(eid) or name not in MEDIA_FILES:
      raise web.HTTPNotFound()
    path = storage.sentinel_root() / eid / name
    if not path.is_file():
      raise web.HTTPNotFound()
    headers = {}
    if request.query.get("download"):
      headers["Content-Disposition"] = f'attachment; filename="sentinel-{eid}-{name}"'
    return web.FileResponse(path, headers=headers)  # supports Range -> seeking works

  app = web.Application(middlewares=[auth])
  app.add_routes([
    web.get("/", index),
    web.get("/api/status", status),
    web.get("/api/config", get_config),
    web.post("/api/config", post_config),
    web.get("/api/events", events),
    web.post("/api/events/{eid}/delete", delete_event),
    web.post("/api/events/{eid}/lock", lock_event),
    web.post("/api/record", record_now),
    web.get("/media/{eid}/{name}", media),
  ])
  return app


def main() -> None:
  web.run_app(make_app(), host="0.0.0.0", port=PORT, print=None, access_log=None)


if __name__ == "__main__":
  main()
