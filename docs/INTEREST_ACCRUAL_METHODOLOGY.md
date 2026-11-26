# INTEREST ACCRUAL METHODOLOGY

This document is part of the v0.5B official validation evidence package.

## Scope

The validation framework replays cache-first historical datasets and applies deterministic synthetic stress overlays to compare flat LTV, static haircut, and dynamic collateral risk engine lending outcomes. It does not call live providers during normal CI, does not execute broker orders, and does not require production databases or real API keys.

## Reproducibility

Runs record deterministic seeds, manifest checksums, simulation configuration version, model versions, interest settings, provider coverage metadata, warnings, and stress assumptions.

## Outputs

The reporting layer produces official validation manifest JSON, metrics JSON, metrics CSV, validation report Markdown, provider coverage report, data methodology, interest accrual methodology, and simulation assumptions.

## Accrual and capitalization (approved correction, 26 Nov 2026)

Interest **accrual** and **capitalization** are separate operations.

* `accrue_interest` only accrues: interest for the interval is added to
  `accrued_interest`, computed on `principal` (which already contains interest
  capitalized at earlier boundaries). Uncapitalized accrued interest earns no
  interest.
* `accrue_scheduled_periods` walks the contractual capitalization schedule and,
  for compound loans, moves accrued interest into principal **only at
  contractual boundaries**. Boundaries are counted from
  `InterestPolicy.schedule_anchor` (normally the drawdown date; monitoring sets
  it to the registration time when the client does not supply it). Without an
  anchor, calendar boundaries are used.
* Consequence: the balance at any time depends only on the start and end of the
  accrual period, never on how often the account was monitored. This is tested
  for every frequency, both compounding modes, anchored and calendar schedules,
  with random monitoring splits (`app/tests/test_financial_correctness_nov26.py`).

Interest reserves for recovery convert trading sessions to calendar days
(5 sessions = 7 days) and, for forced recovery, cover the delay, the full
execution window of the sale and settlement.
