"""SQLite-дедупликация телефонов между запусками."""
import sqlite3
from contextlib import contextmanager
from typing import Iterable

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS loaded_phones (
    phone TEXT PRIMARY KEY,
    project TEXT,
    loaded_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


@contextmanager
def _conn():
    con = sqlite3.connect(config.DEDUP_DB_PATH)
    try:
        con.execute(_SCHEMA)
        yield con
        con.commit()
    finally:
        con.close()


def is_duplicate(phone: str) -> bool:
    with _conn() as con:
        cur = con.execute("SELECT 1 FROM loaded_phones WHERE phone = ?", (phone,))
        return cur.fetchone() is not None


def mark_loaded(phone: str, project: str) -> None:
    with _conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO loaded_phones(phone, project) VALUES (?, ?)",
            (phone, project),
        )


def mark_loaded_batch(rows: Iterable[tuple[str, str]]) -> None:
    rows = list(rows)
    if not rows:
        return
    with _conn() as con:
        con.executemany(
            "INSERT OR IGNORE INTO loaded_phones(phone, project) VALUES (?, ?)",
            rows,
        )


def filter_new(phones: Iterable[str]) -> set[str]:
    """Возвращает подмножество телефонов, которых ещё нет в базе."""
    phones = list({p for p in phones if p})
    if not phones:
        return set()
    with _conn() as con:
        placeholders = ",".join("?" * len(phones))
        cur = con.execute(
            f"SELECT phone FROM loaded_phones WHERE phone IN ({placeholders})",
            phones,
        )
        existing = {row[0] for row in cur.fetchall()}
    return set(phones) - existing
