"""The invariant the whole system rests on.

    For ANY account state, market condition and AI judgment, a trade the engine
    approves can never — in its worst case — reach the firm's hard floors.

Everything else in this codebase is a convenience. This is the safety argument.
If this test ever fails, the account is not protected and nothing should ship.

Two strategies are used deliberately:

  hostile_scenarios()  — wide, mostly-invalid input. Proves the engine never
                         crashes and never approves something it shouldn't.
  viable_scenarios()   — plausible setups on healthy accounts. Actually exercises
                         the sizing path, so the invariant tests are not vacuous.

A first version of this file used only the hostile strategy and every single
generated case was blocked — the invariant tests passed without ever evaluating
an approved trade. `test_viable_strategy_actually_exercises_sizing` exists to
make sure that silent failure can never come back.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from propdesk.models import (
    AccountState,
    Direction,
    Judgment,
    MarketSnapshot,
    OpenPosition,
    Target,
)
from propdesk.risk.engine import compute_floors, evaluate, open_risk

from .conftest import NOW

INITIAL = Decimal(100_000)


def _dec(lo: str, hi: str, places: int = 2):
    return st.decimals(min_value=Decimal(lo), max_value=Decimal(hi), places=places)


@st.composite
def hostile_scenarios(draw):
    """Wide, deliberately adversarial input. Most of these should be blocked."""
    price = draw(_dec("1800", "3200"))
    direction = draw(st.sampled_from([Direction.LONG, Direction.SHORT]))

    zone = draw(_dec("0.10", "5.00"))
    stop_offset = draw(_dec("0.10", "30.00"))
    target_offset = draw(_dec("0.50", "80.00"))

    entry_high = price
    entry_low = price - zone
    if direction is Direction.LONG:
        invalidation = entry_low - stop_offset
        target = entry_high + target_offset
    else:
        invalidation = entry_high + stop_offset
        target = entry_low - target_offset

    judgment = Judgment(
        decision=direction,
        conviction=draw(st.integers(min_value=0, max_value=100)),
        entry_low=entry_low,
        entry_high=entry_high,
        invalidation=invalidation,
        targets=[Target(price=target)],
        expected_hold_minutes=draw(st.integers(min_value=15, max_value=400)),
    )

    positions = []
    if draw(st.booleans()):
        positions.append(
            OpenPosition(
                direction=direction,
                entry_price=price,
                stop_price=price - draw(_dec("0.50", "20.00")) * Decimal(direction.sign),
                lots=draw(_dec("0.01", "3.00")),
                opened_at=NOW - timedelta(minutes=30),
            )
        )

    last_loss_minutes = draw(st.one_of(st.none(), st.integers(min_value=0, max_value=500)))

    account = AccountState(
        phase=draw(st.sampled_from(["challenge", "verification", "funded"])),
        initial_balance=INITIAL,
        balance=draw(_dec("85000", "125000")),
        equity=draw(_dec("85000", "125000")),
        prev_day_closing_balance=draw(_dec("88000", "125000")),
        trades_today=draw(st.integers(min_value=0, max_value=8)),
        consecutive_losses=draw(st.integers(min_value=0, max_value=8)),
        week_pnl=draw(_dec("-12000", "8000")),
        last_loss_at=None if last_loss_minutes is None else NOW - timedelta(minutes=last_loss_minutes),
        open_positions=positions,
    )

    market = MarketSnapshot(
        as_of=NOW,
        price=price,
        atr_m15=draw(_dec("0.10", "25.00")),
        minutes_to_cutoff=draw(st.integers(min_value=0, max_value=700)),
        minutes_to_next_high_impact=draw(st.one_of(st.none(), st.integers(min_value=0, max_value=2000))),
        minutes_since_last_high_impact=draw(st.one_of(st.none(), st.integers(min_value=0, max_value=2000))),
    )

    return judgment, account, market


@st.composite
def viable_scenarios(draw):
    """Plausible setups on healthy accounts — the ones that should mostly trade.

    Constraints mirror the config: stop inside the $2.50-$15.00 band, R:R above
    1.5, enough runway before the cutoff, no open position (max concurrent is 1),
    and equity comfortably clear of both floors.
    """
    price = draw(_dec("1800", "3200"))
    direction = draw(st.sampled_from([Direction.LONG, Direction.SHORT]))

    zone = draw(_dec("0.10", "3.00"))
    entry_high = price
    entry_low = price - zone
    entry_ref = entry_high if direction is Direction.LONG else entry_low

    ai_stop = draw(_dec("2.50", "12.00"))
    atr = draw(_dec("0.10", "6.00"))
    distance = max(ai_stop, Decimal("1.5") * atr, Decimal("2.50"))
    rr = draw(_dec("1.60", "4.00"))

    invalidation = entry_ref - ai_stop * Decimal(direction.sign)
    target = entry_ref + distance * rr * Decimal(direction.sign)

    judgment = Judgment(
        decision=direction,
        conviction=draw(st.integers(min_value=60, max_value=100)),
        entry_low=entry_low,
        entry_high=entry_high,
        invalidation=invalidation,
        targets=[Target(price=target)],
        expected_hold_minutes=draw(st.integers(min_value=15, max_value=150)),
    )

    prev_close = draw(_dec("98000", "112000"))
    drawdown_today = draw(_dec("0", "1500"))

    last_loss_minutes = draw(st.one_of(st.none(), st.integers(min_value=60, max_value=500)))

    account = AccountState(
        phase=draw(st.sampled_from(["challenge", "verification"])),
        initial_balance=INITIAL,
        balance=prev_close,
        equity=prev_close - drawdown_today,
        prev_day_closing_balance=prev_close,
        trades_today=draw(st.integers(min_value=0, max_value=2)),
        consecutive_losses=draw(st.integers(min_value=0, max_value=1)),
        week_pnl=draw(_dec("-3000", "5000")),
        last_loss_at=None if last_loss_minutes is None else NOW - timedelta(minutes=last_loss_minutes),
        open_positions=[],
    )

    market = MarketSnapshot(
        as_of=NOW,
        price=price,
        atr_m15=atr,
        minutes_to_cutoff=draw(st.integers(min_value=200, max_value=600)),
        minutes_to_next_high_impact=draw(st.one_of(st.none(), st.integers(min_value=30, max_value=2000))),
        minutes_since_last_high_impact=draw(st.one_of(st.none(), st.integers(min_value=30, max_value=2000))),
    )

    return judgment, account, market


def any_scenario():
    """Both strategies, so the invariants are checked across the whole space."""
    return st.one_of(viable_scenarios(), hostile_scenarios())


SETTINGS = settings(
    max_examples=600,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


def _worst_case(account, market, verdict, cfg) -> Decimal:
    return (
        account.equity
        - open_risk(account, market, cfg)
        - verdict.risk_usd
        - cfg.soft_limits.slippage_buffer_usd
    )


# --------------------------------------------------------------------------
# Anti-vacuity guard — runs first conceptually, matters most
# --------------------------------------------------------------------------


def test_viable_strategy_actually_exercises_sizing(cfg):
    """The invariant tests below are worthless if nothing is ever approved.

    Guards against a strategy change that silently makes every generated case
    blocked, which is exactly what the first version of this file did.
    """
    counts: Counter[bool] = Counter()

    @given(scenario=viable_scenarios())
    @settings(
        max_examples=400,
        deadline=None,
        database=None,
        suppress_health_check=list(HealthCheck),
    )
    def collect(scenario):
        judgment, account, market = scenario
        counts[evaluate(judgment, account, market, cfg).is_tradeable] += 1

    collect()

    total = sum(counts.values())
    tradeable = counts[True]
    assert total > 0
    assert tradeable / total > 0.5, (
        f"only {tradeable}/{total} viable scenarios produced a tradeable verdict — "
        "the invariant tests are passing vacuously"
    )


# --------------------------------------------------------------------------
# The invariants
# --------------------------------------------------------------------------


@given(scenario=any_scenario())
@SETTINGS
def test_approved_trades_never_reach_the_hard_floors(scenario, cfg):
    """THE invariant. Worst case must stay strictly above both firm limits."""
    judgment, account, market = scenario
    verdict = evaluate(judgment, account, market, cfg)
    if not verdict.is_tradeable:
        return

    floors = compute_floors(account, cfg)
    worst_case = _worst_case(account, market, verdict, cfg)

    assert worst_case > floors.hard_daily, (
        f"worst case {worst_case} would breach the firm's daily floor {floors.hard_daily}"
    )
    assert worst_case > floors.hard_max, (
        f"worst case {worst_case} would breach the firm's max-drawdown floor {floors.hard_max}"
    )


@given(scenario=any_scenario())
@SETTINGS
def test_approved_trades_respect_the_soft_floors_too(scenario, cfg):
    """We should never even reach our own operating limits, let alone the firm's."""
    judgment, account, market = scenario
    verdict = evaluate(judgment, account, market, cfg)
    if not verdict.is_tradeable:
        return
    assert _worst_case(account, market, verdict, cfg) >= compute_floors(account, cfg).binding_soft


@given(scenario=any_scenario())
@SETTINGS
def test_lot_sizes_are_always_valid(scenario, cfg):
    """Never below the broker minimum, always an exact multiple of the lot step."""
    judgment, account, market = scenario
    verdict = evaluate(judgment, account, market, cfg)
    if not verdict.is_tradeable:
        return
    assert verdict.lots >= cfg.instrument.min_lot
    assert verdict.lots % cfg.instrument.lot_step == 0


@given(scenario=any_scenario())
@SETTINGS
def test_risk_never_exceeds_the_configured_budget(scenario, cfg):
    """Rounding must only ever work in our favour."""
    judgment, account, market = scenario
    verdict = evaluate(judgment, account, market, cfg)
    if not verdict.is_tradeable:
        return
    multiplier = cfg.soft_limits.size_multiplier(account.consecutive_losses)
    budget = account.equity * cfg.soft_limits.risk_per_trade_pct / Decimal(100) * multiplier
    assert verdict.risk_usd <= budget


@given(scenario=any_scenario())
@SETTINGS
def test_stop_respects_the_volatility_floor_and_the_width_cap(scenario, cfg):
    judgment, account, market = scenario
    verdict = evaluate(judgment, account, market, cfg)
    if not verdict.is_tradeable:
        return
    assert verdict.stop_distance >= cfg.position_sizing.min_stop_usd
    assert verdict.stop_distance >= cfg.position_sizing.atr_multiple * market.atr_m15
    assert verdict.stop_distance <= cfg.position_sizing.max_stop_usd


@given(scenario=any_scenario())
@SETTINGS
def test_stop_price_sits_on_the_correct_side_of_entry(scenario, cfg):
    judgment, account, market = scenario
    verdict = evaluate(judgment, account, market, cfg)
    if not verdict.is_tradeable:
        return
    assert verdict.entry_price is not None and verdict.stop_price is not None
    if judgment.decision is Direction.LONG:
        assert verdict.stop_price < verdict.entry_price
    else:
        assert verdict.stop_price > verdict.entry_price


@given(scenario=hostile_scenarios())
@SETTINGS
def test_engine_never_raises_on_hostile_input(scenario, cfg):
    """Adversarial input must produce a verdict, never an exception in the live loop."""
    judgment, account, market = scenario
    assert evaluate(judgment, account, market, cfg).status is not None
