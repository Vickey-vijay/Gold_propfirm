"""MT5 account auto-sync.

Reads balance, equity, and open positions from a live MT5 account via a
strictly READ-ONLY investor-password connection. Never touches a master
trading password, never places, modifies, or closes anything — the investor
password is incapable of trade execution by MT5's own design, not merely by
our code's good behavior.

Deliberately scoped: only fields MetaApi can give us unambiguously (balance,
equity, positions) are auto-synced. FTMO-specific bookkeeping that needs
trade-history reconstruction (trades_today, consecutive_losses, week_pnl,
last_loss_at) stays manual rather than being approximated — a wrong guess
there would corrupt the one number (the drawdown floor) this whole system
exists to protect. prev_day_closing_balance is handled correctly via a
dedicated daily rollover job, not guessed from live sync data.
"""
