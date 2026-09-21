"""Rule configuration — loading and validation.

No firm-specific value may be hardcoded anywhere else in the codebase. Everything
the Risk Engine gates on comes from here.

The two-tier limit design is enforced at load time: soft limits must be strictly
stricter than the firm's hard limits. That invariant is what makes the property
test in tests/test_properties.py provable rather than merely likely.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

Phase = Literal["challenge", "verification", "funded"]


class _Base(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Meta(_Base):
    firm: str
    program: str
    account_size: Decimal
    currency: str = "USD"
    researched_on: date | None = None
    verified_against_dashboard: bool = False


class DailyReset(_Base):
    timezone: str
    hour: int = Field(ge=0, le=23)


class HardLimits(_Base):
    """The firm's actual limits. Never used to gate a trade.

    These exist so the engine can assert it never approached them. A crossing
    is a P0 bug, not a bad day.
    """

    daily_loss_pct: Decimal
    daily_loss_basis: Literal["prev_day_closing_balance", "day_start_balance"]
    daily_loss_includes_floating: bool
    max_loss_pct: Decimal
    max_loss_basis: Literal["initial_balance", "highest_balance"]
    max_loss_trailing: bool
    daily_reset: DailyReset


class PhaseRules(_Base):
    profit_target_pct: Decimal | None = None
    min_trading_days: int = 0
    max_days: int | None = None


class Phases(_Base):
    challenge: PhaseRules
    verification: PhaseRules
    funded: PhaseRules

    def for_phase(self, phase: Phase) -> PhaseRules:
        return getattr(self, phase)


class SoftLimits(_Base):
    """Our operating limits. These are what actually block trades."""

    risk_per_trade_pct: Decimal
    daily_stop_pct: Decimal
    max_drawdown_pct: Decimal
    weekly_circuit_breaker_pct: Decimal
    max_trades_per_day: int
    max_concurrent_positions: int
    cooldown_after_loss_minutes: int
    reduce_size_after_consecutive_losses: dict[int, Decimal]
    slippage_buffer_usd: Decimal

    def size_multiplier(self, consecutive_losses: int) -> Decimal:
        """Risk multiplier after N consecutive losses.

        Uses the highest configured threshold at or below the loss count, so
        a 4-loss streak inherits the 3-loss rule rather than falling back to
        full size.
        """
        applicable = [n for n in self.reduce_size_after_consecutive_losses if n <= consecutive_losses]
        if not applicable:
            return Decimal(1)
        return self.reduce_size_after_consecutive_losses[max(applicable)]


class Instrument(_Base):
    symbol: str
    contract_size: Decimal
    usd_per_point_per_lot: Decimal
    min_lot: Decimal
    lot_step: Decimal


class PositionSizing(_Base):
    method: Literal["atr_risk"]
    atr_period: int
    atr_timeframe: str
    atr_multiple: Decimal
    min_stop_usd: Decimal
    max_stop_usd: Decimal
    rounding: Literal["down"]

    @model_validator(mode="after")
    def _stops_ordered(self) -> PositionSizing:
        if self.min_stop_usd >= self.max_stop_usd:
            raise ValueError("min_stop_usd must be below max_stop_usd")
        return self


class PrimaryWindow(_Base):
    start: str
    end: str


class SessionRules(_Base):
    timezone: str
    primary_window: PrimaryWindow
    no_overnight: bool
    flat_by: str
    min_minutes_before_cutoff: int
    expected_hold_minutes: int


class NewsRules(_Base):
    blackout_enabled_in_phase: dict[Phase, bool]
    blackout_minutes_before: int
    blackout_minutes_after: int
    high_impact_events: list[str]
    warn_minutes_before: int

    def blackout_enabled(self, phase: Phase) -> bool:
        return self.blackout_enabled_in_phase.get(phase, True)


class SignalPolicy(_Base):
    min_conviction: int
    min_rr: Decimal
    entry_valid_minutes: int
    max_hold_minutes: int


class RuleConfig(_Base):
    meta: Meta
    hard_limits: HardLimits
    phases: Phases
    soft_limits: SoftLimits
    instrument: Instrument
    position_sizing: PositionSizing
    session: SessionRules
    news: NewsRules
    signal_policy: SignalPolicy

    @model_validator(mode="after")
    def _soft_limits_are_stricter(self) -> RuleConfig:
        """The safety argument of the whole system depends on this holding.

        If a soft limit were ever looser than the firm's hard limit, the engine
        could approve a trade that breaches the account while believing it was
        within bounds.
        """
        if self.soft_limits.daily_stop_pct >= self.hard_limits.daily_loss_pct:
            raise ValueError(
                f"soft daily stop ({self.soft_limits.daily_stop_pct}%) must be stricter "
                f"than the firm's daily loss limit ({self.hard_limits.daily_loss_pct}%)"
            )
        if self.soft_limits.max_drawdown_pct >= self.hard_limits.max_loss_pct:
            raise ValueError(
                f"soft max drawdown ({self.soft_limits.max_drawdown_pct}%) must be stricter "
                f"than the firm's max loss limit ({self.hard_limits.max_loss_pct}%)"
            )
        return self


def load_rules(path: str | Path) -> RuleConfig:
    """Load and validate a rule config from YAML."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return RuleConfig.model_validate(raw)


DEFAULT_RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "rules.ftmo-100k.yaml"
