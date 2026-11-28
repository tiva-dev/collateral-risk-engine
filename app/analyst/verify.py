"""Contextual verification of Analyst answers (review finding 1).

A figure is accepted only if some tool value matches it *and* is consistent with
the figure's context in the answer:

* **account**  if the sentence (or table row) names accounts, the value must come
  from one of those accounts' records (or from book-level data when no account
  record matches the metric);
* **metric**   if the sentence names a metric family (LTV, collateral, cure,
  available credit, probability, ...), the value's field must belong to it;
* **unit**     percentages and ratios match ratio fields; amounts (>= 1000 or with
  a currency sign) match amount fields;
* **direction** "rises / increases" cannot be supported only by a decrease
  between base and stressed values, and "falls / declines" not only by an increase;
* **scenario** for stress answers, every percentage shock stated in the question
  must appear in the scenario the stress tool actually ran.

Sentences are split on line breaks, sentence punctuation and table rows; a
table row inherits the metric named in its first cell.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.analyst.agent import _NUM, _parse

FAMILIES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # family: (answer keywords, field-name fragments)
    "ltv": (("ltv", "loan-to-value", "loan to value"), ("ltv",)),
    "collateral": (("collateral", "portfolio value", "market value"), ("collateral", "market_value")),
    "available": (("available credit", "headroom", "undrawn"), ("available",)),
    "limit": (("approved limit", "credit limit", "approved credit", "capacity"),
              ("limit", "capacity")),
    "cure": (("cure",), ("cure",)),
    "balance": (("loan balance", "outstanding", "balance", "debt"), ("balance",)),
    "probability": (("probability", "likelihood", "chance"), ("p_", "probability")),
    "risk_score": (("risk score",), ("risk_score",)),
    "exposure": (("exposure",), ("exposure", "market_value")),
    "slippage": (("slippage", "execution cost"), ("slippage",)),
}
RATIO_FIELDS = ("ltv", "rate", "p_", "probability", "share", "risk_score", "slippage", "change")
AMOUNT_FIELDS = ("value", "balance", "credit", "amount", "limit", "capacity", "exposure", "cure", "collateral",
                 "available", "income", "cost", "loss")
UP = re.compile(r"\b(rise|rises|rising|rose|increase|increases|increased|up|higher|grow|grows|jump|jumps)\b", re.IGNORECASE)
DOWN = re.compile(r"\b(fall|falls|falling|fell|decline|declines|declined|drop|drops|dropped|lower|decrease|"
                  r"decreases|decreased|down|shrink|shrinks)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Leaf:
    value: float
    field: str
    account: str | None
    derived: str = ""        # "" | "change" | "relchange"


def _leaves(obj: Any, field: str = "", account: str | None = None, path: str = "") -> list[tuple[str, Leaf]]:
    out: list[tuple[str, Leaf]] = []
    if isinstance(obj, dict):
        acct = obj.get("account_ref") if isinstance(obj.get("account_ref"), str) else account
        for k, v in obj.items():
            out += _leaves(v, str(k), acct, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += _leaves(v, field, account, f"{path}[{i}]")
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.append((path, Leaf(float(obj), field, account)))
    return out


def tool_leaves(results: list[dict[str, Any]]) -> list[Leaf]:
    leaves: list[Leaf] = []
    for r in results:
        pairs = _leaves(r.get("data", {}))
        by_path = dict(pairs)
        leaves += [lf for _, lf in pairs]
        for p, lf in pairs:
            for a, b in ((".base.", ".stressed."), ("base_", "stressed_")):
                if a in p and p.replace(a, b) in by_path:
                    s = by_path[p.replace(a, b)]
                    leaves.append(Leaf(s.value - lf.value, lf.field.replace("base_", ""), lf.account, "change"))
                    if lf.value:
                        leaves.append(Leaf((s.value - lf.value) / lf.value, lf.field.replace("base_", ""),
                                           lf.account, "relchange"))
    return leaves


def _family_of(field: str) -> set[str]:
    f = field.lower()
    return {fam for fam, (_, frags) in FAMILIES.items() if any(fr in f for fr in frags)}


def _segments(answer: str) -> list[str]:
    segs: list[str] = []
    for line in answer.splitlines():
        if line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            segs.append(" | ".join(cells))
        else:
            segs += [s for s in re.split(r"(?<=[.;!?])\s+(?=[A-Z*(])", line) if s.strip()]
    return segs


def _matches(x: float, dec: int, unit: str, leaf: Leaf) -> bool:
    f = leaf.field.lower()
    is_ratio = any(k in f for k in RATIO_FIELDS) or leaf.derived == "relchange"
    is_amount = any(k in f for k in AMOUNT_FIELDS) and leaf.derived != "relchange"
    v = leaf.value
    if unit in ("%", "pp"):
        if not is_ratio:
            return False
        c = x / 100
        tol = 0.5 * 10 ** (-dec) / 100
    else:
        c = x
        tol = 0.5 * 10 ** (-dec)
        if unit in ("k", "K", "m", "M", "bn"):
            return is_amount and abs(abs(v) - c) <= 0.05 * abs(c)
        if abs(c) >= 1000 and not is_amount:
            return False
    return abs(abs(v) - abs(c)) <= tol + 1e-12 or (abs(c) >= 1000 and abs(abs(v) - abs(c)) <= 0.005 * abs(c))


def verify_answer(answer: str, results: list[dict[str, Any]], question: str,
                  tool_calls: list[dict[str, Any]], accounts: list[str]) -> list[str]:
    issues: list[str] = []
    leaves = tool_leaves(results)
    question_vals = {p[0] for m in _NUM.finditer(question) if (p := _parse(m.group()))}
    for seg in _segments(answer):
        low = seg.lower()
        named = {a for a in accounts if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(a)}(?![A-Za-z0-9_-])", seg)}
        first_cell = seg.split(" | ")[0].lower() if " | " in seg else low
        fams = {fam for fam, (kws, _) in FAMILIES.items() if any(k in first_cell for k in kws)}
        for m in _NUM.finditer(seg):
            tok = m.group().strip()
            p = _parse(tok)
            if p is None:
                continue
            x, dec, unit = p
            if unit == "" and dec == 0 and abs(x) <= 12:
                continue                                            # counts, numbering, sessions
            if x in question_vals:
                continue                                            # restating the question
            cands = [lf for lf in leaves if _matches(x, dec, unit, lf)]
            if not cands:
                issues.append(f"{tok}: not found in any tool result")
                continue
            if named:
                in_acct = [c for c in cands if c.account in named]
                book = [c for c in cands if c.account is None]
                if in_acct:
                    cands = in_acct
                elif book and not any(c.account for c in cands):
                    cands = book
                else:
                    issues.append(f"{tok}: value belongs to a different account than {', '.join(sorted(named))}")
                    continue
            if fams:
                fit = [c for c in cands if _family_of(c.field) & fams]
                if not fit:
                    issues.append(f"{tok}: value is not a {'/'.join(sorted(fams))} figure")
                    continue
                cands = fit
            signed = tok.lstrip().startswith(("-", "+"))
            changes = [c for c in cands if c.derived]
            if changes and len(changes) == len(cands) and not signed:
                if UP.search(seg) and not DOWN.search(seg) and all(c.value < 0 for c in changes):
                    issues.append(f"{tok}: stated as an increase but the tool reports a decrease")
                elif DOWN.search(seg) and not UP.search(seg) and all(c.value > 0 for c in changes):
                    issues.append(f"{tok}: stated as a decrease but the tool reports an increase")
    issues += _scenario_issues(question, tool_calls)
    return issues


def _scenario_issues(question: str, tool_calls: list[dict[str, Any]]) -> list[str]:
    stress = [c for c in tool_calls if c.get("tool") == "stress_test" and c.get("ok")]
    if not stress:
        return []
    applied: set[float] = set()
    for c in stress:
        sc = (c.get("args") or {}).get("scenario") or {}
        applied |= {round(abs(float(s.get("change", 0))), 4) for s in sc.get("price_shocks", [])}
        if sc.get("volume_change") is not None:
            applied.add(round(abs(float(sc["volume_change"])), 4))
    out = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s?%", question):
        v = round(float(m.group(1)) / 100, 4)
        if v not in applied:
            out.append(f"scenario: the question's {m.group(0)} shock is not in the scenario that was run")
    return out
