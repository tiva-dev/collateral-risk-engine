from __future__ import annotations

import unittest

from app.api.routes import check_credit_draw, monitor_loan, originate_credit, pre_trade_check
from app.api.schemas import DrawCheckRequest, MonitorRequest, OriginateRequest, PreTradeCheckRequest
from app.core.enums import LifecycleDecisionValue, RiskDecision


BASE_LTV = {
    "cash": 0.95,
    "bond": 0.80,
    "bond_fund": 0.78,
    "etf": 0.70,
    "listed_equity": 0.65,
    "high_volatility_equity": 0.35,
    "crypto": 0.20,
    "option": 0.05,
    "private_asset": 0.0,
    "other": 0.0,
}


def policy_payload() -> dict:
    return {"base_ltv": BASE_LTV}


def holdings_payload(quantity: float = 100.0) -> list[dict]:
    return [{"asset_id": "SPY", "asset_type": "etf", "quantity": quantity, "currency": "USD"}]


def market_payload() -> dict:
    return {
        "SPY": {
            "asset_id": "SPY",
            "last_price": 100.0,
            "bid": 99.90,
            "ask": 100.10,
            "average_daily_volume": 1_000_000,
            "average_dollar_volume": 100_000_000,
            "volatility_30d": 0.15,
            "volatility_90d": 0.15,
            "data_quality_score": 1.0,
            "order_book": {"bids": [{"price": 99.90, "quantity": 10_000}]},
        }
    }


class LifecycleEndpointTests(unittest.TestCase):
    def test_credit_origination_endpoint(self) -> None:
        response = originate_credit(
            OriginateRequest.model_validate(
                {
                    "account_ref": "acct_api_origin",
                    "policy": policy_payload(),
                    "holdings": holdings_payload(),
                    "market_data": market_payload(),
                }
            )
        )

        self.assertEqual(response.result.decision, LifecycleDecisionValue.APPROVED)
        self.assertEqual(response.result.current_outstanding_balance, 0.0)
        self.assertEqual(response.result.projected_outstanding_balance, 0.0)
        self.assertGreater(response.result.approved_credit_limit, 0.0)

    def test_draw_check_endpoint(self) -> None:
        response = check_credit_draw(
            DrawCheckRequest.model_validate(
                {
                    "account_ref": "acct_api_draw",
                    "current_loan": {"principal": 1_000.0, "accrued_interest": 0.0, "fees": 0.0},
                    "requested_draw_amount": 500.0,
                    "policy": policy_payload(),
                    "holdings": holdings_payload(),
                    "market_data": market_payload(),
                }
            )
        )

        self.assertEqual(response.result.decision, LifecycleDecisionValue.APPROVED)
        self.assertEqual(response.result.current_outstanding_balance, 1_000.0)
        self.assertEqual(response.result.projected_outstanding_balance, 1_500.0)
        self.assertIsNotNone(response.result.projected_available_credit)

    def test_loan_monitor_endpoint(self) -> None:
        response = monitor_loan(
            MonitorRequest.model_validate(
                {
                    "account_ref": "acct_api_monitor",
                    "loan": {"principal": 1_000.0, "accrued_interest": 0.0, "fees": 0.0},
                    "policy": policy_payload(),
                    "holdings": holdings_payload(),
                    "market_data": market_payload(),
                }
            )
        )

        self.assertEqual(response.result.decision, LifecycleDecisionValue.SAFE)
        self.assertEqual(response.result.current_outstanding_balance, 1_000.0)
        self.assertEqual(response.result.projected_outstanding_balance, 1_000.0)
        self.assertEqual(response.result.projected_margin_state.value, "safe")

    def test_pre_trade_endpoint_reject_and_reduced_available_credit_contract(self) -> None:
        reject_response = pre_trade_check(
            PreTradeCheckRequest.model_validate(
                {
                    "account_ref": "acct_api_pretrade_reject",
                    "loan": {"principal": 1_000.0, "accrued_interest": 0.0, "fees": 0.0},
                    "policy": policy_payload(),
                    "holdings": holdings_payload(),
                    "market_data": market_payload(),
                    "proposed_holding_changes": [
                        {"asset_id": "SPY", "asset_type": "etf", "quantity": -200.0, "currency": "USD"}
                    ],
                }
            )
        )
        self.assertEqual(reject_response.result.decision, RiskDecision.REJECT)
        self.assertIsNone(reject_response.result.reduced_available_credit)
        self.assertIsNone(reject_response.result.projected_available_credit)

        reduce_response = pre_trade_check(
            PreTradeCheckRequest.model_validate(
                {
                    "account_ref": "acct_api_pretrade_reduce",
                    "loan": {"principal": 1_000.0, "accrued_interest": 0.0, "fees": 0.0},
                    "policy": policy_payload(),
                    "holdings": holdings_payload(),
                    "market_data": market_payload(),
                    "proposed_holding_changes": [
                        {"asset_id": "SPY", "asset_type": "etf", "quantity": -10.0, "currency": "USD"}
                    ],
                }
            )
        )
        self.assertEqual(reduce_response.result.decision, RiskDecision.REDUCE_AVAILABLE_CREDIT)
        self.assertEqual(reduce_response.result.reduced_available_credit, reduce_response.result.projected_available_credit)

        approve_response = pre_trade_check(
            PreTradeCheckRequest.model_validate(
                {
                    "account_ref": "acct_api_pretrade_approve",
                    "loan": {"principal": 1_000.0, "accrued_interest": 0.0, "fees": 0.0},
                    "policy": policy_payload(),
                    "holdings": holdings_payload(),
                    "market_data": market_payload(),
                    "proposed_holding_changes": [],
                }
            )
        )
        self.assertEqual(approve_response.result.decision, RiskDecision.APPROVE)
        self.assertIsNone(approve_response.result.reduced_available_credit)


if __name__ == "__main__":
    unittest.main()
