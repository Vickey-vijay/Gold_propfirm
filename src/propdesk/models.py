"""Core domain models.

The architectural boundary of the whole system lives in this file: `Judgment`
has no field for position size, and forbids extra fields. The AI physically
cannot express how much to trade. Sizing belongs to the Risk Engine alone.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NO_TRADE = "NO_TRADE"

    @property
    def sign(self) -> int:
        return {Direction.LONG: 1, Direction.SHORT: -1}.get(self, 0)


class VerdictStatus(str, Enum):
    APPROVED = "APPROVED"
    RESIZED = "RESIZED"
    BLOCKED = "BLOCKED"


class BlockReason(str, Enum):
    NO_TRADE = "no_trade"
    LOW_CONVICTION = "low_conviction"
    STOP_TOO_WIDE = "stop_too_wide"
    POOR_RR = "poor_risk_reward"
    DRAWDOWN_HEADROOM = "insufficient_drawdown_headroom"
    NEWS_BLACKOUT = "news_blackout"
    INSUFFICIENT_TIME = "insufficient_time_before_cutoff"
    MAX_TRADES = "max_trades_per_day"
    MAX_CONCURRENT = "max_concurrent_positions"
    COOLDOWN = "cooldown_after_loss"
    WEEKLY_BREAKER = "weekly_circuit_breaker"
    LOSS_STREAK = "consecutive_loss_streak"
    BELOW_MIN_LOT = "size_below_minimum_lot"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OpenPosition(_Base):
    direction: Direction
    entry_price: Decimal
    stop_price: Decimal
    lots: Decimal
    opened_at: datetime

    def risk_usd(self, current_price: Decimal, usd_per_point_per_lot: Decimal) -> Decimal:
        """Money still at risk if this position stops out from here.

        Zero once the stop is at or beyond breakeven in our favour — a position
        whose stop is in profit cannot lose money.
        """
        if self.direction is Direction.LONG:
            distance = current_price - self.stop_price
        else:
            distance = self.stop_price - current_price
        return max(Decimal(0), distance) * self.lots * usd_per_point_per_lot


class AccountState(_Base):
    """User-maintained truth about the account.

    `prev_day_closing_balance` is the basis FTMO measures the daily loss from —
    it is not the same as today's opening equity once a position is floating.
    """

    phase: str
    initial_balance: Decimal
    balance: Decimal
    equity: Decimal
    prev_day_closing_balance: Decimal
    trading_days_used: int = 0
    trades_today: int = 0
    consecutive_losses: int = 0
    week_pnl: Decimal = Decimal(0)
    last_loss_at: datetime | None = None
    open_positions: list[OpenPosition] = Field(default_factory=list)


class Target(_Base):
    price: Decimal
    rationale: str = ""


class Judgment(_Base):
    """The AI's opinion. Deliberately has no size, lot, leverage or risk field.

    `extra="forbid"` means a model that tries to emit one gets a validation
    error rather than having it silently ignored.
    """

    decision: Direction
    conviction: int = Field(ge=0, le=100)

    market_read: str = ""
    positioning_read: str = ""
    reasoning: str = ""
    what_would_change_my_mind: str = ""
    kill_conditions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)

    entry_low: Decimal | None = None
    entry_high: Decimal | None = None
    invalidation: Decimal | None = None
    targets: list[Target] = Field(default_factory=list)
    expected_hold_minutes: int | None = None

    @model_validator(mode="after")
    def _trade_fields_present(self) -> Judgment:
        if self.decision is Direction.NO_TRADE:
            return self
        missing = [
            name
            for name, value in (
                ("entry_low", self.entry_low),
                ("entry_high", self.entry_high),
                ("invalidation", self.invalidation),
            )
            if value is None
        ]
        if missing:
            raise ValueError(f"{self.decision.value} judgment missing: {', '.join(missing)}")
        if not self.targets:
            raise ValueError(f"{self.decision.value} judgment has no targets")
        if self.entry_low > self.entry_high:  # type: ignore[operator]
            raise ValueError("entry_low must not exceed entry_high")
        return self

    @property
    def entry_reference(self) -> Decimal:
        """The price sizing is computed against.

        Uses the far edge of the entry zone — the worst fill in the zone — so
        the stop distance we size on is never optimistic.
        """
        assert self.entry_low is not None and self.entry_high is not None
        return self.entry_low if self.decision is Direction.SHORT else self.entry_high


class MarketSnapshot(_Base):
    """The market facts the Risk Engine needs. Not the full Context Packet."""

    as_of: datetime
    price: Decimal
    atr_m15: Decimal
    minutes_to_cutoff: int
    minutes_to_next_high_impact: int | None = None
    minutes_since_last_high_impact: int | None = None


class Verdict(_Base):
    status: VerdictStatus
    lots: Decimal = Decimal(0)
    risk_usd: Decimal = Decimal(0)
    stop_distance: Decimal = Decimal(0)
    entry_price: Decimal | None = None
    stop_price: Decimal | None = None
    blocked_reasons: list[BlockReason] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def is_tradeable(self) -> bool:
        return self.status in (VerdictStatus.APPROVED, VerdictStatus.RESIZED)


class Floors(_Base):
    """Equity levels that must never be crossed.

    `hard_*` are the firm's. `soft_*` are ours and are always stricter, so
    respecting the soft floors provably respects the hard ones.
    """

    hard_daily: Decimal
    hard_max: Decimal
    soft_daily: Decimal
    soft_max: Decimal

    @property
    def binding_soft(self) -> Decimal:
        return max(self.soft_daily, self.soft_max)

    @property
    def binding_hard(self) -> Decimal:
        return max(self.hard_daily, self.hard_max)
