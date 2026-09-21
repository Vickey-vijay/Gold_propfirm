# Prop Desk — project context

Gold signal and rule-guard system for an FTMO $100,000 account. Read `docs/PRD.md`
and `docs/TECHNICAL_DESIGN.md` before making changes.

## The one rule that governs the codebase

**The AI decides *what* to trade. The code decides *how much*.**

The Judgment Engine's output schema has no position-size field. The Risk Engine
never calls an LLM. Do not blur this boundary for convenience — it is the entire
safety argument of the system.

## Non-negotiables

- **No automated execution.** The system signals; the human places every trade.
  Never add broker or FTMO API integration, and never store trading credentials.
- **Risk Engine changes require tests.** Every rule needs a unit test at the exact
  boundary, and the hypothesis property test asserting no approved trade can cross
  a hard floor must keep passing. It is a release gate.
- **`as_of` everywhere.** Every data access takes an `as_of` timestamp and filters
  in SQL. Live mode passes `now()`. Never add a data path that bypasses it — that
  is how replay leakage gets baked in permanently.
- **Only closed candles.** A candle with `close_time > as_of` is excluded entirely,
  never partially revealed.
- **Calendar results are future data.** Scheduled event *times* are knowable ahead;
  their `actual` values are not. Keep `actual` null until `event_time <= as_of`.

## Layout

```
config/rules.ftmo-100k.yaml   firm rules — no firm-specific values in code
prompts/trader_persona_v*.md  versioned; every Judgment records prompt_version
docs/                         PRD, technical design, AI spec
src/propdesk/context/         L1 — deterministic market context
src/propdesk/judgment/        L2 — Claude Sonnet 5
src/propdesk/risk/            L3 — deterministic sizing and rule gate
src/propdesk/replay/          point-in-time replay + leakage audit
src/propdesk/sim/             simulated FTMO account
```

## Claude API conventions

- Model `claude-sonnet-5` (operator chose Sonnet for cost; revisit against replay in P5)
- `thinking={"type": "adaptive"}`
- Structured output via `output_config={"format": {...}}` — not the deprecated
  `output_format`
- Prompt caching on the persona prefix; the persona must contain no timestamps or
  prices, or the cache silently never hits
- No assistant prefill (400s on Sonnet 5 and the whole 4.6+ family)

## Trading conventions

- XAUUSD only. 1 lot = 100 oz; a $1.00 move = $100 per lot.
- Lot sizes always round **down**.
- No overnight positions, ever.
- FTMO daily loss is measured from the previous day's **closing balance** against
  current **equity including floating P&L**. Max drawdown is static at $90,000.
- Daily reset is midnight Europe/Prague — DST-aware, not a fixed UTC offset.

## Status

Planning complete, implementation not started. Current phase: **P0**.
Assumptions A1 (risk 0.5%/day 2%/week 4%) and A2 (Anthropic API billing) are
pending user confirmation. FTMO rules were researched 2026-09-21 and must be
re-verified against the live dashboard before any real-money phase.
