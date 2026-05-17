from __future__ import annotations

import unittest

from app.api.routes import check_portfolio_action
from app.api.schemas import PortfolioActionCheckRequest
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
from app.lifecycle.service import CreditLifecycleEngine


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
        self.assertEqual(result.projected_account_state.pledged_cash_balance, 4_000.0)

    def test_sell_asset_and_withdraw_proceeds_is_rejected_when_remaining_collateral_is_unsafe(
        self,
    ) -> None:
        result = self.check(
            self.account(),
            PortfolioActionCheck(
                PortfolioActionType.SELL,
                asset_id="SPY",
                quantity=70.0,
                withdraw_proceeds=True,
            ),
        )

        self.assertEqual(result.decision, RiskDecision.LIQUIDATION)
        self.assertEqual(result.projected_margin_state, MarginState.LIQUIDATION)
        self.assertGreater(result.required_repayment_amount, 0.0)

    def test_buy_safer_asset_can_be_approved(self) -> None:
        result = self.check(
            self.account(loan_principal=1_000.0),
            PortfolioActionCheck(
                PortfolioActionType.BUY,
                asset_id="BND",
                asset_type=AssetType.BOND,
                quantity=10.0,
            ),
        )

        self.assertEqual(result.decision, RiskDecision.APPROVE)
        self.assertEqual(result.projected_margin_state, MarginState.SAFE)

    def test_buy_riskier_asset_reduces_available_credit_when_safe(self) -> None:
        result = self.check(
            self.account(loan_principal=1_000.0, pledged_cash=1_000.0),
            PortfolioActionCheck(
                PortfolioActionType.BUY,
                asset_id="NVDA",
                asset_type=AssetType.HIGH_VOLATILITY_EQUITY,
                quantity=10.0,
            ),
        )

        self.assertEqual(result.decision, RiskDecision.REDUCE_AVAILABLE_CREDIT)
        self.assertEqual(result.projected_margin_state, MarginState.SAFE)
        self.assertLess(result.projected_available_credit, 4_000.0)

    def test_withdraw_securities_while_owing_is_rejected_below_safety(self) -> None:
        result = self.check(
            self.account(),
            PortfolioActionCheck(
                PortfolioActionType.WITHDRAW_SECURITY, asset_id="SPY", quantity=50.0
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
                PortfolioActionType.WITHDRAW_SECURITY, asset_id="SPY", quantity=50.0
            ),
        )

        self.assertEqual(result.decision, RiskDecision.MARGIN_CALL)
        self.assertEqual(result.projected_margin_state, MarginState.MARGIN_CALL)

    def test_post_action_liquidation_is_returned(self) -> None:
        result = self.check(
            self.account(),
            PortfolioActionCheck(
                PortfolioActionType.WITHDRAW_SECURITY, asset_id="SPY", quantity=70.0
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
        self.assertEqual(response.result.projected_margin_state, MarginState.SAFE)


if __name__ == "__main__":
    unittest.main()
