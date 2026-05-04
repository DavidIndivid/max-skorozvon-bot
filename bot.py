#!/usr/bin/env python3
"""Max Bot — управление проектами Скорозвон. Webhook mode."""
import json
import os
import time
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests

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
    return r.json()

def get_projects_state() -> dict:
    data = _skoro("GET", "/call_projects").get("data", [])
    return {p["id"]: p.get("state", "unknown") for p in data}

def project_action(pid: int, action: str):
    return _skoro("POST", f"/call_projects/{pid}/{action}")

def get_project_stats(pid: int) -> dict:
    return _skoro("GET", f"/call_projects/{pid}/statistic")

# ── Max Bot API ────────────────────────────────────────────────────────────────
def _max(method: str, path: str, **kw):
    headers = kw.pop("headers", {})
    headers["Authorization"] = TOKEN
    r = requests.request(
        method, f"{MAX_BASE}{path}", headers=headers, timeout=40, **kw
    )
    try:
        data = r.json()
        if r.status_code != 200:
            log.warning(f"API {method} {path} → {r.status_code}: {data}")
        return data
    except Exception:
        log.warning(f"API {method} {path} → {r.status_code}: {r.text[:200]}")
        return {}

def send(chat_id: int, text: str, buttons=None, user_id: int = None):
    body: dict = {"text": text}
    if buttons:
        body["attachments"] = [{
            "type": "inline_keyboard",
            "payload": {"buttons": buttons},
        }]
    recipient = {"user_id": user_id} if user_id else {"chat_id": chat_id}
    result = _max("POST", "/messages", json={"recipient": recipient, "body": body})
    log.info(f"Send to {recipient} → {result}")
    return result

def notify_cb(callback_id: str, text: str):
    try:
        _max("POST", "/answers", json={"callback_id": callback_id, "notification": text})
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
        if state == "active":
            icon = "▶️"
            btn_text = f"⏸ Стоп — {p['name']}"
            btn_pay  = f"stop_{p['id']}"
        else:
            icon = "⏸"
            btn_text = f"▶️ Старт — {p['name']}"
            btn_pay  = f"start_{p['id']}"
        lines.append(f"  {icon} {p['name']} — {state}")
        buttons.append([{"type": "callback", "text": btn_text, "payload": btn_pay}])

    buttons.append([{"type": "callback", "text": "🔄 Обновить", "payload": "projects"}])
    return "\n".join(lines), buttons

def _build_stats_view(pid: int, pname: str):
    try:
        s = get_project_stats(pid)
        return (
            f"📊 {pname}\n"
            f"Всего контактов: {s.get('cases_count', '?')}\n"
            f"Дозвонились: {s.get('completed_cases_count', '?')}\n"
            f"Недоступны: {s.get('failed_cases_count', '?')}\n"
            f"Всего звонков: {s.get('calls_count', '?')}\n"
            f"Статус: {s.get('state', '?')}"
        )
    except Exception as e:
        return f"❌ Ошибка статистики: {e}"

def _build_main_menu():
    return (
        "Выберите действие:",
        [[{"type": "callback", "text": "📋 Проекты", "payload": "projects"}]]
    )

# ── Event handlers ─────────────────────────────────────────────────────────────
def on_message(chat_id: int, user_id: int, text: str):
    if ALLOWED and user_id not in ALLOWED:
        send(chat_id, "⛔ Нет доступа.", user_id=user_id)
        return
    log.info(f"Message from user_id={user_id} chat_id={chat_id}: {text!r}")
    txt, btns = _build_main_menu()
    send(chat_id, txt, btns, user_id=user_id)

def on_callback(chat_id: int, user_id: int, callback_id: str, payload: str):
    if ALLOWED and user_id not in ALLOWED:
        notify_cb(callback_id, "⛔ Нет доступа")
        return

    log.info(f"Callback from user_id={user_id}: {payload!r}")

    if payload == "projects":
        txt, btns = _build_projects_view()
        send(chat_id, txt, btns, user_id=user_id)
        return

    if payload == "main_menu":
        txt, btns = _build_main_menu()
        send(chat_id, txt, btns, user_id=user_id)
        return

    if payload.startswith("stats_"):
        pid = int(payload.split("_", 1)[1])
        pname = next((p["name"] for p in PROJECTS if p["id"] == pid), str(pid))
        send(chat_id, _build_stats_view(pid, pname), user_id=user_id)
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
        send(chat_id, txt, btns, user_id=user_id)

# ── Process single update ───────────────────────────────────────────────────────
def handle_update(upd: dict):
    utype = upd.get("update_type", "")

    if utype == "message_created":
        msg     = upd.get("message", {})
        chat_id = msg.get("recipient", {}).get("chat_id")
        user_id = msg.get("sender", {}).get("user_id", 0)
        text    = msg.get("body", {}).get("text", "")
        if chat_id:
            on_message(chat_id, user_id, text)

    elif utype == "message_callback":
        cb          = upd.get("callback", {})
        chat_id     = cb.get("message", {}).get("recipient", {}).get("chat_id")
        user_id     = cb.get("user", {}).get("user_id", 0)
        callback_id = cb.get("callback_id", "")
        payload     = cb.get("payload", "")
        if chat_id:
            on_callback(chat_id, user_id, callback_id, payload)

    else:
        log.info(f"Unknown update type: {utype}")

# ── Webhook HTTP server ────────────────────────────────────────────────────────
def make_handler():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else self.rfile.read()
            log.info(f"POST {self.path} headers={dict(self.headers)} body={body[:500]}")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            if body:
                try:
                    upd = json.loads(body)
                    log.info(f"Webhook update_type={upd.get('update_type')}")
                    handle_update(upd)
                except Exception as e:
                    log.error(f"Webhook parse error: {e}")

        def log_message(self, *args):
            pass

    return Handler

def register_webhook():
    """Регистрируем webhook URL в MAX API."""
    webhook_url = os.environ.get("WEBHOOK_URL", "https://max-skorozvon-bot.onrender.com")
    log.info(f"Registering webhook: {webhook_url}")

    # Список существующих подписок
    try:
        subs = _max("GET", "/subscriptions")
        log.info(f"Current subscriptions: {subs}")
        for s in subs.get("subscriptions", []) or []:
            sub_url = s.get("url", "")
            if sub_url and sub_url != webhook_url:
                log.info(f"Removing old subscription: {sub_url}")
                _max("DELETE", "/subscriptions", params={"url": sub_url})
    except Exception as e:
        log.warning(f"Failed to list subscriptions: {e}")

    # Регистрируем новый webhook
    try:
        result = _max("POST", "/subscriptions", json={
            "url": webhook_url,
            "update_types": ["message_created", "message_callback"],
        })
        log.info(f"Webhook registration result: {result}")
    except Exception as e:
        log.error(f"Webhook registration failed: {e}")


def polling_loop():
    """Fallback на long-polling если webhook не работает."""
    log.info("Starting polling loop as fallback")
    marker = None
    while True:
        try:
            params: dict = {"timeout": 30}
            if marker:
                params["marker"] = marker
            resp = _max("GET", "/updates", params=params)
            marker = resp.get("marker", marker)
            updates = resp.get("updates", [])
            if updates:
                log.info(f"Polling: got {len(updates)} updates")
                for upd in updates:
                    handle_update(upd)
        except requests.exceptions.Timeout:
            pass
        except requests.exceptions.ConnectionError as e:
            log.warning(f"Polling connection error: {e}")
            time.sleep(10)
        except Exception as e:
            log.error(f"Polling error: {e}")
            time.sleep(5)


def main():
    import threading
    log.info(f"Token prefix: {TOKEN[:8]}...")
    me = _max("GET", "/me")
    log.info(f"Bot info: {me}")

    register_webhook()

    # Запускаем polling в фоне как страховку
    threading.Thread(target=polling_loop, daemon=True).start()

    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), make_handler())
    log.info(f"Webhook server listening on port {port}")
    server.serve_forever()

if __name__ == "__main__":
    main()
