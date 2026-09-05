from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from tempfile import TemporaryDirectory

from app.api.schemas import LiquidationExecutionPolicyIn, PolicyIn
from app.core.enums import AssetType
from app.core.evaluator import CollateralRiskEngine
from app.core.models import (
    Holding,
    Loan,
    MarketData,
    OrderBook,
    OrderBookLevel,
    Policy,
)
from app.credit.interest import InterestPolicy, principal_capacity_from_obligation
from app.historical_data.models import (
    HistoricalBar,
    HistoricalFXRate,
    HistoricalFXSeries,
)
from app.market_data.live_providers import AlphaVantageDailyFXProvider
from app.market_data.providers import FXRate
from app.monitoring.repositories import SQLiteMonitoredAccountRepository
from app.risk.features import calculate_historical_risk_features
from app.simulations.replay import HistoricalReplayEngine, _conventional_ltv
from app.simulations.run_official_validation import _bar_from_payload
from app.simulations.scenarios.official_portfolios import OfficialPortfolioScenario
from app.tests.test_monitoring_v04 import register, service


def market(volatility: float) -> MarketData:
    return MarketData(
        asset_id="TEST",
        last_price=100.0,
        bid=99.9,
        ask=100.1,
        average_daily_volume=1_000_000,
        average_dollar_volume=100_000_000,
        volatility_30d=volatility,
        volatility_90d=volatility,
        volatility_252d=volatility,
        max_drawdown_252d=0.10 if volatility < 0.50 else 0.50,
        timestamp=datetime.now(UTC),
    )


def test_observed_market_risk_replaces_permanent_equity_label_penalty() -> None:
    engine = CollateralRiskEngine()
    holding = Holding("TEST", AssetType.LISTED_EQUITY, 100)
    stable = engine.evaluate(
        "stable",
        [holding],
        Loan(0.0),
        Policy.default(),
        {"TEST": market(0.10)},
    ).asset_results[0]
    volatile = engine.evaluate(
        "volatile",
        [holding],
        Loan(0.0),
        Policy.default(),
        {"TEST": market(1.00)},
    ).asset_results[0]

    assert stable.effective_ltv == stable.base_ltv
    assert volatile.effective_ltv < stable.effective_ltv
    assert volatile.stressed_liquidation_value < stable.stressed_liquidation_value
    assert (volatile.safe_participation_rate or 0) < (
        stable.safe_participation_rate or 0
    )


def test_top_of_book_is_not_misrepresented_as_full_market_depth() -> None:
    engine = CollateralRiskEngine()
    holding = Holding("TEST", AssetType.LISTED_EQUITY, 100)
    proxy = market(0.20)
    top_only = MarketData(
        **{
            **proxy.__dict__,
            "order_book": OrderBook(
                bids=[OrderBookLevel(price=99.9, quantity=1.0)]
            ),
            "metadata": {"depth": "top_of_book"},
        }
    )
    result = engine.evaluate(
        "top-only",
        [holding],
        Loan(0.0),
        Policy.default(),
        {"TEST": top_only},
    ).asset_results[0]

    assert "proxy_liquidity_model_used_for_recovery_estimate" in result.notes


def test_missing_volume_is_unknown_not_zero_turnover() -> None:
    prices = [100.0, 101.0, 99.0, 102.0]
    unknown = calculate_historical_risk_features(prices, [None] * len(prices))
    inconsistent_zero = calculate_historical_risk_features(
        prices, [0.0] * len(prices)
    )
    observed_zero = calculate_historical_risk_features(
        [100.0] * len(prices), [0.0] * len(prices)
    )

    assert unknown.average_daily_volume_30d is None
    assert unknown.average_dollar_volume_30d is None
    assert unknown.volume_coverage_30d == 0.0
    assert inconsistent_zero.inconsistent_zero_volume_count_30d == 3
    assert inconsistent_zero.volume_coverage_30d == 0.0
    assert observed_zero.average_daily_volume_30d == 0.0
    assert observed_zero.volume_coverage_30d == 1.0


def test_official_replay_loader_preserves_missing_volume() -> None:
    missing = _bar_from_payload(
        {
            "instrument": "GTCO",
            "date": "2026-01-02",
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
            "volume": None,
        }
    )
    observed_zero = _bar_from_payload(
        {
            "instrument": "GTCO",
            "date": "2026-01-03",
            "open": 100,
            "high": 100,
            "low": 100,
            "close": 100,
            "volume": 0,
        }
    )

    assert missing.volume is None
    assert observed_zero.volume == 0.0


def test_client_does_not_supply_liquidation_participation_rate() -> None:
    assert "max_participation_rate" not in PolicyIn.model_fields
    assert "max_participation_rate" not in LiquidationExecutionPolicyIn.model_fields


def test_shorter_at_maturity_loan_reserves_less_interest() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    short = InterestPolicy(
        quoted_interest_rate=0.04,
        rate_period="monthly",
        payment_frequency="at_maturity",
        term_days=30,
    )
    long = InterestPolicy(
        quoted_interest_rate=0.04,
        rate_period="monthly",
        payment_frequency="at_maturity",
        term_days=365,
    )

    assert principal_capacity_from_obligation(10_000.0, short, now) > (
        principal_capacity_from_obligation(10_000.0, long, now)
    )
    resolved = short.resolve_contract_dates(now)
    assert resolved.contractual_end(datetime(2026, 1, 15, tzinfo=UTC)) == datetime(
        2026, 1, 31, tzinfo=UTC
    )


def test_flat_benchmark_uses_ngx_30_and_us_50() -> None:
    ngx = Holding("GTCO", AssetType.LISTED_EQUITY, 1, "NGN", "NGX")
    us = Holding("SPY", AssetType.ETF, 1, "USD", "NYSE")

    assert _conventional_ltv(ngx) == 0.30
    assert _conventional_ltv(us) == 0.50


def test_replay_preserves_pre_start_risk_warmup_history() -> None:
    first = date(2025, 1, 1)
    bars = [
        HistoricalBar(
            "TEST",
            first + timedelta(days=index),
            100.0 + index,
            101.0 + index,
            99.0 + index,
            100.0 + index,
            volume=10_000.0 + index,
        )
        for index in range(40)
    ]
    scenario = OfficialPortfolioScenario(
        "warmup",
        [Holding("TEST", AssetType.LISTED_EQUITY, 10, "USD", "US")],
        "USD",
    )
    result = HistoricalReplayEngine(seed=1).replay(
        scenario,
        {"TEST": bars},
        start_date=first + timedelta(days=30),
        end_date=first + timedelta(days=35),
    )
    features = result["records"][0]["data_quality"]["observations"]["TEST"][
        "risk_features"
    ]

    assert features["observation_count"] == 31
    assert features["volatility_30d"] is not None
    assert features["average_daily_volume_30d"] is not None


def test_live_fx_adapter_exposes_daily_close_provenance() -> None:
    provider = AlphaVantageDailyFXProvider()
    provider.history.fetch_fx_history = lambda *args, **kwargs: HistoricalFXSeries(
        "USD",
        "NGN",
        [
            HistoricalFXRate(
                "USD",
                "NGN",
                1_500.0,
                date.today(),
                provider_name="alpha_vantage",
            )
        ],
        "alpha_vantage",
        datetime.now(UTC),
        date.today(),
        date.today(),
    )

    rate = provider.get_fx_rate("USD", "NGN")

    assert rate is not None
    assert rate.rate == 1_500.0
    assert "daily_fx_close_not_intraday" in rate.warnings


def test_monitored_draw_and_repayment_notifications_are_idempotent() -> None:
    monitoring = service()
    register(monitoring, "lifecycle_notifications", loan=0.0)

    account, draw_events = monitoring.apply_draw_notification(
        account_ref="lifecycle_notifications",
        amount=1_000.0,
        draw_reference="draw-1",
    )
    assert account.loan.principal == 1_000.0
    assert any(event.event_type.value == "draw_applied" for event in draw_events)

    duplicate, _ = monitoring.apply_draw_notification(
        account_ref="lifecycle_notifications",
        amount=1_000.0,
        draw_reference="draw-1",
    )
    assert duplicate.loan.principal == 1_000.0

    repaid, repayment_events = monitoring.apply_repayment_notification(
        account_ref="lifecycle_notifications",
        amount=400.0,
        repayment_reference="repay-1",
    )
    assert repaid.loan.principal == 600.0
    assert any(
        event.event_type.value == "repayment_applied"
        for event in repayment_events
    )

    duplicate_repayment, _ = monitoring.apply_repayment_notification(
        account_ref="lifecycle_notifications",
        amount=400.0,
        repayment_reference="repay-1",
    )
    assert duplicate_repayment.loan.principal == 600.0


def test_liquidation_execution_feedback_reduces_collateral_and_debt() -> None:
    monitoring = service()
    register(monitoring, "liquidation_feedback", loan=8_000.0)

    account, _ = monitoring.apply_liquidation_fills(
        account_ref="liquidation_feedback",
        fills=[
            {
                "asset_id": "SPY",
                "quantity": 10.0,
                "execution_price": 100.0,
                "fees": 5.0,
            }
        ],
        execution_reference="fill-1",
    )
    assert account.holdings[0].quantity == 90.0
    assert account.loan.principal == 7_005.0

    duplicate, _ = monitoring.apply_liquidation_fills(
        account_ref="liquidation_feedback",
        fills=[
            {
                "asset_id": "SPY",
                "quantity": 10.0,
                "execution_price": 100.0,
                "fees": 5.0,
            }
        ],
        execution_reference="fill-1",
    )
    assert duplicate.holdings[0].quantity == 90.0
    assert duplicate.loan.principal == 7_005.0


def test_sqlite_account_state_survives_process_repository_reopen() -> None:
    monitoring = service()
    account, _ = register(monitoring, "persistent_account", loan=1_000.0)
    account.client_supplied_fx_rates[("USD", "NGN")] = FXRate(
        "USD",
        "NGN",
        1_500.0,
        timestamp=datetime.now(UTC),
    )
    with TemporaryDirectory() as temporary:
        path = f"{temporary}/monitoring.sqlite3"
        SQLiteMonitoredAccountRepository(path).save(account)
        restored = SQLiteMonitoredAccountRepository(path).get(account.account_ref)

    assert restored is not None
    assert restored.loan == account.loan
    assert restored.client_supplied_fx_rates[("USD", "NGN")].rate == 1_500.0
