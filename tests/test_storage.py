"""Account state persistence round-trips."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from propdesk.models import Direction, OpenPosition
from propdesk.storage import AccountStore


def test_empty_store_has_no_latest(tmp_path):
    store = AccountStore(tmp_path / "test.db")
    assert store.latest() is None


def test_save_and_retrieve_round_trips_decimals_exactly(tmp_path, account):
    store = AccountStore(tmp_path / "test.db")
    store.save(account)
    latest = store.latest()
    assert latest.equity == account.equity
    assert latest.initial_balance == account.initial_balance
    assert isinstance(latest.equity, Decimal)


def test_open_positions_round_trip(tmp_path, account):
    store = AccountStore(tmp_path / "test.db")
    position = OpenPosition(
        direction=Direction.LONG,
        entry_price=Decimal("2650.00"),
        stop_price=Decimal("2643.00"),
        lots=Decimal("0.71"),
        opened_at=datetime(2026, 9, 21, 19, 0, tzinfo=timezone.utc),
    )
    updated = account.model_copy(update={"open_positions": [position]})
    store.save(updated)
    latest = store.latest()
    assert len(latest.open_positions) == 1
    assert latest.open_positions[0].direction is Direction.LONG
    assert latest.open_positions[0].entry_price == Decimal("2650.00")


def test_latest_is_the_most_recent_save(tmp_path, account):
    store = AccountStore(tmp_path / "test.db")
    store.save(account)
    updated = account.model_copy(update={"equity": Decimal("99000")})
    store.save(updated)
    assert store.latest().equity == Decimal("99000")


def test_history_returns_newest_first(tmp_path, account):
    store = AccountStore(tmp_path / "test.db")
    for equity in (Decimal("100000"), Decimal("99500"), Decimal("99000")):
        store.save(account.model_copy(update={"equity": equity}))
    history = store.history(limit=10)
    assert history[0]["equity"] == "99000"
    assert history[-1]["equity"] == "100000"


def test_history_respects_limit(tmp_path, account):
    store = AccountStore(tmp_path / "test.db")
    for i in range(5):
        store.save(account.model_copy(update={"trades_today": i}))
    assert len(store.history(limit=3)) == 3


def test_nothing_saved_is_never_lost_on_reopen(tmp_path, account):
    path = tmp_path / "persist.db"
    store1 = AccountStore(path)
    store1.save(account)
    store1.close()

    store2 = AccountStore(path)
    assert store2.latest().equity == account.equity
