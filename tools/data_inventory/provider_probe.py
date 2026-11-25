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
    for sym in ("ZENITHBANK", "GTCO", "UBA", "ACCESSCORP", "DANGSUGAR", "MTNN", "SEPLAT"):
        ngn(f"/companies/{sym}/dividends")
    for path in ("/companies/ZENITHBANK/corporate-actions", "/companies/ZENITHBANK/actions",
                 "/companies/ZENITHBANK/bonus", "/companies/ZENITHBANK/splits",
                 "/companies/ZENITHBANK", "/forex/current"):
        ngn(path)
    for url in ("https://docs.ngnmarket.com/introduction", "https://docs.ngnmarket.com/llms.txt",
                "https://docs.ngnmarket.com/llms-full.txt"):
        code, body = get(url)
        text = re.sub(r"<[^>]+>", " ", body)
        text = re.sub(r"\s+", " ", text)
        hits = [m.start() for m in re.finditer(
            r"(?i)forex/history|exchange rate|rate_date|source:|official|nfem|nafem|parallel|cbn|dividend|bonus|split|adjust", text)]
        print(f"\n### docs {url} -> HTTP {code}, {len(text)} chars, {len(hits)} mentions")
        seen = -1000
        for h in hits:
            if h - seen < 300:
                continue
            seen = h
            print("   ...", text[max(0, h - 200): h + 300])
    for url in ("https://www.cbn.gov.ng/rates/ExchRateByCurrency.html",
                "https://www.cbn.gov.ng/api/GetAllExchangeRates",
                "https://www.cbn.gov.ng/api/GetAllNFEMRates"):
        code, body = get(url)
        refs = sorted(set(re.findall(r"""["'(]([^"'()\s]*(?:api|json|\.asp|ashx|getdata)[^"'()\s]*)["')]""", body, re.I)))
        print(f"\n### CBN {url} -> HTTP {code}, {len(body)} bytes; data refs: {refs[:25]}")
        print("   head:", re.sub(r"\s+", " ", body[:700]))
    manifest = sys.argv[1] if len(sys.argv) > 1 else None
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
