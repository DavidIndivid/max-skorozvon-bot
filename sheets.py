"""Чтение входных Google Sheets и запись выходной таблицы."""
import logging
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config

log = logging.getLogger(__name__)


def _service():
    creds = service_account.Credentials.from_service_account_info(
        config.GOOGLE_SERVICE_ACCOUNT_INFO, scopes=config.GOOGLE_SCOPES,
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _col_letter(idx: int) -> str:
    s = ""
    n = idx
    while True:
        n, r = divmod(n, 26)
        s = chr(ord("A") + r) + s
        if n == 0:
            break
        n -= 1
    return s


def _first_sheet_title(svc, spreadsheet_id: str) -> str:
    meta = svc.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties.title",
    ).execute()
    sheets_meta = meta.get("sheets", [])
    if not sheets_meta:
        raise RuntimeError(f"В таблице {spreadsheet_id} нет ни одного листа")
    return sheets_meta[0]["properties"]["title"]


def _fetch_range(svc, spreadsheet_id: str, sheet_title: str,
                 start_row: int, end_col_letter: str) -> list[list]:
    a1 = f"'{sheet_title}'!A{start_row}:{end_col_letter}"
    resp = svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=a1,
    ).execute()
    return resp.get("values", [])


def read_input_sheet(sheet_config: dict) -> list[dict]:
    spreadsheet_id = sheet_config["spreadsheet_id"]
    if not spreadsheet_id:
        raise ValueError(f"{sheet_config.get('name')}: пустой spreadsheet_id")
    cols: dict = sheet_config["columns"]
    start_row = int(sheet_config.get("data_start_row", 3))
    sheet_name = sheet_config.get("sheet_name") or ""
    default_project = sheet_config.get("default_project")

    used_indexes = [v for v in cols.values() if isinstance(v, int)]
    if not used_indexes:
        raise ValueError(f"{sheet_config.get('name')}: пустой columns")
    end_letter = _col_letter(max(used_indexes))

    svc = _service()
    try:
        rows = _fetch_range(svc, spreadsheet_id, sheet_name, start_row, end_letter)
    except HttpError as exc:
        if exc.resp.status != 400:
            raise
        log.warning("Лист %r не найден в %s — fallback на первый лист",
                    sheet_name, spreadsheet_id)
        sheet_name = _first_sheet_title(svc, spreadsheet_id)
        rows = _fetch_range(svc, spreadsheet_id, sheet_name, start_row, end_letter)

    out: list[dict] = []

    def _cell(row: list, idx: Optional[int]) -> str:
        if idx is None or idx >= len(row):
            return ""
        v = row[idx]
        return "" if v is None else str(v).strip()

    for offset, row in enumerate(rows):
        if not row or all(not (str(c) if c is not None else "").strip() for c in row):
            continue
        rownum = start_row + offset

        project = _cell(row, cols.get("project")) or (default_project or "")
        created = _cell(row, cols.get("created"))
        status  = _cell(row, cols.get("status"))
        comment = _cell(row, cols.get("comment"))
        tag     = _cell(row, cols.get("tag"))

        base = {
            "project": project, "created": created, "status": status,
            "comment": comment, "tag": tag, "_row": rownum, "_sheet": sheet_name,
        }

        phones: list[str] = []
        p1 = _cell(row, cols.get("phone"))
        if p1:
            phones.append(p1)
        p2_idx = cols.get("phone2")
        if p2_idx is not None:
            p2 = _cell(row, p2_idx)
            if p2 and p2 != p1:
                phones.append(p2)

        if not phones:
            out.append({**base, "phone": ""})
            continue
        for ph in phones:
            out.append({**base, "phone": ph})

    return out


def batch_update_cells(spreadsheet_id: str, sheet_name: str,
                       updates: list[dict]) -> Optional[dict]:
    if not updates:
        return None
    svc = _service()
    data = [
        {"range": f"'{sheet_name}'!{u['range']}", "values": [[u["value"]]]}
        for u in updates
    ]
    return svc.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()


def append_row(spreadsheet_id: str, sheet_name: str, row_values: list):
    svc = _service()
    return svc.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"'{sheet_name}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [row_values]},
    ).execute()


def col_letter(idx: int) -> str:
    return _col_letter(idx)
