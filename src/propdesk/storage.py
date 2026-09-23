"""SQLite persistence for account state.

Every save is a new row, never an update-in-place — the table is an append-only
log. "Current state" is just the most recent row. This gives us a free history
of how the account actually evolved with zero extra design effort, which the
dashboard's history panel and, later, the journal both build on.

Decimals are stored as TEXT (str(Decimal)) rather than REAL, because SQLite's
REAL is a float and floats have no place anywhere near money in this codebase —
see risk/sizing.py for why.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from propdesk.models import AccountState, Direction, OpenPosition

SCHEMA = """
CREATE TABLE IF NOT EXISTS account_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    phase TEXT NOT NULL,
    initial_balance TEXT NOT NULL,
    balance TEXT NOT NULL,
    equity TEXT NOT NULL,
    prev_day_closing_balance TEXT NOT NULL,
    trading_days_used INTEGER NOT NULL,
    trades_today INTEGER NOT NULL,
    consecutive_losses INTEGER NOT NULL,
    week_pnl TEXT NOT NULL,
    last_loss_at TEXT,
    open_positions_json TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual'
);

CREATE TABLE IF NOT EXISTS mt5_connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    login TEXT NOT NULL,
    server TEXT NOT NULL,
    encrypted_investor_password TEXT NOT NULL,
    encrypted_metaapi_token TEXT NOT NULL,
    metaapi_account_id TEXT,
    created_at TEXT NOT NULL,
    last_synced_at TEXT,
    last_sync_error TEXT,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS daily_rollovers (
    date TEXT PRIMARY KEY,
    closing_balance TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
"""


class AccountStore:
    """Append-only account state log backed by SQLite (WAL mode)."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        for statement in SCHEMA.strip().split(";"):
            if statement.strip():
                self._conn.execute(statement)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """CREATE TABLE IF NOT EXISTS does nothing for a table that already
        exists with an older shape — it silently does NOT add new columns.
        A database from before the 'source' column existed would otherwise
        crash on the next save() with 'no such column: source'."""
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(account_snapshots)")}
        if "source" not in cols:
            self._conn.execute("ALTER TABLE account_snapshots ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")

    def save(self, account: AccountState, source: str = "manual") -> None:
        """Persist a new snapshot. `source` is 'manual' or 'mt5-sync', purely
        informational — it lets the dashboard show which fields came from
        auto-sync versus hand entry, and does not change any risk math."""
        positions = [
            {
                "direction": p.direction.value,
                "entry_price": str(p.entry_price),
                "stop_price": str(p.stop_price),
                "lots": str(p.lots),
                "opened_at": p.opened_at.isoformat(),
            }
            for p in account.open_positions
        ]
        self._conn.execute(
            """
            INSERT INTO account_snapshots (
                ts, phase, initial_balance, balance, equity,
                prev_day_closing_balance, trading_days_used, trades_today,
                consecutive_losses, week_pnl, last_loss_at, open_positions_json,
                source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                account.phase,
                str(account.initial_balance),
                str(account.balance),
                str(account.equity),
                str(account.prev_day_closing_balance),
                account.trading_days_used,
                account.trades_today,
                account.consecutive_losses,
                str(account.week_pnl),
                account.last_loss_at.isoformat() if account.last_loss_at else None,
                json.dumps(positions),
                source,
            ),
        )
        self._conn.commit()

    def latest(self) -> AccountState | None:
        row = self._conn.execute(
            "SELECT * FROM account_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return self._row_to_state(row)

    def history(self, limit: int = 50) -> list[dict]:
        cols = [d[1] for d in self._conn.execute("PRAGMA table_info(account_snapshots)")]
        rows = self._conn.execute(
            "SELECT * FROM account_snapshots ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(zip(cols, row)) for row in rows]

    def _row_to_state(self, row: tuple) -> AccountState:
        cols = [d[1] for d in self._conn.execute("PRAGMA table_info(account_snapshots)")]
        r = dict(zip(cols, row))
        positions = [
            OpenPosition(
                direction=Direction(p["direction"]),
                entry_price=Decimal(p["entry_price"]),
                stop_price=Decimal(p["stop_price"]),
                lots=Decimal(p["lots"]),
                opened_at=datetime.fromisoformat(p["opened_at"]),
            )
            for p in json.loads(r["open_positions_json"])
        ]
        return AccountState(
            phase=r["phase"],
            initial_balance=Decimal(r["initial_balance"]),
            balance=Decimal(r["balance"]),
            equity=Decimal(r["equity"]),
            prev_day_closing_balance=Decimal(r["prev_day_closing_balance"]),
            trading_days_used=r["trading_days_used"],
            trades_today=r["trades_today"],
            consecutive_losses=r["consecutive_losses"],
            week_pnl=Decimal(r["week_pnl"]),
            last_loss_at=datetime.fromisoformat(r["last_loss_at"]) if r["last_loss_at"] else None,
            open_positions=positions,
        )

    # ---- MT5 connection (credential + sync status) -----------------------

    def save_mt5_connection(
        self, *, login: str, server: str, encrypted_investor_password: str, encrypted_metaapi_token: str
    ) -> int:
        """Replaces any existing connection — there is only ever one at a time."""
        self._conn.execute("DELETE FROM mt5_connections")
        cur = self._conn.execute(
            """
            INSERT INTO mt5_connections (login, server, encrypted_investor_password,
                encrypted_metaapi_token, created_at, enabled)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (login, server, encrypted_investor_password, encrypted_metaapi_token, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_mt5_connection(self) -> dict | None:
        row = self._conn.execute("SELECT * FROM mt5_connections ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        cols = [d[1] for d in self._conn.execute("PRAGMA table_info(mt5_connections)")]
        return dict(zip(cols, row))

    def set_mt5_account_id(self, connection_id: int, metaapi_account_id: str) -> None:
        self._conn.execute(
            "UPDATE mt5_connections SET metaapi_account_id = ? WHERE id = ?",
            (metaapi_account_id, connection_id),
        )
        self._conn.commit()

    def record_sync_result(self, connection_id: int, *, error: str | None) -> None:
        self._conn.execute(
            "UPDATE mt5_connections SET last_synced_at = ?, last_sync_error = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), error, connection_id),
        )
        self._conn.commit()

    def disconnect_mt5(self) -> None:
        self._conn.execute("DELETE FROM mt5_connections")
        self._conn.commit()

    # ---- daily rollover (prev_day_closing_balance) ------------------------

    def record_rollover(self, date_str: str, closing_balance: Decimal) -> None:
        """Idempotent — running the rollover job twice for the same date is safe."""
        self._conn.execute(
            "INSERT OR REPLACE INTO daily_rollovers (date, closing_balance, recorded_at) VALUES (?, ?, ?)",
            (date_str, str(closing_balance), datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def last_rollover_date(self) -> str | None:
        row = self._conn.execute("SELECT date FROM daily_rollovers ORDER BY date DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def close(self) -> None:
        self._conn.close()
