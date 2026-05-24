import time
from datetime import date
from collections import defaultdict
import config
import dedup
import sheets
from skorozvon_api import SkorozvonAPI
from utils import normalize_phone, setup_logging

log = setup_logging("upload")

_IMPORT_POLL_INTERVAL = 5   # секунд между проверками статуса
_IMPORT_MAX_WAIT = 300       # максимум 5 минут ждём один импорт

def _wait_for_import(api, import_id: int, pid: int) -> None:
    """Опрашивает статус асинхронного импорта до завершения или таймаута."""
    deadline = time.time() + _IMPORT_MAX_WAIT
    while time.time() < deadline:
        time.sleep(_IMPORT_POLL_INTERVAL)
        try:
            status_resp = api.get_import_status(import_id)
            log.info(f"Проект {pid} import {import_id} статус: {status_resp}")
            state = None
            if isinstance(status_resp, dict):
                state = (status_resp.get("state") or
                         (status_resp.get("data") or {}).get("state"))
            inserted = status_resp.get("inserted_count", 0) if isinstance(status_resp, dict) else 0
            dupes    = status_resp.get("duplicates_count", 0) if isinstance(status_resp, dict) else 0
            if state == "loaded":
                if inserted == 0 and dupes == 0:
                    log.warning(f"Проект {pid}: импорт {import_id} завершён — "
                                f"0 вставлено, 0 дублей. Все {status_resp.get('total_count', '?')} "
                                f"лидов уже есть в Скорозвоне (тихие дубли).")
                else:
                    log.info(f"Проект {pid}: импорт {import_id} завершён — "
                             f"вставлено {inserted}, дублей {dupes}")
                return
            if state == "failed":
                log.error(f"Проект {pid}: импорт {import_id} завершился с ошибкой: {status_resp}")
                return
            # state == "processing"/"duplicates"/None — продолжаем ждать
        except Exception as exc:
            log.warning(f"Ошибка при проверке статуса импорта {import_id}: {exc}")
    log.warning(f"Проект {pid}: импорт {import_id} — таймаут ожидания ({_IMPORT_MAX_WAIT}с)")

def main():
    api = SkorozvonAPI()
    log.info("Чтение мастер-таблицы...")
    rows = sheets.read_input_sheet(config.MASTER_SPREADSHEET)
    
    leads_by_project = defaultdict(list)
    seen = set()

    for r in rows:
        phone = normalize_phone(r.get("phone"))
        tag_value = r.get("tag")        # D(3): название проекта ("Стяжка ЮФО", "Ремонт Краснодар"...)
        org_name = r.get("status")      # E(4): субпроект/организация ("B2_Бахтеев", "_Мария мебель"...)

        if not phone or not tag_value:
            continue
        if phone in seen or dedup.is_duplicate(phone):
            continue

        pid = config.PROJECT_MAPPING.get(tag_value)
        if not pid:
            log.warning(f"Неизвестный тег '{tag_value}' у номера {phone}. Пропускаю.")
            continue

        lead = {
            "name": tag_value,
            "phones": [phone],
            "tags": [tag_value],
            "custom_fields": {str(config.CUSTOM_FIELD_CREATED_DATE_ID): date.today().isoformat()}
        }

        leads_by_project[pid].append(lead)
        seen.add(phone)

    for pid, leads in leads_by_project.items():
        log.info(f"Загрузка {len(leads)} лидов в проект {pid}")
        resp = api.import_leads(pid, leads)
        log.info(f"Ответ API import для проекта {pid}: {resp}")

        import_id = None
        if isinstance(resp, dict):
            import_id = resp.get("id") or (resp.get("data") or {}).get("id")

        if import_id:
            _wait_for_import(api, import_id, pid)
        else:
            log.warning(f"Проект {pid}: API не вернул import_id, ждём {config.IMPORT_DELAY_SECONDS}с")
            time.sleep(config.IMPORT_DELAY_SECONDS)

        dedup.mark_loaded_batch((l["phones"][0], str(pid)) for l in leads)

if __name__ == "__main__":
    main()