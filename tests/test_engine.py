"""Risk Engine behaviour — one test per rule, plus the exact boundaries."""

from __future__ import annotations

from decimal import Decimal

import pytest

from propdesk.models import (
    AccountState,
    BlockReason,
    Direction,
    Judgment,
    OpenPosition,
    Target,
    VerdictStatus,
)
from propdesk.risk.engine import compute_floors, evaluate


class TestHappyPath:
    def test_clean_setup_is_approved(self, judgment, account, market, cfg):
        v = evaluate(judgment, account, market, cfg)
        assert v.status is VerdictStatus.APPROVED
        assert v.blocked_reasons == []

    def test_sizing_matches_hand_calculation(self, judgment, account, market, cfg):
        """$100k * 0.5% = $500 budget; $7 stop -> 0.71 lots -> $497 actual risk."""
        v = evaluate(judgment, account, market, cfg)
        assert v.stop_distance == Decimal("7.00")
        assert v.lots == Decimal("0.71")
        assert v.risk_usd == Decimal("497.00")
        assert v.entry_price == Decimal("2650.00")
        assert v.stop_price == Decimal("2643.00")

    def test_actual_risk_never_exceeds_budget(self, judgment, account, market, cfg):
        v = evaluate(judgment, account, market, cfg)
        budget = account.equity * cfg.soft_limits.risk_per_trade_pct / Decimal(100)
        assert v.risk_usd <= budget

    def test_short_is_sized_symmetrically(self, account, market, cfg):
        j = Judgment(
            decision=Direction.SHORT,
            conviction=75,
            entry_low=Decimal("2650.00"),
            entry_high=Decimal("2652.00"),
            invalidation=Decimal("2657.00"),
            targets=[Target(price=Decimal("2638.00"))],
        )
        v = evaluate(j, account, market, cfg)
        assert v.status is VerdictStatus.APPROVED
        # Short sizes off the low edge of the zone — the worst fill.
        assert v.entry_price == Decimal("2650.00")
        assert v.stop_price == Decimal("2657.00")


class TestSetupQuality:
    def test_no_trade_is_blocked(self, account, market, cfg):
        v = evaluate(Judgment(decision=Direction.NO_TRADE, conviction=0), account, market, cfg)
        assert v.status is VerdictStatus.BLOCKED
        assert v.blocked_reasons == [BlockReason.NO_TRADE]

    def test_low_conviction_is_blocked(self, judgment, account, market, cfg):
        j = judgment.model_copy(update={"conviction": cfg.signal_policy.min_conviction - 1})
        assert BlockReason.LOW_CONVICTION in evaluate(j, account, market, cfg).blocked_reasons

    def test_conviction_exactly_at_threshold_passes(self, judgment, account, market, cfg):
        j = judgment.model_copy(update={"conviction": cfg.signal_policy.min_conviction})
        assert BlockReason.LOW_CONVICTION not in evaluate(j, account, market, cfg).blocked_reasons

    def test_poor_risk_reward_is_blocked(self, judgment, account, market, cfg):
        # $7 stop with a $7 target is 1.0 R:R, below the 1.5 minimum.
        j = judgment.model_copy(update={"targets": [Target(price=Decimal("2657.00"))]})
        assert BlockReason.POOR_RR in evaluate(j, account, market, cfg).blocked_reasons

    def test_stop_wider_than_max_is_blocked(self, judgment, account, market, cfg):
        j = judgment.model_copy(
            update={
                "invalidation": Decimal("2630.00"),  # $20 stop, max is $15
                "targets": [Target(price=Decimal("2700.00"))],
            }
        )
        assert BlockReason.STOP_TOO_WIDE in evaluate(j, account, market, cfg).blocked_reasons


class TestTiming:
    def test_insufficient_time_before_cutoff_is_blocked(self, judgment, account, market, cfg):
        m = market.model_copy(update={"minutes_to_cutoff": 60})
        assert BlockReason.INSUFFICIENT_TIME in evaluate(judgment, account, m, cfg).blocked_reasons

    def test_exact_minimum_time_is_allowed(self, judgment, account, market, cfg):
        need = cfg.session.expected_hold_minutes + cfg.session.min_minutes_before_cutoff
        m = market.model_copy(update={"minutes_to_cutoff": need})
        assert BlockReason.INSUFFICIENT_TIME not in evaluate(judgment, account, m, cfg).blocked_reasons

    def test_ai_hold_estimate_overrides_the_default(self, judgment, account, market, cfg):
        """A trade the AI expects to run long needs proportionally more runway."""
        j = judgment.model_copy(update={"expected_hold_minutes": 300})
        m = market.model_copy(update={"minutes_to_cutoff": 240})
        assert BlockReason.INSUFFICIENT_TIME in evaluate(j, account, m, cfg).blocked_reasons


class TestNewsBlackout:
    def test_no_blackout_during_challenge(self, judgment, account, market, cfg):
        """FTMO permits news trading in evaluation, so the blackout is off."""
        m = market.model_copy(update={"minutes_to_next_high_impact": 1})
        assert BlockReason.NEWS_BLACKOUT not in evaluate(judgment, account, m, cfg).blocked_reasons

    def test_blackout_before_news_on_funded_account(self, judgment, market, cfg):
        acct = AccountState(
            phase="funded",
            initial_balance=Decimal(100_000),
            balance=Decimal(100_000),
            equity=Decimal(100_000),
            prev_day_closing_balance=Decimal(100_000),
        )
        m = market.model_copy(update={"minutes_to_next_high_impact": 3})
        assert BlockReason.NEWS_BLACKOUT in evaluate(judgment, acct, m, cfg).blocked_reasons

    def test_blackout_after_news_on_funded_account(self, judgment, market, cfg):
        acct = AccountState(
            phase="funded",
            initial_balance=Decimal(100_000),
            balance=Decimal(100_000),
            equity=Decimal(100_000),
            prev_day_closing_balance=Decimal(100_000),
        )
        m = market.model_copy(update={"minutes_since_last_high_impact": 2})
        assert BlockReason.NEWS_BLACKOUT in evaluate(judgment, acct, m, cfg).blocked_reasons

    def test_outside_the_window_is_fine(self, judgment, market, cfg):
        acct = AccountState(
            phase="funded",
            initial_balance=Decimal(100_000),
            balance=Decimal(100_000),
            equity=Decimal(100_000),
            prev_day_closing_balance=Decimal(100_000),
        )
        m = market.model_copy(update={"minutes_to_next_high_impact": 90})
        assert BlockReason.NEWS_BLACKOUT not in evaluate(judgment, acct, m, cfg).blocked_reasons


class TestBehaviouralLimits:
    def test_max_trades_per_day(self, judgment, account, market, cfg):
        a = account.model_copy(update={"trades_today": cfg.soft_limits.max_trades_per_day})
        assert BlockReason.MAX_TRADES in evaluate(judgment, a, market, cfg).blocked_reasons

    def test_max_concurrent_positions(self, judgment, account, market, cfg):
        pos = OpenPosition(
            direction=Direction.LONG,
            entry_price=Decimal("2645"),
            stop_price=Decimal("2640"),
            lots=Decimal("0.50"),
            opened_at=market.as_of,
        )
        a = account.model_copy(update={"open_positions": [pos]})
        assert BlockReason.MAX_CONCURRENT in evaluate(judgment, a, market, cfg).blocked_reasons

    def test_cooldown_after_a_loss(self, judgment, account, market, cfg, minutes_ago):
        a = account.model_copy(update={"last_loss_at": minutes_ago(10)})
        assert BlockReason.COOLDOWN in evaluate(judgment, a, market, cfg).blocked_reasons

    def test_cooldown_expires(self, judgment, account, market, cfg, minutes_ago):
        a = account.model_copy(
            update={"last_loss_at": minutes_ago(cfg.soft_limits.cooldown_after_loss_minutes + 1)}
        )
        assert BlockReason.COOLDOWN not in evaluate(judgment, a, market, cfg).blocked_reasons

    def test_weekly_circuit_breaker(self, judgment, account, market, cfg):
        a = account.model_copy(update={"week_pnl": Decimal(-4_000)})
        assert BlockReason.WEEKLY_BREAKER in evaluate(judgment, a, market, cfg).blocked_reasons

    def test_loss_streak_halves_size_then_stops(self, judgment, account, market, cfg):
        two = evaluate(judgment, account.model_copy(update={"consecutive_losses": 2}), market, cfg)
        assert two.status is VerdictStatus.APPROVED
        assert two.lots == Decimal("0.35")  # half of 0.71, rounded down

        three = evaluate(judgment, account.model_copy(update={"consecutive_losses": 3}), market, cfg)
        assert BlockReason.LOSS_STREAK in three.blocked_reasons

    def test_longer_streak_inherits_the_strictest_rule(self, judgment, account, market, cfg):
        """A 5-loss streak must not fall back to full size."""
        v = evaluate(judgment, account.model_copy(update={"consecutive_losses": 5}), market, cfg)
        assert BlockReason.LOSS_STREAK in v.blocked_reasons


class TestDrawdownGate:
    def test_blocked_when_equity_sits_on_the_soft_floor(self, judgment, account, market, cfg):
        a = account.model_copy(update={"equity": Decimal(98_000)})
        v = evaluate(judgment, a, market, cfg)
        assert v.status is VerdictStatus.BLOCKED
        assert BlockReason.DRAWDOWN_HEADROOM in v.blocked_reasons

    def test_blocked_below_the_soft_floor(self, judgment, account, market, cfg):
        a = account.model_copy(update={"equity": Decimal(97_500)})
        assert BlockReason.DRAWDOWN_HEADROOM in evaluate(judgment, a, market, cfg).blocked_reasons

    def test_resized_when_headroom_is_thin(self, judgment, account, market, cfg):
        """$300 of headroom cannot fund a $500 risk — take the smaller trade."""
        a = account.model_copy(update={"equity": Decimal("98350.00")})
        v = evaluate(judgment, a, market, cfg)
        assert v.status is VerdictStatus.RESIZED
        assert v.risk_usd < a.equity * cfg.soft_limits.risk_per_trade_pct / Decimal(100)
        assert v.lots >= cfg.instrument.min_lot

    def test_blocked_when_headroom_cannot_fund_the_minimum_lot(self, judgment, account, market, cfg):
        """Headroom below one minimum lot's risk must block, not approve a rounded-down zero."""
        a = account.model_copy(update={"equity": Decimal("98053.00")})
        v = evaluate(judgment, a, market, cfg)
        assert v.status is VerdictStatus.BLOCKED
        assert BlockReason.BELOW_MIN_LOT in v.blocked_reasons

    def test_open_risk_consumes_headroom(self, judgment, market, cfg):
        """An open position's risk must be subtracted before sizing a new trade."""
        pos = OpenPosition(
            direction=Direction.LONG,
            entry_price=Decimal("2650"),
            stop_price=Decimal("2640"),  # $10 from price 2650 -> $1000 at 1.0 lot
            lots=Decimal("1.00"),
            opened_at=market.as_of,
        )
        a = AccountState(
            phase="challenge",
            initial_balance=Decimal(100_000),
            balance=Decimal(100_000),
            equity=Decimal("98600.00"),
            prev_day_closing_balance=Decimal(100_000),
            open_positions=[pos],
        )
        # $600 above the floor, but $1000 already at risk -> negative headroom.
        v = evaluate(judgment, a, market, cfg)
        assert v.status is VerdictStatus.BLOCKED
        assert BlockReason.DRAWDOWN_HEADROOM in v.blocked_reasons

    def test_stop_in_profit_contributes_no_open_risk(self, market, cfg):
        pos = OpenPosition(
            direction=Direction.LONG,
            entry_price=Decimal("2640"),
            stop_price=Decimal("2655"),  # above current price — locked in profit
            lots=Decimal("1.00"),
            opened_at=market.as_of,
        )
        assert pos.risk_usd(market.price, cfg.instrument.usd_per_point_per_lot) == Decimal(0)

    def test_daily_floor_binds_once_the_account_is_in_profit(self, judgment, market, cfg):
        """Up 8% overall, the 2% daily stop binds long before the 6% max drawdown.

        Equity of 106,100 is 16,100 clear of the 90,000 max floor but only $100
        above the 106,000 daily floor. The daily floor is what must govern.
        """
        a = AccountState(
            phase="challenge",
            initial_balance=Decimal(100_000),
            balance=Decimal(108_000),
            equity=Decimal("106100.00"),
            prev_day_closing_balance=Decimal(108_000),
        )
        floors = compute_floors(a, cfg)
        assert floors.binding_soft == floors.soft_daily == Decimal(106_000)

        v = evaluate(judgment, a, market, cfg)
        # $50 of usable headroom after the slippage buffer — a real trade, but tiny.
        assert v.status is VerdictStatus.RESIZED
        assert v.lots == Decimal("0.07")
        assert v.risk_usd == Decimal("49.00")

    def test_max_floor_binds_when_the_account_is_deep_underwater(self, judgment, market, cfg):
        """After a losing day the static max floor can bind instead of the daily one."""
        a = AccountState(
            phase="challenge",
            initial_balance=Decimal(100_000),
            balance=Decimal(95_000),
            equity=Decimal(95_000),
            prev_day_closing_balance=Decimal(95_000),
        )
        floors = compute_floors(a, cfg)
        # daily floor 93,000 vs static max floor 94,000 — the max floor is higher.
        assert floors.binding_soft == floors.soft_max == Decimal(94_000)
        assert evaluate(judgment, a, market, cfg).is_tradeable


class TestMultipleReasons:
    def test_all_violated_rules_are_reported(self, judgment, account, market, cfg):
        """Blocked verdicts collect every reason, not just the first."""
        j = judgment.model_copy(update={"conviction": 10})
        a = account.model_copy(
            update={"trades_today": 5, "week_pnl": Decimal(-9_000)}
        )
        m = market.model_copy(update={"minutes_to_cutoff": 10})
        reasons = set(evaluate(j, a, m, cfg).blocked_reasons)
        assert {
            BlockReason.LOW_CONVICTION,
            BlockReason.MAX_TRADES,
            BlockReason.WEEKLY_BREAKER,
            BlockReason.INSUFFICIENT_TIME,
        } <= reasons


class TestSchemaBoundary:
    def test_judgment_rejects_a_position_size_field(self):
        """The AI cannot express sizing — the schema refuses it."""
        with pytest.raises(Exception):
            Judgment(
                decision=Direction.LONG,
                conviction=80,
                entry_low=Decimal("2648"),
                entry_high=Decimal("2650"),
                invalidation=Decimal("2643"),
                targets=[Target(price=Decimal("2662"))],
                lots=Decimal("1.0"),  # type: ignore[call-arg]
            )

    def test_trade_judgment_requires_an_invalidation_level(self):
        with pytest.raises(Exception):
            Judgment(
                decision=Direction.LONG,
                conviction=80,
                entry_low=Decimal("2648"),
                entry_high=Decimal("2650"),
                targets=[Target(price=Decimal("2662"))],
            )

    def test_trade_judgment_requires_targets(self):
        with pytest.raises(Exception):
            Judgment(
                decision=Direction.LONG,
                conviction=80,
                entry_low=Decimal("2648"),
                entry_high=Decimal("2650"),
                invalidation=Decimal("2643"),
            )

    def test_no_trade_needs_no_levels(self):
        j = Judgment(decision=Direction.NO_TRADE, conviction=0, reasoning="Range, no driver.")
        assert j.decision is Direction.NO_TRADE
