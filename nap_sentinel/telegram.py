"""Telegram notifications for sentinel, through the user's own bot.

Linking (driven from the web panel):
  1. the user creates a bot with @BotFather and pastes its token in the web;
  2. the web generates a one-time code and shows t.me/<bot>?start=<code>;
  3. the user taps it and presses START; the poller here receives
     "/start <code>", checks it and stores that chat as the only recipient.

Messages:
  * alert as soon as an event fires (reason, strength, time, battery);
  * the low-quality wide camera clip (H.264 ~1 Mbps) once it is exported,
    as a reply to the alert; retried for 24 h if there is no connection;
  * a follow-up if the event was discarded because the car was started.

Commands from the linked chat: /estado /grabar /activar /desactivar /ultimo
"""
from __future__ import annotations

import hmac
import html
import queue
import secrets
import threading
import time
from pathlib import Path

from nap_sentinel import config, storage

API = "https://api.telegram.org/bot{token}/{method}"
MAX_VIDEO_BYTES = 49 * 1024 * 1024       # bot API upload limit is 50 MB
RETRY_FOR_S = 24 * 3600
LINK_CODE_TTL_S = 15 * 60
REASONS = {"impact": "💥 Golpe", "tilt": "📐 Inclinación", "rotate": "↔️ Balanceo", "manual": "🎬 Grabación manual"}
COMMANDS = [
  ("estado", "Estado del coche y de sentinel"),
  ("grabar", "Grabar un clip ahora"),
  ("ultimo", "Reenviar el último vídeo"),
  ("activar", "Activar sentinel"),
  ("desactivar", "Desactivar sentinel"),
]


class TelegramError(Exception):
  def __init__(self, msg: str, code: int | None = None, retry_after: int | None = None):
    super().__init__(msg)
    self.code = code
    self.retry_after = retry_after


def api(token: str, method: str, data: dict | None = None, files: dict | None = None, timeout: float = 20):
  import requests
  try:
    r = requests.post(API.format(token=token, method=method), data=data, files=files, timeout=timeout)
    j = r.json()
  except ValueError as e:
    raise TelegramError(f"respuesta no válida de Telegram ({e})") from None
  except requests.RequestException as e:
    # never let the bot token end up in logs (requests puts the URL in the message)
    raise TelegramError(f"sin conexión con Telegram ({type(e).__name__})") from None
  if not j.get("ok"):
    params = j.get("parameters") or {}
    raise TelegramError(j.get("description", "error"), j.get("error_code"), params.get("retry_after"))
  return j["result"]


def get_me(token: str) -> dict:
  return api(token, "getMe", timeout=10)


def new_link_code() -> str:
  return secrets.token_urlsafe(9).replace("-", "a").replace("_", "b")[:12]


def fmt_time(wall: float, tz: str = "Europe/Madrid") -> str:
  try:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(wall, ZoneInfo(tz)).strftime("%d/%m %H:%M:%S")
  except Exception:
    return time.strftime("%d/%m %H:%M:%S", time.localtime(wall))


def video_meta(path: Path) -> dict:
  """width/height/duration so Telegram shows the clip correctly."""
  try:
    import av
    with av.open(str(path)) as c:
      v = c.streams.video[0]
      dur = float(v.duration * v.time_base) if v.duration else (c.duration or 0) / 1e6
      return {"width": v.codec_context.width, "height": v.codec_context.height, "duration": max(1, round(dur))}
  except Exception:
    return {}


def small_thumbnail(jpg: Path) -> bytes | None:
  """Telegram wants JPEG, max 320x320 and < 200 kB, or it rejects/ignores it."""
  try:
    import io
    from PIL import Image
    im = Image.open(jpg).convert("RGB")
    im.thumbnail((320, 320))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=70)
    return buf.getvalue() if buf.tell() < 200_000 else None
  except Exception:
    return None


def video_thumbnail(video: Path, at_s: float = 0.0) -> bytes | None:
  """Thumbnail taken from the clip actually being sent (so a wide-camera clip
  doesn't show a front-camera preview), resized to Telegram's limits."""
  try:
    import io
    import av
    with av.open(str(video)) as c:
      for frame in c.decode(c.streams.video[0]):
        if frame.time is not None and frame.time + 1e-3 < at_s:
          continue
        im = frame.to_image()
        im.thumbnail((320, 320))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=70)
        return buf.getvalue() if buf.tell() < 200_000 else None
  except Exception:
    return None
  return None


LOG_MAX_BYTES = 64_000


def log_path() -> Path:
  return config.INSTALL_DIR / "telegram.log"


def append_log(line: str) -> None:
  try:
    p = log_path()
    if p.exists() and p.stat().st_size > LOG_MAX_BYTES:
      tail = p.read_text(errors="replace")[-LOG_MAX_BYTES // 2:]
      p.write_text(tail[tail.find("\n") + 1:])
    with open(p, "a") as f:
      f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line.replace("\n", " ") + "\n")
  except Exception:
    pass


def read_log(lines: int = 60) -> list[str]:
  try:
    return log_path().read_text(errors="replace").splitlines()[-lines:]
  except OSError:
    return []


def _tg_state_path(eid: str) -> Path:
  return storage.sentinel_root() / eid / "telegram.json"


def load_tg_state(eid: str) -> dict:
  return config.read_json(_tg_state_path(eid), default={}) or {}


def save_tg_state(eid: str, st: dict) -> None:
  d = storage.sentinel_root() / eid
  if d.is_dir():
    config.atomic_write_json(_tg_state_path(eid), st)


def alert_text(ev: dict, status: dict) -> str:
  reason = REASONS.get(ev.get("reason", ""), ev.get("reason", "Evento"))
  detail = ""
  if ev.get("reason") == "impact" and ev.get("peak"):
    detail = f" ({ev['peak']:.2f} g)"
  elif ev.get("reason") == "tilt":
    mag = next((t["magnitude"] for t in ev.get("triggers", []) if t.get("reason") == "tilt"), 0)
    detail = f" ({mag:.1f}°)" if mag else ""
  lines = [f"<b>{html.escape(reason)}</b>{detail}", f"🕒 {fmt_time(ev.get('wall_time', time.time()))}"]  # noqa: TID251
  if status.get("voltage"):
    lines.append(f"🔋 {status['voltage']:.2f} V")
  lines.append("🎥 Grabando… te envío el vídeo al terminar." if config.load()["telegram_video"] else "🎥 Grabando.")
  return "\n".join(lines)


def status_text(status: dict, cfg: dict) -> str:
  names = {"off": "desactivado", "arming": "armando", "armed": "vigilando", "recording": "grabando"}
  state = "conduciendo" if status.get("onroad") else names.get(status.get("state", ""), "?")
  if not cfg["enabled"]:
    state = "desactivado"
  lines = [f"🛡️ <b>Sentinel: {state}</b>"]
  if cfg["enabled"] and cfg["prerecord"]:
    lines.append(f"⏪ Pre-grabación: {status.get('prerecord_buffer_s', 0)} s en memoria")
  if status.get("voltage"):
    lines.append(f"🔋 Batería 12 V: {status['voltage']:.2f} V")
  if status.get("power_draw_w") is not None:
    lines.append(f"⚡ Consumo: {status['power_draw_w']:.1f} W")
  if status.get("max_temp_c"):
    lines.append(f"🌡️ {round(status['max_temp_c'])} °C")
  if status.get("parked_h"):
    lines.append(f"🅿️ Aparcado: {status['parked_h']:.1f} h")
  ev = next(iter(storage.list_events()), None)
  if ev:
    lines.append(f"📼 Último evento: {html.escape(REASONS.get(ev.get('reason', ''), ev.get('reason', '')))} · {fmt_time(ev['wall_time'])}")
  if status.get("t") and time.time() - status["t"] > 15:  # noqa: TID251
    lines.append("⚠️ sentineld no está actualizando el estado")
  return "\n".join(lines)


class TelegramService:
  """Two daemon threads: a sender (queue) and a long-poll receiver."""
  def __init__(self, log=print, clock=time.monotonic):
    self._ext_log = log
    self.clock = clock
    self.q: queue.Queue = queue.Queue()
    self.offset = 0
    self._stop = threading.Event()
    self._last_scan = 0.0
    self._retry_at: dict[str, float] = {}
    self._threads: list[threading.Thread] = []

  def log(self, msg: str) -> None:
    append_log(msg)
    try:
      self._ext_log(f"telegram: {msg}")
    except Exception:
      pass

  # ── public API (called from sentineld) ────────────────────
  def start(self) -> None:
    for target in (self._sender, self._poller):
      t = threading.Thread(target=target, daemon=True)
      t.start()
      self._threads.append(t)

  def stop(self) -> None:
    self._stop.set()

  def notify_trigger(self, eid: str) -> None:
    self.q.put(("alert", eid))

  def notify_ready(self, eid: str) -> None:
    self.q.put(("video", eid))

  def notify_discard(self, eid: str, alert_msg_id: int | None) -> None:
    self.q.put(("discard", alert_msg_id))

  # ── sender ─────────────────────────────────────────────────
  def _sender(self) -> None:
    while not self._stop.is_set():
      try:
        kind, arg = self.q.get(timeout=5)
      except queue.Empty:
        kind, arg = None, None
      try:
        if kind is not None:
          self.handle(kind, arg)
        self._scan_pending()
      except Exception as e:
        self.log(f"sender: {type(e).__name__}: {e}")

  def handle(self, kind: str, arg) -> None:
    cfg = config.load()
    if not config.telegram_ready(cfg):
      return
    if kind == "alert":
      self._send_alert(cfg, arg)
    elif kind == "video":
      self._send_video(cfg, arg)
    elif kind == "discard":
      api(cfg["telegram_token"], "sendMessage", {
        "chat_id": cfg["telegram_chat_id"],
        "text": "✅ El coche se ha arrancado durante la grabación: era el dueño. Evento descartado.",
        **({"reply_to_message_id": arg} if arg else {}),
      })
    elif kind == "text":
      api(cfg["telegram_token"], "sendMessage", {"chat_id": cfg["telegram_chat_id"], "text": arg, "parse_mode": "HTML"})

  def _send_alert(self, cfg: dict, eid: str) -> None:
    if not cfg["telegram_alerts"]:
      return
    ev = storage.load_event(eid)
    if ev is None:
      return
    st = load_tg_state(eid)
    if st.get("alert_msg_id"):
      return
    status = config.read_json(config.STATUS_FILE, default={}) or {}
    try:
      msg = api(cfg["telegram_token"], "sendMessage", {
        "chat_id": cfg["telegram_chat_id"], "text": alert_text(ev, status), "parse_mode": "HTML"})
      st["alert_msg_id"] = msg.get("message_id")
      st["alert_sent"] = time.time()  # noqa: TID251
    except Exception as e:
      st["alert_error"] = str(e)
      self.log(f"aviso {eid} falló: {e}")
    save_tg_state(eid, st)

  def _pick_video(self, eid: str) -> Path | None:
    d = storage.sentinel_root() / eid
    for name in ("wide_lq.mp4", "road.mp4"):
      f = d / name
      if f.is_file() and f.stat().st_size > 20_000:   # skip empty/broken exports
        return f
    return None

  def _send_video(self, cfg: dict, eid: str, force: bool = False) -> str:
    """Sends the event clip. Returns a short human-readable outcome."""
    if not cfg["telegram_video"] and not force:
      return "envío de vídeo desactivado"
    ev = storage.load_event(eid)
    if ev is None:
      return "el evento ya no existe"
    if ev.get("status") != "ready":
      return f"el vídeo aún no está listo ({ev.get('status')})"
    st = load_tg_state(eid)
    if (st.get("video_sent") or st.get("video_failed")) and not force:
      return "ya enviado"
    if cfg["telegram_video_wifi_only"] and not force:
      net = str((config.read_json(config.STATUS_FILE, default={}) or {}).get("network", ""))
      if "wifi" not in net.lower():
        st["video_waiting_wifi"] = True
        save_tg_state(eid, st)
        return "esperando Wi-Fi"
    video = self._pick_video(eid)
    if video is None:
      errs = ev.get("export_errors") or {}
      why = "; ".join(f"{k}: {v}" for k, v in errs.items() if k in ("wide_lq.h264", "qcamera.h264"))
      st["video_failed"] = True
      st["video_error"] = "no hay vídeo ligero exportado" + (f" ({why})" if why else "")
      save_tg_state(eid, st)
      return st["video_error"]

    token, chat = cfg["telegram_token"], cfg["telegram_chat_id"]
    reply = {"reply_to_message_id": st["alert_msg_id"], "allow_sending_without_reply": "true"} if st.get("alert_msg_id") else {}
    cam = "gran angular" if video.name == "wide_lq.mp4" else "frontal (no hay clip de la gran angular)"
    pre = f" · {round(ev['prerecord_s'])} s antes del golpe" if ev.get("prerecord_s") else ""
    caption = f"🎥 {cam} · {fmt_time(ev['wall_time'])} · {round(ev.get('duration_s') or 0)} s{pre}"
    size = video.stat().st_size
    try:
      if size > MAX_VIDEO_BYTES:
        note = f"{caption}\nEl vídeo ocupa {size / 1e6:.0f} MB (Telegram admite 50 MB). Míralo en el panel web."
        api(token, "sendMessage", {"chat_id": chat, **reply, "text": note})
      else:
        self._upload_video(token, chat, video, caption, reply, at_s=ev.get("trigger_at_s") or 0.0)
      st["video_sent"] = time.time()  # noqa: TID251
      for k in ("video_waiting_wifi", "video_error", "video_failed"):
        st.pop(k, None)
      outcome = "enviado"
      self.log(f"vídeo {eid} enviado ({video.name}, {size / 1e6:.1f} MB)")
    except TelegramError as e:
      st["video_error"] = str(e)
      st["video_tries"] = st.get("video_tries", 0) + 1
      if e.code is not None and e.code != 429 and 400 <= e.code < 500:
        # Telegram rejected it: retrying won't help, tell the user why
        st["video_failed"] = True
        outcome = f"Telegram lo rechazó: {e}"
        self.log(f"vídeo {eid} rechazado: {e} (code {e.code})")
        try:
          api(token, "sendMessage", {"chat_id": chat, **reply,
                                     "text": f"⚠️ No pude enviar el vídeo ({e}). Está guardado en el panel web."})
        except Exception:
          pass
      else:
        delay = e.retry_after or min(600, 30 * 2 ** min(st["video_tries"], 5))
        self._retry_at[eid] = self.clock() + delay
        outcome = f"sin conexión, reintento en {delay} s ({e})"
        self.log(f"vídeo {eid}: {outcome}")
    except Exception as e:
      st["video_error"] = f"{type(e).__name__}: {e}"
      st["video_tries"] = st.get("video_tries", 0) + 1
      self._retry_at[eid] = self.clock() + 120
      outcome = f"error: {st['video_error']}"
      self.log(f"vídeo {eid}: {outcome}")
    save_tg_state(eid, st)
    return outcome

  def _upload_video(self, token: str, chat: str, video: Path, caption: str, reply: dict, at_s: float = 0.0) -> None:
    """sendVideo with metadata and a valid thumbnail; if Telegram rejects the
    request (400), retry plainer, and as a last resort send it as a file."""
    meta = video_meta(video)
    thumb = video_thumbnail(video, at_s)
    base = {"chat_id": chat, "caption": caption}
    attempts = [
      ("sendVideo", {**base, **reply, **meta, "supports_streaming": "true"}, thumb),
      ("sendVideo", {**base, "supports_streaming": "true"}, None),
      ("sendDocument", {**base}, None),
    ]
    last: TelegramError | None = None
    for method, data, th in attempts:
      field = "video" if method == "sendVideo" else "document"
      with open(video, "rb") as f:
        files = {field: (video.name, f, "video/mp4")}
        if th:
          files["thumbnail"] = ("thumb.jpg", th, "image/jpeg")
        try:
          api(token, method, data, files=files, timeout=300)
          return
        except TelegramError as e:
          if e.code != 400:
            raise
          last = e
          self.log(f"{method} rechazado ({e}); probando de otra forma")
    assert last is not None
    raise last

  def _scan_pending(self) -> None:
    """Retry videos that could not be sent (no connection, wifi-only, crash)."""
    now = self.clock()
    if now - self._last_scan < 60:
      return
    self._last_scan = now
    cfg = config.load()
    if not (config.telegram_ready(cfg) and cfg["telegram_video"]):
      return
    for ev in storage.list_events():
      if ev.get("status") != "ready" or time.time() - ev.get("wall_time", 0) > RETRY_FOR_S:  # noqa: TID251
        continue
      st = load_tg_state(ev["id"])
      if st.get("video_sent") or now < self._retry_at.get(ev["id"], 0):
        continue
      self._send_video(cfg, ev["id"])

  # ── receiver (linking + commands) ─────────────────────────
  def _poller(self) -> None:
    last_token = None
    while not self._stop.is_set():
      cfg = config.load()
      token = cfg["telegram_token"]
      linking = bool(cfg["telegram_link_code"]) and time.time() < cfg["telegram_link_expires"]  # noqa: TID251
      if not token or not (linking or cfg["telegram_chat_id"]):
        time.sleep(3)
        continue
      if token != last_token:
        self.offset, last_token = 0, token
      try:
        updates = api(token, "getUpdates", {"offset": self.offset, "timeout": 25,
                                            "allowed_updates": '["message"]'}, timeout=40)
        for u in updates:
          self.offset = max(self.offset, u["update_id"] + 1)
          self.handle_update(cfg, u)
      except TelegramError as e:
        if e.code == 409:  # a webhook is set: getUpdates is not allowed
          try:
            api(token, "deleteWebhook")
          except Exception:
            pass
        time.sleep(e.retry_after or 10)
      except Exception as e:
        self.log(f"poll: {e}")
        time.sleep(15)

  def handle_update(self, cfg: dict, u: dict) -> None:
    msg = u.get("message") or {}
    chat = msg.get("chat") or {}
    text = (msg.get("text") or "").strip()
    if not text or not chat.get("id"):
      return
    chat_id = str(chat["id"])
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    token = cfg["telegram_token"]

    if cmd == "/start" and arg:
      code = cfg["telegram_link_code"]
      if code and time.time() < cfg["telegram_link_expires"] and hmac.compare_digest(arg.strip(), code):  # noqa: TID251
        name = chat.get("first_name") or chat.get("title") or chat.get("username") or chat_id
        config.update({"telegram_chat_id": chat_id, "telegram_chat_name": name,
                       "telegram_link_code": "", "telegram_link_expires": 0.0})
        api(token, "sendMessage", {"chat_id": chat_id, "parse_mode": "HTML", "text":
            "✅ <b>Vinculado con Sentinel</b>\n"
            "Te avisaré aquí de los golpes y te enviaré el vídeo de la cámara gran angular.\n"
            "Comandos: /estado /grabar /ultimo /activar /desactivar"})
        try:
          import json
          api(token, "setMyCommands", {"commands": json.dumps([{"command": c, "description": d} for c, d in COMMANDS])})
        except Exception:
          pass
      return

    if chat_id != cfg["telegram_chat_id"]:
      return  # only the linked chat can talk to the car

    status = config.read_json(config.STATUS_FILE, default={}) or {}
    reply = None
    if cmd in ("/estado", "/status"):
      reply = status_text(status, cfg)
    elif cmd in ("/grabar", "/record"):
      if not cfg["enabled"]:
        reply = "Sentinel está desactivado. Usa /activar primero."
      elif status.get("onroad"):
        reply = "El coche está encendido; sentinel solo graba aparcado."
      else:
        config.TRIGGER_FILE.touch()
        reply = "🎬 Grabando un clip…"
    elif cmd in ("/activar", "/on"):
      config.update({"enabled": True})
      reply = "🛡️ Sentinel activado. Se armará cuando el coche esté aparcado."
    elif cmd in ("/desactivar", "/off"):
      config.update({"enabled": False})
      reply = "Sentinel desactivado."
    elif cmd in ("/ultimo", "/last"):
      ev = next((e for e in storage.list_events() if e.get("status") == "ready"), None)
      if ev is None:
        reply = "No hay vídeos todavía."
      else:
        api(token, "sendMessage", {"chat_id": chat_id, "text": f"📤 Enviando el vídeo del {fmt_time(ev['wall_time'])}…"})
        outcome = self._send_video(config.load(), ev["id"], force=True)
        if outcome != "enviado":
          reply = f"⚠️ No se pudo enviar: {html.escape(outcome)}"
    elif cmd in ("/start", "/help", "/ayuda"):
      reply = "Comandos: /estado /grabar /ultimo /activar /desactivar"
    if reply:
      api(token, "sendMessage", {"chat_id": chat_id, "text": reply, "parse_mode": "HTML"})
