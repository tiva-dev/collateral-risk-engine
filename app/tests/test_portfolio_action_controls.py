from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.api.routes import check_portfolio_action
from app.api.schemas import PortfolioActionCheckRequest
from app.audit.logger import AuditLogger
from app.core.enums import AssetType, MarginState, PortfolioActionType, RiskDecision
from app.core.evaluator import CollateralRiskEngine
from app.core.models import (
    AccountState,
    Holding,
    Loan,
    MarketData,
    Policy,
    PortfolioActionCheck,
)
from app.lifecycle.service import CreditLifecycleEngine, pledged_cash_asset_id


class PortfolioActionControlsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lifecycle = CreditLifecycleEngine(
            CollateralRiskEngine(audit_logger=None), audit_logger=None
        )
        self.policy = Policy.default()
        self.market_data = {
            "SPY": MarketData(
                asset_id="SPY",
                last_price=100.0,
                bid=99.90,
                ask=100.10,
                average_daily_volume=1_000_000,
                average_dollar_volume=100_000_000,
                volatility_30d=0.15,
                volatility_90d=0.15,
                data_quality_score=1.0,
            ),
            "BND": MarketData(
                asset_id="BND",
                last_price=100.0,
                bid=99.95,
                ask=100.05,
                average_daily_volume=1_000_000,
                average_dollar_volume=100_000_000,
                volatility_30d=0.05,
                volatility_90d=0.05,
                data_quality_score=1.0,
            ),
            "NVDA": MarketData(
                asset_id="NVDA",
                last_price=100.0,
                bid=99.00,
                ask=101.00,
                average_daily_volume=1_000_000,
                average_dollar_volume=100_000_000,
                volatility_30d=0.80,
                volatility_90d=0.80,
                data_quality_score=1.0,
            ),
        }

    def account(
        self, loan_principal: float = 2_500.0, pledged_cash: float = 0.0
    ) -> AccountState:
        return AccountState(
            account_ref="acct_action",
            holdings=[Holding("SPY", AssetType.ETF, 100.0)],
            pledged_cash_balance=pledged_cash,
            loan=Loan(principal=loan_principal),
            approved_credit_limit=0.0,
            available_credit=0.0,
            last_margin_state=MarginState.SAFE,
        )

    def check(self, account: AccountState, action: PortfolioActionCheck):
        return self.lifecycle.check_portfolio_action(
            account, action, self.policy, self.market_data
        )

    def test_sell_asset_and_keep_proceeds_as_pledged_cash_is_approved(self) -> None:
        result = self.check(
            self.account(),
            PortfolioActionCheck(
                PortfolioActionType.SELL, asset_id="SPY", quantity=40.0
            ),
        )

        self.assertEqual(result.decision, RiskDecision.APPROVE)
        self.assertEqual(result.projected_margin_state, MarginState.SAFE)
        self.assertEqual(result.projected_account_state.pledged_cash_balance, 3_996.0)

    def test_sell_asset_and_withdraw_proceeds_is_rejected_when_remaining_collateral_is_unsafe(
        self,
    ) -> None:
        result = self.check(
            self.account(),
            PortfolioActionCheck(
                PortfolioActionType.SELL,
                asset_id="SPY",
                quantity=80.0,
                withdraw_proceeds=True,
            ),
        )

        self.assertEqual(result.decision, RiskDecision.LIQUIDATION)
        self.assertEqual(result.projected_margin_state, MarginState.LIQUIDATION)
        self.assertGreater(result.required_repayment_amount, 0.0)

    def test_buy_with_enough_pledged_cash_is_approved(self) -> None:
        result = self.check(
            self.account(loan_principal=0.0, pledged_cash=1_500.0),
            PortfolioActionCheck(
                PortfolioActionType.BUY,
                asset_id="BND",
                asset_type=AssetType.BOND,
                quantity=10.0,
            ),
        )

        self.assertEqual(result.decision, RiskDecision.APPROVE)
        self.assertEqual(result.projected_margin_state, MarginState.SAFE)
        self.assertEqual(result.projected_account_state.pledged_cash_balance, 499.5)

    def test_buy_without_enough_pledged_cash_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "buy action requires sufficient pledged cash"
        ):
            self.check(
                self.account(loan_principal=1_000.0, pledged_cash=100.0),
                PortfolioActionCheck(
                    PortfolioActionType.BUY,
                    asset_id="BND",
                    asset_type=AssetType.BOND,
                    quantity=10.0,
                ),
            )

    def test_buy_with_explicit_draw_funding_is_projected(self) -> None:
        result = self.check(
            self.account(loan_principal=1_000.0, pledged_cash=100.0),
            PortfolioActionCheck(
                PortfolioActionType.BUY,
                asset_id="BND",
                asset_type=AssetType.BOND,
                quantity=10.0,
                funding_source="draw",
            ),
        )

        self.assertEqual(result.projected_loan_balance, 1_900.5)
        self.assertEqual(result.projected_account_state.pledged_cash_balance, 0.0)

    def test_buy_with_external_cash_funding_is_supported(self) -> None:
        result = self.check(
            self.account(loan_principal=1_000.0, pledged_cash=100.0),
            PortfolioActionCheck(
                PortfolioActionType.BUY,
                asset_id="BND",
                asset_type=AssetType.BOND,
                quantity=10.0,
                funding_source="external_cash",
            ),
        )

        self.assertEqual(result.projected_loan_balance, 1_000.0)
        self.assertEqual(result.projected_account_state.pledged_cash_balance, 0.0)

    def test_buy_riskier_asset_reduces_available_credit_when_safe(self) -> None:
        result = self.check(
            self.account(loan_principal=1_000.0, pledged_cash=1_100.0),
            PortfolioActionCheck(
                PortfolioActionType.BUY,
                asset_id="NVDA",
                asset_type=AssetType.HIGH_VOLATILITY_EQUITY,
                quantity=10.0,
            ),
        )

        self.assertEqual(result.decision, RiskDecision.REDUCE_AVAILABLE_CREDIT)
        self.assertEqual(result.projected_margin_state, MarginState.SAFE)
        self.assertLess(
            result.projected_available_credit,
            result.current_available_credit,
        )

    def test_withdraw_securities_while_owing_is_rejected_below_safety(self) -> None:
        result = self.check(
            self.account(),
            PortfolioActionCheck(
                PortfolioActionType.WITHDRAW_SECURITY, asset_id="SPY", quantity=70.0
            ),
        )

        self.assertEqual(result.decision, RiskDecision.MARGIN_CALL)
        self.assertEqual(result.projected_margin_state, MarginState.MARGIN_CALL)

    def test_repay_loan_reduces_projected_balance_and_improves_safety(self) -> None:
        result = self.check(
            self.account(loan_principal=2_500.0),
            PortfolioActionCheck(PortfolioActionType.REPAY, amount=1_500.0),
        )

        self.assertEqual(result.decision, RiskDecision.APPROVE)
        self.assertEqual(result.projected_loan_balance, 1_000.0)
        self.assertEqual(result.projected_margin_state, MarginState.SAFE)

    def test_post_action_margin_call_is_returned(self) -> None:
        result = self.check(
            self.account(),
            PortfolioActionCheck(
                PortfolioActionType.WITHDRAW_SECURITY, asset_id="SPY", quantity=70.0
            ),
        )

        self.assertEqual(result.decision, RiskDecision.MARGIN_CALL)
        self.assertEqual(result.projected_margin_state, MarginState.MARGIN_CALL)

    def test_post_action_liquidation_is_returned(self) -> None:
        result = self.check(
            self.account(),
            PortfolioActionCheck(
                PortfolioActionType.WITHDRAW_SECURITY, asset_id="SPY", quantity=80.0
            ),
        )

        self.assertEqual(result.decision, RiskDecision.LIQUIDATION)
        self.assertEqual(result.projected_margin_state, MarginState.LIQUIDATION)
        self.assertGreater(result.required_repayment_amount, 0.0)

    def test_draw_action_delegates_to_draw_check_behavior(self) -> None:
        result = self.check(
            self.account(loan_principal=1_000.0),
            PortfolioActionCheck(PortfolioActionType.DRAW, amount=500.0),
        )

        self.assertEqual(result.decision, RiskDecision.APPROVE)
        self.assertIn("delegated to credit draw check", result.reason)
        self.assertEqual(result.projected_loan_balance, 1_500.0)
        self.assertEqual(result.projected_margin_state, MarginState.SAFE)

    def test_portfolio_action_check_endpoint_contract(self) -> None:
        response = check_portfolio_action(
            PortfolioActionCheckRequest.model_validate(
                {
                    "account_state": {
                        "account_ref": "acct_endpoint",
                        "holdings": [
                            {
                                "asset_id": "SPY",
                                "asset_type": "etf",
                                "quantity": 100.0,
                                "currency": "USD",
                            }
                        ],
                        "pledged_cash_balance": 0.0,
                        "loan_principal": 2_500.0,
                        "accrued_interest": 0.0,
                        "fees": 0.0,
                        "loan_currency": "USD",
                        "approved_credit_limit": 0.0,
                        "available_credit": 0.0,
                        "last_margin_state": "safe",
                    },
                    "policy": {
                        "base_ltv": {
                            asset.value: ltv
                            for asset, ltv in self.policy.base_ltv.items()
                        }
                    },
                    "market_data": {
                        "SPY": {
                            "asset_id": "SPY",
                            "last_price": 100.0,
                            "bid": 99.90,
                            "ask": 100.10,
                            "average_daily_volume": 1_000_000,
                            "average_dollar_volume": 100_000_000,
                            "volatility_30d": 0.15,
                            "volatility_90d": 0.15,
                            "timestamp": "2025-01-02T10:00:00+00:00",
                            "data_quality_score": 1.0,
                        }
                    },
                    "proposed_action": {
                        "action_type": "sell",
                        "asset_id": "SPY",
                        "quantity": 40.0,
                    },
                }
            )
        )

        self.assertEqual(response.result.decision, RiskDecision.APPROVE)
        self.assertEqual(response.result.current_outstanding_balance, 2_500.0)
        self.assertGreater(response.result.current_available_credit, 0.0)
        self.assertEqual(response.result.projected_outstanding_balance, 2_500.0)
        self.assertEqual(response.result.projected_loan_balance, 2_500.0)
        self.assertEqual(response.result.projected_margin_state, MarginState.SAFE)

    def test_projection_keeps_same_asset_id_with_different_asset_type_separate(
        self,
    ) -> None:
        account = AccountState(
            account_ref="acct_identity",
            holdings=[
                Holding("DUP", AssetType.ETF, 10.0),
                Holding("DUP", AssetType.LISTED_EQUITY, 5.0),
            ],
            pledged_cash_balance=0.0,
            loan=Loan(principal=100.0),
            approved_credit_limit=0.0,
            available_credit=0.0,
            last_margin_state=MarginState.SAFE,
        )
        market_data = {
            **self.market_data,
            "DUP": MarketData(
                asset_id="DUP",
                last_price=100.0,
                bid=99.0,
                ask=101.0,
                average_daily_volume=1_000_000,
                average_dollar_volume=100_000_000,
                volatility_30d=0.15,
                volatility_90d=0.15,
                data_quality_score=1.0,
            ),
        }

        result = self.lifecycle.check_portfolio_action(
            account,
            PortfolioActionCheck(
                PortfolioActionType.WITHDRAW_SECURITY,
                asset_id="DUP",
                asset_type=AssetType.ETF,
                quantity=3.0,
            ),
            self.policy,
            market_data,
        )

        quantities = {
            (holding.asset_id, holding.asset_type, holding.currency): holding.quantity
            for holding in result.projected_account_state.holdings
        }
        self.assertEqual(quantities[("DUP", AssetType.ETF, "USD")], 7.0)
        self.assertEqual(quantities[("DUP", AssetType.LISTED_EQUITY, "USD")], 5.0)

    def test_non_usd_pledged_cash_uses_currency_specific_asset_id(self) -> None:
        account = AccountState(
            account_ref="acct_ngn_cash",
            holdings=[],
            pledged_cash_balance=500_000.0,
            loan=Loan(principal=10_000.0, currency="NGN"),
            approved_credit_limit=0.0,
            available_credit=0.0,
            last_margin_state=MarginState.SAFE,
        )

        result = self.lifecycle.check_portfolio_action(
            account,
            PortfolioActionCheck(PortfolioActionType.WITHDRAW_CASH, amount=100_000.0),
            self.policy,
            {},
        )

        cash_asset_id = pledged_cash_asset_id("NGN")
        evaluation_ids = {
            asset.asset_id for asset in result.evaluation_result.asset_results
        }
        self.assertIn(cash_asset_id, evaluation_ids)
        self.assertEqual(result.projected_account_state.pledged_cash_balance, 400_000.0)

    def test_invalid_action_writes_audit_record_before_raising(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_path = Path(temp_dir) / "audit.jsonl"
            lifecycle = CreditLifecycleEngine(
                CollateralRiskEngine(audit_logger=AuditLogger(audit_path)),
                audit_logger=AuditLogger(audit_path),
            )

            with self.assertRaisesRegex(
                ValueError, "security action requires asset_id"
            ):
                lifecycle.check_portfolio_action(
                    self.account(),
                    PortfolioActionCheck(
                        PortfolioActionType.WITHDRAW_SECURITY, quantity=1.0
                    ),
                    self.policy,
                    self.market_data,
                )

            records = [json.loads(line) for line in audit_path.read_text().splitlines()]
            rejected = [
                record
                for record in records
                if record.get("event_type") == "portfolio_action_check_rejected"
            ]
            self.assertEqual(len(rejected), 1)
            self.assertEqual(rejected[0]["decision"], "reject")
            self.assertIn("security action requires asset_id", rejected[0]["reason"])


if __name__ == "__main__":
    unittest.main()
