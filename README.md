# Prop Desk

Gold (XAUUSD) signal and rule-guard system for an FTMO $100,000 account.

Prop accounts are lost to **rule breaches**, not bad analysis. This system makes
a breach structurally impossible: every signal is pre-validated against FTMO's
exact limits and sized by deterministic code before it ever reaches you.

It does **not** place trades. It signals; you execute manually and report the
outcome back. The system holds no broker credentials and cannot trade even if
fully compromised.

## The governing rule

> **The AI decides *what* to trade. The code decides *how much*.**

The Judgment Engine's output schema has no position-size field and forbids extra
fields — a model that tries to emit one gets a validation error. The Risk Engine
never calls an LLM. A hallucination can produce a bad *opinion*; it cannot
produce a rule breach.

## Architecture

| Layer | Role | AI? |
|---|---|---|
| L1 Context Engine | Assembles everything knowable now: prices, volatility, structure, macro, calendar, news | No |
| L2 Judgment Engine | Reads the context like an experienced trader; returns direction, levels, reasoning | Claude Sonnet 5 |
| L3 Risk & Rule Engine | Sizes the position and gates it against every limit | No |
| L4 Journal & Memory | Stores context + judgment + verdict + outcome; feeds back | Weekly review only |

Replay mode runs the identical L1→L2→L3 path against historical timestamps with
a hard `as_of` cutoff, so the AI only ever sees what was knowable at the time.

## Status

**P0 complete** — config schema, domain models, Risk Engine, test suite.
Next: P1, the Context Engine and data providers.

```
config/rules.ftmo-100k.yaml   FTMO rules — no firm-specific value lives in code
src/propdesk/config.py        rule loading + the two-tier limit invariant
src/propdesk/models.py        domain models; the AI/code boundary lives here
src/propdesk/risk/            sizing arithmetic and the rule gate
tests/                        80 tests, incl. 7 property-based invariants
docs/                         PRD, technical design, AI judgment spec
```

## Setup

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe -m pytest
```

## Testing philosophy

The Risk Engine carries the entire safety promise, so it gets disproportionate
test effort. The headline test is `test_approved_trades_never_reach_the_hard_floors`:
across thousands of generated account states, market conditions and judgments,
no approved trade's worst case ever reaches FTMO's limits.

One trap worth knowing about: property tests can pass **vacuously** if every
generated scenario happens to be blocked. The first version of this suite did
exactly that — 100% of cases blocked, invariants never actually evaluated.
`test_viable_strategy_actually_exercises_sizing` now asserts that a majority of
viable scenarios produce a tradeable verdict, so that failure cannot return.

## Safety boundaries

- No automated execution, ever. No broker or FTMO API integration.
- Risk Engine changes require tests; the property suite is a release gate.
- Every data access takes an `as_of` timestamp and filters in SQL — this is what
  keeps replay honest.
- FTMO rules here were researched from public sources on 2026-09-21 and **must be
  re-verified against the live account dashboard** before any real-money phase.
  `test_dashboard_verification_still_outstanding` fails deliberately once you do.
