from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from propdesk.config import DEFAULT_RULES_PATH, RuleConfig, load_rules
from propdesk.models import AccountState, Direction, Judgment, MarketSnapshot, Target

NOW = datetime(2026, 9, 21, 19, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def cfg() -> RuleConfig:
    return load_rules(DEFAULT_RULES_PATH)


@pytest.fixture
def account() -> AccountState:
    """A clean $100k account at the start of a trading day."""
    return AccountState(
        phase="challenge",
        initial_balance=Decimal(100_000),
        balance=Decimal(100_000),
        equity=Decimal(100_000),
        prev_day_closing_balance=Decimal(100_000),
    )


@pytest.fixture
def market() -> MarketSnapshot:
    return MarketSnapshot(
        as_of=NOW,
        price=Decimal("2650.00"),
        atr_m15=Decimal("3.20"),
        minutes_to_cutoff=240,
    )


@pytest.fixture
def judgment() -> Judgment:
    """A clean long: entry 2650, invalidation 2643 ($7 stop), target 2662 (1.71 R:R)."""
    return Judgment(
        decision=Direction.LONG,
        conviction=75,
        entry_low=Decimal("2648.00"),
        entry_high=Decimal("2650.00"),
        invalidation=Decimal("2643.00"),
        targets=[Target(price=Decimal("2662.00"), rationale="prior day high")],
        reasoning="Soft CPI, dollar offered, sellers from 2645 trapped.",
    )


@pytest.fixture
def minutes_ago():
    def _f(n: int) -> datetime:
        return NOW - timedelta(minutes=n)

    return _f
