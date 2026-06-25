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
from app.historical_data.models import HistoricalBar, HistoricalDatasetManifest, HistoricalFXRate, HistoricalFXSeries
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

def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value

def build_fx_curves(fx_input: Any) -> dict[tuple[str, str], list[tuple[date, float]]]:
    """Normalize latest-rate and HistoricalFXSeries inputs into date-indexed curves."""
    curves: dict[tuple[str, str], list[tuple[date, float]]] = {}
    if not fx_input:
        return curves
    if isinstance(fx_input, dict):
        for key, value in fx_input.items():
            if isinstance(value, HistoricalFXSeries):
                curves[(value.from_currency, value.to_currency)] = sorted([(_as_date(r.timestamp), float(r.rate)) for r in value.rates], key=lambda x: x[0])
            elif isinstance(value, list):
                pair = key if isinstance(key, tuple) else tuple(str(key).split("/", 1))
                if len(pair) == 2:
                    rows=[]
                    for r in value:
                        if isinstance(r, HistoricalFXRate): rows.append((_as_date(r.timestamp), float(r.rate)))
                        elif isinstance(r, dict): rows.append((_as_date(_parse_dt(r.get("timestamp") or r.get("date"))), float(r.get("rate", 0))))
                    curves[(pair[0], pair[1])] = sorted(rows, key=lambda x: x[0])
            else:
                pair = key if isinstance(key, tuple) else tuple(str(key).split("/", 1))
                if len(pair) == 2:
                    curves[(pair[0], pair[1])] = [(date.max, float(value))]
    return {k: [r for r in v if r[1] > 0] for k, v in curves.items()}

def _parse_dt(value: Any) -> date | datetime:
    if isinstance(value, (date, datetime)): return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

def lookup_fx_rate(from_currency: str, to_currency: str, fx_curves: dict[tuple[str,str], list[tuple[date,float]]], as_of: date | None = None, stale_after_days: int = 5, stress: StressOverlay | None = None) -> tuple[float | None, dict[str, Any]]:
    if from_currency == to_currency: return 1.0, {"fx_missing": False, "fx_stale": False}
    if stress and stress.missing_fx: return None, {"fx_missing": True, "missing_required_fx": True}
    d = as_of or date.max
    inverse = False; rows = fx_curves.get((from_currency, to_currency))
    if rows is None:
        rows = fx_curves.get((to_currency, from_currency)); inverse = rows is not None
    chosen = None
    for rd, rate in rows or []:
        rd_eff = d if rd == date.max else rd
        if rd_eff <= d: chosen = (rd_eff, rate)
    if not chosen: return None, {"fx_missing": True, "missing_required_fx": True}
    rd, rate = chosen; rate = (1 / rate) if inverse else rate
    if stress and from_currency == "NGN": rate *= 1 - stress.fx_devaluation
    stale = rd != date.max and (d - rd).days > stale_after_days
    return rate, {"fx_missing": False, "missing_required_fx": False, "fx_stale": stale, "fx_rate_date": rd.isoformat() if rd != date.max else "latest", "fx_inverse": inverse}

def apply_fx(value: float, from_currency: str, to_currency: str, fx_rates: dict[tuple[str,str],float] | dict[tuple[str,str],list[tuple[date,float]]], stress: StressOverlay|None=None, as_of: date|None=None) -> tuple[float,bool,dict[str,Any]]:
    curves = fx_rates if any(isinstance(v, list) and (not v or isinstance(v[0], tuple)) for v in fx_rates.values()) else build_fx_curves(fx_rates)
    rate, meta = lookup_fx_rate(from_currency, to_currency, curves, as_of, stress=stress)
    if rate is None: return 0.0, True, meta
    return value * rate, False, meta


def convert_market_data_currency(
    market_data: MarketData,
    to_currency: str,
    fx_rates: dict[tuple[str, str], Any],
    stress: StressOverlay | None = None,
    as_of: date | None = None,
) -> tuple[MarketData, bool]:
    from_currency = market_data.metadata.get("currency", to_currency)
    converted_price, missing, fx_meta = apply_fx(market_data.last_price, from_currency, to_currency, fx_rates, stress, as_of)
    converted_bid, bid_missing = (
        apply_fx(market_data.bid, from_currency, to_currency, fx_rates, stress, as_of)[:2]
        if market_data.bid is not None
        else (None, False)
    )
    converted_ask, ask_missing = (
        apply_fx(market_data.ask, from_currency, to_currency, fx_rates, stress, as_of)[:2]
        if market_data.ask is not None
        else (None, False)
    )
    converted_adv, adv_missing = (
        apply_fx(market_data.average_dollar_volume, from_currency, to_currency, fx_rates, stress, as_of)[:2]
        if market_data.average_dollar_volume is not None
        else (None, False)
    )
    converted_order_book = None
    order_book_missing = False
    if market_data.order_book is not None:
        converted_bids = []
        converted_asks = []
        for level in market_data.order_book.bids:
            price, level_missing, _ = apply_fx(level.price, from_currency, to_currency, fx_rates, stress, as_of)
            if not level_missing: converted_bids.append(replace(level, price=price))
            order_book_missing = order_book_missing or level_missing
        for level in market_data.order_book.asks:
            price, level_missing, _ = apply_fx(level.price, from_currency, to_currency, fx_rates, stress, as_of)
            if not level_missing: converted_asks.append(replace(level, price=price))
            order_book_missing = order_book_missing or level_missing
        converted_order_book = None if order_book_missing else OrderBook(converted_bids, converted_asks)
    any_missing = missing or bid_missing or ask_missing or adv_missing or order_book_missing
    return replace(
        market_data,
        last_price=converted_price,
        bid=None if any_missing else converted_bid,
        ask=None if any_missing else converted_ask,
        average_dollar_volume=0.0 if any_missing else converted_adv,
        order_book=None if any_missing else converted_order_book,
        data_quality_score=min(market_data.data_quality_score, 0.05) if any_missing else market_data.data_quality_score,
        metadata={
            **market_data.metadata,
            "original_currency": from_currency,
            "currency": to_currency,
            "fx_missing": any_missing,
            "missing_required_fx": any_missing,
            **fx_meta,
        },
    ), any_missing

def _static_haircut(asset_type: AssetType) -> float:
    return {AssetType.CASH:0.02, AssetType.BOND:0.20, AssetType.BOND_FUND:0.22, AssetType.ETF:0.30, AssetType.LISTED_EQUITY:0.35, AssetType.HIGH_VOLATILITY_EQUITY:0.60, AssetType.CRYPTO:0.80}.get(asset_type, 1.0)

def _baseline_record(d: date, collateral_value: float, loan_balance: float, credit_limit: float, status: str) -> dict[str, Any]:
    return {"date": d.isoformat(), "collateral_value": collateral_value, "loan_balance": loan_balance, "credit_limit": credit_limit, "available_credit": max(0.0, credit_limit - loan_balance), "shortfall": max(0.0, loan_balance - credit_limit), "margin_state": status}

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
        loan=Loan(0.0,currency=scenario.loan_currency); policy=Policy.default(); policy=replace(policy, base_ltv={k: min(v, scenario.base_ltv_policy) for k,v in policy.base_ltv.items()}, risk_appetite=scenario.risk_appetite); records=[]; flat_records=[]; static_records=[]; dynamic_records=[]; events=[]; prev_dt=datetime.combine(all_dates[0], datetime.min.time(), tzinfo=timezone.utc) if all_dates else datetime.now(timezone.utc)
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
                converted_md, missing = convert_market_data_currency(raw_md, scenario.loan_currency, fx_rates, stress, d)
                fx_missing = fx_missing or missing
                md[s]=converted_md
            if not md: continue
            if fx_missing: missing_fx_dates.append(d.isoformat())
            if loan.principal==0:
                gross=sum((md[h.asset_id].last_price*h.quantity if h.asset_id in md else 0) for h in scenario.holdings)
                loan=Loan((gross if gross>0 else sum((latest.get(h.asset_id).close*h.quantity if latest.get(h.asset_id) else 0) for h in scenario.holdings))*scenario.initial_draw_assumption*scenario.base_ltv_policy,currency=scenario.loan_currency)
            now=datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc); loan, acc=accrue_scheduled_periods(loan, scenario.loan_terms, prev_dt, now); prev_dt=now
            ev=None
            try:
                ev=self.risk_engine.evaluate(scenario.name, scenario.holdings, loan, policy, md)
                safe=min(ev.approved_credit_limit, ev.stressed_liquidation_value/max(ev.trigger_levels.dynamic_warning_coverage,1e-9))
                state=ev.margin_state.value if hasattr(ev.margin_state,'value') else str(ev.margin_state)
            except Exception as exc:
                if not fx_missing: raise
                safe=0.0; state=MarginState.LIQUIDATION.value
            if state != MarginState.SAFE.value: events.append({"date":d.isoformat(),"state":state,"severity":"warning"})
            collateral_value=sum((md[h.asset_id].last_price*h.quantity if h.asset_id in md else 0) for h in scenario.holdings)
            flat_limit=collateral_value*0.70
            static_limit=sum((md[h.asset_id].last_price*h.quantity*(1-_static_haircut(h.asset_type)) if h.asset_id in md else 0) for h in scenario.holdings)
            flat_records.append(_baseline_record(d, collateral_value, loan.balance, flat_limit, "flat_ltv"))
            static_records.append(_baseline_record(d, collateral_value, loan.balance, static_limit, "static_haircut"))
            dynamic_records.append(_baseline_record(d, collateral_value, loan.balance, safe, state))
            records.append({"date":d.isoformat(),"collateral_value":collateral_value,"credit_limit":safe,"available_credit":max(0,safe-loan.balance),"loan_balance":loan.balance,"principal":loan.principal,"accrued_interest":loan.accrued_interest,"interest_accrued":acc.interest_accrued,"approved_credit_limit":ev.approved_credit_limit if ev is not None else 0.0,"lifecycle_safe_credit_limit":safe,"margin_state":state,"shortfall":max(0,loan.balance-safe),"with_interest_balance":loan.balance,"without_interest_balance":loan.principal,"fx_missing":fx_missing,"fx_stale":any(x.metadata.get("fx_stale") for x in md.values()),"missing_data":fx_missing,"simulated_transition_events":include_monitoring,"model_versions":{"core":RISK_MODEL_VERSION,"lifecycle":LIFECYCLE_MODEL_VERSION}})
        return {"scenario":scenario.name,"seed":self.seed,"records":records,"baseline_results":{"flat_ltv":flat_records,"static_haircut":static_records,"dynamic_engine":dynamic_records},"events":events,"missing_fx_dates":missing_fx_dates,"stress_assumptions":asdict(stress),"interest_policy":asdict(scenario.loan_terms)}
