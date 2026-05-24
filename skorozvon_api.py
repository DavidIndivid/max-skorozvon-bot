"""Тонкая обёртка над REST API Скорозвон с retry на 429."""
import logging
import time
from typing import Iterable, Optional

import requests

import config
from auth import SkorozvonAuth

log = logging.getLogger(__name__)

# Reports API имеет отдельный rate limit — нужна пауза между запросами
_REPORT_SLICE_S = 3600    # нарезаем на часовые куски
_REPORT_MAX_ROWS = 5000   # максимум строк за один запрос
_REPORT_PAUSE_S = 15      # пауза между успешными запросами
_REPORT_429_PAUSE_S = 60  # пауза после 429 (даём API отдышаться)


class SkorozvonAPI:
    def __init__(self, auth: Optional[SkorozvonAuth] = None) -> None:
        self.auth = auth or SkorozvonAuth()

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{config.SKOROZVON_BASE_URL}{path}"
        for attempt in range(1, config.MAX_RETRIES + 1):
            headers = kwargs.pop("headers", {}) or {}
            headers.update(self.auth.auth_headers())
            resp = requests.request(method, url, headers=headers, timeout=300, **kwargs)
            if resp.status_code == 429:
                log.warning("429 от Скорозвона (попытка %s), ждём %ss",
                            attempt, config.RATE_LIMIT_RETRY_DELAY)
                time.sleep(config.RATE_LIMIT_RETRY_DELAY)
                continue
            if resp.status_code == 401 and attempt == 1:
                log.warning("401 — пробуем перевыпустить токен")
                self.auth._token = None
                continue
            try:
                resp.raise_for_status()
            except requests.HTTPError:
                log.error("Ошибка %s %s: %s", method, path, resp.text[:500])
                raise
            if not resp.content:
                return {}
            return resp.json()
        raise RuntimeError(f"Превышены повторы для {method} {path}")

    # ---------- Проекты ----------

    def list_call_projects(self) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            payload = self._request(
                "GET", "/api/v2/call_projects",
                params={"type": "call", "page": page, "length": config.PAGE_SIZE},
            )
            data = payload.get("data", [])
            out.extend(data)
            pg = payload.get("pagination", {})
            if page >= pg.get("total_pages", 1) or not data:
                break
            page += 1
        return out

    # ---------- Импорт лидов ----------

    def import_leads(self, call_project_id: int, leads: list[dict],
                     duplicates: str = "skip") -> dict:
        body = {
            "call_project_id": call_project_id,
            "duplicates": duplicates,
            "data": leads,
        }
        return self._request(
            "POST", "/api/v2/leads/import",
            json=body, headers={"Content-Type": "application/json"},
        )

    def get_import_status(self, import_id: int) -> dict:
        return self._request("GET", f"/api/v2/leads/import/{import_id}")

    # ---------- Лиды ----------

    def list_leads(self, call_project_id: int, case_state: str = None,
                   start_time: float = None, end_time: float = None) -> Iterable[dict]:
        page = 1
        while True:
            params = {
                "call_project_id": call_project_id,
                "page": page,
                "length": config.PAGE_SIZE,
            }
            if case_state is not None:
                params["case_state"] = case_state
            if start_time is not None:
                params["start_time"] = start_time
            if end_time is not None:
                params["end_time"] = end_time
            payload = self._request("GET", "/api/v2/leads", params=params)
            data = payload.get("data", [])
            for item in data:
                yield item
            pg = payload.get("pagination", {})
            if page >= pg.get("total_pages", 1) or not data:
                break
            page += 1

    # ---------- Звонки ----------

    def list_calls(self, start_time: float, end_time: float,
                   call_project_id: Optional[int] = None) -> Iterable[dict]:
        """Звонки за интервал. Если задан call_project_id — фильтр по проекту."""
        page = 1
        while True:
            params = {
                "start_time": start_time,
                "end_time": end_time,
                "page": page,
                "length": config.PAGE_SIZE,
            }
            if call_project_id is not None:
                params["call_project_id"] = call_project_id
            payload = self._request("GET", "/api/v2/calls", params=params)
            data = payload.get("data", payload if isinstance(payload, list) else [])
            for item in data:
                yield item
            pg = payload.get("pagination", {}) if isinstance(payload, dict) else {}
            if page >= pg.get("total_pages", 1) or not data:
                break
            page += 1

    # ---------- Кастомные поля ----------

    def create_custom_field(self, name: str, lead_type: str = "lead",
                            field_type: str = "date") -> dict:
        return self._request(
            "POST", "/api/v2/custom_fields",
            json={"name": name, "lead_type": lead_type, "field_type": field_type},
            headers={"Content-Type": "application/json"},
        )

    def list_custom_fields(self) -> list[dict]:
        payload = self._request("GET", "/api/v2/custom_fields")
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    # ---------- Результаты лидов (комментарии операторов) ----------

    def get_lead_results(self, lead_id: int) -> list[dict]:
        """Результаты лида — содержат поле comment (оператор)."""
        payload = self._request("GET", f"/api/v2/leads/{lead_id}/results")
        return payload.get("data", [])

    # ---------- Отчёты (комментарии операторов) ----------

    def list_report_calls(self, start_time: float, end_time: float,
                          slice_seconds: int = _REPORT_SLICE_S) -> Iterable[dict]:
        """Записи звонков из Reports API — содержат поле comment (оператор).
        API не поддерживает фильтрацию по проекту, возвращает все проекты аккаунта.
        Нарезает на куски slice_seconds, чтобы не превысить _REPORT_MAX_ROWS на кусок.
        """
        t = start_time
        is_first = True
        while t < end_time:
            if not is_first:
                time.sleep(_REPORT_PAUSE_S)
            is_first = False
            t_end = min(t + slice_seconds, end_time)
            body = {
                "start_time": int(t),
                "end_time": int(t_end),
                "page": 1,
                "length": _REPORT_MAX_ROWS,
            }
            for attempt in range(config.MAX_RETRIES + 3):
                try:
                    resp = requests.post(
                        f"{config.SKOROZVON_BASE_URL}/api/reports/calls_total.json",
                        headers=self.auth.auth_headers(),
                        json=body,
                        timeout=120,
                    )
                    if resp.status_code == 429:
                        log.warning("Reports 429 (попытка %s), ждём %ss",
                                    attempt + 1, _REPORT_429_PAUSE_S)
                        time.sleep(_REPORT_429_PAUSE_S)
                        continue
                    resp.raise_for_status()
                    records = resp.json().get("data", [])
                    if len(records) == _REPORT_MAX_ROWS:
                        log.warning(
                            "Reports %s-%s: ровно %s записей — данные могут быть обрезаны",
                            int(t), int(t_end), _REPORT_MAX_ROWS,
                        )
                    for r in records:
                        yield r
                    break
                except Exception as exc:
                    if attempt >= config.MAX_RETRIES + 2:
                        log.error("Reports API ошибка для %s-%s: %s", int(t), int(t_end), exc)
                        break
                    log.warning("Reports API retry (%s): %s", attempt + 1, exc)
                    time.sleep(config.RATE_LIMIT_RETRY_DELAY)
            t = t_end
