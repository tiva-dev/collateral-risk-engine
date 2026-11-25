# Validation Record

This record states, per engine version, what has and has not been validated.
It overrides any statement elsewhere in the repository that implies broader
validation.

## Status by version

| Version | Code | Provider-backed financial validation | Status |
|---|---|---|---|
| v0.6.2 | `main` 5898756 (`app/` identical to f7cce7f and 3e14b49) | GitHub Actions run 30455533912, 29 Jul 2026, 260 replays, 10 portfolios | Completed. Evidence limitations below. |
| v0.7.0 | PR #21 head 2b47314 | None completed. Run 34000523667 (6 Sep 2026) failed during dataset build: inverted date inputs and a lapsed NGNMarket plan. | **Not validated.** v0.6.2 results must not be cited for v0.7.0. |

## v0.6.2 evidence (run 30455533912)

* Evidence ZIP SHA-256 recorded at the time:
  `302e2a99a3843de3ca73c72931250bd5def194a759bdb03cd0d5cf182dfa3e8a`.
* The workflow did not set `retention-days`. With GitHub's default 90-day
  retention the artifact is expected to have expired around late October
  2026. If any copy exists, archive it outside GitHub with its checksum.
* What the evidence supports: replay mechanics (interest accrual, state
  transitions, fill accounting, proceeds waterfall), provider coverage and
  evidence-integrity QA.
* What it does not support:
  * The forced-liquidation boundary sets collateral value to 1.08 × the drawn
    loan (`replay.py`), so 10/10 full recoveries follow from how the scenario
    was built. It tests mechanics, not protection.
  * Baseline portfolios are small relative to market volume, so participation
    limits never bind.
  * Bids and asks are modelled from volume buckets, not observed quotes.
  * Economic shortfall uses realized same-day execution costs (often zero),
    not expected liquidation costs.
  * Zero observed losses in a finite sample do not establish zero loss
    probability.

## v0.7.0 failed run (34000523667) — cause confirmed

* Trigger: `workflow_dispatch` on `agent/cri-v070-recoverability` at 2b47314,
  6 Sep 2026, about 20 seconds. Failing step 7, "Build or plan official
  provider dataset", exit code 1 ("Provider dataset build produced no usable
  equity histories"). Job logs read through the GitHub API on 25 Nov 2026.
* **Cause 1, inverted date window (operator input).** The run was dispatched
  with `START_DATE=2025-09-05` and `END_DATE=2025-09-04`. Alpaca therefore
  returned no bars for all 11 US symbols and Alpha Vantage returned no FX.
  The builder did not validate the window, so the error surfaced only as
  "no usable equity histories". Fixed: `OfficialDatasetBuilder.build` now
  rejects an end date that is not after the start date
  (`app/tests/test_dataset_window_validation.py`).
* **Cause 2, NGNMarket plan.** Every NGNMarket request returned
  `HTTP 403 PLAN_REQUIRED: This endpoint requires a hobby plan or higher`
  (15 NGX equities and the FX endpoints). This is independent of the dates:
  NGX data cannot be rebuilt until the NGNMarket subscription is restored or
  upgraded.
* Not the cause: Alpaca and Alpha Vantage credentials (no auth errors were
  reported), dry-run inputs, or the v0.7.0 data-building changes.
* The run's evidence artifact (ID 9979342512, digest
  `sha256:b9e22f2f4eb2730b4e6d5d4576302190e47dc16e3d385e37a122397f8ecd98e4`)
  expires on 5 Dec 2026. Its content (manifest and build log) is the same as
  the job log summarised above.

## Acceptance requirements for any future version

Defined in the methodology document (section 14). Minimum: rolling
origination cohorts, out-of-sample evaluation, institutional position sizes,
non-circular stress scenarios, separate mechanics and protection tests, and
statistical uncertainty on every loss estimate.
