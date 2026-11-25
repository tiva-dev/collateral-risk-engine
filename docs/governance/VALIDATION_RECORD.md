# Validation Record

This record states, per engine version, what has and has not been validated.
It overrides any statement elsewhere in the repository that implies broader
validation.

## Status by version

| Version | Code | Provider-backed financial validation | Status |
|---|---|---|---|
| v0.6.2 | `main` 5898756 (`app/` identical to f7cce7f and 3e14b49) | GitHub Actions run 30455533912, 29 Jul 2026, 260 replays, 10 portfolios | Completed. Evidence limitations below. |
| v0.7.0 | PR #21 head 2b47314 | None completed. Run 34000523667 (6 Sep 2026) failed during dataset build. | **Not validated.** v0.6.2 results must not be cited for v0.7.0. |

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

## v0.7.0 failed run (34000523667)

* Trigger: `workflow_dispatch` on `agent/cri-v070-recoverability` at 2b47314,
  6 Sep 2026. Total duration about 20 seconds. Failing step: 7,
  "Build or plan official provider dataset", exit code 1.
* The only fast non-zero exit in `build_official_dataset.py` is
  `SystemExit("Provider dataset build produced no usable equity histories")`,
  raised when every equity fetch fails. A 20-second run is too short to have
  fetched 26 equity histories, so the fetches most likely failed immediately.
  Candidate causes, in order of likelihood:
  1. Provider credential or entitlement failures (expired or rotated
     `ALPACA_*`, `NGNMARKET_API_KEY` secrets), combined with the July cache
     having been evicted (GitHub evicts caches unused for 7 days).
  2. An NGNMarket pre-flight failure, combined with Alpaca failures.
  3. Workflow inputs left at `dry_run=true` cannot produce this error; a
     dry run exits 0 (reproduced locally).
* Logs require GitHub sign-in and were not available to the auditor. The run
  uploaded a 126 KB artifact (`official-provider-validation-evidence`,
  digest `sha256:b9e22f2f…98e4`) that should contain `dataset_build.log` and
  the manifest's sanitized `missing_symbol_reasons`. **It expires around
  5 Dec 2026 under default retention and must be downloaded before then.**

## Acceptance requirements for any future version

Defined in the methodology document (section 14). Minimum: rolling
origination cohorts, out-of-sample evaluation, institutional position sizes,
non-circular stress scenarios, separate mechanics and protection tests, and
statistical uncertainty on every loss estimate.
