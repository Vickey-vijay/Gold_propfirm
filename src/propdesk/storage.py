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
    open_positions_json TEXT NOT NULL
);
"""


class AccountStore:
    """Append-only account state log backed by SQLite (WAL mode)."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(SCHEMA)
        self._conn.commit()

    def save(self, account: AccountState) -> None:
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
                consecutive_losses, week_pnl, last_loss_at, open_positions_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def close(self) -> None:
        self._conn.close()
