"""Register the staging sandbox test portfolios through the public API.

Loans are sized from the latest ingested prices at a target loan-to-value, so
each portfolio starts in a known state. Every call carries a stable
Idempotency-Key, so re-running the script never duplicates an account.
These are test portfolios for monitoring only; no loan is real.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

US = "listed_equity"
PORTFOLIOS = [
    # ref, loan currency, target LTV, [(stable key prefix, asset type, quantity)]
    ("sbx-us-tsla-concentrated", "USD", 0.40, [("NASDAQ:TSLA:USD", US, 400)]),
    ("sbx-us-diversified", "USD", 0.35, [("NASDAQ:AAPL:USD", US, 300), ("NASDAQ:NVDA:USD", US, 400),
                                          ("NYSE:SPY:USD", "etf", 100)]),
    ("sbx-us-etf", "USD", 0.45, [("NYSE:SPY:USD", "etf", 150), ("NASDAQ:QQQ:USD", "etf", 150)]),
    ("sbx-us-nvda-near-call", "USD", 0.60, [("NASDAQ:NVDA:USD", US, 1000)]),
    ("sbx-ngx-banks", "NGN", 0.30, [("NGX:ZENITHBANK:NGN", US, 200000), ("NGX:GTCO:NGN", US, 150000)]),
    ("sbx-ngx-telco", "NGN", 0.30, [("NGX:MTNN:NGN", US, 20000), ("NGX:AIRTELAFRI:NGN", US, 2000)]),
    ("sbx-ngx-mixed", "NGN", 0.40, [("NGX:DANGCEM:NGN", US, 8000), ("NGX:MTNN:NGN", US, 10000),
                                     ("NGX:GTCO:NGN", US, 50000)]),
]


def stable(prefix: str, asset_type: str) -> str:
    return f"{prefix}:{asset_type.upper()}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--key", required=True, help="sandbox tenant API key")
    ap.add_argument("--admin-key", required=True, help="operator key (reads latest prices)")
    ap.add_argument("--wait-seconds", type=int, default=900)
    args = ap.parse_args()
    client = httpx.Client(base_url=args.url.rstrip("/"), timeout=60.0)
    admin = {"Authorization": f"Bearer {args.admin_key}"}
    sandbox = {"Authorization": f"Bearer {args.key}"}

    needed = {stable(p, t) for _, _, _, legs in PORTFOLIOS for p, t, _ in legs}
    deadline = time.time() + args.wait_seconds
    prices: dict[str, float] = {}
    while True:
        ops = client.get("/admin/operations", headers=admin).raise_for_status().json()
        prices = {q["stable_key"]: float(q["price"]) for q in ops["latest_quotes"]}
        missing = sorted(needed - prices.keys())
        if not missing or time.time() > deadline:
            break
        print(f"waiting for ingested prices: {missing}", flush=True)
        time.sleep(60)

    results = []
    for ref, ccy, ltv, legs in PORTFOLIOS:
        if any(stable(p, t) not in prices for p, t, _ in legs):
            results.append({"account_ref": ref, "skipped": "no ingested price yet"})
            continue
        value = sum(prices[stable(p, t)] * q for p, t, q in legs)
        body = {
            "account_ref": ref,
            "holdings": [{"asset_id": p, "asset_type": t, "quantity": q, "currency": p.split(":")[2],
                          "exchange": p.split(":")[0]} for p, t, q in legs],
            "loan": {"principal": round(value * ltv, 2), "currency": ccy},
            "loan_currency": ccy,
            "policy": {"base_ltv": {"listed_equity": 0.65, "etf": 0.70}},
            "interest_policy": {"annual_interest_rate": 0.25 if ccy == "NGN" else 0.10},
            "data_mode": "provided_by_us",
        }
        r = client.post("/monitoring/accounts", json=body,
                        headers={**sandbox, "Idempotency-Key": f"seed-{ref}-v1"})
        out = {"account_ref": ref, "status": r.status_code, "replayed": r.headers.get("Idempotent-Replayed") == "true",
               "collateral_value": round(value, 2), "loan": body["loan"]["principal"], "currency": ccy}
        if r.status_code == 200:
            acct = r.json().get("account", {})
            out["margin_state"] = acct.get("last_margin_state")
        else:
            out["error"] = r.text[:400]
        results.append(out)

    hooks = client.get("/webhooks/endpoints", headers=sandbox).raise_for_status().json()["endpoints"]
    receiver = f"{args.url.rstrip('/')}/sandbox/webhook-receiver/tiva-sandbox"
    if not any(h["url"] == receiver and h["active"] for h in hooks):
        r = client.post("/webhooks/endpoints", json={"url": receiver, "event_types": ["*"]},
                        headers={**sandbox, "Idempotency-Key": "seed-webhook-receiver-v1"})
        detail = {"endpoint_id": r.json().get("endpoint_id")} if r.status_code < 300 else r.text[:300]
        results.append({"webhook_endpoint": r.status_code, "detail": detail})
    print(json.dumps(results, indent=2))
    return 0 if all("error" not in x for x in results) else 1


if __name__ == "__main__":
    sys.exit(main())
