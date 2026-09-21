"""Position sizing arithmetic.

Pure functions, Decimal throughout. Float rounding on a lot size is the kind of
bug that silently risks 1.0001x what you intended, so money and lots never touch
binary floating point here.

XAUUSD contract math: 1 standard lot = 100 troy oz, so a $1.00 price move is
$100 of P&L per lot.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from propdesk.config import Instrument, PositionSizing
from propdesk.models import Direction, Floors


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round down to a multiple of `step`. Always down — never risk more than intended."""
    if step <= 0:
        raise ValueError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def stop_distance(
    *,
    entry: Decimal,
    invalidation: Decimal,
    atr_m15: Decimal,
    sizing: PositionSizing,
) -> Decimal:
    """Distance in dollars between entry and stop.

    The AI supplies an invalidation level; we take the wider of that and the
    volatility floor. The AI can widen a stop, never tighten it below what
    current volatility justifies — a stop inside the noise is not a stop.
    """
    ai_distance = abs(entry - invalidation)
    atr_floor = sizing.atr_multiple * atr_m15
    return max(ai_distance, atr_floor, sizing.min_stop_usd)


def stop_price(*, entry: Decimal, distance: Decimal, direction: Direction) -> Decimal:
    if direction is Direction.LONG:
        return entry - distance
    if direction is Direction.SHORT:
        return entry + distance
    raise ValueError("NO_TRADE has no stop price")


def risk_to_lots(
    *,
    risk_usd: Decimal,
    stop_distance: Decimal,
    instrument: Instrument,
) -> Decimal:
    """Convert a dollar risk budget into a lot size, rounded down to lot_step."""
    if stop_distance <= 0:
        raise ValueError("stop_distance must be positive")
    raw = risk_usd / (stop_distance * instrument.usd_per_point_per_lot)
    return floor_to_step(raw, instrument.lot_step)


def lots_to_risk(
    *,
    lots: Decimal,
    stop_distance: Decimal,
    instrument: Instrument,
) -> Decimal:
    """The actual dollars at risk for a given lot size."""
    return lots * stop_distance * instrument.usd_per_point_per_lot


def compute_floors(
    *,
    initial_balance: Decimal,
    prev_day_closing_balance: Decimal,
    hard_daily_pct: Decimal,
    hard_max_pct: Decimal,
    soft_daily_pct: Decimal,
    soft_max_pct: Decimal,
) -> Floors:
    """Equity floors.

    Daily floors are measured from the previous day's *closing balance* — FTMO's
    basis. Max-drawdown floors are measured from the *initial balance* and are
    static: they do not trail up as the account grows.
    """
    hundred = Decimal(100)
    return Floors(
        hard_daily=prev_day_closing_balance - (initial_balance * hard_daily_pct / hundred),
        hard_max=initial_balance * (hundred - hard_max_pct) / hundred,
        soft_daily=prev_day_closing_balance - (initial_balance * soft_daily_pct / hundred),
        soft_max=initial_balance * (hundred - soft_max_pct) / hundred,
    )
