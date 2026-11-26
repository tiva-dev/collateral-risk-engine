"""Load test for a CRI environment's API (run from GitHub Actions).

Creates (or reuses) a dedicated sandbox tenant ``loadtest``, registers N
accounts on live ingested instruments, then drives read and evaluation traffic
at increasing concurrency and reports latency percentiles and error rates.
Finally the tenant is suspended so it adds no ongoing monitoring load.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time

import httpx

INSTRUMENTS = [("NASDAQ:AAPL:USD", "listed_equity"), ("NASDAQ:NVDA:USD", "listed_equity"),
               ("NYSE:SPY:USD", "etf"), ("NASDAQ:QQQ:USD", "etf"), ("NASDAQ:TSLA:USD", "listed_equity")]


def pctl(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(p * (len(xs) - 1)))]


async def drive(client: httpx.AsyncClient, reqs, concurrency: int, seconds: float) -> dict:
    lat: list[float] = []
    errors = 0
    status: dict[int, int] = {}
    stop = time.monotonic() + seconds
    i = 0

    async def worker() -> None:
        nonlocal errors, i
        while time.monotonic() < stop:
            method, path, kw = reqs[i % len(reqs)]
            i += 1
            t = time.perf_counter()
            try:
                r = await client.request(method, path, **kw)
                status[r.status_code] = status.get(r.status_code, 0) + 1
                if r.status_code >= 500:
                    errors += 1
            except httpx.HTTPError:
                errors += 1
            lat.append((time.perf_counter() - t) * 1000)

    await asyncio.gather(*[worker() for _ in range(concurrency)])
    return {"concurrency": concurrency, "requests": len(lat), "rps": round(len(lat) / seconds, 1),
            "p50_ms": round(pctl(lat, 0.5), 1), "p95_ms": round(pctl(lat, 0.95), 1),
            "p99_ms": round(pctl(lat, 0.99), 1), "mean_ms": round(statistics.fmean(lat), 1) if lat else None,
            "errors_5xx_or_network": errors, "status": status}


async def main_async(a: argparse.Namespace) -> dict:
    out: dict = {}
    admin = {"Authorization": f"Bearer {a.admin_key}"}
    async with httpx.AsyncClient(base_url=a.url, timeout=60.0) as c:
        r = await c.post("/admin/tenants", headers={**admin, "Idempotency-Key": "loadtest-tenant-v1"},
                         json={"tenant_id": "loadtest", "name": "Load test", "kind": "sandbox"})
        if r.status_code == 201:
            key = r.json()["api_key"]
        else:
            k = await c.post("/admin/tenants/loadtest/keys", headers={**admin, "Idempotency-Key": f"lt-key-{time.time()}"},
                             json={"role": "client", "label": "loadtest"})
            key = k.json()["api_key"]
        await c.post("/admin/tenants/loadtest/status", headers={**admin, "Idempotency-Key": f"lt-on-{time.time()}"},
                     json={"value": "active", "reason": "load test start (automated)"})
        h = {"Authorization": f"Bearer {key}"}
        t0 = time.perf_counter()
        reg_lat = []
        for n in range(a.accounts):
            aid, at = INSTRUMENTS[n % len(INSTRUMENTS)]
            body = {"account_ref": f"lt-{n:04d}", "holdings": [{"asset_id": aid, "asset_type": at, "quantity": 100 + n,
                                                                 "currency": "USD", "exchange": aid.split(":")[0]}],
                    "loan": {"principal": 1000.0 + n, "currency": "USD"}, "loan_currency": "USD",
                    "policy": {"base_ltv": {"listed_equity": 0.5, "etf": 0.6}}, "data_mode": "provided_by_us"}
            s = time.perf_counter()
            rr = await c.post("/monitoring/accounts", headers={**h, "Idempotency-Key": f"lt-acct-{n}"}, json=body)
            reg_lat.append((time.perf_counter() - s) * 1000)
            if rr.status_code not in (200, 201):
                out.setdefault("registration_errors", []).append(rr.text[:200])
        out["registration"] = {"accounts": a.accounts, "seconds": round(time.perf_counter() - t0, 1),
                               "p50_ms": round(pctl(reg_lat, 0.5), 1), "p95_ms": round(pctl(reg_lat, 0.95), 1)}
        reqs = [("GET", "/health", {}),
                ("GET", "/monitoring/accounts", {"headers": h}),
                ("GET", "/monitoring/accounts/lt-0001", {"headers": h}),
                ("GET", "/explain/accounts/lt-0002", {"headers": h}),
                ("GET", "/monitoring/events?limit=20", {"headers": h})]
        out["mixed_read_and_explain"] = [await drive(c, reqs, conc, a.seconds) for conc in (5, 20, 50)]
        out["health_only"] = [await drive(c, [("GET", "/health", {})], 50, a.seconds / 2)]
        await c.post("/admin/tenants/loadtest/status", headers={**admin, "Idempotency-Key": f"lt-off-{time.time()}"},
                     json={"value": "suspended", "reason": "load test finished (automated)"})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--admin-key", required=True)
    ap.add_argument("--accounts", type=int, default=100)
    ap.add_argument("--seconds", type=float, default=45.0)
    print(json.dumps(asyncio.run(main_async(ap.parse_args())), indent=2))


if __name__ == "__main__":
    main()
