"""Rule config loading and the two-tier limit invariant."""

from __future__ import annotations

import copy
from decimal import Decimal

import pytest
import yaml

from propdesk.config import DEFAULT_RULES_PATH, RuleConfig, load_rules


@pytest.fixture(scope="module")
def raw() -> dict:
    return yaml.safe_load(DEFAULT_RULES_PATH.read_text(encoding="utf-8"))


class TestFtmoConfig:
    def test_loads(self, cfg):
        assert cfg.meta.firm == "FTMO"
        assert cfg.instrument.symbol == "XAUUSD"

    def test_ftmo_hard_limits_match_researched_values(self, cfg):
        assert cfg.hard_limits.daily_loss_pct == Decimal("5.0")
        assert cfg.hard_limits.max_loss_pct == Decimal("10.0")
        assert cfg.hard_limits.max_loss_trailing is False
        assert cfg.hard_limits.daily_loss_basis == "prev_day_closing_balance"
        assert cfg.hard_limits.daily_loss_includes_floating is True

    def test_daily_reset_is_prague_not_a_fixed_offset(self, cfg):
        """Must be a DST-aware zone — CET/CEST shifts the reset by an hour."""
        assert cfg.hard_limits.daily_reset.timezone == "Europe/Prague"

    def test_gold_contract_math(self, cfg):
        assert cfg.instrument.contract_size == Decimal(100)
        assert cfg.instrument.usd_per_point_per_lot == Decimal(100)

    def test_no_overnight_is_enabled(self, cfg):
        assert cfg.session.no_overnight is True

    def test_news_blackout_only_applies_when_funded(self, cfg):
        """FTMO allows news trading during evaluation; funded accounts are restricted."""
        assert cfg.news.blackout_enabled("challenge") is False
        assert cfg.news.blackout_enabled("verification") is False
        assert cfg.news.blackout_enabled("funded") is True

    def test_blackout_buffer_is_wider_than_ftmos_two_minutes(self, cfg):
        """Execution is manual, so we leave more room than the firm requires."""
        assert cfg.news.blackout_minutes_before >= 2
        assert cfg.news.blackout_minutes_after >= 2

    def test_dashboard_verification_still_outstanding(self, cfg):
        """Fails deliberately once verified — a prompt to update the docs.

        Rules were researched from the web, not read off the live account.
        """
        assert cfg.meta.verified_against_dashboard is False, (
            "Rules now verified against the dashboard — update PRD assumption A3 "
            "and delete this test."
        )


class TestSoftLimitsMustBeStricter:
    def test_current_config_satisfies_the_invariant(self, cfg):
        assert cfg.soft_limits.daily_stop_pct < cfg.hard_limits.daily_loss_pct
        assert cfg.soft_limits.max_drawdown_pct < cfg.hard_limits.max_loss_pct

    def test_looser_daily_stop_is_rejected(self, raw):
        bad = copy.deepcopy(raw)
        bad["soft_limits"]["daily_stop_pct"] = 5.0  # equal to FTMO's limit
        with pytest.raises(ValueError, match="daily"):
            RuleConfig.model_validate(bad)

    def test_looser_max_drawdown_is_rejected(self, raw):
        bad = copy.deepcopy(raw)
        bad["soft_limits"]["max_drawdown_pct"] = 12.0
        with pytest.raises(ValueError, match="max drawdown"):
            RuleConfig.model_validate(bad)

    def test_unknown_key_is_rejected(self, raw):
        """Typos in a rule file must fail loudly, not be silently ignored."""
        bad = copy.deepcopy(raw)
        bad["soft_limits"]["risk_per_trade_percent"] = 1.0
        with pytest.raises(ValueError):
            RuleConfig.model_validate(bad)


class TestSizeMultiplier:
    def test_full_size_with_no_losses(self, cfg):
        assert cfg.soft_limits.size_multiplier(0) == Decimal(1)
        assert cfg.soft_limits.size_multiplier(1) == Decimal(1)

    def test_halved_after_two(self, cfg):
        assert cfg.soft_limits.size_multiplier(2) == Decimal("0.5")

    def test_zero_after_three(self, cfg):
        assert cfg.soft_limits.size_multiplier(3) == Decimal(0)

    def test_longer_streaks_inherit_the_strictest_rule(self, cfg):
        """A 7-loss streak must not fall through to full size."""
        for n in range(3, 10):
            assert cfg.soft_limits.size_multiplier(n) == Decimal(0)


def test_load_rules_from_path():
    cfg = load_rules(DEFAULT_RULES_PATH)
    assert isinstance(cfg, RuleConfig)
