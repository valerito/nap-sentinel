#!/usr/bin/env python3
"""sentinel_webd: small web UI on the comma device to watch sentinel events.

  http://<device ip>:8090

Reachable from any device on the same network: the comma's own hotspot, your
home Wi-Fi, or a VPN such as Tailscale. Set a password with the
web_password setting (from the page itself) to require HTTP basic auth.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import os
import time
from pathlib import Path

from aiohttp import web

from nap_sentinel import config, storage, telegram

PORT = int(os.environ.get("SENTINEL_WEB_PORT", "8090"))
WEB_DIR = Path(__file__).parent / "web"
MEDIA_FILES = {"road.mp4", "fcamera.mp4", "ecamera.mp4", "dcamera.mp4", "wide_lq.mp4", "thumb.jpg"}

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
      # Telegram identity is only changed through /api/telegram/*
      data = {k: v for k, v in data.items()
              if not k.startswith("telegram_") or k in ("telegram_alerts", "telegram_video", "telegram_video_wifi_only")}
      data.pop("web_password", None)
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

  # ── Telegram ────────────────────────────────────────────
  async def _body(request) -> dict:
    try:
      return await request.json() if request.can_read_body else {}
    except ValueError:
      return {}

  def _tg_public() -> dict:
    return config.public(config.load())

  async def tg_token(request):
    token = str((await _body(request)).get("token", "")).strip()
    if not token or ":" not in token:
      return web.json_response({"error": "Token no válido. Cópialo entero desde @BotFather (123456:ABC…)."}, status=400)
    loop = asyncio.get_running_loop()
    try:
      me = await loop.run_in_executor(None, telegram.get_me, token)
      await loop.run_in_executor(None, lambda: telegram.api(token, "deleteWebhook"))
    except Exception as e:
      return web.json_response({"error": f"Telegram rechazó el token: {e}"}, status=400)
    cfg = config.load()
    same_bot = cfg["telegram_token"] == token
    config.update({"telegram_token": token, "telegram_bot": me.get("username", ""),
                   **({} if same_bot else {"telegram_chat_id": "", "telegram_chat_name": ""})})
    return web.json_response(_tg_public())

  async def tg_link(request):
    cfg = config.load()
    if not cfg["telegram_token"]:
      return web.json_response({"error": "Primero guarda el token del bot."}, status=409)
    code = telegram.new_link_code()
    config.update({"telegram_link_code": code, "telegram_link_expires": time.time() + telegram.LINK_CODE_TTL_S})  # noqa: TID251
    bot = cfg["telegram_bot"]
    return web.json_response({"code": code, "bot": bot, "url": f"https://t.me/{bot}?start={code}",
                              "expires_s": telegram.LINK_CODE_TTL_S})

  async def tg_unlink(request):
    data = await _body(request)
    changes = {"telegram_chat_id": "", "telegram_chat_name": "", "telegram_link_code": "", "telegram_link_expires": 0.0}
    if data.get("forget_bot"):
      changes.update({"telegram_token": "", "telegram_bot": ""})
    config.update(changes)
    return web.json_response(_tg_public())

  async def tg_test(request):
    cfg = config.load()
    if not config.telegram_ready(cfg):
      return web.json_response({"error": "Telegram no está vinculado."}, status=409)
    status = config.read_json(config.STATUS_FILE, default={}) or {}
    text = "🧪 <b>Prueba de Sentinel</b>\nSi ves esto, los avisos funcionan.\n\n" + telegram.status_text(status, cfg)
    loop = asyncio.get_running_loop()
    try:
      await loop.run_in_executor(None, lambda: telegram.api(cfg["telegram_token"], "sendMessage",
                                                            {"chat_id": cfg["telegram_chat_id"], "text": text, "parse_mode": "HTML"}))
    except Exception as e:
      return web.json_response({"error": str(e)}, status=502)
    return web.json_response({"ok": True})

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
    web.post("/api/telegram/token", tg_token),
    web.post("/api/telegram/link", tg_link),
    web.post("/api/telegram/unlink", tg_unlink),
    web.post("/api/telegram/test", tg_test),
    web.get("/media/{eid}/{name}", media),
  ])
  return app


def main() -> None:
  web.run_app(make_app(), host="0.0.0.0", port=PORT, print=None, access_log=None)


if __name__ == "__main__":
  main()
