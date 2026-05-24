#!/usr/bin/env python3
"""
Ежедневный экспорт: Мастер-таблица + Skorozvon → 5 таблиц заказчиков.

Логика:
  0. Сначала запускаем upload — отправляем все новые номера в Скорозвон
  1. Читаем ВСЕ номера из Мастер-таблицы (она источник истины о лидах)
  2. Берём статусы+комментарии из Скорозвона за последние 14 дней
  3. Роутинг по тегу (кол. F мастера)
  4. Существующие строки → обновляем только ячейки E (статус) и F (комментарий)
  5. Новые строки → пишем точечно в A:F, не трогаем G+ (партнёрские данные)
"""
import config
import sheets
import upload as upload_module
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from googleapiclient.discovery import build
from google.oauth2 import service_account
from skorozvon_api import SkorozvonAPI
from utils import normalize_phone, setup_logging

log = setup_logging("export_final")

LOOKBACK_DAYS = 14
MSK = ZoneInfo("Europe/Moscow")


def _svc():
    creds = service_account.Credentials.from_service_account_info(
        config.GOOGLE_SERVICE_ACCOUNT_INFO, scopes=config.GOOGLE_SCOPES
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def get_call_status(call: dict) -> str:
    result_name = (call.get("result_name") or "").strip()
    if result_name and result_name in config.RESULT_NAME_TO_STATUS:
        return config.RESULT_NAME_TO_STATUS[result_name]
    group = (call.get("scenario_result_group_title") or "").strip()
    if group and group in config.RESULT_GROUP_TO_STATUS:
        return config.RESULT_GROUP_TO_STATUS[group]
    return config.DEFAULT_STATUS


def parse_call_dt(call: dict) -> datetime:
    raw = call.get("created_at") or call.get("started_at")
    if isinstance(raw, dict):
        raw = raw.get("utc") or raw.get("iso")
    if not raw:
        return datetime.min
    try:
        s = str(raw).replace(" UTC", "").replace("Z", "").replace("T", " ").split(".")[0].strip()
        return datetime.fromisoformat(s)
    except Exception:
        return datetime.min


def parse_master_date(date_str: str) -> datetime:
    if not date_str:
        return datetime.min
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    return datetime.min


# ── Запись в Google Sheets ─────────────────────────────────────────────────────

def col_letter(idx: int) -> str:
    return sheets.col_letter(idx)


def write_cell_updates(spreadsheet_id: str, sheet_name: str, updates: list) -> int:
    if not updates:
        return 0
    svc = _svc()
    data = [
        {"range": f"'{sheet_name}'!{u['range']}", "values": [[u["value"]]]}
        for u in updates
    ]
    result = svc.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()
    return result.get("totalUpdatedCells", 0)


def write_new_rows(spreadsheet_id: str, sheet_name: str,
                   start_row: int, rows: list) -> int:
    if not rows:
        return 0
    svc = _svc()
    last_col = col_letter(len(rows[0]) - 1)
    end_row = start_row + len(rows) - 1
    range_str = f"'{sheet_name}'!A{start_row}:{last_col}{end_row}"
    result = svc.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=range_str,
        valueInputOption="USER_ENTERED",
        body={"values": rows},
    ).execute()
    return result.get("updatedRows", 0)


def sort_sheet_by_date(spreadsheet_id: str, sheet_name: str,
                       data_start_row: int, date_col_idx: int) -> None:
    svc = _svc()
    meta = svc.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties"
    ).execute()
    sheet_gid = None
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == sheet_name:
            sheet_gid = s["properties"]["sheetId"]
            break
    if sheet_gid is None:
        log.warning(f"Лист '{sheet_name}' не найден для сортировки")
        return
    svc.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{
            "sortRange": {
                "range": {
                    "sheetId": sheet_gid,
                    "startRowIndex": data_start_row - 1,
                },
                "sortSpecs": [{"dimensionIndex": date_col_idx, "sortOrder": "ASCENDING"}]
            }
        }]}
    ).execute()


def build_new_row(cfg: dict, phone: str, info: dict) -> list:
    cols = cfg["columns"]
    max_col = max(cols.values())
    row = [""] * (max_col + 1)
    row[0]               = info.get("master_id") or ""
    row[cols["project"]] = info.get("pname") or ""
    row[cols["phone"]]   = phone
    row[cols["created"]] = info.get("master_created") or ""
    row[cols["status"]]  = info.get("status") or ""
    if cols.get("comment") is not None:
        row[cols["comment"]] = info.get("comment") or ""
    return row


# ── Обработка одной таблицы ────────────────────────────────────────────────────

def process_project(cfg: dict, project_data: dict, skoro_by_phone: dict):
    name = cfg["name"]

    try:
        existing_rows = sheets.read_input_sheet(cfg)
    except Exception as e:
        log.error(f"  {name}: не удалось прочитать таблицу: {e}")
        return

    phone_to_row: dict[str, int] = {}
    for r in existing_rows:
        ph = normalize_phone(r.get("phone") or "")
        if ph and ph not in phone_to_row:
            phone_to_row[ph] = r["_row"]

    log.info(f"  {name}: в таблице {len(phone_to_row)} номеров, "
             f"из мастера {len(project_data)} номеров")

    cols = cfg["columns"]
    updates = []
    new_rows = []
    skipped = 0
    updated_from_table = 0

    for phone, row_idx in phone_to_row.items():
        skoro = skoro_by_phone.get(phone)
        if not skoro:
            continue
        new_status  = skoro.get("status") or ""
        new_comment = skoro.get("comment") or ""
        if new_status:
            updates.append({"range": f"{col_letter(cols['status'])}{row_idx}", "value": new_status})
        if new_comment:
            updates.append({"range": f"{col_letter(cols['comment'])}{row_idx}", "value": new_comment})
        if new_status or new_comment:
            updated_from_table += 1

    for phone, info in project_data.items():
        if phone in phone_to_row:
            continue
        if not info.get("pname"):
            skipped += 1
            continue
        sort_key = parse_master_date(info.get("master_created"))
        new_rows.append((sort_key, build_new_row(cfg, phone, info)))

    if skipped:
        log.info(f"  {name}: пропущено {skipped} новых без источника")

    updated_cells = 0
    if updates:
        try:
            updated_cells = write_cell_updates(cfg["spreadsheet_id"], cfg["sheet_name"], updates)
        except Exception as e:
            log.error(f"  {name}: ошибка обновления ячеек: {e}", exc_info=True)

    added_rows = 0
    if new_rows:
        new_rows.sort(key=lambda x: x[0])
        sorted_rows = [r for _, r in new_rows]
        last_our_row = max(phone_to_row.values()) if phone_to_row else cfg["data_start_row"] - 1
        start_at = last_our_row + 1
        try:
            added_rows = write_new_rows(cfg["spreadsheet_id"], cfg["sheet_name"],
                                        start_at, sorted_rows)
        except Exception as e:
            log.error(f"  {name}: ошибка добавления строк: {e}", exc_info=True)

    log.info(f"  {name}: ✓ обновлено {updated_cells} ячеек ({updated_from_table} из таблицы), "
             f"добавлено {added_rows} новых строк")

    try:
        sort_sheet_by_date(cfg["spreadsheet_id"], cfg["sheet_name"],
                           cfg["data_start_row"], cfg["columns"]["created"])
        log.info(f"  {name}: ✓ таблица отсортирована по дате")
    except Exception as e:
        log.warning(f"  {name}: не удалось отсортировать: {e}")


# ── Главная функция ────────────────────────────────────────────────────────────

def main():
    log.info("=" * 80)
    log.info("ЭКСПОРТ: Мастер-таблица + Скорозвон → 5 таблиц заказчиков")
    log.info("=" * 80)

    # ── 0. Загружаем новые номера в Скорозвон ─────────────────────────────────
    log.info("▶ Шаг 0: загрузка новых номеров в Скорозвон...")
    try:
        upload_module.main()
        log.info("✓ Upload завершён")
    except Exception as e:
        log.error(f"✗ Upload ошибка: {e}", exc_info=True)

    # ── 1. Читаем ВСЕ номера из Мастер-таблицы ────────────────────────────────
    log.info("▶ Шаг 1: чтение Мастер-таблицы...")
    try:
        master_rows = sheets.read_input_sheet(config.MASTER_SPREADSHEET)
    except Exception as e:
        log.error(f"✗ Не удалось прочитать Мастер-таблицу: {e}")
        return

    master_by_phone: dict[str, dict] = {}
    no_source = 0
    for r in master_rows:
        ph = normalize_phone(r.get("phone"))
        if not ph:
            continue
        pname_raw = (r.get("status") or "").strip()
        pname = pname_raw.split("_", 1)[1] if "_" in pname_raw else pname_raw
        tag   = (r.get("tag") or "").lower().strip()
        if not pname:
            no_source += 1
            continue
        if ph not in master_by_phone:
            master_by_phone[ph] = {
                "master_id":      (r.get("project") or "").strip(),
                "pname":          pname,
                "master_created": (r.get("created") or "").strip(),
                "tag":            tag,
            }

    log.info(f"✓ Мастер-таблица: {len(master_by_phone)} номеров с источником "
             f"(без источника: {no_source})")

    # ── 2. Строим маппинг result_id → статус по сценариям 5 проектов ─────────
    log.info("▶ Шаг 2: маппинг результатов сценариев...")
    now_msk = datetime.now(MSK)
    api = SkorozvonAPI()

    result_id_to_status: dict[int, str] = {}
    for cfg in config.INPUT_SPREADSHEETS:
        try:
            proj = api._request("GET", f"/api/v2/call_projects/{cfg['skorozvon_project_id']}")
            sid = proj.get("scenario_id")
            if not sid:
                continue
            scenario_results = api._request("GET", f"/api/v2/scenarios/{sid}/results",
                                            params={"length": 200}).get("data", [])
            for r in scenario_results:
                gt = r.get("group_title") or ""
                status = config.RESULT_GROUP_TO_STATUS.get(gt, "")
                if r.get("id") and status:
                    result_id_to_status[r["id"]] = status
        except Exception as e:
            log.warning(f"Не удалось получить результаты сценария для {cfg['name']}: {e}")

    log.info(f"✓ Маппинг result_id: {len(result_id_to_status)} записей")

    # ── 2б. Статусы + комментарии из лидов проектов за LOOKBACK_DAYS дней ────
    log.info(f"▶ Шаг 2б: статусы и комментарии из лидов за {LOOKBACK_DAYS} дней...")
    cutoff_str = (now_msk - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    phone_to_lead: dict[str, int] = {}
    for cfg in config.INPUT_SPREADSHEETS:
        for lead in api.list_leads(cfg["skorozvon_project_id"]):
            lcd = lead.get("last_call_date") or ""
            if lcd and lcd[:10] >= cutoff_str:
                ph = normalize_phone(lead.get("phones") or "")
                if ph:
                    phone_to_lead[ph] = lead["id"]

    log.info(f"✓ Лидов с звонками за {LOOKBACK_DAYS} дней: {len(phone_to_lead)}")

    phones_to_query = set(phone_to_lead.keys())
    skoro_by_phone: dict[str, dict] = {}

    def _fetch_status_and_comment(ph: str) -> tuple[str, str, str]:
        try:
            results = api.get_lead_results(phone_to_lead[ph])
            sorted_results = sorted(
                results,
                key=lambda x: (x.get("created_at") or {}).get("utc") or "",
                reverse=True
            )
            status, comment = "", ""
            for r in sorted_results:
                rid = r.get("result_id")
                if not status and rid and rid in result_id_to_status:
                    status = result_id_to_status[rid]
                if not comment:
                    comment = (r.get("comment") or "").strip()
                if status and comment:
                    break
            if not status:
                status = config.DEFAULT_STATUS
            return ph, status, comment
        except Exception as e:
            log.debug(f"Ошибка results лида {phone_to_lead[ph]}: {e}")
            return ph, "", ""

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_fetch_status_and_comment, ph): ph for ph in phones_to_query}
        for future in as_completed(futures):
            ph, status, comment = future.result()
            if status or comment:
                skoro_by_phone[ph] = {"status": status, "comment": comment}

    has_status  = sum(1 for v in skoro_by_phone.values() if v["status"])
    has_comment = sum(1 for v in skoro_by_phone.values() if v["comment"])
    log.info(f"✓ Телефонов: статус={has_status}, комментарий={has_comment} "
             f"(запрошено лидов: {len(phones_to_query)})")

    # ── 3. Роутинг: распределяем по 5 таблицам ────────────────────────────────
    log.info("▶ Шаг 3: роутинг по тегу...")
    valid_cfgs = {cfg["client_keyword"].lower(): cfg for cfg in config.INPUT_SPREADSHEETS}

    data_by_table: dict[str, dict] = {kw: {} for kw in valid_cfgs}
    no_route = 0

    for ph, m in master_by_phone.items():
        matched_kw = next((kw for kw in valid_cfgs if kw in m["tag"]), None)
        if not matched_kw:
            no_route += 1
            continue
        skoro = skoro_by_phone.get(ph, {})
        data_by_table[matched_kw][ph] = {
            "master_id":      m["master_id"],
            "pname":          m["pname"],
            "master_created": m["master_created"],
            "status":         skoro.get("status") or "",
            "comment":        skoro.get("comment") or "",
        }

    summary = ", ".join(f"{kw}={len(v)}" for kw, v in data_by_table.items())
    log.info(f"✓ Распределено: {summary}")
    if no_route:
        log.warning(f"⚠ Без маршрута (неизвестный тег): {no_route}")

    # ── 4. Записываем в каждую таблицу ────────────────────────────────────────
    log.info("\n" + "=" * 80)
    log.info("ЗАПИСЬ В ТАБЛИЦЫ")
    log.info("=" * 80)

    for kw, cfg in valid_cfgs.items():
        process_project(cfg, data_by_table.get(kw, {}), skoro_by_phone)

    log.info("\n" + "=" * 80)
    log.info("ЗАВЕРШЕНО")
    log.info("=" * 80)


if __name__ == "__main__":
    main()
