"""Read-only CRI calculation tools exposed to the Analyst.

Every number a tool returns is computed by the frozen RC1 engine or by the
Optimizer reading RC1 output. Each result carries ``provenance``: the release
identity and fingerprint, the evaluation timestamp and the scenario applied.
No tool can lend, change limits, message borrowers, or liquidate.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable

from app.analyst.book import AccountView, BookSource
from app.analyst.scenario import SCHEMA as SCENARIO_SCHEMA
from app.analyst.scenario import Scenario, apply


def _release() -> dict[str, str]:
    from app.candidate.rc import ENGINE_VERSION, RC_ID, rc_fingerprint
    return {"release": RC_ID, "engine": ENGINE_VERSION, "fingerprint": rc_fingerprint()[:16]}


def _evaluate(v: AccountView, n_paths: int, seed: int = 11) -> Any:
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
    return {"collateral_value": round(mv, 2), "loan_balance": round(bal, 2),
            "current_ltv": round(bal / mv, 4) if mv else None,
            "approved_credit_limit": round(e.approved_credit_limit, 2),
            "available_credit": round(e.current_available_credit, 2),
            "margin_state": e.margin_state.value, "cure_amount": round(e.cure_amount, 2),
            "threshold_ltv": {k: (round(x, 4) if x is not None else None) for k, x in e.threshold_ltv.items()},
            "p_recovery_shortfall_if_recovery_after_grace": round(e.p_infeasible_after_grace(bal), 4),
            "data_status": e.data_status, "restriction": e.restriction, "deterioration": e.deterioration}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., dict[str, Any]]
    mutates: bool = False   # every registered tool must be read-only


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
            rows.append({"account_ref": ref, **{k: s[k] for k in ("collateral_value", "loan_balance", "current_ltv",
                                                                  "available_credit", "margin_state")}})
        tot_c = sum(r["collateral_value"] for r in rows)
        tot_b = sum(r["loan_balance"] for r in rows)
        return {"accounts": rows, "totals": {"accounts": len(rows), "collateral_value": round(tot_c, 2),
                                             "loan_balance": round(tot_b, 2),
                                             "book_ltv": round(tot_b / tot_c, 4) if tot_c else None}}

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
        refs = account_refs or self.book.account_refs()
        rows, tot = [], {"base_collateral": 0.0, "stressed_collateral": 0.0, "balance": 0.0,
                         "base_available": 0.0, "stressed_available": 0.0, "base_cure": 0.0, "stressed_cure": 0.0}
        for ref in refs:
            v = self.book.account(ref)
            b = _summary(self._eval(v))
            sv, hit = apply(v, sc, sectors)
            s = _summary(_evaluate(sv, self.n_paths))
            rows.append({"account_ref": ref, "shocked_holdings": hit,
                         "base": {k: b[k] for k in ("collateral_value", "current_ltv", "available_credit",
                                                    "margin_state", "cure_amount",
                                                    "p_recovery_shortfall_if_recovery_after_grace")},
                         "stressed": {k: s[k] for k in ("collateral_value", "current_ltv", "available_credit",
                                                        "margin_state", "cure_amount",
                                                        "p_recovery_shortfall_if_recovery_after_grace")}})
            tot["base_collateral"] += b["collateral_value"]
            tot["stressed_collateral"] += s["collateral_value"]
            tot["balance"] += b["loan_balance"]
            tot["base_available"] += b["available_credit"]
            tot["stressed_available"] += s["available_credit"]
            tot["base_cure"] += b["cure_amount"]
            tot["stressed_cure"] += s["cure_amount"]
        states = [r["stressed"]["margin_state"] for r in rows]
        return {"scenario": sc.describe(), "accounts": rows,
                "book": {**{k: round(x, 2) for k, x in tot.items()},
                         "base_book_ltv": round(tot["balance"] / tot["base_collateral"], 4) if tot["base_collateral"] else None,
                         "stressed_book_ltv": round(tot["balance"] / tot["stressed_collateral"], 4) if tot["stressed_collateral"] else None,
                         "accounts_not_safe_after_stress": sum(1 for x in states if x != "safe"),
                         "stressed_margin_states": {st: states.count(st) for st in sorted(set(states))}},
                "limitations": ["instantaneous shock; no path between now and the shocked state",
                                "sector membership from the book's instrument reference data",
                                "volume and settlement stress act through RC1's liquidity and timing model"]}

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
                "caution": "recovery-conditional view; appetite presets are illustrative research settings, "
                           "not Tiva's production risk appetite"}

    def deteriorating_assets(self, top: int = 10) -> dict[str, Any]:
        agg: dict[str, dict[str, Any]] = {}
        for ref in self.book.account_refs():
            e = self._eval(self.book.account(ref))
            for a in e.asset_results:
                if a.asset_id == "USD" or a.market_value <= 0:
                    continue
                r = agg.setdefault(a.asset_id, {"instrument": a.asset_id, "exposure": 0.0, "accounts": 0,
                                                "risk_score": a.risk_score, "data_status": a.data_status})
                r["exposure"] += a.market_value
                r["accounts"] += 1
                r["risk_score"] = max(r["risk_score"], a.risk_score)
        rows = sorted(agg.values(), key=lambda r: -r["risk_score"])[:max(1, min(top, 50))]
        for r in rows:
            r["exposure"] = round(r["exposure"], 2)
            r["risk_score"] = round(r["risk_score"], 4)
        return {"ranking": "RC1 per-instrument risk score (higher = riskier), exposure across the book",
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
                                                "account_refs": {"type": "array", "maxItems": 200, "items": acct}}},
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
