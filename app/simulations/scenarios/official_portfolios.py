from __future__ import annotations

from dataclasses import dataclass, field

from app.core.enums import AssetType, RiskAppetite
from app.core.models import Holding
from app.credit.interest import InterestPolicy
from app.liquidation.policy import LiquidationExecutionPolicy


@dataclass(frozen=True)
class OfficialPortfolioScenario:
    name: str
    holdings: list[Holding]
    loan_currency: str
    base_ltv_policy: float = 0.70
    risk_appetite: RiskAppetite = RiskAppetite.BALANCED
    initial_draw_assumption: float = 1.0
    loan_terms: InterestPolicy = field(
        default_factory=lambda: InterestPolicy(
            annual_interest_rate=0.10,
            payment_frequency="at_maturity",
            term_days=365,
        )
    )
    conventional_flat_ltv: float | None = None
    execution_policy: LiquidationExecutionPolicy = field(
        default_factory=LiquidationExecutionPolicy
    )
    rebalance: bool = False
    monitoring_frequency: str = "daily"
    methodology_notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not 0 <= self.initial_draw_assumption <= 1:
            raise ValueError("initial_draw_assumption must be between zero and one")
        if self.conventional_flat_ltv is not None and not (
            0 <= self.conventional_flat_ltv <= 1
        ):
            raise ValueError("conventional_flat_ltv must be between zero and one")

    @property
    def benchmark_flat_ltv(self) -> float:
        if self.conventional_flat_ltv is not None:
            return self.conventional_flat_ltv
        return 0.30 if self.loan_currency.upper() == "NGN" else 0.50


def _h(s, q, c="USD", t=AssetType.LISTED_EQUITY):
    exchange = "NGX" if c.upper() == "NGN" else "US"
    return Holding(s, t, q, c, exchange)


def official_portfolio_scenarios() -> dict[str, OfficialPortfolioScenario]:
    rows = [
        (
            "us_diversified_etf_portfolio",
            [_h("SPY", 100, "USD", AssetType.ETF), _h("QQQ", 80, "USD", AssetType.ETF)],
            "USD",
        ),
        (
            "us_concentrated_mega_cap_portfolio",
            [_h("AAPL", 120), _h("MSFT", 90)],
            "USD",
        ),
        (
            "us_high_volatility_concentrated_portfolio",
            [_h("TSLA", 150, "USD", AssetType.HIGH_VOLATILITY_EQUITY)],
            "USD",
        ),
        (
            "ngx_diversified_large_cap_portfolio",
            [_h("DANGCEM", 1000, "NGN"), _h("MTNN", 1500, "NGN")],
            "NGN",
        ),
        (
            "ngx_banking_heavy_portfolio",
            [_h("GTCO", 2500, "NGN"), _h("ZENITHBANK", 2500, "NGN")],
            "NGN",
        ),
        (
            "ngx_energy_industrial_portfolio",
            [_h("SEPLAT", 500, "NGN"), _h("BUACEMENT", 1000, "NGN")],
            "NGN",
        ),
        (
            "mixed_ngx_us_portfolio_ngn_loan",
            [_h("SPY", 50), _h("DANGCEM", 1000, "NGN")],
            "NGN",
        ),
        (
            "mixed_ngx_us_portfolio_usd_loan",
            [_h("MSFT", 40), _h("MTNN", 1000, "NGN")],
            "USD",
        ),
        (
            "cross_currency_portfolio_eur_loan",
            [_h("AAPL", 50), _h("ZENITHBANK", 1500, "NGN")],
            "EUR",
        ),
        ("thin_liquidity_portfolio", [_h("THIN", 1000, "USD")], "USD"),
        ("single_name_concentration_portfolio", [_h("AAPL", 300)], "USD"),
    ]
    scenarios: dict[str, OfficialPortfolioScenario] = {}
    for name, holdings, currency in rows:
        nigeria_terms = currency.upper() == "NGN"
        scenarios[name] = OfficialPortfolioScenario(
            name,
            holdings,
            currency,
            loan_terms=(
                InterestPolicy(
                    annual_interest_rate=0.48,
                    quoted_interest_rate=0.04,
                    rate_period="monthly",
                    accrual_frequency="monthly",
                    payment_frequency="at_maturity",
                    term_days=365,
                )
                if nigeria_terms
                else InterestPolicy(
                    annual_interest_rate=0.10,
                    payment_frequency="at_maturity",
                    term_days=365,
                )
            ),
            conventional_flat_ltv=0.30 if nigeria_terms else 0.50,
            methodology_notes=[
                "Official provider-backed validation scenario",
                "Policy-originated exposure draws 100% of the approved limit.",
                (
                    "NGN policy uses a 4% monthly simple rate with a one-year term."
                    if nigeria_terms
                    else "USD/EUR policy uses 10% annual simple interest with a one-year term."
                ),
            ],
        )
    return scenarios
