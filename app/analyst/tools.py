"""Read-only CRI calculation tools exposed to the Analyst.

Every number a tool returns is computed by the frozen RC1 engine or by the
Optimizer reading RC1 output. Each result carries ``provenance``: the release
identity and fingerprint, the evaluation timestamp and the scenario applied.
No tool can lend, change limits, message borrowers, or liquidate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.analyst.book import AccountView, BookSource
from app.analyst.scenario import SCHEMA as SCENARIO_SCHEMA
from app.analyst.scenario import Scenario, apply


def _release() -> dict[str, str]:
    from app.candidate.rc import ENGINE_VERSION, RC_ID, rc_fingerprint
    return {"release": RC_ID, "engine": ENGINE_VERSION, "fingerprint": rc_fingerprint()[:16]}


SEED = 11


def simulation_info(n_paths: int) -> dict[str, Any]:
    """Actual simulation settings behind every Analyst figure (review finding 2)."""
    from app.candidate.rc import rc_config
    frozen = rc_config().n_paths
    return {"monte_carlo_paths": n_paths, "seed": SEED, "frozen_release_paths": frozen,
            "configuration": "frozen RC1 parameters with a reduced path count; figures carry Monte Carlo "
                             "error and are not identical to the frozen evaluation" if n_paths < frozen
            else "frozen RC1 configuration"}


def _evaluate(v: AccountView, n_paths: int, seed: int = SEED) -> Any:
    from dataclasses import replace

    from app.candidate import CandidateEngine
    from app.candidate.rc import rc_config
    cfg = replace(rc_config(), n_paths=n_paths)
    return CandidateEngine(cfg).evaluate(v.account_ref, v.holdings, v.loan, v.policy, v.market_data, bars=v.bars,
                                         execution_policy=v.execution_policy, interest_policy=v.interest_policy,
                                         as_of=v.as_of, seed=seed)


def _summary(e: Any) -> dict[str, Any]:
    mv = e.portfolio_market_value or 0.0
    bal = e.current_outstanding_balance
    return {"currency": e.loan_currency, "as_of": e.as_of,
            "collateral_value": round(mv, 2), "loan_balance": round(bal, 2),
            "current_ltv": round(bal / mv, 4) if mv else None,
            "approved_credit_limit": round(e.approved_credit_limit, 2),
            "available_credit": round(e.current_available_credit, 2),
            "margin_state": e.margin_state.value, "cure_amount": round(e.cure_amount, 2),
            "threshold_ltv": {k: (round(x, 4) if x is not None else None) for k, x in e.threshold_ltv.items()},
            "p_recovery_shortfall_if_recovery_after_grace": round(e.p_infeasible_after_grace(bal), 4),
            "data_status": e.data_status, "restriction": e.restriction, "deterioration": e.deterioration}


def _snapshot(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Account snapshot times behind a book-level answer (finding 8)."""
    from datetime import datetime as _dt
    ts = sorted(_dt.fromisoformat(str(r["as_of"])) for r in rows if r.get("as_of"))
    if not ts:
        return {"snapshot": None}
    spread = (ts[-1] - ts[0]).total_seconds() / 3600
    return {"snapshot": {"earliest": ts[0].isoformat(), "latest": ts[-1].isoformat(),
                         "spread_hours": round(spread, 1),
                         "consistent": spread <= 24.0}}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., dict[str, Any]]
    mutates: bool = False   # every registered tool must be read-only


# Every capacity figure the Analyst returns carries its category, so approved RC1 credit is
# never presented as, or next to without distinction from, experimental or research estimates.
CAPACITY_CLASSIFICATION: dict[str, dict[str, str]] = {
    "rc1_approved_credit_limit": {
        "category": "approved_rc1_credit",
        "meaning": "credit limit under the approved RC1 methodology (contractual ceiling applied)"},
    "risk_supported_capacity": {
        "category": "experimental_capacity",
        "meaning": "research capacity supported by an illustrative appetite preset; not approved for lending"},
    "risk_supported_ltv": {
        "category": "experimental_capacity",
        "meaning": "the experimental capacity as a fraction of collateral value; not approved for lending"},
    "points": {
        "category": "recovery_conditional_estimate",
        "meaning": "shortfall probability and tail given that a recovery is triggered at that balance; "
                   "not a lifetime loss probability"},
    "lifetime_estimates": {
        "category": "lifetime_risk_estimate",
        "meaning": "not produced for a single account: lifetime loss, ES99 and economics come from the "
                   "portfolio-level lifecycle frontier (RC2 research)"},
}


class Toolset:
    def __init__(self, book: BookSource, n_paths: int = 800):
        self.book = book
        self.n_paths = n_paths
        self._cache: dict[tuple[str, str], Any] = {}

    def _eval(self, v: AccountView, key: str = "base") -> Any:
        k = (v.account_ref, key)
        if k not in self._cache:
            self._cache[k] = _evaluate(v, self.n_paths)
        return self._cache[k]

    # ----------------------------------------------------------- tools

    def list_accounts(self) -> dict[str, Any]:
        rows = []
        for ref in self.book.account_refs():
            e = self._eval(self.book.account(ref))
            s = _summary(e)
            rows.append({"account_ref": ref, **{k: s[k] for k in ("currency", "as_of", "collateral_value", "loan_balance",
                                                                  "current_ltv", "available_credit", "margin_state")}})
        by_ccy: dict[str, dict[str, Any]] = {}
        for r in rows:          # amounts are never summed across currencies
            t = by_ccy.setdefault(r["currency"], {"accounts": 0, "collateral_value": 0.0, "loan_balance": 0.0})
            t["accounts"] += 1
            t["collateral_value"] += r["collateral_value"]
            t["loan_balance"] += r["loan_balance"]
        for t in by_ccy.values():
            t["book_ltv"] = round(t["loan_balance"] / t["collateral_value"], 4) if t["collateral_value"] else None
            t["collateral_value"] = round(t["collateral_value"], 2)
            t["loan_balance"] = round(t["loan_balance"], 2)
        out: dict[str, Any] = {"accounts": rows, "totals_by_currency": by_ccy, **_snapshot(rows)}
        if len(by_ccy) == 1:
            out["totals"] = {**next(iter(by_ccy.values())), "currency": next(iter(by_ccy))}
        else:
            out["note"] = "several loan currencies: totals are reported per currency"
        return out

    def account_risk(self, account_ref: str) -> dict[str, Any]:
        e = self._eval(self.book.account(account_ref))
        return {"account_ref": account_ref, **_summary(e), "reasons": e.reasons[:8],
                "threshold_reasoning": dict(list(e.threshold_reasoning.items())[:6]),
                "holdings": [{"instrument": a.asset_id, "market_value": round(a.market_value, 2),
                              "risk_score": round(a.risk_score, 4), "lending_value_eligible": a.lending_value_eligible,
                              "estimated_slippage_rate": round(a.estimated_slippage_rate, 4),
                              "data_status": a.data_status} for a in e.asset_results]}

    def stress_test(self, scenario: dict[str, Any], account_refs: list[str] | None = None) -> dict[str, Any]:
        sc = Scenario.from_dict(scenario)
        sectors = self.book.sector_of()
        refs = list(dict.fromkeys(account_refs)) if account_refs else self.book.account_refs()
        keys = ("collateral_value", "current_ltv", "available_credit", "margin_state", "cure_amount",
                "p_recovery_shortfall_if_recovery_after_grace")
        rows: list[dict[str, Any]] = []
        totals: dict[str, dict[str, float]] = {}
        for ref in refs:
            v = self.book.account(ref)
            b = _summary(self._eval(v))
            sv, hit = apply(v, sc, sectors)
            s = _summary(_evaluate(sv, self.n_paths))
            rows.append({"account_ref": ref, "currency": b["currency"], "as_of": b["as_of"], "shocked_holdings": hit,
                         "base": {k: b[k] for k in keys}, "stressed": {k: s[k] for k in keys}})
            t = totals.setdefault(b["currency"], {"base_collateral": 0.0, "stressed_collateral": 0.0, "balance": 0.0,
                                                  "base_available": 0.0, "stressed_available": 0.0,
                                                  "base_cure": 0.0, "stressed_cure": 0.0})
            for k, x in (("base_collateral", b["collateral_value"]), ("stressed_collateral", s["collateral_value"]),
                         ("balance", b["loan_balance"]), ("base_available", b["available_credit"]),
                         ("stressed_available", s["available_credit"]), ("base_cure", b["cure_amount"]),
                         ("stressed_cure", s["cure_amount"])):
                t[k] += x
        books: dict[str, dict[str, Any]] = {}
        for ccy, t in totals.items():       # never summed across currencies
            states = [r["stressed"]["margin_state"] for r in rows if r["currency"] == ccy]
            books[ccy] = {**{k: round(x, 2) for k, x in t.items()},
                          "base_book_ltv": round(t["balance"] / t["base_collateral"], 4) if t["base_collateral"] else None,
                          "stressed_book_ltv": round(t["balance"] / t["stressed_collateral"], 4)
                          if t["stressed_collateral"] else None,
                          "accounts_not_safe_after_stress": sum(1 for x in states if x != "safe"),
                          "stressed_margin_states": {st: states.count(st) for st in sorted(set(states))}}
        out: dict[str, Any] = {"scenario": sc.describe(), "accounts": rows, "book_by_currency": books,
                               **_snapshot(rows),
                               "limitations": ["instantaneous shock; no path between now and the shocked state",
                                               "sector membership from the book's instrument reference data",
                                               ("volume and settlement stress act through RC1's liquidity and "
                                                "timing model"),
                                               ("amounts are in each account's loan currency and are not "
                                                "converted or summed across currencies")]}
        if len(books) == 1:
            out["book"] = {**next(iter(books.values())), "currency": next(iter(books))}
        return out

    def capacity_frontier(self, account_ref: str, appetite: str = "balanced") -> dict[str, Any]:
        from app.optimizer import PRESETS, capacity_for_appetite, frontier
        if appetite not in PRESETS:
            raise ValueError(f"appetite must be one of {sorted(PRESETS)}")
        e = self._eval(self.book.account(account_ref))
        f = frontier([e], n_points=11)
        cap = capacity_for_appetite([e], PRESETS[appetite])
        return {"account_ref": account_ref, "view": f.view, "notes": f.notes,
                "points": [{k: (round(x, 4) if isinstance(x, float) else x) for k, x in p.items()
                            if k in ("balance", "ltv", "p_shortfall", "expected_shortfall_rate", "cvar_rate")}
                           for p in f.points],
                "appetite": appetite, "risk_supported_capacity": round(cap["risk_supported_capacity"], 2),
                "risk_supported_ltv": round(cap["risk_supported_ltv"], 4) if cap["risk_supported_ltv"] else None,
                "rc1_approved_credit_limit": round(cap["rc1_approved_credit_limit"], 2),
                "classification": CAPACITY_CLASSIFICATION,
                "caution": "recovery-conditional view; appetite presets are illustrative research settings, "
                           "not Tiva's production risk appetite"}

    def deteriorating_assets(self, top: int = 10) -> dict[str, Any]:
        agg: dict[str, dict[str, Any]] = {}
        for ref in self.book.account_refs():
            e = self._eval(self.book.account(ref))
            for a in e.asset_results:
                if a.instrument_key.upper().endswith(":CASH") or a.market_value <= 0:
                    continue
                # stable identity (exchange:symbol), not the bare ticker; exposure per currency
                r = agg.setdefault(a.instrument_key, {"instrument_key": a.instrument_key, "instrument": a.asset_id,
                                                      "exposure_by_currency": {}, "accounts": 0,
                                                      "risk_score": a.risk_score, "data_status": a.data_status})
                ccy = e.loan_currency
                r["exposure_by_currency"][ccy] = r["exposure_by_currency"].get(ccy, 0.0) + a.market_value
                r["accounts"] += 1
                r["risk_score"] = max(r["risk_score"], a.risk_score)
        rows = sorted(agg.values(), key=lambda r: -r["risk_score"])[:max(1, min(top, 50))]
        for r in rows:
            r["exposure_by_currency"] = {k: round(v, 2) for k, v in r["exposure_by_currency"].items()}
            if len(r["exposure_by_currency"]) == 1:
                r["exposure"] = next(iter(r["exposure_by_currency"].values()))
            r["risk_score"] = round(r["risk_score"], 4)
        return {"ranking": "RC1 per-instrument risk score (higher = riskier); exposure in the loan currency of "
                           "the accounts holding it, per currency",
                "instruments": rows}

    def instrument_reference(self) -> dict[str, Any]:
        sectors = self.book.sector_of()
        seen: dict[str, dict[str, Any]] = {}
        for ref in self.book.account_refs():
            for h in self.book.account(ref).holdings:
                if h.asset_type.value == "cash":
                    continue
                seen.setdefault(h.asset_id, {"instrument": h.asset_id, "market": h.exchange,
                                             "asset_type": h.asset_type.value,
                                             "sector": sectors.get(h.asset_id.upper(), "unclassified")})
        return {"instruments": sorted(seen.values(), key=lambda r: r["instrument"]),
                "sectors": sorted({r["sector"] for r in seen.values()})}

    # ------------------------------------------------------- registry

    def tools(self) -> dict[str, Tool]:
        acct = {"type": "string", "maxLength": 64, "pattern": "^[A-Za-z0-9_.:-]+$"}
        return {
            "instrument_reference": Tool("instrument_reference", "Instruments held in the book with market, asset "
                                         "type and sector; use it to map sector words in a question to scenario "
                                         "selectors.", {"type": "object", "properties": {},
                                                        "additionalProperties": False}, self.instrument_reference),
            "list_accounts": Tool("list_accounts", "Loan book overview: every monitored account with collateral "
                                  "value, balance, LTV, available credit and RC1 margin state.",
                                  {"type": "object", "properties": {}, "additionalProperties": False},
                                  self.list_accounts),
            "account_risk": Tool("account_risk", "RC1 risk evaluation of one account: approved limit, available "
                                 "credit, threshold LTVs, margin state, cure amount, reasons and holdings.",
                                 {"type": "object", "required": ["account_ref"], "additionalProperties": False,
                                  "properties": {"account_ref": acct}}, self.account_risk),
            "stress_test": Tool("stress_test", "Re-evaluate accounts with RC1 under a structured stress scenario "
                                "(price shocks by sector/market/instrument, volume change, settlement delay).",
                                {"type": "object", "required": ["scenario"], "additionalProperties": False,
                                 "properties": {"scenario": SCENARIO_SCHEMA,
                                                "account_refs": {"type": "array", "maxItems": 200, "uniqueItems": True,
                                                                 "items": acct}}},
                                self.stress_test),
            "capacity_frontier": Tool("capacity_frontier", "Recovery-conditional risk-capacity frontier for one "
                                      "account and the capacity supported by a risk-appetite preset.",
                                      {"type": "object", "required": ["account_ref"], "additionalProperties": False,
                                       "properties": {"account_ref": acct,
                                                      "appetite": {"type": "string",
                                                                   "enum": ["conservative", "balanced", "aggressive"]}}},
                                      self.capacity_frontier),
            "deteriorating_assets": Tool("deteriorating_assets", "Instruments ranked by RC1 risk score with their "
                                         "exposure across the loan book.",
                                         {"type": "object", "additionalProperties": False,
                                          "properties": {"top": {"type": "integer", "minimum": 1, "maximum": 50}}},
                                         self.deteriorating_assets),
        }


def release_info() -> dict[str, str]:
    return _release()
