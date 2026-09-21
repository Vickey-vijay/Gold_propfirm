# AI Judgment Engine — Specification

**Version** 1.0 · **Date** 2026-09-21 · **Model** `claude-sonnet-5`

---

## 1. Design intent

The operator's explicit requirement: **not a pattern-matcher reciting concepts.** Named setups — order blocks, fair value gaps, engulfing patterns — are descriptions of what happened, not explanations of why. A trader with twenty years on the desk does not think *"that's a bullish engulfing."* They think *"the sellers who pushed it down there just got run over, and there's nobody left below."*

The persona must reason about **cause**, not vocabulary:

- **Who is positioned, and how badly?** Where is the pain? Who has to get out, and where?
- **What is actually moving gold today?** Real yields? Dollar? A risk event? Or nothing — is this just noise in a range?
- **Is there a reason for this move, or is it drift?** Moves without a driver tend to revert.
- **What is the path of least resistance,** and what would have to happen to invalidate that read?
- **Is this worth risking money on right now,** or is the honest answer "nothing here"?

Named patterns may appear in the *reasoning* as shorthand, but they must never be the *justification*. "Bullish engulfing on M15" is not a reason. "Sellers stepped in at 2650 twice, failed both times, and now CPI came in soft — the people short from 2650 are trapped and the level above is thin" is a reason.

## 2. The no-trade default

The system's most common correct output is `NO_TRADE`. This must be stated forcefully in the persona and reinforced in scoring, because an LLM asked "what's the trade here" will almost always find one. An experienced trader's edge is substantially *selectivity*.

Target distribution across triggered invocations:

| Outcome | Target share |
|---|---|
| `NO_TRADE` | 70–85% |
| Trade with conviction 60–75 | 10–20% |
| Trade with conviction 76+ | 5–10% |

If replay shows the AI producing trades on >40% of invocations, the persona is too eager and must be tightened before anything else is tuned.

## 3. Context Packet schema

The single input. Everything the AI knows.

```jsonc
{
  "as_of": "2026-09-21T18:45:00Z",
  "symbol": "XAUUSD",
  "price": { "bid": 2648.30, "ask": 2648.60, "spread": 0.30 },

  "candles": {
    "M5":  [ /* last 60  OHLCV, closed only */ ],
    "M15": [ /* last 60  */ ],
    "H1":  [ /* last 48  */ ],
    "H4":  [ /* last 30  */ ],
    "D1":  [ /* last 30  */ ]
  },

  "volatility": {
    "atr_m15": 3.20, "atr_h1": 7.80, "atr_d1": 24.50,
    "realized_vol_percentile_60d": 42,
    "adr_20": 26.10,
    "adr_consumed_pct": 58            // how much of today's usual range is gone
  },

  "structure": {
    "swing_highs": [2661.40, 2655.10],
    "swing_lows":  [2639.80, 2644.20],
    "prior_day":  { "high": 2659.00, "low": 2641.20, "close": 2652.10 },
    "prior_week": { "high": 2672.50, "low": 2631.00 },
    "session":    { "high": 2654.80, "low": 2645.90 },
    "key_levels": [ { "price": 2650.00, "touches": 4, "last_test": "2026-09-21T17:10:00Z" } ]
  },

  "macro": {
    "dxy":        { "last": 104.22, "chg_pct": -0.31 },
    "us10y":      { "last": 4.118,  "chg_bp": -4.2 },
    "us10y_real": { "last": 1.842,  "chg_bp": -5.1 },
    "spx":        { "last": 5810.2, "chg_pct": 0.44 },
    "vix":        { "last": 14.8,   "chg_pct": -2.1 }
  },

  "calendar": {
    "next_high_impact": {
      "event": "FOMC Rate Decision",
      "time": "2026-09-22T18:00:00Z",
      "minutes_away": 1395,
      "actual": null                  // null until the event has passed
    },
    "released_today": [
      { "event": "US CPI m/m", "actual": 0.2, "forecast": 0.3, "previous": 0.4,
        "time": "2026-09-21T12:30:00Z" }
    ]
  },

  "news": [
    { "published_at": "2026-09-21T18:02:00Z",
      "headline": "Dollar slips as soft CPI revives October cut bets",
      "relevance": "high" }
  ],

  "session_state": {
    "active": "NY",
    "minutes_to_cutoff": 165,
    "minutes_to_session_close": 240
  },

  "recent_journal": [ /* last 20 trades: setup summary, R, exit reason */ ],
  "similar_setups": [ /* retrieved past trades with outcomes */ ]
}
```

**Replay invariant:** every field is derived through the `as_of`-filtered data provider. `calendar.next_high_impact.actual` is `null` whenever the event time is after `as_of` — leaking a future release value would invalidate the entire run.

## 4. Judgment output schema

Enforced via `output_config.format` with `json_schema`.

```jsonc
{
  "decision": "LONG" | "SHORT" | "NO_TRADE",
  "conviction": 0-100,

  "market_read": "What is actually driving gold right now, in plain language.",
  "positioning_read": "Who is offside, where the pain is, what has to happen to them.",

  "entry": { "low": 2646.00, "high": 2647.50, "type": "limit" | "market" },
  "invalidation": 2641.00,
  "targets": [
    { "price": 2658.00, "rationale": "prior day high, thin above" },
    { "price": 2666.00, "rationale": "weekly level, expect supply" }
  ],

  "reasoning": "Full prose. Causal, not descriptive.",
  "what_would_change_my_mind": "The specific observation that invalidates this read.",
  "kill_conditions": [
    "M15 closes back below 2644 — the move failed, get out",
    "DXY reclaims 104.50 — the dollar driver is gone"
  ],
  "risks": [ "FOMC tomorrow — do not hold size into it" ],
  "expected_hold_minutes": 120
}
```

**There is no size, lot, leverage, or risk field. Deliberately.** The schema is the enforcement mechanism for the layer boundary — the AI physically cannot express a position size.

When `decision` is `NO_TRADE`, `entry`, `invalidation` and `targets` are null; `market_read` and `reasoning` are still required. A well-argued no-trade is a valuable output and is stored and scored like any other.

## 5. Request configuration

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
        {
            "type": "text",
            "text": TRADER_PERSONA,          # stable — never interpolate time/price
            "cache_control": {"type": "ephemeral"},
        }
    ],
    messages=[{"role": "user", "content": json.dumps(context_packet)}],
)
```

**Caching discipline.** The cache is prefix-matched — any byte change before the breakpoint invalidates everything after it. The persona must contain **no timestamps, no prices, no per-request IDs**. Serialize the context packet with sorted keys so identical state produces identical bytes. Verify `usage.cache_read_input_tokens > 0` on the second and subsequent calls; if it stays zero, a silent invalidator is present.

**Effort.** Start at `high`. During P5, sweep `medium` / `high` / `xhigh` over an identical replay decision set and keep the lowest level that holds quality — this is a measured decision, not a guess.

**No prefill.** Assistant prefills return a 400 on Sonnet 5. Format control comes from the structured output schema.

## 6. Persona outline

The full prompt lives in `prompts/trader_persona_v1.md` and is version-controlled — every Judgment records its `prompt_version` so replay comparisons are valid.

Structure:

1. **Identity** — twenty years trading gold and macro. Seen 2008, 2013, 2020, 2022. Has been wrong expensively and learned from it.
2. **How you think** — causally. Who is positioned, what is the driver, where is the pain, what is the path of least resistance. Named patterns are shorthand for describing, never a reason for acting.
3. **What you ignore** — indicator crossovers with no story behind them, patterns without a driver, and the urge to trade because you are watching.
4. **Selectivity mandate** — most of the time the answer is no trade. State this explicitly and repeatedly.
5. **Risk awareness** — this is a funded evaluation. One bad day ends it. You do not need to catch every move.
6. **Output contract** — the schema, and the standing rule that you never specify position size; the risk desk handles that.

### Anti-patterns the persona must suppress

| Anti-pattern | Why it fails |
|---|---|
| "Bullish engulfing formed, going long" | Describes the candle; explains nothing |
| "RSI oversold, expecting a bounce" | Indicator state is not a driver |
| Finding a trade because it was asked | Produces the overtrading the system exists to prevent |
| High conviction on thin reasoning | Conviction must track evidence, and it drives the dispatch gate |
| Vague invalidation ("if it goes against me") | The Risk Engine needs a concrete price |

## 7. Evaluating the persona

Prompt changes are validated against replay, never against intuition.

1. Freeze a decision set of ~300 timestamps spanning varied regimes — trending, ranging, high and low volatility, news days and quiet days.
2. Run variant A and variant B over the **identical** timestamps.
3. Compare: expectancy in R with bootstrap 95% CI, no-trade rate, conviction calibration (do 80-conviction trades actually outperform 60-conviction ones?), and failure clustering.
4. Ship a variant only if its CI lower bound improves. A higher mean with an overlapping CI is noise, not progress.

**Conviction calibration is the highest-signal diagnostic.** If high-conviction trades do not outperform low-conviction ones, the model does not know what it knows, and conviction cannot be trusted as a dispatch gate — that finding matters more than headline expectancy.

## 8. Weekly review (secondary use)

A separate, lower-frequency call takes the week's trades with their full context and produces a review: what worked, what failed, whether losses cluster in an identifiable condition, and one concrete suggestion for the config or persona. Output is advisory — it is shown to the operator and never auto-applied to `rules.yaml`.
