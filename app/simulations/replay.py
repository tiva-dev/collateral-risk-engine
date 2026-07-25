from __future__ import annotations

import json
import math
import random
from bisect import bisect_right
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from app.core.enums import AssetType, MarginState
from app.core.evaluator import CollateralRiskEngine
from app.core.models import Loan, MarketData, OrderBook, OrderBookLevel, Policy
from app.credit.interest import accrue_scheduled_periods
from app.historical_data.models import (
    HistoricalBar,
    HistoricalDatasetManifest,
    HistoricalFXRate,
    HistoricalFXSeries,
)
from app.lifecycle.service import CreditLifecycleEngine
from app.simulations.scenarios.official_portfolios import OfficialPortfolioScenario
from app.version import LIFECYCLE_MODEL_VERSION, RISK_MODEL_VERSION

COMMON_EXPOSURE = "common_exposure_surveillance"
POLICY_ORIGINATION = "policy_origination_outcome"


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


def _adjusted_ohlc(bar: HistoricalBar) -> tuple[float, float, float, float]:
    if bar.adjusted_close is None or bar.close <= 0:
        return bar.open, bar.high, bar.low, bar.close
    factor = bar.adjusted_close / bar.close
    return (
        bar.open * factor,
        bar.high * factor,
        bar.low * factor,
        bar.adjusted_close,
    )


def historical_bar_to_market_data(
    bar: HistoricalBar,
    returns: list[float] | None = None,
    stress: StressOverlay | None = None,
    average_volume: float | None = None,
    *,
    use_synthetic_depth: bool = False,
) -> MarketData:
    """Convert a historical observation without presenting synthetic depth as observed."""
    stress = stress or StressOverlay()
    _, _, _, adjusted_close = _adjusted_ohlc(bar)
    price = max(
        0.0,
        adjusted_close
        * (1 - stress.price_gap)
        * (1 - stress.correlated_selloff)
        * (1 - stress.single_name_crash.get(bar.instrument, 0.0)),
    )
    rolling_volume = (
        max(0.0, average_volume * (1 - stress.volume_collapse))
        if average_volume is not None
        else None
    )
    spread = estimate_spread(price, rolling_volume) * max(1.0, stress.spread_widening)
    volatility = rolling_volatility(returns or [], min(len(returns or []), 30)) or None
    timestamp = (
        bar.timestamp
        if isinstance(bar.timestamp, datetime)
        else datetime.combine(bar.timestamp, time.min, tzinfo=UTC)
    )
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    if stress.market_data_stale:
        timestamp -= timedelta(days=10)

    order_book = None
    if use_synthetic_depth:
        order_book = generate_synthetic_order_book(
            price,
            rolling_volume or 0.0,
            spread,
            thinning=stress.order_book_thinning,
        )
    return MarketData(
        asset_id=bar.instrument,
        last_price=price,
        bid=max(0.01, price - spread / 2) if price else None,
        ask=price + spread / 2 if price else None,
        average_daily_volume=rolling_volume,
        average_dollar_volume=(
            rolling_volume * price if rolling_volume is not None else None
        ),
        volatility_30d=volatility,
        recent_return_1d=(returns or [None])[-1],
        timestamp=timestamp,
        data_quality_score=(
            0.5 if stress.market_data_stale else bar.data_quality_score
        ),
        halted=stress.trading_halt,
        order_book=order_book,
        metadata={
            "currency": bar.currency,
            "provider": bar.provider_name,
            "price_adjustment": (
                "adjusted_close" if bar.adjusted_close is not None else "raw_close"
            ),
            "spread_method": "historical_volume_proxy",
            "depth_method": (
                "synthetic_sensitivity" if use_synthetic_depth else "not_observed"
            ),
        },
    )


def rolling_volatility(returns: list[float], window: int = 30) -> float:
    values = [value for value in returns[-window:] if value is not None]
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance) * math.sqrt(252)


def estimate_spread(price: float, average_volume: float | None = None) -> float:
    basis_points = (
        0.001
        if (average_volume or 0) > 1_000_000
        else 0.005
        if (average_volume or 0) > 100_000
        else 0.02
    )
    return max(0.01, price * basis_points)


def generate_synthetic_order_book(
    price: float,
    volume: float,
    spread: float,
    levels: int = 5,
    participation: float = 0.1,
    thinning: float = 0.0,
) -> OrderBook:
    """Generate deterministic depth for a separately labelled sensitivity only."""
    depth = max(1.0, volume * participation * (1 - thinning)) / levels
    widening = 1 + thinning
    bids = [
        OrderBookLevel(
            max(0.01, price - spread / 2 - index * spread * widening),
            depth / (index + 1),
        )
        for index in range(levels)
    ]
    asks = [
        OrderBookLevel(
            price + spread / 2 + index * spread * widening,
            depth / (index + 1),
        )
        for index in range(levels)
    ]
    return OrderBook(bids, asks)


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def _parse_dt(value: Any) -> date | datetime:
    if isinstance(value, (date, datetime)):
        return value
    return datetime.fromisoformat(str(value))


def build_fx_curves(
    fx_input: Any,
) -> dict[tuple[str, str], list[tuple[date, float]]]:
    curves: dict[tuple[str, str], list[tuple[date, float]]] = {}
    if not fx_input:
        return curves
    for key, value in fx_input.items():
        pair = key if isinstance(key, tuple) else tuple(str(key).split("/", 1))
        if len(pair) != 2:
            continue
        normalized_pair = (str(pair[0]).upper(), str(pair[1]).upper())
        rows: list[tuple[date, float]] = []
        if isinstance(value, HistoricalFXSeries):
            normalized_pair = (
                value.from_currency.upper(),
                value.to_currency.upper(),
            )
            rows = [(_as_date(row.timestamp), float(row.rate)) for row in value.rates]
        elif isinstance(value, list):
            for row in value:
                if isinstance(row, HistoricalFXRate):
                    rows.append((_as_date(row.timestamp), float(row.rate)))
                elif isinstance(row, dict):
                    rows.append(
                        (
                            _as_date(
                                _parse_dt(row.get("timestamp") or row.get("date"))
                            ),
                            float(row.get("rate", 0)),
                        )
                    )
        else:
            rows = [(date.max, float(value))]
        curves[normalized_pair] = sorted(
            (row for row in rows if row[1] > 0), key=lambda row: row[0]
        )
    return curves


def lookup_fx_rate(
    from_currency: str,
    to_currency: str,
    fx_curves: dict[tuple[str, str], list[tuple[date, float]]],
    as_of: date | None = None,
    stale_after_days: int = 5,
    stress: StressOverlay | None = None,
) -> tuple[float | None, dict[str, Any]]:
    source = from_currency.upper()
    target = to_currency.upper()
    if source == target:
        return 1.0, {"fx_missing": False, "fx_stale": False}
    if stress and stress.missing_fx:
        return None, {"fx_missing": True, "missing_required_fx": True}

    observation_date = as_of or date.max
    inverse = False
    rows = fx_curves.get((source, target))
    if rows is None:
        rows = fx_curves.get((target, source))
        inverse = rows is not None
    chosen: tuple[date, float] | None = None
    if rows:
        if len(rows) == 1 and rows[0][0] == date.max:
            chosen = (observation_date, rows[0][1])
        else:
            index = bisect_right(rows, observation_date, key=lambda row: row[0])
            if index:
                chosen = rows[index - 1]
    if chosen is None:
        return None, {"fx_missing": True, "missing_required_fx": True}

    rate_date, rate = chosen
    oriented_rate = 1 / rate if inverse else rate
    devaluation = stress.fx_devaluation if stress else 0.0
    if devaluation:
        if source == "NGN" and target != "NGN":
            oriented_rate *= 1 - devaluation
        elif target == "NGN" and source != "NGN":
            oriented_rate /= max(1 - devaluation, 1e-9)
    stale = (
        rate_date != date.max and (observation_date - rate_date).days > stale_after_days
    )
    return oriented_rate, {
        "fx_missing": False,
        "missing_required_fx": False,
        "fx_stale": stale,
        "fx_rate_date": (rate_date.isoformat() if rate_date != date.max else "latest"),
        "fx_inverse": inverse,
        "fx_stress": devaluation,
    }


def apply_fx(
    value: float,
    from_currency: str,
    to_currency: str,
    fx_rates: dict[tuple[str, str], Any],
    stress: StressOverlay | None = None,
    as_of: date | None = None,
    *,
    fx_curves: dict[tuple[str, str], list[tuple[date, float]]] | None = None,
) -> tuple[float, bool, dict[str, Any]]:
    curves = fx_curves if fx_curves is not None else build_fx_curves(fx_rates)
    rate, metadata = lookup_fx_rate(
        from_currency, to_currency, curves, as_of, stress=stress
    )
    if rate is None:
        return 0.0, True, metadata
    return value * rate, False, metadata


def convert_market_data_currency(
    market_data: MarketData,
    to_currency: str,
    fx_rates: dict[tuple[str, str], Any],
    stress: StressOverlay | None = None,
    as_of: date | None = None,
    *,
    fx_curves: dict[tuple[str, str], list[tuple[date, float]]] | None = None,
) -> tuple[MarketData, bool]:
    from_currency = str(market_data.metadata.get("currency", to_currency)).upper()

    def convert(value: float | None) -> tuple[float | None, bool, dict[str, Any]]:
        if value is None:
            return None, False, {}
        converted, missing, metadata = apply_fx(
            value,
            from_currency,
            to_currency,
            fx_rates,
            stress,
            as_of,
            fx_curves=fx_curves,
        )
        return converted, missing, metadata

    converted_price, price_missing, fx_metadata = convert(market_data.last_price)
    converted_bid, bid_missing, _ = convert(market_data.bid)
    converted_ask, ask_missing, _ = convert(market_data.ask)
    converted_adv, adv_missing, _ = convert(market_data.average_dollar_volume)
    any_missing = price_missing or bid_missing or ask_missing or adv_missing
    return (
        replace(
            market_data,
            last_price=float(converted_price or 0.0),
            bid=None if any_missing else converted_bid,
            ask=None if any_missing else converted_ask,
            average_dollar_volume=None if any_missing else converted_adv,
            order_book=None,
            data_quality_score=(
                min(market_data.data_quality_score, 0.05)
                if any_missing
                else market_data.data_quality_score
            ),
            metadata={
                **market_data.metadata,
                "original_currency": from_currency,
                "currency": to_currency.upper(),
                "fx_missing": any_missing,
                "missing_required_fx": any_missing,
                **fx_metadata,
            },
        ),
        any_missing,
    )


def _static_haircut(asset_type: AssetType) -> float:
    return {
        AssetType.CASH: 0.02,
        AssetType.BOND: 0.20,
        AssetType.BOND_FUND: 0.22,
        AssetType.ETF: 0.30,
        AssetType.LISTED_EQUITY: 0.35,
        AssetType.HIGH_VOLATILITY_EQUITY: 0.60,
        AssetType.CRYPTO: 0.80,
    }.get(asset_type, 1.0)


def _baseline_record(
    observation_date: date,
    market_value: float,
    loan: Loan,
    credit_limit: float,
    status: str,
    proceeds: float,
    costs: float,
) -> dict[str, Any]:
    obligation = loan.balance
    return {
        "date": observation_date.isoformat(),
        "market_value": market_value,
        "collateral_value": market_value,
        "total_obligation": obligation,
        "policy_credit_limit": credit_limit,
        "credit_limit": credit_limit,
        "credit_limit_breach": max(0.0, obligation - credit_limit),
        "economic_recovery_shortfall": max(0.0, obligation + costs - proceeds),
        "recovery_coverage_ratio": proceeds / max(obligation + costs, 1e-9),
        "margin_state": status,
    }


class HistoricalReplayEngine:
    def __init__(
        self,
        manifest: HistoricalDatasetManifest | dict[str, Any] | None = None,
        seed: int = 42,
    ):
        self.manifest = manifest
        # Deterministic scenario generation; no security-sensitive randomness.
        self.random = random.Random(seed)  # nosec B311
        self.seed = seed
        self.risk_engine = CollateralRiskEngine()
        self.lifecycle = CreditLifecycleEngine(self.risk_engine)

    @classmethod
    def load_manifest(cls, path: str | Path, seed: int = 42):
        return cls(json.loads(Path(path).read_text()), seed)

    def replay(
        self,
        scenario: OfficialPortfolioScenario,
        bars_by_symbol: dict[str, list[HistoricalBar]],
        fx_rates: dict[tuple[str, str], Any] | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        stress: StressOverlay | None = None,
        include_monitoring: bool = True,
        flat_ltv: float | None = None,
        comparison_regime: str = COMMON_EXPOSURE,
    ) -> dict[str, Any]:
        if comparison_regime not in {COMMON_EXPOSURE, POLICY_ORIGINATION}:
            raise ValueError(f"unknown comparison regime: {comparison_regime}")
        fx_rates = fx_rates or {}
        fx_curves = build_fx_curves(fx_rates)
        stress = stress or StressOverlay()
        configured_flat_ltv = scenario.base_ltv_policy if flat_ltv is None else flat_ltv
        all_dates = sorted(
            {
                _as_date(bar.timestamp)
                for bars in bars_by_symbol.values()
                for bar in bars
            }
        )
        if start_date:
            all_dates = [item for item in all_dates if item >= start_date]
        if end_date:
            all_dates = [item for item in all_dates if item <= end_date]

        policy = replace(
            Policy.default(),
            base_ltv={
                key: min(value, scenario.base_ltv_policy)
                for key, value in Policy.default().base_ltv.items()
            },
            risk_appetite=scenario.risk_appetite,
        )
        dated_bars = {
            symbol: sorted(bars, key=lambda bar: bar.timestamp)
            for symbol, bars in bars_by_symbol.items()
        }
        positions = {symbol: 0 for symbol in bars_by_symbol}
        latest: dict[str, HistoricalBar] = {}
        returns = {symbol: [] for symbol in bars_by_symbol}
        volumes = {symbol: [] for symbol in bars_by_symbol}
        previous_adjusted_price: dict[str, float] = {}
        previous_observation_date: dict[str, date] = {}

        records: list[dict[str, Any]] = []
        flat_records: list[dict[str, Any]] = []
        static_records: list[dict[str, Any]] = []
        dynamic_records: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        previous_state: str | None = None
        previous_time = (
            datetime.combine(all_dates[0], time.min, tzinfo=UTC)
            if all_dates
            else datetime.now(UTC)
        )
        loans: dict[str, Loan] | None = None
        missing_fx_dates: list[str] = []

        for observation_date in all_dates:
            market_data: dict[str, MarketData] = {}
            observation_provenance: dict[str, Any] = {}
            fx_missing = False
            for symbol, bars in dated_bars.items():
                new_observation = False
                while positions[symbol] < len(bars):
                    candidate = bars[positions[symbol]]
                    if _as_date(candidate.timestamp) > observation_date:
                        break
                    latest[symbol] = candidate
                    positions[symbol] += 1
                    new_observation = True
                bar = latest.get(symbol)
                if bar is None:
                    continue
                bar_date = _as_date(bar.timestamp)
                if (
                    new_observation
                    and previous_observation_date.get(symbol) != bar_date
                ):
                    adjusted_price = _adjusted_ohlc(bar)[3]
                    previous = previous_adjusted_price.get(symbol)
                    if previous and previous > 0:
                        returns[symbol].append(adjusted_price / previous - 1)
                    previous_adjusted_price[symbol] = adjusted_price
                    previous_observation_date[symbol] = bar_date
                    volumes[symbol].append(max(0.0, bar.volume))
                average_volume = (
                    sum(volumes[symbol][-30:]) / len(volumes[symbol][-30:])
                    if volumes[symbol]
                    else None
                )
                raw_market = historical_bar_to_market_data(
                    bar,
                    returns[symbol],
                    stress,
                    average_volume,
                )
                converted, missing = convert_market_data_currency(
                    raw_market,
                    scenario.loan_currency,
                    fx_rates,
                    stress,
                    observation_date,
                    fx_curves=fx_curves,
                )
                age = max(0, (observation_date - bar_date).days)
                converted = replace(
                    converted,
                    metadata={
                        **converted.metadata,
                        "observation_date": bar_date.isoformat(),
                        "observation_age_days": age,
                        "carried_forward_for_valuation": age > 0,
                    },
                )
                market_data[symbol] = converted
                fx_missing = fx_missing or missing
                observation_provenance[symbol] = {
                    "provider": bar.provider_name,
                    "observation_date": bar_date.isoformat(),
                    "age_days": age,
                    "adjustment": converted.metadata["price_adjustment"],
                }
            if not market_data:
                continue
            if fx_missing:
                missing_fx_dates.append(observation_date.isoformat())

            market_value = sum(
                market_data[holding.asset_id].last_price * holding.quantity
                for holding in scenario.holdings
                if holding.asset_id in market_data
            )
            raw_fallback_value = sum(
                _adjusted_ohlc(latest[holding.asset_id])[3] * holding.quantity
                for holding in scenario.holdings
                if holding.asset_id in latest
            )
            flat_limit = market_value * configured_flat_ltv
            static_limit = sum(
                market_data[holding.asset_id].last_price
                * holding.quantity
                * (1 - _static_haircut(holding.asset_type))
                for holding in scenario.holdings
                if holding.asset_id in market_data
            )

            if loans is None:
                common_value = market_value or raw_fallback_value
                common_balance = (
                    common_value
                    * scenario.initial_draw_assumption
                    * scenario.base_ltv_policy
                )
                zero_evaluation = None
                try:
                    zero_evaluation = self.risk_engine.evaluate(
                        scenario.name,
                        scenario.holdings,
                        Loan(0.0, currency=scenario.loan_currency),
                        policy,
                        market_data,
                    )
                except Exception:
                    if not fx_missing:
                        raise
                dynamic_limit = (
                    self.lifecycle._safe_credit_limit(zero_evaluation)
                    if zero_evaluation is not None
                    else 0.0
                )
                if comparison_regime == COMMON_EXPOSURE:
                    balances = {
                        "dynamic": common_balance,
                        "flat": common_balance,
                        "static": common_balance,
                    }
                else:
                    balances = {
                        "dynamic": dynamic_limit * scenario.initial_draw_assumption,
                        "flat": flat_limit * scenario.initial_draw_assumption,
                        "static": static_limit * scenario.initial_draw_assumption,
                    }
                loans = {
                    key: Loan(value, currency=scenario.loan_currency)
                    for key, value in balances.items()
                }

            current_time = datetime.combine(observation_date, time.min, tzinfo=UTC)
            accruals = {}
            for key, current_loan in loans.items():
                loans[key], accruals[key] = accrue_scheduled_periods(
                    current_loan,
                    scenario.loan_terms,
                    previous_time,
                    current_time,
                )
            previous_time = current_time

            evaluation = None
            try:
                if include_monitoring:
                    monitoring = self.lifecycle.monitor(
                        scenario.name,
                        loans["dynamic"],
                        scenario.holdings,
                        policy,
                        market_data,
                    )
                    evaluation = monitoring.evaluation
                else:
                    evaluation = self.risk_engine.evaluate(
                        scenario.name,
                        scenario.holdings,
                        loans["dynamic"],
                        policy,
                        market_data,
                    )
                safe_limit = self.lifecycle._safe_credit_limit(evaluation)
                state = evaluation.margin_state.value
            except Exception:
                if not fx_missing:
                    raise
                safe_limit = 0.0
                state = MarginState.LIQUIDATION.value

            if state != previous_state:
                severity = {
                    MarginState.SAFE.value: "info",
                    MarginState.WATCH.value: "warning",
                    MarginState.RESTRICT_NEW_BORROWING.value: "warning",
                    MarginState.MARGIN_CALL.value: "critical",
                    MarginState.LIQUIDATION.value: "critical",
                }.get(state, "warning")
                events.append(
                    {
                        "timestamp": current_time.isoformat(),
                        "date": observation_date.isoformat(),
                        "from_state": previous_state,
                        "state": state,
                        "severity": severity,
                        "event_type": "monitoring_state_transition",
                    }
                )
                previous_state = state

            proceeds = (
                evaluation.stressed_liquidation_value if evaluation is not None else 0.0
            )
            costs = 0.0
            obligation = loans["dynamic"].balance
            quality_one_limit = None
            if evaluation is not None:
                quality_one_market = {
                    key: replace(value, data_quality_score=1.0)
                    for key, value in market_data.items()
                }
                quality_one_evaluation = self.risk_engine.evaluate(
                    scenario.name,
                    scenario.holdings,
                    loans["dynamic"],
                    policy,
                    quality_one_market,
                )
                quality_one_limit = self.lifecycle._safe_credit_limit(
                    quality_one_evaluation
                )
            data_quality_impact = (
                max(0.0, quality_one_limit - safe_limit)
                if quality_one_limit is not None
                else None
            )
            fx_stale = any(
                item.metadata.get("fx_stale") for item in market_data.values()
            )
            record = {
                "date": observation_date.isoformat(),
                "comparison_regime": comparison_regime,
                "market_value": market_value,
                "collateral_value": market_value,
                "policy_credit_limit": safe_limit,
                "credit_limit": safe_limit,
                "available_credit": max(0.0, safe_limit - obligation),
                "principal": loans["dynamic"].principal,
                "interest": loans["dynamic"].accrued_interest,
                "fees": loans["dynamic"].fees,
                "total_obligation": obligation,
                "loan_balance": obligation,
                "interest_accrued": accruals["dynamic"].interest_accrued,
                "approved_credit_limit": (
                    evaluation.approved_credit_limit if evaluation else 0.0
                ),
                "lifecycle_safe_credit_limit": safe_limit,
                "stressed_liquidation_proceeds": proceeds,
                "liquidation_costs": costs,
                "credit_limit_breach": max(0.0, obligation - safe_limit),
                "economic_recovery_shortfall": max(0.0, obligation + costs - proceeds),
                "recovery_coverage_ratio": proceeds / max(obligation + costs, 1e-9),
                "margin_state": state,
                "liquidation_plan": (
                    asdict(evaluation.liquidation_plan)
                    if evaluation and evaluation.liquidation_plan
                    else None
                ),
                "with_interest_balance": obligation,
                "without_interest_balance": loans["dynamic"].principal,
                "data_quality_haircut_impact": data_quality_impact,
                "data_quality": {
                    "fx_missing": fx_missing,
                    "fx_stale": fx_stale,
                    "providers": sorted(
                        {
                            str(item.metadata.get("provider", "unknown"))
                            for item in market_data.values()
                        }
                    ),
                    "observations": observation_provenance,
                },
                "fx_missing": fx_missing,
                "fx_stale": fx_stale,
                "missing_data": fx_missing,
                "model_versions": {
                    "core": RISK_MODEL_VERSION,
                    "lifecycle": LIFECYCLE_MODEL_VERSION,
                },
            }
            records.append(record)
            flat_record = _baseline_record(
                observation_date,
                market_value,
                loans["flat"],
                flat_limit,
                "flat_ltv",
                proceeds,
                costs,
            )
            static_record = _baseline_record(
                observation_date,
                market_value,
                loans["static"],
                static_limit,
                "static_haircut",
                proceeds,
                costs,
            )
            dynamic_record = _baseline_record(
                observation_date,
                market_value,
                loans["dynamic"],
                safe_limit,
                state,
                proceeds,
                costs,
            )
            flat_records.append(flat_record)
            static_records.append(static_record)
            dynamic_records.append(dynamic_record)

        return {
            "scenario": scenario.name,
            "comparison_regime": comparison_regime,
            "seed": self.seed,
            "records": records,
            "baseline_results": {
                "flat_ltv": flat_records,
                "static_haircut": static_records,
                "dynamic_engine": dynamic_records,
            },
            "events": events,
            "missing_fx_dates": missing_fx_dates,
            "stress_assumptions": asdict(stress),
            "interest_policy": asdict(scenario.loan_terms),
            "synthetic_depth_used": False,
        }
