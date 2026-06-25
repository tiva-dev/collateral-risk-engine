from __future__ import annotations
from dataclasses import dataclass, field
from app.core.enums import AssetType, RiskAppetite
from app.core.models import Holding
from app.credit.interest import InterestPolicy

@dataclass(frozen=True)
class OfficialPortfolioScenario:
    name: str
    holdings: list[Holding]
    loan_currency: str
    base_ltv_policy: float = 0.70
    risk_appetite: RiskAppetite = RiskAppetite.BALANCED
    initial_draw_assumption: float = 0.60
    loan_terms: InterestPolicy = field(default_factory=lambda: InterestPolicy(0.10))
    rebalance: bool = False
    monitoring_frequency: str = "daily"
    methodology_notes: list[str] = field(default_factory=list)

def _h(s,q,c="USD",t=AssetType.LISTED_EQUITY): return Holding(s,t,q,c)

def official_portfolio_scenarios() -> dict[str, OfficialPortfolioScenario]:
    rows=[
        ("us_diversified_etf_portfolio",[_h("SPY",100,"USD",AssetType.ETF),_h("QQQ",80,"USD",AssetType.ETF)],"USD"),
        ("us_concentrated_mega_cap_portfolio",[_h("AAPL",120),_h("MSFT",90)],"USD"),
        ("us_high_volatility_concentrated_portfolio",[_h("TSLA",150,"USD",AssetType.HIGH_VOLATILITY_EQUITY)],"USD"),
        ("ngx_diversified_large_cap_portfolio",[_h("DANGCEM",1000,"NGN"),_h("MTNN",1500,"NGN")],"NGN"),
        ("ngx_banking_heavy_portfolio",[_h("GTCO",2500,"NGN"),_h("ZENITHBANK",2500,"NGN")],"NGN"),
        ("ngx_energy_industrial_portfolio",[_h("SEPLAT",500,"NGN"),_h("BUACEMENT",1000,"NGN")],"NGN"),
        ("mixed_ngx_us_portfolio_ngn_loan",[_h("SPY",50),_h("DANGCEM",1000,"NGN")],"NGN"),
        ("mixed_ngx_us_portfolio_usd_loan",[_h("MSFT",40),_h("MTNN",1000,"NGN")],"USD"),
        ("cross_currency_portfolio_eur_loan",[_h("AAPL",50),_h("ZENITHBANK",1500,"NGN")],"EUR"),
        ("thin_liquidity_portfolio",[_h("THIN",1000,"USD")],"USD"),
        ("single_name_concentration_portfolio",[_h("AAPL",300)],"USD"),
    ]
    return {n: OfficialPortfolioScenario(n,h,c,methodology_notes=["Official v0.5B validation scenario"]) for n,h,c in rows}
