from __future__ import annotations
import json, math, random
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from app.core.enums import AssetType, MarginState
from app.core.evaluator import CollateralRiskEngine
from app.core.models import Holding, Loan, MarketData, OrderBook, OrderBookLevel, Policy
from app.credit.interest import InterestPolicy, accrue_scheduled_periods
from app.historical_data.models import HistoricalBar, HistoricalDatasetManifest
from app.lifecycle.service import CreditLifecycleEngine
from app.simulations.scenarios.official_portfolios import OfficialPortfolioScenario
from app.version import RISK_MODEL_VERSION, LIFECYCLE_MODEL_VERSION

@dataclass(frozen=True)
class StressOverlay:
    price_gap: float = 0.0
    fx_devaluation: float = 0.0
    volume_collapse: float = 0.0
    spread_widening: float = 1.0
    order_book_thinning: float = 0.0
    trading_halt: bool = False
    market_data_stale: bool = False
    missing_fx: bool = False
    single_name_crash: dict[str, float] = field(default_factory=dict)
    correlated_selloff: float = 0.0

def historical_bar_to_market_data(bar: HistoricalBar, returns: list[float] | None=None, stress: StressOverlay | None=None) -> MarketData:
    stress=stress or StressOverlay(); price=max(0.0, bar.close*(1-stress.price_gap)*(1-stress.correlated_selloff)*(1-stress.single_name_crash.get(bar.instrument,0.0)))
    adv=max(0.0, bar.volume*(1-stress.volume_collapse)); spread=estimate_spread(price, adv)*max(1.0, stress.spread_widening)
    vol=rolling_volatility(returns or [], min(len(returns or []),30)) or None
    ts = bar.timestamp if isinstance(bar.timestamp, datetime) else datetime.combine(bar.timestamp, datetime.min.time(), tzinfo=timezone.utc)
    if stress.market_data_stale: ts -= timedelta(days=10)
    ob=generate_synthetic_order_book(price, adv, spread, thinning=stress.order_book_thinning)
    return MarketData(bar.instrument, price, max(0.01, price-spread/2) if price else None, price+spread/2 if price else None, adv, adv*price, vol, None, None, (returns or [None])[-1], ts, 0.5 if stress.market_data_stale else bar.data_quality_score, stress.trading_halt, ob, {"currency":bar.currency,"provider":bar.provider_name,"synthetic_spread":spread})

def rolling_volatility(returns: list[float], window: int=30) -> float:
    vals=[r for r in returns[-window:] if r is not None]
    if len(vals)<2: return 0.0
    mean=sum(vals)/len(vals); var=sum((x-mean)**2 for x in vals)/(len(vals)-1)
    return math.sqrt(var)*math.sqrt(252)

def estimate_spread(price: float, average_volume: float|None=None) -> float:
    bps=0.001 if (average_volume or 0)>1_000_000 else 0.005 if (average_volume or 0)>100_000 else 0.02
    return max(0.01, price*bps)

def generate_synthetic_order_book(price: float, volume: float, spread: float, levels: int=5, participation: float=0.1, thinning: float=0.0) -> OrderBook:
    depth=max(1.0, volume*participation*(1-thinning))/levels
    widen=1+thinning
    bids=[OrderBookLevel(max(0.01, price-spread/2-i*spread*widen), depth/(i+1)) for i in range(levels)]
    asks=[OrderBookLevel(price+spread/2+i*spread*widen, depth/(i+1)) for i in range(levels)]
    return OrderBook(bids,asks)

def apply_fx(value: float, from_currency: str, to_currency: str, fx_rates: dict[tuple[str,str],float], stress: StressOverlay|None=None) -> tuple[float,bool]:
    if from_currency==to_currency: return value, False
    if stress and stress.missing_fx: return value, True
    rate=fx_rates.get((from_currency,to_currency))
    if rate is None:
        inv=fx_rates.get((to_currency,from_currency)); rate=(1/inv) if inv else None
    if rate is None: return value, True
    if stress and from_currency=="NGN": rate*=1-stress.fx_devaluation
    return value*rate, False


def convert_market_data_currency(
    market_data: MarketData,
    to_currency: str,
    fx_rates: dict[tuple[str, str], float],
    stress: StressOverlay | None = None,
) -> tuple[MarketData, bool]:
    from_currency = market_data.metadata.get("currency", to_currency)
    converted_price, missing = apply_fx(market_data.last_price, from_currency, to_currency, fx_rates, stress)
    converted_bid, bid_missing = (
        apply_fx(market_data.bid, from_currency, to_currency, fx_rates, stress)
        if market_data.bid is not None
        else (None, False)
    )
    converted_ask, ask_missing = (
        apply_fx(market_data.ask, from_currency, to_currency, fx_rates, stress)
        if market_data.ask is not None
        else (None, False)
    )
    converted_adv, adv_missing = (
        apply_fx(market_data.average_dollar_volume, from_currency, to_currency, fx_rates, stress)
        if market_data.average_dollar_volume is not None
        else (None, False)
    )
    converted_order_book = None
    order_book_missing = False
    if market_data.order_book is not None:
        converted_bids = []
        converted_asks = []
        for level in market_data.order_book.bids:
            price, level_missing = apply_fx(level.price, from_currency, to_currency, fx_rates, stress)
            converted_bids.append(replace(level, price=price))
            order_book_missing = order_book_missing or level_missing
        for level in market_data.order_book.asks:
            price, level_missing = apply_fx(level.price, from_currency, to_currency, fx_rates, stress)
            converted_asks.append(replace(level, price=price))
            order_book_missing = order_book_missing or level_missing
        converted_order_book = OrderBook(converted_bids, converted_asks)
    any_missing = missing or bid_missing or ask_missing or adv_missing or order_book_missing
    return replace(
        market_data,
        last_price=converted_price,
        bid=converted_bid,
        ask=converted_ask,
        average_dollar_volume=converted_adv,
        order_book=converted_order_book,
        metadata={
            **market_data.metadata,
            "original_currency": from_currency,
            "currency": to_currency,
            "fx_missing": any_missing,
        },
    ), any_missing

class HistoricalReplayEngine:
    def __init__(self, manifest: HistoricalDatasetManifest|dict[str,Any]|None=None, seed:int=42):
        self.manifest=manifest; self.random=random.Random(seed); self.seed=seed; self.risk_engine=CollateralRiskEngine(); self.lifecycle=CreditLifecycleEngine(self.risk_engine)
    @classmethod
    def load_manifest(cls,path:str|Path,seed:int=42):
        return cls(json.loads(Path(path).read_text()), seed)
    def replay(self, scenario: OfficialPortfolioScenario, bars_by_symbol: dict[str,list[HistoricalBar]], fx_rates: dict[tuple[str,str],float]|None=None, start_date:date|None=None, end_date:date|None=None, stress:StressOverlay|None=None, include_monitoring:bool=True) -> dict[str,Any]:
        fx_rates=fx_rates or {}; stress=stress or StressOverlay(); all_dates=sorted({(b.timestamp.date() if isinstance(b.timestamp,datetime) else b.timestamp) for bars in bars_by_symbol.values() for b in bars})
        if start_date: all_dates=[d for d in all_dates if d>=start_date]
        if end_date: all_dates=[d for d in all_dates if d<=end_date]
        loan=Loan(0.0,currency=scenario.loan_currency); policy=Policy.default(); records=[]; events=[]; prev_dt=datetime.combine(all_dates[0], datetime.min.time(), tzinfo=timezone.utc) if all_dates else datetime.now(timezone.utc)
        returns={s:[] for s in bars_by_symbol}; prev_price={}; positions={s:0 for s in bars_by_symbol}; latest={}; missing_fx_dates=[]
        dated_bars={s: sorted(bars, key=lambda b: b.timestamp) for s,bars in bars_by_symbol.items()}
        for d in all_dates:
            md={}; fx_missing=False
            for s,bars in dated_bars.items():
                while positions[s] < len(bars):
                    candidate=bars[positions[s]]
                    candidate_date=candidate.timestamp.date() if isinstance(candidate.timestamp,datetime) else candidate.timestamp
                    if candidate_date>d: break
                    latest[s]=candidate; positions[s]+=1
                b=latest.get(s)
                if not b: continue
                if s in prev_price and prev_price[s]>0: returns.setdefault(s,[]).append(b.close/prev_price[s]-1)
                prev_price[s]=b.close
                raw_md=historical_bar_to_market_data(b, returns.get(s,[]), stress)
                converted_md, missing = convert_market_data_currency(raw_md, scenario.loan_currency, fx_rates, stress)
                fx_missing = fx_missing or missing
                md[s]=converted_md
            if not md: continue
            if fx_missing: missing_fx_dates.append(d.isoformat())
            if loan.principal==0:
                gross=sum((md[h.asset_id].last_price*h.quantity if h.asset_id in md else 0) for h in scenario.holdings)
                loan=Loan(gross*scenario.initial_draw_assumption*scenario.base_ltv_policy,currency=scenario.loan_currency)
            now=datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc); loan, acc=accrue_scheduled_periods(loan, scenario.loan_terms, prev_dt, now); prev_dt=now
            ev=self.risk_engine.evaluate(scenario.name, scenario.holdings, loan, policy, md)
            safe=min(ev.approved_credit_limit, ev.stressed_liquidation_value/max(ev.trigger_levels.dynamic_warning_coverage,1e-9))
            state=ev.margin_state.value if hasattr(ev.margin_state,'value') else str(ev.margin_state)
            if state != MarginState.SAFE.value: events.append({"date":d.isoformat(),"state":state,"severity":"warning"})
            records.append({"date":d.isoformat(),"loan_balance":loan.balance,"principal":loan.principal,"accrued_interest":loan.accrued_interest,"interest_accrued":acc.interest_accrued,"approved_credit_limit":ev.approved_credit_limit,"lifecycle_safe_credit_limit":safe,"margin_state":state,"shortfall":max(0,loan.balance-safe),"with_interest_balance":loan.balance,"without_interest_balance":loan.principal,"fx_missing":fx_missing,"model_versions":{"core":RISK_MODEL_VERSION,"lifecycle":LIFECYCLE_MODEL_VERSION}})
        return {"scenario":scenario.name,"seed":self.seed,"records":records,"events":events,"missing_fx_dates":missing_fx_dates,"stress_assumptions":asdict(stress),"interest_policy":asdict(scenario.loan_terms)}
