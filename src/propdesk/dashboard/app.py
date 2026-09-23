"""Account dashboard — FastAPI app.

Lets the operator enter account state by hand (balance, equity, phase, open
positions) and see, computed live from the SAME deterministic Risk Engine
code that gates real signals, exactly how much headroom is left before the
firm's daily and max-drawdown floors.

No AI, no market feed, no broker connection. Just the one thing the operator
promised to keep updated manually, plus the math that actually matters.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from propdesk.config import DEFAULT_RULES_PATH, load_rules
from propdesk.models import AccountState, Direction, OpenPosition
from propdesk.risk.sizing import compute_floors
from propdesk.storage import AccountStore

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("PROPDESK_DB", BASE_DIR.parents[2] / "data" / "propdesk.db"))
RULES_PATH = Path(os.environ.get("PROPDESK_RULES", DEFAULT_RULES_PATH))

app = FastAPI(title="Prop Desk — Account Dashboard")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
store = AccountStore(DB_PATH)
cfg = load_rules(RULES_PATH)

DEFAULT_STATE = AccountState(
    phase="challenge",
    initial_balance=cfg.meta.account_size,
    balance=cfg.meta.account_size,
    equity=cfg.meta.account_size,
    prev_day_closing_balance=cfg.meta.account_size,
)


def current_state() -> AccountState:
    return store.latest() or DEFAULT_STATE


def floors_view(account: AccountState, current_price: Decimal | None = None) -> dict:
    """Everything the template needs to render the floors panel.

    `current_price` is the live XAUUSD price, typed in by hand — there is no
    market feed yet (that's P1). Without it we fall back to each position's
    own entry price, i.e. "assume no move since entry": not the true floating
    risk, but a real number rather than a wrong one. Passing account EQUITY
    here instead of a price was a real bug caught before this ever ran.
    """
    floors = compute_floors(
        initial_balance=account.initial_balance,
        prev_day_closing_balance=account.prev_day_closing_balance,
        hard_daily_pct=cfg.hard_limits.daily_loss_pct,
        hard_max_pct=cfg.hard_limits.max_loss_pct,
        soft_daily_pct=cfg.soft_limits.daily_stop_pct,
        soft_max_pct=cfg.soft_limits.max_drawdown_pct,
    )
    open_risk = sum(
        (
            p.risk_usd(current_price if current_price is not None else p.entry_price, cfg.instrument.usd_per_point_per_lot)
            for p in account.open_positions
        ),
        Decimal(0),
    )
    headroom = account.equity - open_risk - cfg.soft_limits.slippage_buffer_usd - floors.binding_soft
    day_pnl = account.equity - account.prev_day_closing_balance
    total_pnl = account.equity - account.initial_balance
    day_pnl_pct = (day_pnl / account.initial_balance * 100) if account.initial_balance else Decimal(0)
    total_pnl_pct = (total_pnl / account.initial_balance * 100) if account.initial_balance else Decimal(0)

    phase_rules = cfg.phases.for_phase(account.phase) if account.phase in ("challenge", "verification", "funded") else None
    target_pct = phase_rules.profit_target_pct if phase_rules else None
    target_progress = (total_pnl_pct / target_pct * 100) if target_pct else None

    def status(headroom_value: Decimal) -> str:
        if headroom_value <= 0:
            return "breached"
        if headroom_value < account.initial_balance * Decimal("0.01"):
            return "warning"
        return "healthy"

    return {
        "hard_daily": floors.hard_daily,
        "hard_max": floors.hard_max,
        "soft_daily": floors.soft_daily,
        "soft_max": floors.soft_max,
        "binding_soft": floors.binding_soft,
        "binding_hard": floors.binding_hard,
        "open_risk": open_risk,
        "open_risk_is_estimated": current_price is None and len(account.open_positions) > 0,
        "headroom": headroom,
        "headroom_status": status(headroom),
        "day_pnl": day_pnl,
        "day_pnl_pct": day_pnl_pct,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "target_pct": target_pct,
        "target_progress": target_progress,
        "risk_per_trade_pct": cfg.soft_limits.risk_per_trade_pct,
        "size_multiplier": cfg.soft_limits.size_multiplier(account.consecutive_losses),
    }


def _optional_dec(raw: str | None) -> Decimal | None:
    if raw is None or raw.strip() == "":
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, current_price: str | None = None):
    account = current_state()
    price = _optional_dec(current_price)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "account": account,
            "floors": floors_view(account, price),
            "history": store.history(limit=15),
            "firm": cfg.meta.firm,
            "instrument": cfg.instrument.symbol,
            "verified": cfg.meta.verified_against_dashboard,
            "current_price": current_price or "",
        },
    )


def _dec(raw: str | None, fallback: Decimal = Decimal(0)) -> Decimal:
    if raw is None or raw.strip() == "":
        return fallback
    try:
        return Decimal(raw)
    except InvalidOperation:
        return fallback


@app.post("/api/state")
def update_state(
    phase: str = Form(...),
    initial_balance: str = Form(...),
    balance: str = Form(...),
    equity: str = Form(...),
    prev_day_closing_balance: str = Form(...),
    trading_days_used: int = Form(0),
    trades_today: int = Form(0),
    consecutive_losses: int = Form(0),
    week_pnl: str = Form("0"),
    had_loss_today: str | None = Form(None),
):
    prior = current_state()
    account = AccountState(
        phase=phase,
        initial_balance=_dec(initial_balance),
        balance=_dec(balance),
        equity=_dec(equity),
        prev_day_closing_balance=_dec(prev_day_closing_balance),
        trading_days_used=trading_days_used,
        trades_today=trades_today,
        consecutive_losses=consecutive_losses,
        week_pnl=_dec(week_pnl),
        last_loss_at=datetime.now(timezone.utc) if had_loss_today else prior.last_loss_at,
        open_positions=prior.open_positions,
    )
    store.save(account)
    return RedirectResponse("/", status_code=303)


@app.post("/api/positions")
def add_position(
    direction: str = Form(...),
    entry_price: str = Form(...),
    stop_price: str = Form(...),
    lots: str = Form(...),
):
    account = current_state()
    position = OpenPosition(
        direction=Direction(direction),
        entry_price=_dec(entry_price),
        stop_price=_dec(stop_price),
        lots=_dec(lots),
        opened_at=datetime.now(timezone.utc),
    )
    updated = account.model_copy(update={"open_positions": [*account.open_positions, position]})
    store.save(updated)
    return RedirectResponse("/", status_code=303)


@app.post("/api/positions/{index}/close")
def close_position(index: int):
    account = current_state()
    remaining = [p for i, p in enumerate(account.open_positions) if i != index]
    updated = account.model_copy(update={"open_positions": remaining})
    store.save(updated)
    return RedirectResponse("/", status_code=303)


@app.get("/health")
def health():
    return {"status": "ok", "firm": cfg.meta.firm, "instrument": cfg.instrument.symbol}
