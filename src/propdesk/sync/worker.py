"""The two scheduled jobs behind auto-sync.

sync_once() — every few minutes: pulls balance/equity/positions from MT5 and
writes a new snapshot, preserving every field auto-sync has no business
touching (phase, trading_days_used, trades_today, consecutive_losses,
week_pnl, last_loss_at — all still yours to maintain by hand until a later
phase can derive them correctly from trade history).

daily_rollover() — once a day at the firm's own reset time: snapshots
whatever balance sync last saw and records it as prev_day_closing_balance
for every day going forward until the next rollover. This is the one FTMO-
specific field auto-sync CAN set correctly without needing trade history,
because it only needs "what was the balance at reset," not "why."
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from propdesk.config import RuleConfig
from propdesk.sync.crypto import decrypt
from propdesk.sync.metaapi_client import MetaApiConnectionError, fetch_account_snapshot
from propdesk.storage import AccountStore

logger = logging.getLogger("propdesk.sync")


async def sync_once(store: AccountStore, cfg: RuleConfig) -> None:
    conn = store.get_mt5_connection()
    if conn is None or not conn["enabled"]:
        return

    try:
        token = decrypt(conn["encrypted_metaapi_token"])
        snapshot = await fetch_account_snapshot(
            token=token,
            metaapi_account_id=conn["metaapi_account_id"],
            fallback_stop_distance=cfg.position_sizing.max_stop_usd,
        )
    except MetaApiConnectionError as exc:
        logger.warning("MT5 sync failed: %s", exc)
        store.record_sync_result(conn["id"], error=str(exc))
        return

    prior = store.latest()
    if prior is None:
        logger.warning("MT5 sync ran with no prior account state to merge into — skipping")
        return

    # Only balance, equity and positions come from MT5. Everything else —
    # phase, the FTMO-specific counters, the daily floor basis — is either
    # still manual or handled by daily_rollover() below, never guessed here.
    updated = prior.model_copy(
        update={
            "balance": snapshot.balance,
            "equity": snapshot.equity,
            "open_positions": snapshot.positions,
        }
    )
    store.save(updated, source="mt5-sync")
    store.record_sync_result(conn["id"], error=None)

    if snapshot.unprotected_position_count:
        logger.warning(
            "%d open position(s) have no stop-loss set in MT5 — their risk is "
            "estimated at the config's max_stop_usd ceiling, not their true "
            "(larger) unbounded risk. Set a stop in the terminal.",
            snapshot.unprotected_position_count,
        )


async def daily_rollover(store: AccountStore, cfg: RuleConfig) -> None:
    """Runs on every scheduler tick but only acts once per calendar day in
    the firm's own reset timezone — safe to call as often as you like."""
    tz = ZoneInfo(cfg.hard_limits.daily_reset.timezone)
    today = datetime.now(tz).date().isoformat()

    if store.last_rollover_date() == today:
        return

    current = store.latest()
    if current is None:
        return

    store.record_rollover(today, current.balance)
    updated = current.model_copy(update={"prev_day_closing_balance": current.balance})
    store.save(updated, source="rollover")
    logger.info("Daily rollover: prev_day_closing_balance set to %s for %s", current.balance, today)
