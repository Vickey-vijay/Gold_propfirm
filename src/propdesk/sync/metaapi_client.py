"""Thin wrapper around the MetaApi.cloud SDK.

Connects to a single MT5 account using ONLY its investor (read-only)
password, and returns balance, equity, and open positions. Never sends a
trade-capable password, never places an order — there is no order-placing
call anywhere in this module, by design, not just by convention.

The exact RPC method names below (`get_account_information`, `get_positions`)
are corroborated by MetaApi's own docs and example repository, but have not
been exercised against a live account yet — that happens on the first real
sync attempt, and any mismatch surfaces as a clear error there rather than
being silently swallowed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from metaapi_cloud_sdk import MetaApi

from propdesk.models import Direction, OpenPosition


@dataclass
class SyncedAccount:
    balance: Decimal
    equity: Decimal
    positions: list[OpenPosition]
    unprotected_position_count: int = 0
    """Positions MT5 reports with no stop-loss set. Their risk is estimated
    conservatively (see fetch_account_snapshot) rather than as zero — flag
    this to the operator so they know to check the broker terminal."""


class MetaApiConnectionError(RuntimeError):
    """Raised for anything that goes wrong talking to MetaApi — connectivity,
    auth, an account that hasn't finished deploying, etc. Callers should show
    this to the operator rather than let a raw SDK exception surface."""


async def provision_account(*, token: str, login: str, investor_password: str, server: str) -> str:
    """Creates (or reuses) the MetaApi-side account object and deploys it.
    Returns the MetaApi account id, which we store for future syncs so we
    never re-provision on every poll."""
    api = MetaApi(token=token)
    try:
        existing = await api.metatrader_account_api.get_accounts_with_infinite_scroll_pagination()
        for acc in existing:
            if acc.login == login and acc.server == server:
                if acc.state != "DEPLOYED":
                    await acc.deploy()
                return acc.id

        account = await api.metatrader_account_api.create_account(
            account={
                "name": f"propdesk-{login}",
                "type": "cloud",
                "login": login,
                "password": investor_password,
                "server": server,
                "platform": "mt5",
                "magic": 0,
            }
        )
        await account.deploy()
        return account.id
    except Exception as exc:  # noqa: BLE001 — surfaced to the operator, not swallowed
        raise MetaApiConnectionError(f"Could not provision MT5 account on MetaApi: {exc}") from exc


async def fetch_account_snapshot(
    *, token: str, metaapi_account_id: str, fallback_stop_distance: Decimal
) -> SyncedAccount:
    """Pulls current balance, equity, and open positions via a read-only
    RPC connection. Raises MetaApiConnectionError on any failure.

    `fallback_stop_distance` is used ONLY for a position MT5 reports with no
    stop-loss set. Defaulting such a position's risk to zero would be wrong
    in the dangerous direction — an unprotected position has the LARGEST
    risk, not the smallest. The caller passes the config's max_stop_usd
    ceiling, so an unprotected position is treated as risking the worst
    width we'd ever knowingly allow, not as risk-free.
    """
    api = MetaApi(token=token)
    try:
        account = await api.metatrader_account_api.get_account(metaapi_account_id)
        if account.state != "DEPLOYED":
            await account.deploy()
        await account.wait_deployed()

        connection = account.get_rpc_connection()
        await connection.connect()
        await connection.wait_synchronized()

        try:
            info = await connection.get_account_information()
            raw_positions = await connection.get_positions()
        finally:
            await connection.close()

        positions = []
        unprotected = 0
        for p in raw_positions:
            direction = Direction.LONG if p["type"] == "POSITION_TYPE_BUY" else Direction.SHORT
            entry = Decimal(str(p["openPrice"]))
            raw_sl = p.get("stopLoss")
            if raw_sl:
                stop = Decimal(str(raw_sl))
            else:
                unprotected += 1
                stop = entry - fallback_stop_distance if direction is Direction.LONG else entry + fallback_stop_distance
            positions.append(
                OpenPosition(
                    direction=direction,
                    entry_price=entry,
                    stop_price=stop,
                    lots=Decimal(str(p["volume"])),
                    opened_at=p["time"],
                )
            )

        return SyncedAccount(
            balance=Decimal(str(info["balance"])),
            equity=Decimal(str(info["equity"])),
            positions=positions,
            unprotected_position_count=unprotected,
        )
    except MetaApiConnectionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise MetaApiConnectionError(f"Sync failed: {exc}") from exc
