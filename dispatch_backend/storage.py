"""SQLite 持久化层。

所有可变状态都落库；服务重启后：
* 待确认（sent_unknown / 等观察）的设备动作既不会变成成功，也不会消失；
* 已确认撤警、已交还授权等结论原样恢复。
写操作在单个事务内完成，连接开启 WAL 以便重启联调时对端进程可独立运行。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS towns (
    name TEXT PRIMARY KEY,
    online INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS delegations (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    delegate TEXT NOT NULL,
    villages_json TEXT NOT NULL,
    channels_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS village_returns (
    id TEXT PRIMARY KEY,
    delegation_id TEXT NOT NULL REFERENCES delegations(id),
    village TEXT NOT NULL,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    UNIQUE(delegation_id, village)
);
CREATE TABLE IF NOT EXISTS warnings (
    id TEXT PRIMARY KEY,
    villages_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS withdrawal_requests (
    id TEXT PRIMARY KEY,
    warning_id TEXT NOT NULL REFERENCES warnings(id),
    villages_json TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    actor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commands (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    action TEXT NOT NULL,
    alert TEXT,
    villages_json TEXT,
    kind TEXT NOT NULL,
    issued_by TEXT NOT NULL,
    handled_by TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    request_json TEXT NOT NULL,
    state TEXT NOT NULL,
    remote_id TEXT,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS command_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id TEXT NOT NULL REFERENCES commands(id),
    at TEXT NOT NULL,
    event TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id TEXT REFERENCES commands(id),
    remote_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    heard INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    stale INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    actor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
"""


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """串行化写事务，避免两个值班员并发派播互相覆盖。"""
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    # -- 基础工具 -------------------------------------------------------

    def audit(self, conn: sqlite3.Connection, at: str, actor: str, action: str, detail: dict) -> None:
        conn.execute(
            "INSERT INTO audit_log(at, actor, action, detail_json) VALUES (?, ?, ?, ?)",
            (at, actor, action, dumps(detail)),
        )

    def command_event(
        self, conn: sqlite3.Connection, command_id: str, at: str, event: str, actor: str, detail: dict
    ) -> None:
        conn.execute(
            "INSERT INTO command_events(command_id, at, event, actor, detail_json)"
            " VALUES (?, ?, ?, ?, ?)",
            (command_id, at, event, actor, dumps(detail)),
        )

    # -- 读方法 ---------------------------------------------------------

    def get_delegation(self, conn: sqlite3.Connection, delegation_id: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM delegations WHERE id=?", (delegation_id,)).fetchone()

    def latest_delegation(self, conn: sqlite3.Connection) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM delegations ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()

    def returned_villages(self, conn: sqlite3.Connection, delegation_id: str) -> set[str]:
        rows = conn.execute(
            "SELECT village FROM village_returns WHERE delegation_id=?", (delegation_id,)
        ).fetchall()
        return {row["village"] for row in rows}

    def warning(self, conn: sqlite3.Connection, warning_id: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM warnings WHERE id=?", (warning_id,)).fetchone()

    def list_warnings(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM warnings ORDER BY id").fetchall()

    def withdrawal_villages(self, conn: sqlite3.Connection, warning_id: str) -> set[str]:
        rows = conn.execute(
            "SELECT villages_json FROM withdrawal_requests WHERE warning_id=?", (warning_id,)
        ).fetchall()
        result: set[str] = set()
        for row in rows:
            result.update(loads(row["villages_json"]))
        return result

    def command(self, conn: sqlite3.Connection, command_id: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()

    def command_by_remote(self, conn: sqlite3.Connection, remote_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM commands WHERE remote_id=? ORDER BY issued_at DESC, rowid DESC LIMIT 1",
            (remote_id,),
        ).fetchone()

    def list_commands(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM commands ORDER BY issued_at, rowid").fetchall()

    def command_events(self, conn: sqlite3.Connection, command_id: str) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM command_events WHERE command_id=? ORDER BY id", (command_id,)
        ).fetchall()

    def observations_for(self, conn: sqlite3.Connection, command_id: str) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM observations WHERE command_id=? ORDER BY observed_at, id", (command_id,)
        ).fetchall()

    def latest_observation(
        self, conn: sqlite3.Connection, command_id: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM observations WHERE command_id=? AND stale=0 ORDER BY observed_at DESC, id DESC LIMIT 1",
            (command_id,),
        ).fetchone()

    def town_online(self, conn: sqlite3.Connection, town: str) -> bool:
        row = conn.execute("SELECT online FROM towns WHERE name=?", (town,)).fetchone()
        return bool(row["online"]) if row else True

    def audit_tail(self, conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
