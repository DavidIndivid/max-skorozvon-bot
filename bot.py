#!/usr/bin/env python3
"""Max Bot — управление проектами Скорозвон. Polling mode."""
import os
import re
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

SKORO_BASE      = "https://api.skorozvon.ru"
SKORO_APP_BASE  = "https://app.skorozvon.ru"
# Шард-специфичный URL (из DevTools): pod5-shard2-lb1.skorozvon.ru
SKORO_SHARD_URL = os.environ.get("SKORO_SHARD_URL", "https://pod5-shard2-lb1.skorozvon.ru")
SKORO_USER = os.environ["SKORO_USERNAME"]
SKORO_KEY  = os.environ["SKORO_API_KEY"]
SKORO_CID  = os.environ["SKORO_CLIENT_ID"]
SKORO_CSEC = os.environ["SKORO_CLIENT_SECRET"]

_raw = os.environ.get("ALLOWED_IDS", "")
ALLOWED: set = set(int(x) for x in _raw.split(",") if x.strip()) if _raw else set()

SKORO_WEB_EMAIL    = os.environ.get("SKORO_WEB_EMAIL", SKORO_USER)
SKORO_WEB_PASSWORD = os.environ.get("SKORO_WEB_PASSWORD", "")

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

_last_mid: dict[int, str] = {}
_seen_updates: set[str] = set()

# ── Skorozvon auth ─────────────────────────────────────────────────────────────
_skoro_cache: dict = {}
_web_cache:   dict = {}

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
    log.info("Skorozvon: API token refreshed")
    return d["access_token"]

def _extract_csrf(html: str) -> str | None:
    """Извлекает CSRF-token из HTML страницы (meta тег)."""
    m = re.search(r'<meta\s[^>]*name=["\']csrf-token["\'][^>]*content=["\']([^"\']+)["\']', html)
    if not m:
        m = re.search(r'<meta\s[^>]*content=["\']([^"\']+)["\'][^>]*name=["\']csrf-token["\']', html)
    return m.group(1) if m else None

def _cache_web_token(tok: str, expires_in: int = 7200):
    _web_cache["tok"] = tok
    _web_cache["exp"] = time.time() + expires_in - 60

def _try_browser_login(pw: str) -> str | None:
    """Браузерный логин через app.skorozvon.ru — получаем auth_token cookie (Skorozvon JWT)."""
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    })

    # Шаг 1: загружаем страницу — получаем cookies и CSRF-token
    csrf_token = None
    for page_url in [f"{SKORO_APP_BASE}/users/sign_in", SKORO_APP_BASE]:
        try:
            r = sess.get(page_url, timeout=15, allow_redirects=True)
            log.info(f"GET {page_url} → {r.status_code} (url={r.url})")
            if r.status_code == 200 and r.text:
                csrf_token = _extract_csrf(r.text)
                if csrf_token:
                    log.info(f"CSRF token found on {page_url}")
                    break
        except Exception as e:
            log.warning(f"GET {page_url} error: {e}")

    def _check_cookies_for_jwt() -> str | None:
        for name in ("auth_token", "access_token", "token"):
            val = sess.cookies.get(name)
            if val and val.startswith("eyJ"):
                log.info(f"Found JWT in cookie '{name}'")
                return val
        return None

    def _check_response_for_jwt(r: requests.Response) -> str | None:
        try:
            d = r.json()
            for key in ("auth_token", "access_token", "token"):
                val = d.get(key)
                if val and val.startswith("eyJ"):
                    log.info(f"Found JWT in response key '{key}'")
                    return val
        except Exception:
            pass
        return _check_cookies_for_jwt()

    # Шаг 2: пробуем form-submit на разные login-эндпоинты
    form_data: dict = {
        "user[email]":    SKORO_WEB_EMAIL,
        "user[password]": pw,
        "utf8":           "✓",
    }
    if csrf_token:
        form_data["authenticity_token"] = csrf_token

    form_endpoints = [
        f"{SKORO_APP_BASE}/users/sign_in",
        f"{SKORO_APP_BASE}/sign_in",
        f"{SKORO_APP_BASE}/login",
        f"{SKORO_SHARD_URL}/users/sign_in",
    ]
    for url in form_endpoints:
        try:
            r = sess.post(url, data=form_data, allow_redirects=True, timeout=15,
                          headers={"Content-Type": "application/x-www-form-urlencoded",
                                   "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9"})
            log.info(f"Form POST {url} → {r.status_code} cookies={list(sess.cookies.keys())}")
            tok = _check_response_for_jwt(r)
            if tok:
                return tok
        except Exception as e:
            log.warning(f"Form POST {url} error: {e}")

    # Шаг 3: пробуем JSON-эндпоинты
    json_endpoints = [
        (f"{SKORO_APP_BASE}/api/v1/users/sign_in", {"email": SKORO_WEB_EMAIL, "password": pw}),
        (f"{SKORO_APP_BASE}/api/sessions",          {"email": SKORO_WEB_EMAIL, "password": pw}),
        (f"{SKORO_APP_BASE}/supreme/sessions",      {"email": SKORO_WEB_EMAIL, "password": pw}),
        (f"{SKORO_APP_BASE}/supreme/auth",          {"email": SKORO_WEB_EMAIL, "password": pw}),
        (f"{SKORO_SHARD_URL}/api/v1/sessions",      {"email": SKORO_WEB_EMAIL, "password": pw}),
    ]
    for url, body in json_endpoints:
        try:
            r = sess.post(url, json=body, allow_redirects=True, timeout=10,
                          headers={"Accept": "application/json"})
            log.info(f"JSON POST {url} → {r.status_code} {r.text[:150]!r}")
            tok = _check_response_for_jwt(r)
            if tok:
                return tok
        except Exception as e:
            log.warning(f"JSON POST {url} error: {e}")

    return None

def _web_token() -> str:
    """Веб-JWT (iss:Skorozvon) для /resurgent/ эндпоинтов."""
    if _web_cache.get("exp", 0) > time.time() + 60:
        return _web_cache["tok"]

    # Приоритет 1: вручную заданный токен
    static = os.environ.get("SKORO_WEB_TOKEN", "")
    if static:
        _cache_web_token(static)
        log.info("Skorozvon: web token from SKORO_WEB_TOKEN env")
        return static

    pw = SKORO_WEB_PASSWORD
    if not pw:
        raise RuntimeError("Установите SKORO_WEB_TOKEN или SKORO_WEB_PASSWORD в Render.")

    # Приоритет 2: браузерный логин — получаем Skorozvon JWT (iss:Skorozvon)
    tok = _try_browser_login(pw)
    if tok:
        _cache_web_token(tok)
        log.info("Skorozvon: web JWT via browser login")
        return tok

    raise RuntimeError(
        "Не удалось получить веб-токен автоматически. "
        "Установите SKORO_WEB_TOKEN в Render env vars вручную."
    )

def _skoro(method: str, path: str, raise_on_4xx: bool = True, **kw):
    h = {"Authorization": f"Bearer {_skoro_token()}"}
    r = requests.request(
        method, f"{SKORO_BASE}/api/v2{path}", headers=h, timeout=15, **kw
    )
    if not raise_on_4xx and 400 <= r.status_code < 500:
        log.warning(f"Skorozvon {method} {path} → {r.status_code} (ignored): {r.text[:200]}")
        return {}
    r.raise_for_status()
    log.info(f"Skorozvon {method} {path} → {r.status_code} body={r.text[:200]!r}")
    try:
        return r.json()
    except Exception:
        return {}

def get_projects_state() -> dict:
    states = {}
    for p in PROJECTS:
        try:
            resp = _skoro("GET", f"/call_projects/{p['id']}")
            data = resp.get("data") or resp
            states[p["id"]] = data.get("state") or "unknown"
        except Exception as e:
            log.warning(f"Failed to get project {p['id']}: {e}")
            states[p["id"]] = "unknown"
    log.info(f"States: {states}")
    return states

def get_project_not_called(pid: int) -> str:
    """Возвращает кол-во лидов 'Ещё не звонили' (case_state=uploaded)."""
    try:
        resp = _skoro("GET", "/leads", params={
            "call_project_id": pid,
            "case_state": "uploaded",
            "page": 1,
            "length": 1,
        })
        total = (resp.get("pagination") or {}).get("total")
        if total is not None:
            return str(total)
        return "?"
    except Exception as e:
        log.warning(f"Stats error for {pid}: {e}")
        return "?"

def project_action(pid: int, action: str) -> str | None:
    """Выполняет start/stop через веб-эндпоинт /resurgent/change_state."""
    state    = "active" if action == "start" else "paused"
    substate = "starting" if action == "start" else "stopping"
    try:
        tok = _web_token()
    except Exception as e:
        log.error(f"Web token failed: {e}")
        return f"Ошибка авторизации: {e}"
    h = {
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    r = requests.put(
        f"{SKORO_SHARD_URL}/resurgent/call_projects/{pid}/change_state",
        headers=h, json={"state": state, "substate": substate}, timeout=15,
    )
    log.info(f"project_action {action} {pid} → {r.status_code} {r.text[:300]!r}")
    if 400 <= r.status_code < 500:
        try:
            errs = r.json().get("errors") or []
            return "; ".join(errs) if errs else r.text[:200]
        except Exception:
            return r.text[:200]
    return None

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
    result = _max("POST", "/messages", params={"user_id": user_id},
                  json=_build_body(text, buttons))
    mid = result.get("message", {}).get("body", {}).get("mid")
    if mid:
        _last_mid[user_id] = mid
    log.info(f"Sent to {user_id}, mid={mid}")
    return mid

def edit(mid: str, user_id: int, text: str, buttons=None):
    result = _max("PUT", "/messages", params={"message_id": mid},
                  json=_build_body(text, buttons))
    log.info(f"Edited mid={mid}: {result}")

def send_or_edit(user_id: int, text: str, buttons=None):
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
def _render_projects(states: dict, not_called: dict | None = None):
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
        nc = ""
        if not_called:
            nc_val = not_called.get(p["id"], "")
            if nc_val:
                nc = f"  (ещё не звонили: {nc_val})"
        lines.append(f"  {icon} {p['name']} — {state_ru}{nc}")
        buttons.append([{"type": "callback", "text": btn_text, "payload": btn_pay}])
    buttons.append([{"type": "callback", "text": "🔄 Обновить", "payload": "projects"}])
    return "\n".join(lines), buttons

def _build_projects_view():
    try:
        states = get_projects_state()
    except Exception as e:
        return f"❌ Ошибка Скорозвона:\n{e}", None
    # Подтягиваем "Ещё не звонили" для каждого проекта
    not_called = {}
    for p in PROJECTS:
        not_called[p["id"]] = get_project_not_called(p["id"])
    return _render_projects(states, not_called)


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
            err = project_action(pid, action)
        except Exception as e:
            notify_cb(callback_id, f"❌ Ошибка: {e}")
            log.error(f"project_action failed: {e}")
            return
        if err:
            notify_cb(callback_id, f"⚠️ Скорозвон: {err}")
            log.warning(f"project_action {action} {pname} ({pid}) rejected: {err}")
        else:
            label = "запускается ▶️" if action == "start" else "останавливается ⏸"
            notify_cb(callback_id, f"✅ {pname} {label}")
            log.info(f"Project {pname} ({pid}) {action}ed by user {user_id}")
        # Пауза — даём Скорозвону обновить состояние, затем показываем реальный статус
        time.sleep(5)
        txt, btns = _build_projects_view()
        send_or_edit(user_id, txt, btns)

# ── Process single update ───────────────────────────────────────────────────────
def handle_update(upd: dict):
    utype = upd.get("update_type", "")

    if utype == "message_created":
        dedup_key = upd.get("message", {}).get("body", {}).get("mid", "")
    elif utype == "message_callback":
        dedup_key = upd.get("callback", {}).get("callback_id", "")
    else:
        dedup_key = ""
    if dedup_key:
        if dedup_key in _seen_updates:
            log.warning(f"Duplicate update ignored: {dedup_key}")
            return
        _seen_updates.add(dedup_key)
        if len(_seen_updates) > 500:
            _seen_updates.clear()

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

@app.route("/", methods=["GET", "HEAD"])
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
