"""The Risk & Rule Engine.

Deterministic. Contains no LLM call and must never gain one. Given a Judgment,
an AccountState and a MarketSnapshot, it returns a Verdict: approved with a lot
size, resized down to fit, or blocked with reasons.

The safety promise of the system reduces to one invariant, asserted here and
proved by property test:

    For any tradeable Verdict, the account's worst-case equity — current equity
    minus risk already open, minus the risk of this new trade, minus a slippage
    buffer — never reaches the firm's hard floors.

Because the config validator guarantees the soft floors are strictly stricter
than the hard ones, sizing against the soft floor is sufficient to prove it.
"""

from __future__ import annotations

from decimal import Decimal

from propdesk.config import RuleConfig
from propdesk.models import (
    AccountState,
    BlockReason,
    Direction,
    Floors,
    Judgment,
    MarketSnapshot,
    Verdict,
    VerdictStatus,
)
from propdesk.risk import sizing as sz


def compute_floors(account: AccountState, cfg: RuleConfig) -> Floors:
    return sz.compute_floors(
        initial_balance=account.initial_balance,
        prev_day_closing_balance=account.prev_day_closing_balance,
        hard_daily_pct=cfg.hard_limits.daily_loss_pct,
        hard_max_pct=cfg.hard_limits.max_loss_pct,
        soft_daily_pct=cfg.soft_limits.daily_stop_pct,
        soft_max_pct=cfg.soft_limits.max_drawdown_pct,
    )


def open_risk(account: AccountState, market: MarketSnapshot, cfg: RuleConfig) -> Decimal:
    """Total money still at risk across open positions if they all stop out."""
    return sum(
        (p.risk_usd(market.price, cfg.instrument.usd_per_point_per_lot) for p in account.open_positions),
        Decimal(0),
    )


def _in_news_blackout(market: MarketSnapshot, account: AccountState, cfg: RuleConfig) -> bool:
    if not cfg.news.blackout_enabled(account.phase):  # type: ignore[arg-type]
        return False
    before = market.minutes_to_next_high_impact
    after = market.minutes_since_last_high_impact
    if before is not None and before <= cfg.news.blackout_minutes_before:
        return True
    if after is not None and after <= cfg.news.blackout_minutes_after:
        return True
    return False


def evaluate(
    judgment: Judgment,
    account: AccountState,
    market: MarketSnapshot,
    cfg: RuleConfig,
) -> Verdict:
    """Rule on a Judgment. The only entry point callers should use."""

    if judgment.decision is Direction.NO_TRADE:
        return Verdict(status=VerdictStatus.BLOCKED, blocked_reasons=[BlockReason.NO_TRADE])

    reasons: list[BlockReason] = []
    notes: list[str] = []

    # --- setup quality -----------------------------------------------------
    if judgment.conviction < cfg.signal_policy.min_conviction:
        reasons.append(BlockReason.LOW_CONVICTION)

    entry = judgment.entry_reference
    assert judgment.invalidation is not None
    distance = sz.stop_distance(
        entry=entry,
        invalidation=judgment.invalidation,
        atr_m15=market.atr_m15,
        sizing=cfg.position_sizing,
    )
    if distance > cfg.position_sizing.max_stop_usd:
        reasons.append(BlockReason.STOP_TOO_WIDE)
        notes.append(f"stop {distance} exceeds max {cfg.position_sizing.max_stop_usd}")

    reward = abs(judgment.targets[0].price - entry)
    rr = reward / distance if distance > 0 else Decimal(0)
    if rr < cfg.signal_policy.min_rr:
        reasons.append(BlockReason.POOR_RR)
        notes.append(f"R:R {rr:.2f} below minimum {cfg.signal_policy.min_rr}")

    # --- timing ------------------------------------------------------------
    hold = judgment.expected_hold_minutes or cfg.session.expected_hold_minutes
    if market.minutes_to_cutoff < hold + cfg.session.min_minutes_before_cutoff:
        reasons.append(BlockReason.INSUFFICIENT_TIME)
        notes.append(f"{market.minutes_to_cutoff}min to cutoff, need {hold + cfg.session.min_minutes_before_cutoff}")

    if _in_news_blackout(market, account, cfg):
        reasons.append(BlockReason.NEWS_BLACKOUT)

    # --- behavioural limits ------------------------------------------------
    if account.trades_today >= cfg.soft_limits.max_trades_per_day:
        reasons.append(BlockReason.MAX_TRADES)
    if len(account.open_positions) >= cfg.soft_limits.max_concurrent_positions:
        reasons.append(BlockReason.MAX_CONCURRENT)
    if account.last_loss_at is not None:
        elapsed = (market.as_of - account.last_loss_at).total_seconds() / 60
        if elapsed < cfg.soft_limits.cooldown_after_loss_minutes:
            reasons.append(BlockReason.COOLDOWN)
            notes.append(f"{elapsed:.0f}min since last loss, cooldown {cfg.soft_limits.cooldown_after_loss_minutes}min")

    weekly_limit = account.initial_balance * cfg.soft_limits.weekly_circuit_breaker_pct / Decimal(100)
    if account.week_pnl <= -weekly_limit:
        reasons.append(BlockReason.WEEKLY_BREAKER)

    multiplier = cfg.soft_limits.size_multiplier(account.consecutive_losses)
    if multiplier <= 0:
        reasons.append(BlockReason.LOSS_STREAK)

    # --- drawdown headroom and sizing --------------------------------------
    floors = compute_floors(account, cfg)
    already_at_risk = open_risk(account, market, cfg)
    buffer = cfg.soft_limits.slippage_buffer_usd

    headroom = account.equity - already_at_risk - buffer - floors.binding_soft
    desired = account.equity * cfg.soft_limits.risk_per_trade_pct / Decimal(100) * multiplier

    if headroom <= 0:
        reasons.append(BlockReason.DRAWDOWN_HEADROOM)
        notes.append(f"no headroom: equity {account.equity} vs floor {floors.binding_soft}")

    if reasons:
        return Verdict(
            status=VerdictStatus.BLOCKED,
            stop_distance=distance,
            blocked_reasons=reasons,
            notes=notes,
        )

    budget = min(desired, headroom)
    lots = sz.risk_to_lots(risk_usd=budget, stop_distance=distance, instrument=cfg.instrument)

    if lots < cfg.instrument.min_lot:
        return Verdict(
            status=VerdictStatus.BLOCKED,
            stop_distance=distance,
            blocked_reasons=[BlockReason.BELOW_MIN_LOT],
            notes=[f"budget {budget:.2f} sizes to {lots} lots, below minimum {cfg.instrument.min_lot}"],
        )

    actual_risk = sz.lots_to_risk(lots=lots, stop_distance=distance, instrument=cfg.instrument)
    stop = sz.stop_price(entry=entry, distance=distance, direction=judgment.decision)

    # --- the invariant -----------------------------------------------------
    # Fail closed. If this ever trips, the config invariant broke and the
    # engine must not hand out a trade, however valid it looked.
    worst_case = account.equity - already_at_risk - actual_risk - buffer
    if worst_case <= floors.binding_hard:
        return Verdict(
            status=VerdictStatus.BLOCKED,
            stop_distance=distance,
            blocked_reasons=[BlockReason.DRAWDOWN_HEADROOM],
            notes=[
                "ENGINE INVARIANT VIOLATION — worst case "
                f"{worst_case} would reach hard floor {floors.binding_hard}. "
                "This is a bug, not a market condition. Alert and investigate."
            ],
        )

    status = VerdictStatus.RESIZED if headroom < desired else VerdictStatus.APPROVED
    if status is VerdictStatus.RESIZED:
        notes.append(f"resized: headroom {headroom:.2f} below desired risk {desired:.2f}")

    return Verdict(
        status=status,
        lots=lots,
        risk_usd=actual_risk,
        stop_distance=distance,
        entry_price=entry,
        stop_price=stop,
        notes=notes,
    )
