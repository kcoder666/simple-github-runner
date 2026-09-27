"""SQLite persistence: targets, settings, events, and small key/value state."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any

from .config import DEFAULT_SETTINGS, coerce_setting

SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    url           TEXT NOT NULL,
    labels        TEXT NOT NULL DEFAULT '',
    runner_group  TEXT NOT NULL DEFAULT 'Default',
    min_idle      INTEGER NOT NULL DEFAULT 1,
    max_runners   INTEGER NOT NULL DEFAULT 4,
    docker_access INTEGER NOT NULL DEFAULT 1,
    cpus          REAL,
    memory        TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    level     TEXT NOT NULL,
    kind      TEXT NOT NULL,
    target_id INTEGER,
    runner    TEXT,
    message   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channels (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    type       TEXT NOT NULL,
    config     TEXT NOT NULL,
    min_level  TEXT NOT NULL DEFAULT 'warn',
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_target ON events(target_id, ts);
"""

TARGET_FIELDS = (
    "name", "url", "labels", "runner_group", "min_idle", "max_runners",
    "docker_access", "cpus", "memory", "enabled",
)

# Keep the events table bounded.
MAX_EVENTS = 20000


class Store:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(os.path.join(data_dir, "controller.db"), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._db.commit()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    # --- targets -----------------------------------------------------------

    def list_targets(self) -> list[dict[str, Any]]:
        return [_target(r) for r in self._query("SELECT * FROM targets ORDER BY name")]

    def get_target(self, target_id: int) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM targets WHERE id = ?", (target_id,))
        return _target(rows[0]) if rows else None

    def create_target(self, data: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        cols = [f for f in TARGET_FIELDS if f in data]
        sql = (
            f"INSERT INTO targets ({', '.join(cols)}, created_at, updated_at) "
            f"VALUES ({', '.join('?' for _ in cols)}, ?, ?)"
        )
        cur = self._exec(sql, tuple(data[c] for c in cols) + (now, now))
        return self.get_target(cur.lastrowid)  # type: ignore[return-value]

    def update_target(self, target_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
        cols = [f for f in TARGET_FIELDS if f in data]
        if cols:
            sets = ", ".join(f"{c} = ?" for c in cols)
            self._exec(
                f"UPDATE targets SET {sets}, updated_at = ? WHERE id = ?",
                tuple(data[c] for c in cols) + (time.time(), target_id),
            )
        return self.get_target(target_id)

    def delete_target(self, target_id: int) -> None:
        self._exec("DELETE FROM targets WHERE id = ?", (target_id,))

    # --- notification channels ------------------------------------------------

    def list_channels(self) -> list[dict[str, Any]]:
        return [_channel(r) for r in self._query("SELECT * FROM channels ORDER BY id")]

    def get_channel(self, channel_id: int) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM channels WHERE id = ?", (channel_id,))
        return _channel(rows[0]) if rows else None

    def create_channel(self, name: str, ctype: str, config: dict[str, Any], min_level: str = "warn",
                       enabled: bool = True) -> dict[str, Any]:
        now = time.time()
        cur = self._exec(
            "INSERT INTO channels (name, type, config, min_level, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, ctype, json.dumps(config), min_level, int(enabled), now, now),
        )
        return self.get_channel(cur.lastrowid)  # type: ignore[return-value]

    def update_channel(self, channel_id: int, name: str, ctype: str, config: dict[str, Any],
                       min_level: str, enabled: bool) -> dict[str, Any] | None:
        self._exec(
            "UPDATE channels SET name = ?, type = ?, config = ?, min_level = ?, enabled = ?, updated_at = ? "
            "WHERE id = ?",
            (name, ctype, json.dumps(config), min_level, int(enabled), time.time(), channel_id),
        )
        return self.get_channel(channel_id)

    def delete_channel(self, channel_id: int) -> None:
        self._exec("DELETE FROM channels WHERE id = ?", (channel_id,))

    # --- settings ----------------------------------------------------------

    def settings(self) -> dict[str, Any]:
        values = dict(DEFAULT_SETTINGS)
        for row in self._query("SELECT key, value FROM settings"):
            if row["key"] in values:
                values[row["key"]] = json.loads(row["value"])
        return values

    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        coerced = {k: coerce_setting(k, v) for k, v in changes.items()}
        for key, value in coerced.items():
            self._exec(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )
        return self.settings()

    # --- kv ----------------------------------------------------------------

    def kv_get(self, key: str, default: Any = None) -> Any:
        rows = self._query("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def kv_set(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    # --- events ------------------------------------------------------------

    def add_event(self, level: str, kind: str, message: str,
                  target_id: int | None = None, runner: str | None = None) -> None:
        cur = self._exec(
            "INSERT INTO events (ts, level, kind, target_id, runner, message) VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), level, kind, target_id, runner, message),
        )
        if cur.lastrowid and cur.lastrowid % 500 == 0:
            self._exec("DELETE FROM events WHERE id <= ?", (cur.lastrowid - MAX_EVENTS,))

    def events(self, limit: int = 200, target_id: int | None = None,
               level: str | None = None) -> list[dict[str, Any]]:
        where, params = [], []
        if target_id is not None:
            where.append("target_id = ?")
            params.append(target_id)
        if level:
            where.append("level = ?")
            params.append(level)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return self._query(
            f"SELECT * FROM events {clause} ORDER BY id DESC LIMIT ?", tuple(params) + (limit,)
        )


def _target(row: dict[str, Any]) -> dict[str, Any]:
    row["enabled"] = bool(row["enabled"])
    row["docker_access"] = bool(row["docker_access"])
    row["labels"] = [label for label in row["labels"].split(",") if label]
    return row


def _channel(row: dict[str, Any]) -> dict[str, Any]:
    row["enabled"] = bool(row["enabled"])
    row["config"] = json.loads(row["config"])
    return row
