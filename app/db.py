"""SQLite storage. One file, WAL mode, a single shared connection guarded by a lock.

All timestamps are stored as ISO-8601 UTC strings, except candle open times
which are integer epoch seconds (fast range queries).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 3   # v1 files are backed up and recreated; v2 -> v3 is upgraded in place

# columns added after a schema version shipped: (table, column, type) - added in place if missing
ADDED_COLUMNS = [
    ("positions", "regime", "TEXT"),
    ("trades", "regime", "TEXT"),
    ("decisions", "regime", "TEXT"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,                        -- standard | moderate | aggressive
    cash REAL NOT NULL,                         -- MT5 "Balance"
    equity REAL NOT NULL,
    peak REAL NOT NULL,
    starting_balance REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',      -- active | paused | paused_daily | paused_weekly | target_hit | halted
    status_reason TEXT,
    started_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prices (
    symbol TEXT NOT NULL,
    tf TEXT NOT NULL,                           -- 5m | 15m
    t INTEGER NOT NULL,                         -- candle open time, epoch seconds
    o REAL, h REAL, l REAL, c REAL, v REAL,
    PRIMARY KEY (symbol, tf, t)
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,       -- MT5 "Ticket"
    account TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,                         -- LONG | SHORT
    lots REAL NOT NULL,                         -- MT5 "Volume"
    units REAL NOT NULL,                        -- lots x contract size
    leverage REAL NOT NULL,                     -- effective leverage at entry (notional / equity)
    entry REAL NOT NULL,
    stop REAL NOT NULL,
    initial_stop REAL NOT NULL,
    target REAL NOT NULL,
    margin REAL NOT NULL,
    risk_usd REAL NOT NULL,
    fees REAL NOT NULL DEFAULT 0,
    swap REAL NOT NULL DEFAULT 0,
    funding REAL NOT NULL DEFAULT 0,
    signal TEXT,
    regime TEXT,
    ai_reason TEXT,
    decision_id INTEGER,
    weekend_halved INTEGER NOT NULL DEFAULT 0,
    opened_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    ticket INTEGER,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    lots REAL NOT NULL,
    units REAL NOT NULL,
    leverage REAL NOT NULL,
    entry REAL NOT NULL,
    exit REAL NOT NULL,
    stop REAL, target REAL,
    pnl_usd REAL NOT NULL,                      -- net of commission and swap
    pnl_r REAL,
    fees REAL NOT NULL DEFAULT 0,
    swap REAL NOT NULL DEFAULT 0,
    funding REAL NOT NULL DEFAULT 0,
    signal TEXT,
    regime TEXT,
    ai_reason TEXT,
    exit_reason TEXT,
    opened_at TEXT NOT NULL,
    closed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    account TEXT NOT NULL,
    symbol TEXT NOT NULL,
    signal TEXT NOT NULL,
    side TEXT NOT NULL,
    regime TEXT,
    signal_note TEXT,
    ai_used INTEGER NOT NULL DEFAULT 0,
    ai_reply TEXT,
    accepted INTEGER NOT NULL DEFAULT 0,
    reject_reason TEXT
);

CREATE TABLE IF NOT EXISTS cost_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    purpose TEXT NOT NULL,                      -- gate:<mode> | news_digest | daily_review:<mode> | weekly_playbook:<mode>
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    ok INTEGER NOT NULL DEFAULT 1,
    error TEXT
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    at TEXT NOT NULL,
    account TEXT NOT NULL,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    margin_used REAL NOT NULL,
    btc_benchmark REAL,
    gold_benchmark REAL,
    PRIMARY KEY (at, account)
);

CREATE TABLE IF NOT EXISTS playbook_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    at TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT NOT NULL                        -- seed | weekly_ai | manual | rollback
);

CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    at TEXT NOT NULL,
    day TEXT NOT NULL,
    lesson TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    source TEXT NOT NULL,                       -- default | ui | reset:<mode> | revert
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS disabled_signals (
    account TEXT NOT NULL,
    symbol TEXT NOT NULL,
    signal TEXT NOT NULL,
    at TEXT NOT NULL,
    reason TEXT,
    PRIMARY KEY (account, symbol, signal)
);

CREATE TABLE IF NOT EXISTS news_items (
    id TEXT PRIMARY KEY,                        -- hash of link
    at TEXT NOT NULL,                           -- published time (UTC)
    source TEXT,
    title TEXT NOT NULL,
    link TEXT,
    symbols TEXT                                -- comma list of related instruments
);

CREATE TABLE IF NOT EXISTS calendar_events (
    id TEXT PRIMARY KEY,
    at TEXT NOT NULL,                           -- event time (UTC)
    currency TEXT NOT NULL,
    impact TEXT NOT NULL,                       -- High | Medium | Low | Holiday
    title TEXT NOT NULL,
    forecast TEXT,
    previous TEXT
);

CREATE TABLE IF NOT EXISTS news_digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    data TEXT NOT NULL                          -- JSON: {instruments: {symbol: {bias, risk, summary}}, market_summary}
);

CREATE TABLE IF NOT EXISTS strategy_state (
    account TEXT NOT NULL,
    signal TEXT NOT NULL,
    regime TEXT NOT NULL,                       -- trend | range | volatile | mixed
    status TEXT NOT NULL,                       -- active | cautious | off | probation
    risk_mult REAL NOT NULL DEFAULT 1,
    since TEXT,                                 -- when the current status started
    until TEXT,                                 -- 'off' until this time, then probation
    n INTEGER NOT NULL DEFAULT 0,
    avg_r REAL,
    reason TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account, signal, regime)
);

CREATE TABLE IF NOT EXISTS tuning_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    account TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0,
    current TEXT,                               -- JSON params before
    best TEXT,                                  -- JSON best params found
    result TEXT,                                -- JSON train/test metrics
    reason TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);

CREATE INDEX IF NOT EXISTS idx_pos_account ON positions(account);
CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(account, closed_at);
CREATE INDEX IF NOT EXISTS idx_decisions_at ON decisions(at);
CREATE INDEX IF NOT EXISTS idx_cost_at ON cost_log(at);
CREATE INDEX IF NOT EXISTS idx_news_at ON news_items(at);
CREATE INDEX IF NOT EXISTS idx_cal_at ON calendar_events(at);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.migrated_from: str | None = None
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._backup_if_old_schema()
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        for table, col, typ in ADDED_COLUMNS:
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.conn.commit()

    def _backup_if_old_schema(self) -> None:
        """A DB file from an older version is moved aside (kept, not deleted) and a fresh one created."""
        p = Path(self.path)
        if not p.exists():
            return
        conn = sqlite3.connect(self.path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        has_tables = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        conn.close()
        if version >= 2 or not has_tables:      # v2+ is upgraded in place, never wiped
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            f = Path(self.path + suffix)
            if f.exists():
                f.rename(f"{self.path}.v{version}-{stamp}.bak{suffix}")
        self.migrated_from = f"{self.path}.v{version}-{stamp}.bak"

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self.conn.executemany(sql, [tuple(r) for r in rows])
            self.conn.commit()

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        with self._lock:
            row = self.conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = self.one(sql, params)
        return next(iter(row.values())) if row else None

    # --- tiny key/value store for engine state (day-start equity, benchmarks...)
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT v FROM kv WHERE k=?", (key,))
        return json.loads(row["v"]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self.execute("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                     (key, json.dumps(value)))

    def backup_to(self, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            target = sqlite3.connect(str(dest))
            with target:
                self.conn.backup(target)
            target.close()

    def ensure_accounts(self, ids: list[str], starting_balance: float) -> None:
        for acc in ids:
            if not self.one("SELECT id FROM accounts WHERE id=?", (acc,)):
                self.execute(
                    "INSERT INTO accounts(id, cash, equity, peak, starting_balance, status, started_at) "
                    "VALUES (?, ?, ?, ?, ?, 'active', ?)",
                    (acc, starting_balance, starting_balance, starting_balance, starting_balance, iso()),
                )

    def reset_account(self, acc: str, starting_balance: float) -> None:
        """Wipe one paper account back to its starting balance (history of that account removed)."""
        for table in ("positions", "trades", "decisions", "equity_snapshots", "disabled_signals",
                      "strategy_state", "tuning_runs"):
            self.execute(f"DELETE FROM {table} WHERE account=?", (acc,))
        self.execute("DELETE FROM accounts WHERE id=?", (acc,))
        self.execute("DELETE FROM kv WHERE k LIKE ?", (f"%:{acc}",))
        self.ensure_accounts([acc], starting_balance)
