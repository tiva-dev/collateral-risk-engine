# Federation privacy analysis (research prototype)

Status: research only. No institutional data is accessed. This is not a claim of institutional privacy readiness.

## Two different protections

| Protection | What it protects | What it does not protect |
|---|---|---|
| Secure aggregation (double masking, Shamir-shared self-masks) | Each institution's individual contribution, from the coordinator. Only the sum of survivors is revealed | What the sum reveals; anything after release |
| Differential privacy (distributed Gaussian noise plus an accountant) | What the released aggregates and models reveal about the privacy unit | An institution's own use of its own data; protocol-level attacks on secure aggregation |

## Privacy units and adjacency

- **Federated ML (`cure_logistic_v1`): institution.**
  - **Adjacency:** add or remove one institution's entire data.
  - **Contribution:** v = [w·δ, w], where δ is clipped to C and w = min(n, W).
  - **Sensitivity:** S = W·√(C² + 1). This covers the weighted numerator and the aggregate denominator.
  - **Weighting:** with W = 1 (uniform), the denominator is the public survivor count. With W > 1, the noised denominator makes size-weighted averaging unusable under DP at six institutions; the measured log loss is 2.32.
- **Federated statistics (`slippage_ols_v1`, `duration_ols_v1`): record.**
  - **Adjacency:** add or remove one loan record in one institution.
  - **Clipping:** standardised features to ±4 and the outcome to ±10.
  - **Sensitivity:** S = √((1+kB²)² + (1+kB²)Y² + Y⁴ + 1).
  - **Small cells:** without noise, cells below the minimum are suppressed. This is disclosure control, not DP. Under DP there is no data-dependent suppression and no row-count metadata, because switching suppression on or off moves the release by about the minimum cell size in records, far above the one-record sensitivity.
- **Shared population and joint accounting:** the three schemas (margin-call outcome, liquidation execution, recovery episode) are attributes of the same loan records. Every mechanism over them is charged to one ledger scope (`loan_records`), so cure-model training, slippage statistics and duration statistics compose against the institution's single budget.
  - An institution-level guarantee implies the record-level one, so the composed figure is a conservative record-level bound.

## Noise and accounting

- **Noise:** every agent adds N(0, (zS)²/m) to every coordinate.
- **Charge per round:** z·√((survivors − c)/m), where c is the assumed number of participants colluding with the coordinator.
- **Composition:** Rényi DP of the Gaussian mechanism; no subsampling amplification is claimed.
- **Agent ledgers:** each agent keeps a persistent, hash-chained ledger in its own environment and charges the worst case (threshold survivors, c colluders) before sending. It refuses a round that would exceed the institution's consented budget, and refuses any noiseless round when the institution requires DP.
- **Coordinator ledger:** also persistent. A restarted coordinator keeps its spending; a replacement coordinator cannot reset the agents' ledgers. Both are demonstrated in `simulate.adversarial`.

## Threats evaluated (simulation)

- **Repeated rounds:** composed by the accountant. Training stops when the budget is exhausted.
- **Malicious participant:** the participant bypasses clipping and sends a 50× update. The aggregate is rejected when it exceeds what clipped contributions plus noise can produce. Robust aggregation beyond this bound is not claimed.
- **Inconsistent dropout declarations:** an agent refuses conflicting survivor/dropout lists within a round, and never reveals both a peer's self-mask share and its pairwise seed.
- **Collusion:** fewer than t shares do not reconstruct a self-mask seed. DP accounting can assume c colluders.
- **Auxiliary information:** no per-institution evaluation metric leaves an agent. The previous version sent holdout metrics in clear.
- **Isolation:** agents ran as separate processes in empty network namespaces (loopback only, no route out; `app/federation/isolated.py`). Each process generates only its own institution's data, which is seeded independently of the others. Isolated agents produced exactly the result of non-isolated agents.

## Reading epsilon

At δ = 10⁻⁶, institution-level ε values of about 12–20 are weak formal guarantees. They are reported as computed and are not described as strong privacy. Whether a value is acceptable depends on the threat model and the participants' agreement.

## Correction to Report 17

Report 17's DP results were obtained with noise scaled by each institution's own weight and an unprotected denominator, so the actual noise was smaller than the accountant assumed. The reported ε understated the privacy loss. Those DP utility figures are superseded by the repeated experiments (`app/federation/experiments.py`).
