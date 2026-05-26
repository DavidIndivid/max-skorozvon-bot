"""Конфигурация интеграции — Render-совместимая версия (секреты из env vars)."""
import base64
import json
import os

# === Скорозвон ===
SKOROZVON_BASE_URL      = "https://api.skorozvon.ru"
SKOROZVON_USERNAME      = os.environ.get("SKORO_USERNAME", "")
SKOROZVON_API_KEY       = os.environ.get("SKORO_API_KEY", "")
SKOROZVON_CLIENT_ID     = os.environ.get("SKORO_CLIENT_ID", "")
SKOROZVON_CLIENT_SECRET = os.environ.get("SKORO_CLIENT_SECRET", "")

TOKEN_STORE_PATH = "/tmp/.skorozvon_token.json"

# === Google Sheets ===
def _load_google_creds() -> dict:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        return {}
    # Убираем все пробелы/переносы — Render и copy-paste часто их добавляют
    clean = "".join(raw.split())
    try:
        return json.loads(base64.b64decode(clean).decode("utf-8"))
    except Exception:
        pass
    # Fallback: может быть передан сырой JSON (не base64)
    try:
        return json.loads(raw)
    except Exception:
        return {}

GOOGLE_SERVICE_ACCOUNT_INFO: dict = _load_google_creds()
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# --- МАСТЕР-ТАБЛИЦА ---
MASTER_SPREADSHEET = {
    "spreadsheet_id": "1vZlFZCtQIeFu8ZOT90HWI4c-3PoG-xtbk1nhYTXQX2I",
    "sheet_name": "Лист1",
    "data_start_row": 2,
    "columns": {
        "created": 0,
        "project": 1,
        "phone": 2,
        "tag": 3,
        "status": 4,
    }
}

# --- МАППИНГ ТЕГОВ → ПРОЕКТОВ ---
PROJECT_MAPPING = {
    "Планета мебели":   20000134384,
    "Стяжка ЮФО":       20000134398,
    "Ремонт Краснодар": 20000134402,
    "Ремонт Побережье": 20000134407,
    "Китай FLS":        20000134411,
}

# --- ТАБЛИЦЫ ЗАКАЗЧИКОВ ---
INPUT_SPREADSHEETS = [
    {
        "name": "Планета мебели",
        "client_keyword": "Планета мебели",
        "spreadsheet_id": "1ANRTxzkKSmclBmckJOSsLxpvF7uPKYYQIgjJBws92XA",
        "sheet_name": "База",
        "data_start_row": 2,
        "skorozvon_project_id": 20000134384,
        "columns": {"phone": 2, "created": 3, "status": 4, "comment": 5, "project": 1}
    },
    {
        "name": "Стяжка ЮФО",
        "client_keyword": "Стяжка ЮФО",
        "spreadsheet_id": "1tnM1x_fpfRSKDdwctgHl3R0vO_l5uFnmUt_yTkcDRuc",
        "sheet_name": "База",
        "data_start_row": 3,
        "skorozvon_project_id": 20000134398,
        "columns": {"phone": 2, "created": 3, "status": 4, "comment": 5, "project": 1}
    },
    {
        "name": "Ремонт Гарантия",
        "client_keyword": "Ремонт Краснодар",
        "spreadsheet_id": "1ONIVfkAP4zDD_iOtvmn8Xnfj5VrcENm80IkVA5Rlzpk",
        "sheet_name": "База Краснодар ",
        "data_start_row": 3,
        "skorozvon_project_id": 20000134402,
        "columns": {"phone": 2, "created": 3, "status": 4, "comment": 5, "project": 1}
    },
    {
        "name": "Ремонт Побережье",
        "client_keyword": "Ремонт Побережье",
        "spreadsheet_id": "1ONIVfkAP4zDD_iOtvmn8Xnfj5VrcENm80IkVA5Rlzpk",
        "sheet_name": "База Побережье",
        "data_start_row": 3,
        "skorozvon_project_id": 20000134407,
        "columns": {"phone": 2, "created": 3, "status": 4, "comment": 5, "project": 1}
    },
    {
        "name": "Китай FLS",
        "client_keyword": "Китай FLS",
        "spreadsheet_id": "1V6txHpVhGcERskuCmZZMrpc8xxWNLz-8WfNc-VQlEXM",
        "sheet_name": "База",
        "data_start_row": 2,
        "skorozvon_project_id": 20000134411,
        "columns": {"phone": 2, "created": 3, "status": 4, "comment": 5, "project": 1}
    }
]

OUTPUT_SPREADSHEET_ID    = "17tjBicGhTgnXkRYc0Nqe06KNzaUtU6krAKY6vJudYZU"
ALSO_LOG_TO_OUTPUT_SHEET = False
DEDUP_DB_PATH            = "/tmp/dedup.db"
LOGS_DIR                 = "/tmp/logs"
CUSTOM_FIELD_CREATED_DATE_ID = 20000018132

RESULT_NAME_TO_STATUS = {
    "ЛИД": "ЛИД", "Успех": "ЛИД", "На будущие": "ЛИД",
    "Отказ": "Отказ", "отказ": "Отказ",
    "Неудобно говорить": "Недозвон", "Перезвонить": "Недозвон",
    "Автоответчик": "Недозвон", "Молчание": "Недозвон"
}
RESULT_GROUP_TO_STATUS = {
    "Успешные": "ЛИД", "Неуспешные": "Отказ",
    "Промежуточные": "Недозвон", "Недозвон": "Недозвон"
}
DEFAULT_STATUS = "Недозвон"

MAX_RETRIES          = 3
PAGE_SIZE            = 100
IMPORT_DELAY_SECONDS = 32
RATE_LIMIT_RETRY_DELAY = 5
