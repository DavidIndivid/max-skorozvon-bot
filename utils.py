"""Общие утилиты: логирование, парсинг дат и телефонов."""
import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import config

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%d.%m.%Y %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%d.%m.%Y %H:%M",
    "%Y-%m-%d",
    "%d.%m.%Y",
)


def setup_logging(script_name: str) -> logging.Logger:
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        os.makedirs(config.LOGS_DIR, exist_ok=True)
        today = date.today().isoformat()
        log_path = os.path.join(config.LOGS_DIR, f"{script_name}_{today}.log")
        handlers.insert(0, logging.FileHandler(log_path, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)
    return logging.getLogger(script_name)


def parse_date(value: str) -> Optional[datetime]:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?$", s)
    if m:
        d, h, mi, sec = m.groups()
        sec = sec or "00"
        try:
            return datetime.strptime(f"{d} {int(h):02d}:{mi}:{sec}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def normalize_phone(value) -> Optional[str]:
    """11 цифр, начинаются с 7."""
    if value is None:
        return None
    if isinstance(value, float):
        if value.is_integer():
            value = str(int(value))
        else:
            value = repr(value)
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".", 1)[0]
    digits = re.sub(r"\D", "", s)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 11 and digits.startswith("7"):
        return digits
    return None


def today_msk() -> date:
    return datetime.now(MOSCOW_TZ).date()


def is_weekend_msk() -> bool:
    return datetime.now(MOSCOW_TZ).weekday() >= 5


def yesterday_and_today_msk() -> tuple[date, date]:
    t = today_msk()
    return t - timedelta(days=1), t


def msk_day_unix_range(day: date) -> tuple[float, float]:
    start = datetime(day.year, day.month, day.day, tzinfo=MOSCOW_TZ)
    end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp()
