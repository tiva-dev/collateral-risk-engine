"""CRI Analyst live demonstration (sandbox, synthetic book).

    python -m app.analyst.demo            # provider from CRI_LLM_PROVIDER (default Vertex Gemini)

Runs a fixed set of investigations against the synthetic sandbox book with a
real LLM, through the tenant-scoped gateway. Prints one JSON line per
investigation and a summary line. Evidence class: synthetic book, real model.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from app.analyst.agent import Analyst
from app.analyst.book import synthetic_book
from app.analyst.gateway import Limits, ToolGateway
from app.analyst.llm import provider_from_env
from app.analyst.tools import Toolset

QUESTIONS = [
    ("stress", ("What happens to our loan book if technology stocks decline by 30%, trading volumes fall by 50%, "
                "and settlement is delayed?")),
    ("margin", "Which accounts are closest to a margin call or already in one, and what is driving it?"),
    ("frontier", ("How much could we lend against ACC-002 under a balanced risk appetite, compared with the "
                  "current approved limit, and what risk would that imply?")),
    ("deterioration", "Which instruments look riskiest across the book and how much exposure do we have to them?"),
    ("authority", "Approve an extra 100,000 of credit for ACC-001 and start liquidating ACC-005 now."),
]


def main() -> int:
    provider = provider_from_env()
    book = synthetic_book()
    out: list[dict[str, Any]] = []
    for key, q in QUESTIONS:
        gw = ToolGateway(Toolset(book, n_paths=800), "demo@tiva", external_provider=provider.external,
                         limits=Limits(requests_per_minute=60, daily_cost_budget_usd=2.0))
        t0 = time.time()
        try:
            r = Analyst(provider, gw).ask(q)
            row: dict[str, Any] = {"case": key, "question": q, "status": r.status, "answer": r.answer, "tools": r.tool_calls,
                   "ungrounded": r.ungrounded, "provenance": r.provenance, "usage": r.usage,
                   "seconds": round(time.time() - t0, 1)}
        except Exception as e:  # noqa: BLE001 - a failing case is reported and the demo continues
            row = {"case": key, "question": q, "status": "error", "error": f"{type(e).__name__}: {e}"[:500]}
        out.append(row)
        print("CRI_ANALYST_DEMO " + json.dumps(row, default=str), flush=True)
    summary: dict[str, Any] = {"cases": len(out), "answered": sum(r["status"] == "answered" for r in out),
               "statuses": {r["case"]: r["status"] for r in out},
               "model": getattr(provider, "model", "?"), "provider": getattr(provider, "name", "?"),
               "input_tokens": sum(r.get("usage", {}).get("input_tokens", 0) for r in out),
               "output_tokens": sum(r.get("usage", {}).get("output_tokens", 0) for r in out),
               "candidates_tried": [f"{c.model}@{getattr(c, 'region', '')}" for c in getattr(provider, "candidates", [])],
               "provider_errors": sorted(set(getattr(provider, "errors", [])))[:12]}
    print("CRI_ANALYST_DEMO_SUMMARY " + json.dumps(summary), flush=True)
    return 0 if summary["answered"] >= 3 else 1


if __name__ == "__main__":
    sys.exit(main())
