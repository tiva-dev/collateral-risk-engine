from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api import routes
from app.core.evaluator import CollateralRiskEngine
from app.examples.client_api_walkthrough import (
    client_quote_payload,
    direct_market_data_payload,
    holding_payload,
    interest_policy_payload,
    policy_payload,
)
from app.lifecycle.service import CreditLifecycleEngine
from app.main import app
from app.monitoring.market_updates import InMemoryMarketDataCache
from app.monitoring.repositories import (
    InMemoryMonitoredAccountRepository,
    InMemoryMonitoringEventRepository,
)
from app.monitoring.service import MonitoringService


class ClientApiJourneyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.event_repo = InMemoryMonitoringEventRepository()
        self.monitoring_service = MonitoringService(
            account_repo=InMemoryMonitoredAccountRepository(),
            event_repo=self.event_repo,
            lifecycle_engine=CreditLifecycleEngine(
                CollateralRiskEngine(audit_logger=None), audit_logger=None
            ),
            market_data_cache=InMemoryMarketDataCache(),
            audit_logger=None,
        )
        self.route_patch = patch.multiple(
            routes,
            monitoring_service=self.monitoring_service,
            monitoring_event_repo=self.event_repo,
        )
        self.route_patch.start()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.route_patch.stop()

    def test_client_can_complete_the_full_http_lifecycle(self) -> None:
        account_ref = "client-journey"
        timestamp = datetime.now(UTC).isoformat()
        origination = self.client.post(
            "/credit/originate",
            json={
                "account_ref": account_ref,
                "policy": policy_payload(),
                "holdings": [holding_payload()],
                "market_data": {
                    "SPY": direct_market_data_payload(100.0, timestamp)
                },
                "loan_terms": interest_policy_payload(),
            },
        )
        self.assertEqual(origination.status_code, 200, origination.text)
        approved_limit = origination.json()["result"]["approved_credit_limit"]
        self.assertGreater(approved_limit, 0.0)

        registration = self.client.post(
            "/monitoring/accounts",
            json={
                "account_ref": account_ref,
                "holdings": [holding_payload()],
                "loan": {"principal": 0.0, "currency": "USD"},
                "loan_currency": "USD",
                "policy": policy_payload(),
                "interest_policy": interest_policy_payload(),
                "data_mode": "client_supplied",
                "client_supplied_quotes": {
                    "SPY": client_quote_payload(100.0, timestamp)
                },
            },
        )
        self.assertEqual(registration.status_code, 200, registration.text)

        draw_amount = round(approved_limit * 0.50, 2)
        draw = self.client.post(
            f"/monitoring/accounts/{account_ref}/draws",
            json={"amount": draw_amount, "draw_reference": "draw-1"},
        )
        self.assertEqual(draw.status_code, 200, draw.text)
        self.assertEqual(draw.json()["account"]["loan"]["principal"], draw_amount)

        duplicate_draw = self.client.post(
            f"/monitoring/accounts/{account_ref}/draws",
            json={"amount": draw_amount, "draw_reference": "draw-1"},
        )
        self.assertEqual(duplicate_draw.status_code, 200, duplicate_draw.text)
        self.assertEqual(
            duplicate_draw.json()["account"]["loan"]["principal"], draw_amount
        )

        market_update = self.client.post(
            "/monitoring/market-data/update",
            json={
                "quote_updates": {
                    "SPY": client_quote_payload(35.0, datetime.now(UTC).isoformat())
                },
                "source": "client_test",
                "trigger_tick": True,
            },
        )
        self.assertEqual(market_update.status_code, 200, market_update.text)
        self.assertEqual(market_update.json()["affected_accounts"], [account_ref])

        repayment = self.client.post(
            f"/monitoring/accounts/{account_ref}/repayments",
            json={"amount": 100.0, "repayment_reference": "repay-1"},
        )
        self.assertEqual(repayment.status_code, 200, repayment.text)
        balance_after_repayment = repayment.json()["account"]["loan"]["principal"]
        self.assertEqual(balance_after_repayment, round(draw_amount - 100.0, 2))

        fill = self.client.post(
            f"/monitoring/accounts/{account_ref}/liquidation/fills",
            json={
                "execution_reference": "fill-1",
                "fills": [
                    {
                        "asset_id": "SPY",
                        "quantity": 1.0,
                        "execution_price": 34.90,
                        "fees": 0.10,
                    }
                ],
            },
        )
        self.assertEqual(fill.status_code, 200, fill.text)
        self.assertEqual(fill.json()["account"]["holdings"][0]["quantity"], 99.0)
        self.assertLess(
            fill.json()["account"]["loan"]["principal"], balance_after_repayment
        )

        events = self.client.get(
            "/monitoring/events", params={"account_ref": account_ref}
        )
        self.assertEqual(events.status_code, 200, events.text)
        event_types = {event["event_type"] for event in events.json()["events"]}
        self.assertIn("draw_applied", event_types)
        self.assertIn("repayment_applied", event_types)

        account = self.client.get(f"/monitoring/accounts/{account_ref}")
        self.assertEqual(account.status_code, 200, account.text)
        self.assertEqual(account.json()["account"]["account_ref"], account_ref)

    def test_swagger_is_usable_with_api_key_authentication_enabled(self) -> None:
        with patch.dict(os.environ, {"CRI_API_KEYS": "client-a:test-key"}):
            self.assertEqual(self.client.get("/docs").status_code, 200)
            self.assertEqual(self.client.get("/openapi.json").status_code, 200)
            self.assertEqual(self.client.get("/health").status_code, 200)
            self.assertEqual(self.client.get("/monitoring/accounts").status_code, 401)
            authorized = self.client.get(
                "/monitoring/accounts",
                headers={"X-CRI-API-Key": "test-key"},
            )
            self.assertEqual(authorized.status_code, 200, authorized.text)

        schema = self.client.get("/openapi.json").json()
        scheme = schema["components"]["securitySchemes"]["CriApiKey"]
        self.assertEqual(scheme["name"], "X-CRI-API-Key")
        self.assertEqual(
            schema["paths"]["/credit/originate"]["post"]["security"],
            [{"CriApiKey": []}],
        )
        self.assertNotIn("security", schema["paths"]["/health"]["get"])

    def test_client_cannot_set_cri_liquidity_participation(self) -> None:
        timestamp = datetime.now(UTC).isoformat()
        response = self.client.post(
            "/monitoring/accounts",
            json={
                "account_ref": "client-liquidity-control",
                "holdings": [holding_payload()],
                "loan": {"principal": 0.0, "currency": "USD"},
                "loan_currency": "USD",
                "policy": policy_payload(),
                "data_mode": "client_supplied",
                "client_supplied_quotes": {
                    "SPY": client_quote_payload(100.0, timestamp)
                },
                "liquidation_execution_policy": {
                    "max_participation_rate": 0.50
                },
            },
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("extra_forbidden", response.text)


if __name__ == "__main__":
    unittest.main()
