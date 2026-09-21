"""Sizing arithmetic. Boundary-exact, Decimal-exact."""

from __future__ import annotations

from decimal import Decimal

import pytest

from propdesk.models import Direction
from propdesk.risk import sizing as sz


class TestFloorToStep:
    @pytest.mark.parametrize(
        "value,step,expected",
        [
            ("0.7142857", "0.01", "0.71"),
            ("1.0", "0.01", "1.00"),
            ("0.009", "0.01", "0.00"),
            ("2.999", "0.01", "2.99"),
            ("5", "0.5", "5.0"),
        ],
    )
    def test_always_rounds_down(self, value, step, expected):
        assert sz.floor_to_step(Decimal(value), Decimal(step)) == Decimal(expected)

    def test_exact_multiple_is_unchanged(self):
        assert sz.floor_to_step(Decimal("0.71"), Decimal("0.01")) == Decimal("0.71")

    def test_rejects_non_positive_step(self):
        with pytest.raises(ValueError):
            sz.floor_to_step(Decimal("1"), Decimal("0"))


class TestStopDistance:
    def test_uses_ai_invalidation_when_wider_than_volatility(self, cfg):
        d = sz.stop_distance(
            entry=Decimal("2650"),
            invalidation=Decimal("2643"),
            atr_m15=Decimal("3.20"),  # atr floor = 4.80
            sizing=cfg.position_sizing,
        )
        assert d == Decimal("7")

    def test_widens_to_atr_floor_when_ai_stop_is_too_tight(self, cfg):
        """The AI may widen a stop but never tighten it inside the noise."""
        d = sz.stop_distance(
            entry=Decimal("2650"),
            invalidation=Decimal("2649"),  # $1 — inside the noise
            atr_m15=Decimal("4.00"),  # atr floor = 6.00
            sizing=cfg.position_sizing,
        )
        assert d == Decimal("6.00")

    def test_min_stop_applies_in_dead_volatility(self, cfg):
        d = sz.stop_distance(
            entry=Decimal("2650"),
            invalidation=Decimal("2649.90"),
            atr_m15=Decimal("0.10"),  # atr floor = 0.15
            sizing=cfg.position_sizing,
        )
        assert d == cfg.position_sizing.min_stop_usd


class TestStopPrice:
    def test_long_stop_sits_below_entry(self):
        assert sz.stop_price(
            entry=Decimal("2650"), distance=Decimal("7"), direction=Direction.LONG
        ) == Decimal("2643")

    def test_short_stop_sits_above_entry(self):
        assert sz.stop_price(
            entry=Decimal("2650"), distance=Decimal("7"), direction=Direction.SHORT
        ) == Decimal("2657")

    def test_no_trade_has_no_stop(self):
        with pytest.raises(ValueError):
            sz.stop_price(entry=Decimal("2650"), distance=Decimal("7"), direction=Direction.NO_TRADE)


class TestRiskToLots:
    def test_documented_example(self, cfg):
        """$500 risk with a $5.00 stop is exactly 1.00 lot on XAUUSD."""
        lots = sz.risk_to_lots(
            risk_usd=Decimal("500"), stop_distance=Decimal("5.00"), instrument=cfg.instrument
        )
        assert lots == Decimal("1.00")

    def test_rounds_down_never_up(self, cfg):
        """$500 / ($7 * 100) = 0.714... must become 0.71, never 0.72."""
        lots = sz.risk_to_lots(
            risk_usd=Decimal("500"), stop_distance=Decimal("7.00"), instrument=cfg.instrument
        )
        assert lots == Decimal("0.71")
        assert sz.lots_to_risk(lots=lots, stop_distance=Decimal("7.00"), instrument=cfg.instrument) <= 500

    def test_rejects_non_positive_stop(self, cfg):
        with pytest.raises(ValueError):
            sz.risk_to_lots(risk_usd=Decimal("500"), stop_distance=Decimal("0"), instrument=cfg.instrument)


class TestFloors:
    def test_fresh_account(self, cfg):
        f = sz.compute_floors(
            initial_balance=Decimal(100_000),
            prev_day_closing_balance=Decimal(100_000),
            hard_daily_pct=Decimal(5),
            hard_max_pct=Decimal(10),
            soft_daily_pct=Decimal(2),
            soft_max_pct=Decimal(6),
        )
        assert f.hard_daily == Decimal(95_000)
        assert f.hard_max == Decimal(90_000)
        assert f.soft_daily == Decimal(98_000)
        assert f.soft_max == Decimal(94_000)
        assert f.binding_soft == Decimal(98_000)
        assert f.binding_hard == Decimal(95_000)

    def test_max_floor_is_static_and_does_not_trail_profit(self):
        """The defining property of FTMO's max drawdown: the floor never moves."""
        f = sz.compute_floors(
            initial_balance=Decimal(100_000),
            prev_day_closing_balance=Decimal(108_000),  # up 8%
            hard_daily_pct=Decimal(5),
            hard_max_pct=Decimal(10),
            soft_daily_pct=Decimal(2),
            soft_max_pct=Decimal(6),
        )
        assert f.hard_max == Decimal(90_000)
        assert f.soft_max == Decimal(94_000)

    def test_daily_floor_tracks_previous_close(self):
        f = sz.compute_floors(
            initial_balance=Decimal(100_000),
            prev_day_closing_balance=Decimal(103_000),
            hard_daily_pct=Decimal(5),
            hard_max_pct=Decimal(10),
            soft_daily_pct=Decimal(2),
            soft_max_pct=Decimal(6),
        )
        assert f.hard_daily == Decimal(98_000)
        assert f.soft_daily == Decimal(101_000)
        # Once in profit the daily floor binds before the max floor.
        assert f.binding_soft == f.soft_daily
