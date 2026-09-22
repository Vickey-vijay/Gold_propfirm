# Technical Design — Prop Desk

**Version** 1.0 · **Date** 2026-09-21 · **Companion to** `PRD.md`

---

## 1. Architectural principle

> **The AI decides *what* to trade. The code decides *how much*. Neither can do the other's job.**

This is a hard boundary, enforced by types and tested. The Judgment Engine's output schema has no field for position size. The Risk Engine never calls an LLM. A bug or hallucination in the AI layer can produce a bad *opinion*; it cannot produce a rule breach.

## 2. System architecture

```
                        ┌──────────────────────────────┐
   Market data ────────▶│      L1  CONTEXT ENGINE      │
   Macro series ───────▶│   (deterministic, no AI)     │
   Calendar / News ────▶│                              │
                        │   → Context Packet (JSON)    │
                        └───────────────┬──────────────┘
                                        │
                              ┌─────────▼─────────┐
                              │   TRIGGER GATE    │  cheap deterministic filter
                              │  (worth asking?)  │  — controls API spend
                              └─────────┬─────────┘
                                        │ yes
                        ┌───────────────▼──────────────┐
                        │     L2  JUDGMENT ENGINE      │
                        │      Claude Sonnet 5         │
                        │   → Judgment (no sizing)     │
                        └───────────────┬──────────────┘
                                        │
                        ┌───────────────▼──────────────┐
                        │   L3  RISK & RULE ENGINE     │   ◀── rules.yaml
                        │  (deterministic, no AI)      │   ◀── Account State
                        │  sizing · limits · verdict   │
                        └───────────────┬──────────────┘
                                        │ APPROVED / RESIZED
                        ┌───────────────▼──────────────┐
                        │      SIGNAL DISPATCHER        │
                        └───┬───────────────────────┬──┘
                            │                       │
                    ┌───────▼──────┐        ┌───────▼──────┐
                    │  Telegram    │        │  Dashboard   │
                    └───────┬──────┘        └──────────────┘
                            │ outcome
                    ┌───────▼──────────────────────────────┐
                    │        L4  JOURNAL & MEMORY          │
                    │  context + judgment + verdict + P&L  │
                    └──────────────────────────────────────┘

   REPLAY MODE: same L1→L2→L3 path, but L1 is fed by ReplayDataProvider
                with a hard as_of cutoff. L3 runs against a simulated account.
```

## 3. Stack

| Concern | Choice | Rationale |
|---|---|---|
| Language | Python 3.11+ | Ecosystem for market data, pandas, TA; matches existing projects |
| API / web | FastAPI + Uvicorn | Async, typed, auto docs |
| Scheduling | APScheduler | In-process, no extra infra |
| DB | SQLite (WAL) → Postgres if needed | Single user; Postgres is premature |
| Validation | Pydantic v2 | Schema enforcement at every layer boundary — load-bearing, not cosmetic |
| LLM | `anthropic` SDK | Official SDK |
| Dashboard | Jinja2 + HTMX + Chart.js | No build step, no SPA overhead |
| Bot | `python-telegram-bot` v21 | Mature, inline keyboards |
| Tests | pytest + hypothesis | Property-based testing for the Risk Engine |
| Deploy | Docker Compose + Caddy | Existing GCP box, automatic TLS |

## 4. Data sources

| Data | Primary | Fallback | Cost |
|---|---|---|---|
| XAUUSD OHLCV (M5–D1) | Twelve Data | Polygon.io | Free tier → $29/mo if rate-limited |
| DXY, SPX, VIX | Twelve Data | Yahoo via `yfinance` | included |
| US 10Y nominal + real yield | FRED API (`DGS10`, `DFII10`) | — | free |
| Economic calendar | Trading Economics / ForexFactory scrape | manual CSV | free–$ |
| News headlines w/ timestamps | **Open item O1** — needs reliable historical publish times for replay | — | TBD |

**Critical requirement for replay:** every source must expose a *publish or close timestamp*, and historical queries must be reproducible. A source that only gives "latest" is usable live but useless for replay. This is the main constraint on source selection.

## 5. Data model

```sql
-- Immutable market snapshots
context_packet(id, ts_utc, symbol, payload_json, source_hash)

-- AI output, 1:1 with a packet
judgment(id, packet_id, model, prompt_version, direction, conviction,
         entry_low, entry_high, invalidation, targets_json,
         reasoning, change_my_mind, kill_conditions_json,
         input_tokens, output_tokens, cost_usd, latency_ms, created_at)

-- Risk engine ruling, 1:1 with a judgment
verdict(id, judgment_id, status, lots, risk_usd, stop_distance,
        blocked_reasons_json, account_snapshot_json, created_at)

-- What the user actually did
trade(id, verdict_id, taken, entry_price, entry_time, exit_price, exit_time,
      exit_reason, gross_pnl, r_multiple, notes)

-- User-maintained truth
account_state(id, ts, phase, balance, equity, day_start_balance,
              trading_days_used, open_positions_json)

-- Firm rule config, versioned
rule_config(id, name, version, yaml, active, created_at)

-- Replay
replay_run(id, config_json, prompt_version, date_from, date_to,
           decisions, expectancy, win_rate, avg_r, max_dd, ci_low, ci_high)
replay_decision(id, run_id, as_of, judgment_json, verdict_json, outcome_json)
```

## 6. Risk Engine — the core algorithms

All values from `rules.yaml`. No magic numbers in code.

### 6.1 Contract math (XAUUSD)

```
1 standard lot = 100 troy oz
$1.00 price move = $100 P&L per lot

lots_raw = risk_usd / (stop_distance_usd * 100)
lots      = floor(lots_raw, 0.01)        # ALWAYS round DOWN — never exceed risk
```

Example: $100,000 equity, 0.5% risk = $500. ATR-derived stop of $5.00.
`lots = 500 / (5.00 * 100) = 1.00 lot`

### 6.2 Stop placement

```
stop_distance = max(atr_multiple * ATR(14, M15), min_stop_usd)
```
Defaults: `atr_multiple = 1.5`, `min_stop_usd = 2.50` (prevents noise-width stops in low volatility).

The AI supplies an *invalidation level*; the Risk Engine takes the wider of the AI's level and the ATR floor. The AI can make a stop wider, never tighter than the volatility floor.

### 6.3 The floors

```
# FTMO hard limits (never touched, exist only as assertions)
hard_daily_floor = prev_day_closing_balance - 0.05 * initial_balance
hard_max_floor   = 0.90 * initial_balance            # $90,000, static

# Our operating limits (what actually gates trades)
soft_daily_floor = prev_day_closing_balance - 0.02 * initial_balance
soft_max_floor   = 0.94 * initial_balance            # $94,000
```

**Note the FTMO subtlety**: the daily loss is measured from the *previous day's closing balance* but compared against *current equity including floating P&L*. Both are modelled.

### 6.4 Pre-trade gate

```python
open_risk = sum(max(0, dist_to_stop(p)) * 100 * p.lots for p in open_positions)
worst_case_equity = equity - open_risk - new_risk_usd - slippage_buffer

floor = max(soft_daily_floor, soft_max_floor)

if worst_case_equity <= floor:          -> BLOCKED (drawdown)
if in_news_blackout(now):               -> BLOCKED (news window)
if minutes_to_cutoff < hold + buffer:   -> BLOCKED (insufficient time)
if trades_today >= max_trades_per_day:  -> BLOCKED (frequency)
if open_positions >= max_concurrent:    -> BLOCKED (concurrency)
if in_cooldown_after_loss():            -> BLOCKED (cooldown)
if week_pnl <= -weekly_breaker:         -> BLOCKED (circuit breaker)
```

If the gate passes but the requested size would breach, the engine **resizes down** and returns `RESIZED` rather than blocking — a smaller valid trade beats no trade. If the resized lot falls below `0.01`, it becomes `BLOCKED`.

### 6.5 Testing the Risk Engine

This layer carries the whole safety promise, so it gets disproportionate test effort:

- **Unit tests** for every rule, including exact-boundary cases (equity precisely at the floor)
- **Property-based tests** (hypothesis): for any randomly generated account state and judgment, the approved trade's worst case never crosses a hard floor. This is the single most important test in the codebase.
- **Scenario replays**: hand-built sequences reproducing known prop-failure patterns (revenge trading, averaging down, holding through news) asserting the engine blocks them
- **CET/CEST transition tests** for the daily reset

## 7. Judgment Engine

**Model:** `claude-sonnet-5` — chosen by the operator for cost. At $2/$10 per MTok versus Opus 5's $5/$25, this cuts the running bill roughly 60%.

This is the reasoning-critical path, so the tradeoff is real and we do not have to guess at it: the replay engine exists precisely to measure model choice. Run the same frozen decision set through Sonnet 5 and Opus 5 in P5 and compare expectancy with confidence intervals. If Opus earns its price difference, the model ID is a one-line change. Adaptive thinking is on either way.

```python
resp = client.messages.create(
    model="claude-sonnet-5",
    max_tokens=8000,
    thinking={"type": "adaptive"},
    output_config={
        "effort": "high",
        "format": {"type": "json_schema", "schema": JUDGMENT_SCHEMA},
    },
    system=[
        {"type": "text", "text": TRADER_PERSONA,
         "cache_control": {"type": "ephemeral"}},   # stable prefix — cached
    ],
    messages=[{"role": "user", "content": context_packet_json}],  # volatile — after cache
)
```

Notes:
- **Structured output** via `output_config.format` (not the deprecated `output_format`). Guarantees a parseable Judgment.
- **Prompt caching** on the persona/system prefix. Cache is prefix-matched, so all volatile content (timestamps, prices) must come *after* the breakpoint. Verify with `usage.cache_read_input_tokens` — if it stays zero, something is invalidating the prefix.
- **No assistant prefill** — rejected by Sonnet 5.
- `JUDGMENT_SCHEMA` has **no size/lot field**. The boundary is enforced by schema, not convention.

See `AI_JUDGMENT_SPEC.md` for the persona, context packet schema and output schema.

### 7.1 Trigger Gate — controlling spend

Calling the AI on a fixed 15-minute timer during a 4.5-hour session is ~18 calls/day. A cheap deterministic pre-filter cuts this to the moments that matter:

- price approaching a mapped key level (within 0.5 × ATR)
- volatility regime change or range expansion
- structure break on M15
- session open / first pullback
- N minutes after a high-impact release

Target: **4–6 AI calls per active day** instead of 18. Same coverage of real opportunities, ~70% lower cost.

## 8. Replay Engine — and why it is trustworthy

The entire value of replay depends on one guarantee: **the AI never sees the future.** Prompt-level instructions are not sufficient. The cutoff is enforced at the data access layer.

```python
class DataProvider(Protocol):
    def candles(self, symbol, tf, n, *, as_of: datetime) -> list[Candle]: ...
    def news(self, *, since, as_of: datetime) -> list[Headline]: ...
    def series(self, code, *, as_of: datetime) -> Series: ...
```

`LiveDataProvider` sets `as_of = now()`. `ReplayDataProvider` sets it to the simulated timestamp and filters **in the SQL query**:

```sql
SELECT * FROM candles  WHERE close_time  <= :as_of ORDER BY close_time DESC LIMIT :n
SELECT * FROM news     WHERE published_at <= :as_of
```

**Four rules that prevent subtle leakage:**

1. **Only closed candles.** A candle whose `close_time > as_of` is excluded entirely — never partially revealed. Partial-bar leakage is the most common and most invisible backtest bug.
2. **Calendar schedule vs. calendar result.** Scheduled events *after* `as_of` are visible (their timing was genuinely knowable). Their `actual` values are `NULL` until `event_time <= as_of`. Getting this backwards leaks the CPI print.
3. **Journal retrieval** only returns trades closed before `as_of`.
4. **Revised series** (macro data that gets restated) use vintage-aware lookups where available, or are excluded.

**Leakage audit (RPL-6)**: an automated test injects sentinel values into every future-dated record and asserts none appear in the serialized Context Packet. This test runs in CI and is a release gate.

### 8.1 Scoring

For a decision at T with entry, stop and targets, walk forward on M1:

- Did price touch the entry zone within `entry_valid_minutes`? If not → `NO_FILL`.
- From fill, which came first: stop or target? **If both land inside the same M1 bar, assume the stop hit first** — conservative and avoids flattering ambiguous bars.
- If neither is hit by `max_hold` (3h) or the no-overnight cutoff → exit at market.

`R = (exit − entry) / |entry − stop|`, signed by direction.

Report: expectancy in R, win rate, average win/loss, max consecutive losses, simulated equity curve max drawdown, and a **bootstrap 95% confidence interval on expectancy**. The CI is the headline number — a positive mean with a CI spanning zero is not evidence of an edge.

### 8.2 Variant comparison

Replay runs are keyed by `(prompt_version, config_version, decision_set)`. Two variants must be scored over the **identical decision timestamps** to be comparable. Judgments are cached by `(packet_hash, prompt_version)` so re-running an unchanged variant costs nothing.

## 9. Market loop

```
every 1 min:   refresh prices, mark open positions to market,
               evaluate kill conditions, check cutoff/news proximity
every 5 min:   rebuild Context Packet, run Trigger Gate
on trigger:    Judgment → Risk verdict → dispatch or log block
at CET midnight: roll the trading day, snapshot closing balance,
               recompute daily floor
```

## 10. Cost

At Sonnet 5 pricing ($2/MTok input, $10/MTok output):

| Item | Volume | Sonnet 5 | Opus 5 for comparison |
|---|---|---|---|
| One judgment call | ~12K input (cached prefix ~3K) + ~2K output | **≈ $0.044** | ≈ $0.11 |
| Live trading | 5 calls/day × 22 days | **≈ $5/month** | ≈ $12/month |
| Weekly review | 4/month, larger context | ≈ $1/month | ≈ $2/month |
| Full replay run | 300 decisions | **≈ $13 per run** | ≈ $33 per run |
| Data feed | Twelve Data | $0–29/month | — |
| Server | existing GCP box | $0 | — |

**Running total: roughly $6–35/month live.** Replay tuning is the variable cost — budget for several runs during P5. Judgments are cached by `(packet_hash, prompt_version)`, so re-running an unchanged variant costs nothing.

Two levers if cost still matters, in order of preference: the Trigger Gate (fewer, better-chosen calls) before `output_config.effort` (less thinking per call). Both are measurable against replay.

## 11. Deployment

**Target host surveyed 2026-09-21, swap confirmed 2026-09-23** — `35.226.195.159`, and it is not an empty box:

| | |
|---|---|
| OS / Python | Debian 12, Python 3.11.2 (meets our `>=3.11`) |
| RAM | **969 MB total, ~445 MB available** |
| Swap | **2 GB swapfile at `/swapfile`, active, persisted in `/etc/fstab`** — present on the box already; the 2026-09-21 survey missed it because `swapon` lives in `/sbin`, off the non-interactive SSH `PATH` |
| Disk | 30 GB, 20 GB free |
| Docker | **not installed** |
| Already running | nginx on `:80`; two Node apps on `:3001`/`:3002` under PM2 (The Cabins UAE site + admin); a Streamlit app on `:8501`; a cloudflared tunnel; GCP agents (~85 MB) |

**This invalidates the Docker Compose + Caddy plan from v1.0.** On a 1 GB box that
is already serving a live client site, the Docker daemon's overhead is not
affordable, and Caddy would collide with nginx on `:80`. Revised plan:

```
/opt/propdesk/
  .venv/                       virtualenv, no container
  app/                         deployed source
  config/rules.ftmo-100k.yaml
  data/propdesk.db             SQLite (WAL)
  .env                         chmod 600, never committed
/etc/systemd/system/propdesk.service
/etc/nginx/sites-available/propdesk   -> proxy to 127.0.0.1:8000
```

- **systemd** unit running uvicorn bound to `127.0.0.1:8000`, `Restart=always`
- **nginx** (already present) proxies a vhost or `/propdesk` location to it, with
  basic auth on the dashboard. Do not touch the existing server blocks.
- **Memory budget:** FastAPI + uvicorn + pydantic lands around 60–80 MB. Adding
  pandas pushes it to 150–200 MB. Prefer plain Python and `statistics` for the
  indicator math in L1 and only reach for pandas if profiling justifies it.
- `MemoryMax=300M` on the systemd unit so this app is the one that dies under
  pressure, never the client site.
- Nightly `sqlite3 .backup` to GCS; `/health` endpoint; APScheduler job failures
  alert to Telegram.

**Open decision:** this host is shared with production client work. A dedicated
`e2-small` (2 GB) is roughly $13/month and removes the contention entirely. Worth
considering before P3 rather than after an incident.

## 12. Security

- **The system holds no broker or FTMO credentials.** By design — it cannot trade even if fully compromised.
- Dashboard behind basic auth + TLS; Telegram bot locked to a single authorized chat ID
- API keys server-side only, never rendered to the dashboard
- SQLite file permissions restricted; backups encrypted at rest in GCS

## 13. Build order

| Phase | Work |
|---|---|
| **P0** | Repo scaffold, Pydantic models, `rules.yaml` schema, FTMO encoding, Risk Engine + property tests |
| **P1** | Data providers behind the `as_of` interface (replay-ready from day one), Context Engine, persistence |
| **P2** | Judgment Engine, persona prompt v1, structured output, cost logging |
| **P3** | Simulated account, Telegram bot, dashboard |
| **P4** | Replay engine, leakage audit, scoring, first 300-decision run |
| **P5** | Tuning loop against replay |
| **P6** | Live sim FTMO Phase 1 |

**Design note on P1:** the `as_of` parameter goes into the data interface from the very first commit, even though live mode always passes `now()`. Retrofitting a cutoff into a codebase that assumes "latest" is how leakage bugs get baked in permanently.
