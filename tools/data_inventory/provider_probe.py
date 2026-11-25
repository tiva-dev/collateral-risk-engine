"""Probe provider endpoints and reference-rate sources (structure only).

Prints response structure for NGNMarket dividend / corporate-action
endpoints, discovers how the CBN exchange-rate page loads its data, extracts
forex-related text from NGNMarket's documentation, and lists NGX one-day
moves above 10% from a dataset manifest (candidates for corporate actions).
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.data_inventory.summarize_payloads import outline  # noqa: E402

KEY = os.environ.get("NGNMARKET_API_KEY", "")
UA = {"User-Agent": "Mozilla/5.0 (CRI data probe)"}


def get(url: str, headers: dict | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:  # nosec B310
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:400]
    except Exception as e:  # noqa: BLE001
        return -1, str(e)


def ngn(path: str) -> None:
    code, body = get("https://api.ngnmarket.com/v1" + path, {"Authorization": f"Bearer {KEY}"})
    print(f"\n### NGNMarket {path} -> HTTP {code}")
    try:
        for line in outline(json.loads(body), 1, 7):
            print(line)
    except json.JSONDecodeError:
        print(body[:300])


def main() -> None:
    ngn("/disclosures/categories")
    ngn("/companies/GTCO/disclosures?category=rights_and_bonus&limit=5")
    ngn("/companies/GTCO/disclosures?limit=3")
    ngn("/market/holidays")
    for url in ("https://www.cbn.gov.ng/api/GetAllNFEM_RatesGRAPH",
                "https://www.cbn.gov.ng/api/GetAllExchangeRatesGRAPH"):
        code, body = get(url)
        print(f"\n### CBN {url} -> HTTP {code}, {len(body)} bytes")
        try:
            data = json.loads(body)
            rows = data if isinstance(data, list) else data.get("data", [])
            print("   rows", len(rows), "first", rows[:2], "last", rows[-2:])
        except Exception as exc:  # noqa: BLE001
            print("   not json", exc, body[:300])
    code, body = get("https://www.cbn.gov.ng/api/GetAllExchangeRates")
    rows = json.loads(body)
    usd = sorted((r for r in rows if "DOLLAR" in r["currency"] and "US" in r["currency"]),
                 key=lambda r: r["ratedate"])
    print("\n### CBN all rates: currencies", sorted({r["currency"] for r in rows}))
    print("   US DOLLAR rows", len(usd), "first", usd[:2], "last", usd[-2:])
    years = {}
    for r in usd:
        years[r["ratedate"][:4]] = years.get(r["ratedate"][:4], 0) + 1
    print("   per year", years)
    manifest = None
    if manifest:
        from app.simulations.run_official_validation import _load_replay_inputs

        bars, _ = _load_replay_inputs(json.loads(Path(manifest).read_text()))
        print("\n### NGX one-day close moves beyond 10%")
        for sym, series in sorted(bars.items()):
            if not series or series[0].currency != "NGN":
                continue
            for a, b in zip(series, series[1:], strict=False):
                if a.close > 0 and b.close > 0:
                    r = b.close / a.close - 1
                    if abs(r) > 0.10:
                        print(f"  {sym:12} {str(b.timestamp)[:10]} {r:+.3f} {a.close} -> {b.close}")


if __name__ == "__main__":
    main()
