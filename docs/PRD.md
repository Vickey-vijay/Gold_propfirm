# PRD — Prop Desk: Gold Signal & Rule-Guard System

**Version** 1.0 · **Date** 2026-09-21 · **Owner** Vickey · **Status** Approved for technical design

---

## 1. Problem

Prop firm accounts are lost to **rule breaches**, not bad analysis. A trader with a decent read on the market still fails because they size a position wrong, take one more trade after a loss, hold into a news blackout, or give back profit past a drawdown line. Humans cannot reliably enforce numeric constraints in real time under pressure.

Separately, discretionary traders have no systematic way to capture *why* they took a trade, so they cannot learn from outcomes at scale.

## 2. Goal

A server-hosted assistant that:

1. **Makes rule breach structurally impossible** — every signal is pre-validated against FTMO's exact limits before the user ever sees it.
2. **Produces high-quality intraday gold signals** with full reasoning, entry, stop, targets, size, and live management guidance.
3. **Learns from the user's actual results** by storing the complete decision context alongside the outcome.
4. **Can be validated before real money** via a live simulated account and a point-in-time replay engine.

**Target outcome:** 5% net per month on a $100,000 FTMO account, sustained, with zero rule violations.

## 3. Success criteria

| # | Criterion | Measure |
|---|---|---|
| S1 | Zero rule breaches | 0 violations across all sim and live trading. Non-negotiable. |
| S2 | Statistically validated edge | >= 300 scored decisions in replay with positive expectancy, 95% CI excluding zero |
| S3 | Signal quality | >= 1.5 average R:R on taken trades; win rate and expectancy tracked per regime |
| S4 | Monthly return | 5% median monthly return in sim over >= 3 simulated months |
| S5 | Latency | Signal delivered to Telegram within 60s of trigger condition |
| S6 | Availability | >= 99% uptime during 12:30–23:00 IST |

**Gate to real money:** S1 and S2 must both pass, plus one full simulated FTMO Phase 1 completed without violation.

## 4. Non-goals

- **No automated execution.** The system never places, modifies, or closes a trade. It signals; the user executes manually.
- **No broker or FTMO API integration.** Account state is entered manually by the user. This is deliberate — it keeps the system read-only and avoids any credential risk.
- **No multi-user / SaaS.** Single operator.
- **No copy trading, no martingale, no grid.** Explicitly out of scope.
- **No financial advice to third parties.** Personal tooling only.

## 5. Scope

**Instrument:** XAUUSD (spot gold) only in v1.
**Firm:** FTMO $100,000 Challenge → Verification → Funded.
**Hold time:** 2–3 hours typical. Hard rule: **no overnight positions.**
**Primary session:** NY / London-NY overlap, 18:30–23:00 IST. Telegram alerts outside this window are permitted but flagged lower priority.
**Expected frequency:** 1–2 signals per active day. The system is expected to return *no trade* most of the time.

## 6. Core concepts

**Context Packet** — a timestamped snapshot of everything knowable about the market at a moment: prices, volatility, structure, correlated assets, calendar, news. The single input to the AI.

**Judgment** — the AI's structured opinion given a Context Packet: direction, conviction, entry zone, invalidation, targets, reasoning, kill conditions. May be `NO_TRADE`.

**Signal** — a Judgment that has passed the Risk Engine, with a concrete position size attached. Only Signals reach the user.

**Account State** — the user-maintained truth: balance, equity, open positions, day's starting balance, phase, trading days used.

**Verdict** — the Risk Engine's ruling on a Judgment: `APPROVED`, `RESIZED`, or `BLOCKED` with reason.

## 7. Functional requirements

### 7.1 Context Engine (FR-CTX)

| ID | Requirement |
|---|---|
| CTX-1 | Poll XAUUSD OHLCV on M5, M15, H1, H4, D1. Maintain rolling windows. |
| CTX-2 | Compute ATR(14) per timeframe, realized volatility percentile vs trailing 60 days, and **% of average daily range already consumed** |
| CTX-3 | Identify market structure: swing highs/lows, current range, prior day high/low/close, prior week high/low, session high/low |
| CTX-4 | Track correlated assets: DXY, US 10Y yield, US 10Y real yield, SPX, VIX |
| CTX-5 | Maintain economic calendar with impact rating; expose countdown to next high-impact event |
| CTX-6 | Ingest news headlines with **publish timestamps**; tag relevance to gold |
| CTX-7 | Expose session state: active session, minutes to session close, minutes to no-overnight cutoff |
| CTX-8 | Every Context Packet is persisted immutably with its timestamp |

### 7.2 Judgment Engine (FR-AI)

| ID | Requirement |
|---|---|
| AI-1 | Accept a Context Packet and return a structured Judgment conforming to a fixed JSON schema |
| AI-2 | Must be able to return `NO_TRADE` and is expected to do so on most invocations |
| AI-3 | Must output: direction, conviction (0–100), entry zone, **invalidation level**, targets, prose reasoning, `what_would_change_my_mind`, and `kill_conditions` |
| AI-4 | **Must never output a position size or lot quantity.** Sizing is the Risk Engine's exclusive responsibility. |
| AI-5 | Receives the user's recent trade journal (last 20 trades with outcomes) as context |
| AI-6 | Receives retrieved similar historical setups from the user's own journal |
| AI-7 | All prompts, responses, token counts and costs are logged |

### 7.3 Risk & Rule Engine (FR-RISK)

| ID | Requirement |
|---|---|
| RISK-1 | Compute position size from account equity, stop distance and risk %. Deterministic, no AI. |
| RISK-2 | Reject any Judgment whose worst case would breach the **soft daily floor** (2% default) |
| RISK-3 | Reject any Judgment whose worst case would breach the **max drawdown floor** ($90,000 static) |
| RISK-4 | Reject entries inside a news blackout window (±5 min around high-impact events on funded accounts) |
| RISK-5 | Reject entries with insufficient time remaining before the no-overnight cutoff |
| RISK-6 | Enforce max concurrent positions, max trades per day, and a post-loss cooldown |
| RISK-7 | Enforce weekly circuit breaker (4% default) — halts all signals for the week |
| RISK-8 | **The Judgment Engine cannot override any Risk Engine verdict.** Architecturally enforced. |
| RISK-9 | All rules loaded from a config file — no hardcoded firm-specific values |
| RISK-10 | Every verdict is logged with the full input state that produced it |

### 7.4 Simulated Account (FR-SIM)

| ID | Requirement |
|---|---|
| SIM-1 | Full FTMO-mirroring paper account: balance, equity, open positions, floating P&L |
| SIM-2 | Daily reset at midnight CET, correctly handling CET/CEST transitions |
| SIM-3 | Live mark-to-market of open positions against real gold prices |
| SIM-4 | Tracks phase progress: profit target, trading days used, drawdown headroom |
| SIM-5 | Logs any rule violation that *would* have occurred, even if the Risk Engine blocked it |
| SIM-6 | Supports reset to a fresh account for a new test cycle |

### 7.5 Replay Engine (FR-REPLAY)

| ID | Requirement |
|---|---|
| RPL-1 | Reconstruct a Context Packet as of any historical timestamp T using **only data knowable at T** |
| RPL-2 | **Hard data cutoff enforcement** — candles with close > T, news with publish time > T, and any forward-looking series are excluded at the data layer, not the prompt layer |
| RPL-3 | Run the identical Judgment Engine and Risk Engine used in live mode |
| RPL-4 | Advance the clock and score each decision against subsequent price action |
| RPL-5 | Produce expectancy, win rate, average R, max drawdown and confidence intervals over the run |
| RPL-6 | **Leakage audit**: an automated test asserting no future data reaches the AI |
| RPL-7 | Support comparing prompt/config variants over the identical decision set |

### 7.6 Journal & Memory (FR-JRN)

| ID | Requirement |
|---|---|
| JRN-1 | User reports outcome per signal: taken/skipped, fill price, exit price, exit reason |
| JRN-2 | Each entry stores the full Context Packet, Judgment, Verdict and result |
| JRN-3 | Weekly AI-generated performance review identifying failure clusters |
| JRN-4 | Adaptive risk: reduce size after consecutive losses; lock out near circuit breakers |
| JRN-5 | Retrieval of similar past setups to feed future Judgments |

### 7.7 Interface (FR-UI)

| ID | Requirement |
|---|---|
| UI-1 | Telegram push on every approved Signal with all trade parameters and reasoning |
| UI-2 | Telegram inline actions: Taken / Skipped, and outcome reporting |
| UI-3 | Telegram alerts for live management events (kill condition triggered, approaching cutoff, news window) |
| UI-4 | Web dashboard: account state, open positions, drawdown headroom, signal history, journal, analytics |
| UI-5 | Manual account state entry form with validation |
| UI-6 | Rule config editor with a preview of the resulting limits |
| UI-7 | Replay run launcher and results viewer |

## 8. Key user journeys

**J1 — Receiving a signal.** Market loop builds a Context Packet → Judgment Engine returns a setup → Risk Engine sizes and approves → Telegram push with entry, stop, target, lots, reasoning, and kill conditions → user places the trade manually → taps *Taken* and enters fill price.

**J2 — Managing a live trade.** System marks to market each minute → detects a kill condition or an approaching news window or the no-overnight cutoff → pushes a management alert → user acts → reports the exit.

**J3 — Being protected.** A valid setup appears but the day is already down 1.8%. Risk Engine blocks it. Telegram receives a *blocked* notice explaining which limit was hit. Nothing else happens. This is the system working correctly.

**J4 — Validating.** User launches a replay over a date range → system generates several hundred point-in-time decisions → results report shows expectancy with confidence intervals → user compares two prompt variants over the same decisions.

## 9. Roadmap

| Phase | Deliverable | Gate |
|---|---|---|
| **P0** | Repo, config schema, FTMO rules encoded, Risk Engine + full unit test suite | Every rule test passes |
| **P1** | Context Engine + data feeds + persistence | Context Packets generating on schedule |
| **P2** | Judgment Engine + prompt v1 + structured output validation | Valid Judgments end to end |
| **P3** | Simulated account + Telegram bot + dashboard | J1 and J3 working live |
| **P4** | Replay engine + leakage audit | RPL-6 passing; first 300-decision run |
| **P5** | Tuning loop — iterate prompt and config against replay | S2 passes |
| **P6** | Live sim FTMO Phase 1 | S1 + S4 pass |
| **GO** | Purchase real FTMO Challenge | All gates green |

## 10. Assumptions & open items

- **A1** Risk per trade 0.5%, soft daily stop 2%, weekly circuit breaker 4%. *Pending user confirmation.*
- **A2** User will provision Anthropic API billing. *Pending user confirmation.*
- **A3** FTMO rules as researched 2026-09-21. **Must be re-verified against the live account dashboard before P6.**
- **A4** Data feed for gold, DXY and yields obtainable within budget — see technical design.
- **O1** Which news/headline source offers reliable historical publish timestamps for replay.
- **O2** Whether to widen the funded-account news blackout beyond ±2 min for manual-execution safety.
