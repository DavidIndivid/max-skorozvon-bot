#!/usr/bin/env python3
"""Max Bot — управление проектами Скорозвон. Polling mode."""
import os
import time
import logging
import threading
import requests
from flask import Flask, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
TOKEN     = os.environ["MAX_BOT_TOKEN"]
MAX_BASE  = "https://botapi.max.ru"

SKORO_BASE = "https://api.skorozvon.ru"
SKORO_USER = os.environ["SKORO_USERNAME"]
SKORO_KEY  = os.environ["SKORO_API_KEY"]
SKORO_CID  = os.environ["SKORO_CLIENT_ID"]
SKORO_CSEC = os.environ["SKORO_CLIENT_SECRET"]

_raw = os.environ.get("ALLOWED_IDS", "")
ALLOWED: set = set(int(x) for x in _raw.split(",") if x.strip()) if _raw else set()

PROJECTS = [
    {"name": "Планета мебели",   "id": 20000134384},
    {"name": "Стяжка ЮФО",      "id": 20000134398},
    {"name": "Ремонт Краснодар", "id": 20000134402},
    {"name": "Ремонт Побережье", "id": 20000134407},
    {"name": "Китай FLS",        "id": 20000134411},
]

STATE_RU = {
    "active":    "активен ▶️",
    "paused":    "на паузе ⏸",
    "stopped":   "остановлен",
    "completed": "завершён",
}

# last message id per user — для редактирования
_last_mid: dict[int, str] = {}

# ── Skorozvon auth ─────────────────────────────────────────────────────────────
_skoro_cache: dict = {}

def _skoro_token() -> str:
    if _skoro_cache.get("exp", 0) > time.time() + 60:
        return _skoro_cache["tok"]
    r = requests.post(
        f"{SKORO_BASE}/oauth/token",
        data={
            "grant_type":    "password",
            "username":      SKORO_USER,
            "api_key":       SKORO_KEY,
            "client_id":     SKORO_CID,
            "client_secret": SKORO_CSEC,
        },
        timeout=15,
    )
    r.raise_for_status()
    d = r.json()
    _skoro_cache["tok"] = d["access_token"]
    _skoro_cache["exp"] = time.time() + d.get("expires_in", 7200)
    log.info("Skorozvon: token refreshed")
    return d["access_token"]

def _skoro(method: str, path: str, **kw):
    h = {"Authorization": f"Bearer {_skoro_token()}"}
    r = requests.request(
        method, f"{SKORO_BASE}/api/v2{path}", headers=h, timeout=15, **kw
    )
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {}

def get_projects_state() -> dict:
    """Запрашиваем каждый проект по ID — быстро и надёжно."""
    states = {}
    for p in PROJECTS:
        try:
            resp = _skoro("GET", f"/call_projects/{p['id']}")
            # ответ может быть {data: {...}} или сам объект
            data = resp.get("data") or resp
            states[p["id"]] = data.get("state") or "unknown"
        except Exception as e:
            log.warning(f"Failed to get project {p['id']}: {e}")
            states[p["id"]] = "unknown"
    log.info(f"States: {states}")
    return states

def project_action(pid: int, action: str):
    return _skoro("POST", f"/call_projects/{pid}/{action}")

# ── Max Bot API ────────────────────────────────────────────────────────────────
def _max(method: str, path: str, **kw):
    headers = kw.pop("headers", {})
    headers["Authorization"] = TOKEN
    r = requests.request(
        method, f"{MAX_BASE}{path}", headers=headers, timeout=40, **kw
    )
    try:
        data = r.json()
        if r.status_code not in (200, 204):
            log.warning(f"API {method} {path} → {r.status_code}: {data}")
        return data
    except Exception:
        log.warning(f"API {method} {path} → {r.status_code}: {r.text[:200]}")
        return {}

def _build_body(text: str, buttons=None) -> dict:
    body: dict = {"text": text}
    if buttons:
        body["attachments"] = [{
            "type": "inline_keyboard",
            "payload": {"buttons": buttons},
        }]
    return body

def send(user_id: int, text: str, buttons=None) -> str | None:
    """Отправляет новое сообщение, возвращает mid."""
    result = _max("POST", "/messages", params={"user_id": user_id},
                  json=_build_body(text, buttons))
    mid = result.get("message", {}).get("body", {}).get("mid")
    if mid:
        _last_mid[user_id] = mid
    log.info(f"Sent to {user_id}, mid={mid}")
    return mid

def edit(mid: str, user_id: int, text: str, buttons=None):
    """Редактирует существующее сообщение."""
    result = _max("PUT", "/messages", params={"message_id": mid},
                  json=_build_body(text, buttons))
    log.info(f"Edited mid={mid}: {result}")

def send_or_edit(user_id: int, text: str, buttons=None):
    """Редактирует последнее сообщение пользователя, или шлёт новое."""
    mid = _last_mid.get(user_id)
    if mid:
        try:
            edit(mid, user_id, text, buttons)
            return
        except Exception as e:
            log.warning(f"Edit failed ({e}), sending new")
    send(user_id, text, buttons)

def notify_cb(callback_id: str, text: str):
    try:
        _max("POST", "/answers", params={"callback_id": callback_id},
             json={"notification": text})
    except Exception:
        pass

# ── UI builders ────────────────────────────────────────────────────────────────
def _build_projects_view():
    try:
        states = get_projects_state()
    except Exception as e:
        return f"❌ Ошибка Скорозвона:\n{e}", None

    lines = ["📋 Проекты Скорозвон:\n"]
    buttons = []
    for p in PROJECTS:
        state = states.get(p["id"], "unknown")
        state_ru = STATE_RU.get(state, state)
        if state == "active":
            icon = "▶️"
            btn_text = f"⏸ Стоп — {p['name']}"
            btn_pay  = f"stop_{p['id']}"
        else:
            icon = "⏸"
            btn_text = f"▶️ Старт — {p['name']}"
            btn_pay  = f"start_{p['id']}"
        lines.append(f"  {icon} {p['name']} — {state_ru}")
        buttons.append([{"type": "callback", "text": btn_text, "payload": btn_pay}])

    buttons.append([{"type": "callback", "text": "🔄 Обновить", "payload": "projects"}])
    return "\n".join(lines), buttons

def _build_main_menu():
    return (
        "Выберите действие:",
        [[{"type": "callback", "text": "📋 Проекты", "payload": "projects"}]]
    )

# ── Event handlers ─────────────────────────────────────────────────────────────
def on_message(chat_id: int, user_id: int, text: str):
    if ALLOWED and user_id not in ALLOWED:
        send(user_id, "⛔ Нет доступа.")
        return
    log.info(f"Message from user_id={user_id}: {text!r}")
    txt, btns = _build_main_menu()
    send(user_id, txt, btns)

def on_callback(user_id: int, callback_id: str, payload: str):
    if ALLOWED and user_id not in ALLOWED:
        notify_cb(callback_id, "⛔ Нет доступа")
        return

    log.info(f"Callback from user_id={user_id}: {payload!r}")

    if payload == "projects":
        txt, btns = _build_projects_view()
        send_or_edit(user_id, txt, btns)
        return

    if payload == "main_menu":
        txt, btns = _build_main_menu()
        send_or_edit(user_id, txt, btns)
        return

    if payload.startswith("start_") or payload.startswith("stop_"):
        action, pid_str = payload.split("_", 1)
        pid   = int(pid_str)
        pname = next((p["name"] for p in PROJECTS if p["id"] == pid), str(pid))
        try:
            project_action(pid, action)
            label = "запущен ▶️" if action == "start" else "остановлен ⏸"
            notify_cb(callback_id, f"✅ {pname} {label}")
            log.info(f"Project {pname} ({pid}) {action}ed by user {user_id}")
        except Exception as e:
            notify_cb(callback_id, f"❌ Ошибка: {e}")
            log.error(f"project_action failed: {e}")
            return
        time.sleep(1)
        txt, btns = _build_projects_view()
        send_or_edit(user_id, txt, btns)

# ── Process single update ───────────────────────────────────────────────────────
def handle_update(upd: dict):
    utype = upd.get("update_type", "")

    if utype == "message_created":
        msg       = upd.get("message", {})
        recipient = msg.get("recipient", {})
        chat_id   = recipient.get("chat_id")
        user_id   = msg.get("sender", {}).get("user_id", 0)
        text      = msg.get("body", {}).get("text", "")
        if chat_id and user_id:
            on_message(chat_id, user_id, text)

    elif utype == "message_callback":
        cb          = upd.get("callback", {})
        user_id     = cb.get("user", {}).get("user_id", 0)
        callback_id = cb.get("callback_id", "")
        payload     = cb.get("payload", "")
        log.info(f"Callback: user_id={user_id} payload={payload!r}")
        if user_id:
            on_callback(user_id, callback_id, payload)

    else:
        log.info(f"Unknown update type: {utype}")

# ── Flask app ──────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/", methods=["GET"])
def health():
    return "ok"

@app.route("/", methods=["POST"])
def webhook():
    data = request.get_json(force=True, silent=True)
    if data:
        try:
            handle_update(data)
        except Exception as e:
            log.error(f"handle_update error: {e}")
    return "ok", 200

# ── Startup ────────────────────────────────────────────────────────────────────
def delete_all_webhooks():
    try:
        subs = _max("GET", "/subscriptions")
        log.info(f"Current subscriptions: {subs}")
        for s in (subs.get("subscriptions") or []):
            url = s.get("url", "")
            if url:
                _max("DELETE", "/subscriptions", params={"url": url})
    except Exception as e:
        log.warning(f"Failed to delete subscriptions: {e}")

def polling_loop():
    log.info("Polling started")
    marker = None
    while True:
        try:
            params: dict = {"timeout": 30}
            if marker:
                params["marker"] = marker
            resp = _max("GET", "/updates", params=params)
            marker = resp.get("marker", marker)
            updates = resp.get("updates") or []
            log.info(f"Poll: marker={marker} updates={len(updates)}")
            for upd in updates:
                handle_update(upd)
        except requests.exceptions.Timeout:
            log.info("Poll timeout, retrying")
            time.sleep(1)
        except requests.exceptions.ConnectionError as e:
            log.warning(f"Connection error: {e}")
            time.sleep(10)
        except Exception as e:
            log.error(f"Polling error: {e}", exc_info=True)
            time.sleep(5)

if __name__ == "__main__":
    log.info(f"Token prefix: {TOKEN[:8]}...")
    me = _max("GET", "/me")
    log.info(f"Bot info: {me}")

    delete_all_webhooks()

    port = int(os.environ.get("PORT", 8080))
    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, use_reloader=False, threaded=True),
        daemon=True,
    )
    flask_thread.start()
    log.info(f"Flask started on port {port}")

    polling_loop()
