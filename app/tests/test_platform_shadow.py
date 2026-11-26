"""Shadow models and backtests on the platform (SQLite; Postgres in Platform CI)."""

from __future__ import annotations

import json
import math
import os
import random
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

try:
    import numpy  # noqa: F401
    import scipy  # noqa: F401

    HAVE_NUMPY = True
except ImportError:  # pragma: no cover
    HAVE_NUMPY = False

from app.platform import backtest, codec, shadow
from app.platform.context import Principal, reset_principal, set_principal
from app.platform.db import Database
from app.platform.keys import create_tenant
from app.platform.schema import migrate
from app.platform.tenancy import TenantServices
from app.tests.test_platform import _engine, _register, _service


def _synthetic_bars(db: Database, key: str, n: int = 220, seed: int = 7) -> None:
    rng = random.Random(seed)
    price = 100.0
    d = date(2025, 9, 1)
    with db.transaction("*") as conn:
        for _ in range(n):
            while d.weekday() >= 5:
                d += timedelta(days=1)
            price *= math.exp(rng.gauss(0, 0.012))
            conn.execute(
                "INSERT INTO market_bars (stable_key, bar_date, open, high, low, close, volume, range_reported, "
                "volume_reported, provider, ingested_at) VALUES (%s,%s,%s,%s,%s,%s,%s,1,1,'test',%s)",
                (key, d.isoformat(), price, price * 1.006, price * 0.994, price, 2_000_000.0,
                 datetime.now(UTC).isoformat()))
            d += timedelta(days=1)


@unittest.skipUnless(HAVE_NUMPY, "numpy/scipy unavailable")
class ShadowTests(unittest.TestCase):
    url = os.environ.get("CRI_TEST_DATABASE_URL")

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(self.url or f"sqlite:///{Path(self.tmp.name) / 's.sqlite3'}")
        if self.db.dialect == "postgres":
            from app.tests.test_platform import _reset_postgres

            _reset_postgres(self.db)
        migrate(self.db)
        with self.db.transaction("*") as conn:
            create_tenant(conn, "alpha", "Alpha", "client")
        self.services = TenantServices(self.db, _engine(), None, None)
        token = set_principal(Principal("alpha", "system", "t"))
        try:
            with self.db.transaction("alpha"):
                _register(_service("alpha"), loan=4_000.0)
        finally:
            reset_principal(token)
        # The test account's SPY holding has asset_id "SPY" (no exchange), so
        # its stable key is UNKNOWN:SPY:USD:ETF.
        _synthetic_bars(self.db, "UNKNOWN:SPY:USD:ETF")

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_all_models_evaluate_and_persist_without_touching_account_state(self) -> None:
        with self.db.transaction("alpha"):
            before = codec.dumps(self.services.service("alpha").account_repo.get("acct"))
        # research models need an exchange profile; give the test instrument one
        from app.markets import profiles

        profiles.PROFILES["US"] = profiles.US.__class__(**{**profiles.US.to_dict(),
                                                           "exchanges": (*profiles.US.exchanges, "UNKNOWN")})
        try:
            out = shadow.run_all(self.db, self.services)
        finally:
            profiles.PROFILES["US"] = profiles.US
        self.assertEqual(out["accounts"], 1)
        with self.db.transaction("alpha"):
            rows = shadow.latest_by_account("alpha", "acct")
            after = codec.dumps(self.services.service("alpha").account_repo.get("acct"))
        by_model = {r["model_id"]: r for r in rows}
        self.assertEqual(set(by_model), set(shadow.MODELS))
        self.assertIsNone(by_model["baseline"]["error"])
        self.assertIsNone(by_model["cre_v070"]["error"], by_model["cre_v070"]["error"])
        self.assertEqual(before, after, "shadow evaluation must not change account state")
        base = by_model["baseline"]
        self.assertGreater(base["collateral_value"], 0)
        self.assertTrue(base["explanation"]["reasons"])
        self.assertTrue(base["explanation"]["assets"])

    def test_research_models_explain_capacity_and_ceiling(self) -> None:
        from app.markets import profiles

        profiles.PROFILES["US"] = profiles.US.__class__(**{**profiles.US.to_dict(),
                                                           "exchanges": (*profiles.US.exchanges, "UNKNOWN")})
        try:
            with self.db.transaction("alpha"):
                acct = self.services.service("alpha").account_repo.get("acct")
                inp = shadow.build_inputs(self.services, "alpha", acct, datetime.now(UTC))
                res = {r.model_id: r for r in shadow.evaluate_all(inp, {k: shadow.MODELS[k] for k in ("A", "B_gap")})}
        finally:
            profiles.PROFILES["US"] = profiles.US
        for m in ("A", "B_gap"):
            self.assertIsNone(res[m].error, res[m].error)
            ex = res[m].explanation
            self.assertLessEqual(ex["capacity_ltv"], ex["client_ceiling_ltv"])
            self.assertIn(ex["binding"], {"client ceiling", "method capacity"})
            self.assertGreater(ex["sessions_used"], 100)
        # B_gap adds stress and a gap term, so it never lends more than A here.
        self.assertLessEqual(res["B_gap"].explanation["capacity_ltv_uncapped"],
                             res["A"].explanation["capacity_ltv_uncapped"] + 1e-9)

    def test_backtest_uses_only_past_bars_and_stores_results(self) -> None:
        with self.db.transaction("*") as conn:
            conn.execute("INSERT INTO backtest_runs (run_id, tenant_id, account_ref, requested_by, models, params, "
                         "status, created_at) VALUES ('btr_t','alpha','acct','test',%s,%s,'queued',%s)",
                         (json.dumps(["baseline", "cre_v070"]), json.dumps({"step": 20}),
                          datetime.now(UTC).isoformat()))
        out = backtest.run(self.db, self.services, "btr_t")
        self.assertEqual(set(out["models"]), {"baseline", "cre_v070"})
        self.assertGreater(out["models"]["baseline"]["evaluations"], 2)
        with self.db.transaction("*") as conn:
            row = conn.fetchone("SELECT status, series FROM backtest_runs WHERE run_id='btr_t'")
        self.assertEqual(row["status"], "succeeded")
        series = json.loads(row["series"])
        self.assertTrue(all(p["date"] for p in series))

    def test_disagreement_report(self) -> None:
        rows = [
            {"model_id": "baseline", "account_ref": "a", "risk_state": "margin_call", "approved_credit": 100.0,
             "margin_call": 1, "error": None, "explanation": {"reasons": ["b"]}},
            {"model_id": "A", "account_ref": "a", "risk_state": "safe", "approved_credit": 150.0,
             "margin_call": 0, "error": None, "explanation": {"reasons": ["a"]}},
            {"model_id": "B", "account_ref": "a", "risk_state": "margin_call", "approved_credit": 105.0,
             "margin_call": 1, "error": None, "explanation": {}},
        ]
        d = shadow.disagreements(rows)
        self.assertEqual([x["model_id"] for x in d], ["A"])
        self.assertEqual(len(d[0]["differences"]), 3)


if __name__ == "__main__":
    unittest.main()
