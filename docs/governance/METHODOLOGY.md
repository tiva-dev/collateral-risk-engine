# Financial Methodology — Status and As-Implemented Equations

This file records the methodology that the code on this branch actually
implements. It is descriptive, not an endorsement. The replacement
methodology is under research (see "CRI Quantitative Risk Methodology —
Research, Comparative Analysis and Proposed Specification") and must not be
implemented in `app/` until it is approved.

Notation: q quantity, P price, MV market value, σ annualized volatility
(maximum of the available windows, floored by an asset-type default in
v0.6.2), h liquidation horizon in trading days, α confidence by risk appetite
(0.995 / 0.99 / 0.975), ES the normal expected shortfall φ(z_α)/(1−α)·σ√(h/252),
DQ data-quality score, s relative bid-ask spread, ADV average daily dollar
volume.

## v0.6.2 (this branch, `main`)

1. Stressed liquidation value, proxy branch (`liquidation/recovery.py`):
   SLV_i = q_i P_i (1 − min(0.95, s_i/2 + 0.08√(MV_i/ADV_i) + ES_α(σ_i, max(h_i,1)) + 0.25(1 − DQ_i))).
2. Effective LTV (`core/evaluator.py`):
   LTV_i = clip(base_i × A_vol × A_liq × A_spread × A_conc × A_stress × A_DQ, 0, cap_i).
   The six factors are in `risk/adjustments.py`. Volatility enters three of
   them (A_vol, A_stress and, through the horizon, A_liq), which is the
   double-counting reviewed in the methodology research.
3. Approved limit: L = min(Σ MV_i LTV_i, MV × portfolio_ltv_cap).
4. Portfolio risk score R = clip(0.75 r̄ + 0.20 HHI + 0.35 w_inelig, 0, 1);
   trigger coverages c_L = clip(1.01 + 0.34R, 1.02, 1.60), c_M, c_R, c_W
   layered above it.
5. Safe limit (`lifecycle/service.py::_safe_credit_limit`):
   C = min(L, SLV / c_W). No interest reserve.
6. Cures: repayment-only max(0, O − SLV/c_M); injection-only
   max(0, c_M O − SLV). Margin-call liquidation plans and monitoring
   advisories use cash-cure amounts as sale targets (known defect, see
   characterization tests).

## v0.7.0 (PR #21, not merged, not validated)

Differences from v0.6.2:

* LTV_i = min(base_i, cap_i, SLV_i / (MV_i c_i)) with
  c_i = clip(1.08 + 0.22 min(σ_i, 1.5) + 0.12·1[ADV unknown], 1.08, 1.55).
  The six adjustment factors no longer reduce LTV; concentration factor = 1.
* Volatility floor by asset type removed; high-volatility equity defaults
  raised (base 0.35 → 0.65, cap 0.50 → 0.75).
* Principal capacity = (C − fixed fees) / Φ, where Φ is the obligation one
  unit of principal grows to by the interest-reserve end date.
* No reserve for price risk during recovery latency.

## Known defects (documented, not fixed)

See `app/tests/test_audit_characterization.py` and the audit document.
