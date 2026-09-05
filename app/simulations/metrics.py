from __future__ import annotations

import statistics
from datetime import date, datetime
from typing import Any


def unavailable(reason: str, *, blocking: bool) -> dict[str, Any]:
    return {"available": False, "reason": reason, "blocking": blocking}


def _average(values: list[float]) -> float | dict[str, Any]:
    if not values:
        return unavailable("no observations", blocking=True)
    return sum(values) / len(values)


def _percentile(values: list[float], percentile: float) -> float | dict[str, Any]:
    if not values:
        return unavailable("no observations", blocking=True)
    ordered = sorted(values)
    index = min(
        len(ordered) - 1,
        max(0, round((len(ordered) - 1) * percentile)),
    )
    return ordered[index]


def _parse_event_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return None
    return None


def _days_between(start: Any, end: Any, reason: str) -> int | dict[str, Any]:
    start_date = _parse_event_date(start)
    end_date = _parse_event_date(end)
    if not start_date or not end_date:
        return unavailable(reason, blocking=False)
    return (end_date - start_date).days


def _severity_distribution(events: list[dict[str, Any]]) -> dict[str, int]:
    severities = sorted(
        {
            str(event["severity"])
            for event in events
            if event.get("severity") is not None
        }
    )
    return {
        severity: sum(1 for event in events if event.get("severity") == severity)
        for severity in severities
    }


def _first_transition(
    events: list[dict[str, Any]], state: str
) -> dict[str, Any] | None:
    return next((event for event in events if event.get("state") == state), None)


def _breach(record: dict[str, Any]) -> float:
    return float(record.get("credit_limit_breach", 0.0))


def _reduction(baseline: list[float], dynamic: list[float]) -> float | dict[str, Any]:
    denominator = sum(baseline)
    if denominator <= 0:
        return unavailable("baseline produced no credit-limit breach", blocking=False)
    return (denominator - sum(dynamic)) / denominator


def compute_simulation_metrics(
    result: dict[str, Any],
    flat_ltv: float | None = None,
    static_haircut: float = 0.30,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del flat_ltv, static_haircut
    records = list(result.get("records", []))
    events = list(result.get("events", []))
    baselines = result.get("baseline_results", {})
    dynamic_records = list(baselines.get("dynamic_engine", records))
    flat_records = list(baselines.get("flat_ltv", []))
    flat_30_records = list(baselines.get("flat_ltv_30") or flat_records)
    flat_50_records = list(baselines.get("flat_ltv_50") or flat_records)
    static_records = list(baselines.get("static_haircut", []))
    initial = dict(result.get("initial_credit_snapshot") or {})
    episodes = list(result.get("liquidation_episodes") or [])

    breaches = [_breach(record) for record in records]
    economic_shortfalls = [
        float(record["economic_recovery_shortfall"])
        for record in records
        if record.get("economic_recovery_shortfall") is not None
    ]
    coverage = [
        float(record["recovery_coverage_ratio"])
        for record in records
        if record.get("recovery_coverage_ratio") is not None
    ]
    capacities = [
        float(record.get("lifecycle_safe_credit_limit", 0.0)) for record in records
    ]
    obligations = [float(record.get("total_obligation", 0.0)) for record in records]
    effective_principal_ltvs = [
        float(record["effective_principal_ltv"])
        for record in records
        if record.get("effective_principal_ltv") is not None
    ]
    future_interest_reserves = [
        float(record["future_interest_reserve"])
        for record in records
        if record.get("future_interest_reserve") is not None
    ]
    participation_rates = [
        float(rate)
        for record in records
        for rate in record.get("cri_derived_participation_rates", [])
        if rate is not None
    ]
    liquidation_recoverability = [
        bool(record["liquidation_advisory_full_debt_covered"])
        for record in records
        if record.get("liquidation_advisory_full_debt_covered") is not None
    ]

    outcome_table = [
        {
            "date": dynamic.get("date"),
            "dynamic_credit_limit_breach": _breach(dynamic),
            "flat_ltv_credit_limit_breach": _breach(flat),
            "flat_ltv_30_credit_limit_breach": _breach(flat_30),
            "flat_ltv_50_credit_limit_breach": _breach(flat_50),
            "static_haircut_credit_limit_breach": _breach(static),
            "dynamic_economic_recovery_shortfall": dynamic.get(
                "economic_recovery_shortfall"
            ),
            "flat_ltv_economic_recovery_shortfall": flat.get(
                "economic_recovery_shortfall"
            ),
            "flat_ltv_30_economic_recovery_shortfall": flat_30.get(
                "economic_recovery_shortfall"
            ),
            "flat_ltv_50_economic_recovery_shortfall": flat_50.get(
                "economic_recovery_shortfall"
            ),
            "static_haircut_economic_recovery_shortfall": static.get(
                "economic_recovery_shortfall"
            ),
        }
        for dynamic, flat, flat_30, flat_50, static in zip(
            dynamic_records,
            flat_records,
            flat_30_records,
            flat_50_records,
            static_records,
            strict=False,
        )
    ]

    margin_events = [event for event in events if event.get("state") == "margin_call"]
    liquidation_events = [
        event for event in events if event.get("state") == "liquidation"
    ]
    warning_event = next(
        (
            event
            for event in events
            if event.get("severity") == "warning" or event.get("state") == "watch"
        ),
        None,
    )
    safe_event = _first_transition(events, "safe")
    watch_event = _first_transition(events, "watch")
    margin_event = _first_transition(events, "margin_call")
    liquidation_event = _first_transition(events, "liquidation")

    plans = [
        record["liquidation_plan"]
        for record in records
        if record.get("liquidation_plan") is not None
    ]
    if plans:
        plan_completeness: float | dict[str, Any] = sum(
            1 for plan in plans if plan.get("plan_complete")
        ) / len(plans)
        unrecovered_target: float | dict[str, Any] = sum(
            float(plan.get("unrecovered_target_amount", 0.0)) for plan in plans
        )
    else:
        reason = "no liquidation plan was required" if records else "no replay records"
        plan_completeness = unavailable(reason, blocking=False)
        unrecovered_target = unavailable(reason, blocking=False)

    data_quality_impacts = [
        float(record["data_quality_haircut_impact"])
        for record in records
        if record.get("data_quality_haircut_impact") is not None
    ]
    if data_quality_impacts:
        data_quality_impact_metric: float | dict[str, Any] = sum(
            data_quality_impacts
        ) / len(data_quality_impacts)
    elif (
        result.get("stress_name") == "missing_fx"
        and records
        and all(record.get("fx_missing") for record in records)
    ):
        data_quality_impact_metric = unavailable(
            "counterfactual cannot be valued in the intentional missing-FX stress",
            blocking=False,
        )
    else:
        data_quality_impact_metric = unavailable(
            "data-quality counterfactual not persisted",
            blocking=True,
        )
    false_triggers = (
        sum(
            1
            for record in records
            if _breach(record) == 0 and record.get("margin_state") not in (None, "safe")
        )
        / len(records)
        if records
        else unavailable("no replay records", blocking=True)
    )
    flat_capacities = [
        float(record.get("policy_credit_limit", 0.0)) for record in flat_records
    ]
    static_capacities = [
        float(record.get("policy_credit_limit", 0.0)) for record in static_records
    ]
    forced_episodes = [
        episode for episode in episodes if episode.get("action") == "full_recovery"
    ]
    completed_forced_episodes = [
        episode
        for episode in forced_episodes
        if episode.get("status") == "fully_recovered"
    ]
    completed_episode_times = [
        float(episode["time_to_completion_observations"])
        for episode in episodes
        if episode.get("time_to_completion_observations") is not None
    ]
    execution_attempts = [
        attempt
        for episode in episodes
        for attempt in episode.get("execution_attempts", [])
    ]
    flat_30_shortfalls = [
        float(record.get("economic_recovery_shortfall", 0.0))
        for record in flat_30_records
    ]
    flat_50_shortfalls = [
        float(record.get("economic_recovery_shortfall", 0.0))
        for record in flat_50_records
    ]
    initial_market_value = float(initial.get("market_value", 0.0))
    initial_approved_limit = float(initial.get("approved_credit_limit", 0.0))
    initial_approved_ltv: float | dict[str, Any] = (
        float(initial["approved_ltv"])
        if "approved_ltv" in initial
        else unavailable("initial credit snapshot absent", blocking=False)
    )
    initial_draw_ltv: float | dict[str, Any] = (
        float(initial["draw_ltv"])
        if "draw_ltv" in initial
        else unavailable("initial credit snapshot absent", blocking=False)
    )
    utilization: float | dict[str, Any] = (
        float(initial["credit_limit_utilization"])
        if "credit_limit_utilization" in initial
        else unavailable("initial credit snapshot absent", blocking=False)
    )

    return {
        "scenario": result.get("scenario"),
        "comparison_regime": result.get("comparison_regime"),
        "base_scenario": result.get("base_scenario", result.get("scenario")),
        "stress_name": result.get("stress_name", "baseline"),
        "initial_portfolio_market_value": (
            initial_market_value
            if initial
            else unavailable("initial credit snapshot absent", blocking=False)
        ),
        "initial_approved_credit_limit": (
            initial_approved_limit
            if initial
            else unavailable("initial credit snapshot absent", blocking=False)
        ),
        "initial_approved_ltv": initial_approved_ltv,
        "initial_draw_ltv": initial_draw_ltv,
        "credit_limit_utilization_at_origination": utilization,
        "initial_ltv_advantage_versus_30pct": (
            float(initial_approved_ltv) - 0.30
            if isinstance(initial_approved_ltv, float)
            else initial_approved_ltv
        ),
        "initial_ltv_advantage_versus_50pct": (
            float(initial_approved_ltv) - 0.50
            if isinstance(initial_approved_ltv, float)
            else initial_approved_ltv
        ),
        "incremental_lendable_value_versus_30pct": (
            initial_approved_limit - 0.30 * initial_market_value
            if initial
            else unavailable("initial credit snapshot absent", blocking=False)
        ),
        "incremental_lendable_value_versus_50pct": (
            initial_approved_limit - 0.50 * initial_market_value
            if initial
            else unavailable("initial credit snapshot absent", blocking=False)
        ),
        "credit_limit_breach_rate": (
            sum(value > 0 for value in breaches) / len(breaches)
            if breaches
            else unavailable("no replay records", blocking=True)
        ),
        "credit_limit_breach_severity": _average(
            [value for value in breaches if value > 0]
        )
        if any(value > 0 for value in breaches)
        else 0.0
        if breaches
        else unavailable("no replay records", blocking=True),
        "worst_credit_limit_breach": (
            max(breaches)
            if breaches
            else unavailable("no replay records", blocking=True)
        ),
        "economic_recovery_shortfall_rate": (
            sum(value > 0 for value in economic_shortfalls) / len(economic_shortfalls)
            if economic_shortfalls
            else unavailable("economic recovery fields absent", blocking=True)
        ),
        "worst_economic_recovery_shortfall": (
            max(economic_shortfalls)
            if economic_shortfalls
            else unavailable("economic recovery fields absent", blocking=True)
        ),
        "average_recovery_coverage_ratio": _average(coverage),
        "full_economic_recovery_observation_rate": (
            sum(value <= 0.01 for value in economic_shortfalls)
            / len(economic_shortfalls)
            if economic_shortfalls
            else unavailable("economic recovery fields absent", blocking=True)
        ),
        "liquidation_advisory_full_debt_recovery_rate": (
            sum(liquidation_recoverability) / len(liquidation_recoverability)
            if liquidation_recoverability
            else unavailable(
                "no liquidation advisory was triggered",
                blocking=False,
            )
        ),
        "liquidation_plan_completeness": plan_completeness,
        "unrecovered_liquidation_target": unrecovered_target,
        "liquidation_episode_count": len(episodes),
        "forced_liquidation_episode_count": len(forced_episodes),
        "forced_liquidation_full_recovery_rate": (
            len(completed_forced_episodes) / len(forced_episodes)
            if forced_episodes
            else unavailable(
                "no forced-liquidation episode occurred", blocking=False
            )
        ),
        "average_observations_to_recovery": (
            sum(completed_episode_times) / len(completed_episode_times)
            if completed_episode_times
            else unavailable("no completed recovery episode", blocking=False)
        ),
        "gross_executed_liquidation_proceeds": sum(
            float(episode.get("gross_proceeds", 0.0)) for episode in episodes
        ),
        "liquidation_execution_costs": sum(
            float(episode.get("execution_costs", 0.0)) for episode in episodes
        ),
        "net_executed_liquidation_proceeds": sum(
            float(episode.get("net_proceeds", 0.0)) for episode in episodes
        ),
        "settled_liquidation_recovery": sum(
            float(episode.get("settled_recovery", 0.0)) for episode in episodes
        ),
        "realized_creditor_loss": sum(
            float(episode.get("realized_creditor_loss", 0.0))
            for episode in episodes
        ),
        "terminal_unresolved_exposure": float(
            result.get("terminal_unresolved_exposure", 0.0)
        ),
        "unexecuted_liquidation_episode_count": sum(
            episode.get("status") in {"failed", "unresolved_at_replay_end"}
            for episode in episodes
        ),
        "failed_liquidation_episode_count": sum(
            episode.get("status") == "failed" for episode in episodes
        ),
        "right_censored_liquidation_episode_count": sum(
            episode.get("status") == "unresolved_at_replay_end"
            for episode in episodes
        ),
        "partial_fill_attempt_count": sum(
            attempt.get("status") == "partial" for attempt in execution_attempts
        ),
        "unfilled_attempt_count": sum(
            attempt.get("status") == "unfilled" for attempt in execution_attempts
        ),
        "flat_30pct_economic_recovery_shortfall_rate": (
            sum(value > 0 for value in flat_30_shortfalls)
            / len(flat_30_shortfalls)
            if flat_30_shortfalls
            else unavailable("30% benchmark records absent", blocking=False)
        ),
        "flat_50pct_economic_recovery_shortfall_rate": (
            sum(value > 0 for value in flat_50_shortfalls)
            / len(flat_50_shortfalls)
            if flat_50_shortfalls
            else unavailable("50% benchmark records absent", blocking=False)
        ),
        "average_approved_credit": _average(
            [float(record.get("approved_credit_limit", 0.0)) for record in records]
        ),
        "average_lifecycle_safe_credit_limit": _average(capacities),
        "average_credit_capacity_preserved": _average(capacities),
        "median_credit_capacity_preserved": (
            statistics.median(capacities)
            if capacities
            else unavailable("no replay records", blocking=True)
        ),
        "p5_credit_capacity": _percentile(capacities, 0.05),
        "p95_credit_capacity": _percentile(capacities, 0.95),
        "average_effective_principal_ltv": _average(effective_principal_ltvs),
        "median_effective_principal_ltv": (
            statistics.median(effective_principal_ltvs)
            if effective_principal_ltvs
            else unavailable("effective principal LTV absent", blocking=True)
        ),
        "p5_effective_principal_ltv": _percentile(
            effective_principal_ltvs, 0.05
        ),
        "p95_effective_principal_ltv": _percentile(
            effective_principal_ltvs, 0.95
        ),
        "average_future_interest_reserve": _average(future_interest_reserves),
        "average_cri_derived_participation_rate": _average(participation_rates),
        "credit_capacity_versus_flat_ltv": (
            sum(capacities) / len(capacities)
            - sum(flat_capacities) / len(flat_capacities)
            if capacities and flat_capacities
            else unavailable("baseline capacity absent", blocking=True)
        ),
        "credit_capacity_versus_static_haircut": (
            sum(capacities) / len(capacities)
            - sum(static_capacities) / len(static_capacities)
            if capacities and static_capacities
            else unavailable("baseline capacity absent", blocking=True)
        ),
        "warning_lead_time": _days_between(
            warning_event and warning_event.get("date"),
            margin_event and margin_event.get("date"),
            "warning or margin-call transition absent",
        ),
        "first_warning_date": warning_event and warning_event.get("date"),
        "first_margin_call_date": margin_event and margin_event.get("date"),
        "first_liquidation_date": (liquidation_event and liquidation_event.get("date")),
        "event_count": len(events),
        "event_severity_distribution": _severity_distribution(events),
        "state_transition_path": [
            event.get("state") for event in events if event.get("state")
        ],
        "time_from_safe_to_watch": _days_between(
            safe_event and safe_event.get("date"),
            watch_event and watch_event.get("date"),
            "safe or watch transition absent",
        ),
        "time_from_watch_to_margin_call": _days_between(
            watch_event and watch_event.get("date"),
            margin_event and margin_event.get("date"),
            "watch or margin-call transition absent",
        ),
        "margin_call_frequency": (
            len(margin_events) / len(records)
            if records
            else unavailable("no replay records", blocking=True)
        ),
        "liquidation_frequency": (
            len(liquidation_events) / len(records)
            if records
            else unavailable("no replay records", blocking=True)
        ),
        "false_trigger_proxy": false_triggers,
        "event_volume": len(events),
        "total_interest_accrued": sum(
            float(record.get("interest_accrued", 0.0)) for record in records
        ),
        "average_loan_balance": _average(obligations),
        "peak_loan_balance": (
            max(obligations)
            if obligations
            else unavailable("no replay records", blocking=True)
        ),
        "credit_limit_breach_with_interest_included": sum(breaches),
        "credit_limit_breach_without_interest": sum(
            max(
                0.0,
                float(record.get("without_interest_balance", 0.0))
                - float(record.get("lifecycle_safe_credit_limit", 0.0)),
            )
            for record in records
        ),
        "interest_contribution_to_margin_events": sum(
            max(
                0.0,
                float(record.get("with_interest_balance", 0.0))
                - float(record.get("without_interest_balance", 0.0)),
            )
            for record in records
            if _breach(record) > 0
        ),
        "missing_data_count": sum(
            bool(record.get("missing_data")) for record in records
        ),
        "stale_data_count": sum(
            bool(record.get("fx_stale"))
            or any(
                observation.get("age_days", 0) > 0
                for observation in record.get("data_quality", {})
                .get("observations", {})
                .values()
            )
            for record in records
        ),
        "fx_missing_events": sum(bool(record.get("fx_missing")) for record in records),
        "data_quality_haircut_impact": data_quality_impact_metric,
        "provider_coverage_by_symbol": (manifest or {}).get(
            "provider_coverage_summary", {}
        ),
        "earliest_available_date_by_symbol": (manifest or {}).get(
            "earliest_available_date_by_symbol", {}
        ),
        "credit_limit_breach_reduction_versus_flat_ltv": _reduction(
            [_breach(record) for record in flat_records],
            [_breach(record) for record in dynamic_records],
        ),
        "credit_limit_breach_reduction_versus_static_haircut": _reduction(
            [_breach(record) for record in static_records],
            [_breach(record) for record in dynamic_records],
        ),
        "dynamic_engine_versus_static_ltv_outcome_table": outcome_table,
    }
