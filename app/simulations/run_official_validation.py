from __future__ import annotations

import argparse
import json
import warnings
from datetime import UTC, date, datetime
from pathlib import Path

from app.historical_data.cache import content_hash
from app.historical_data.models import HistoricalBar, HistoricalFXRate
from app.simulations.calibration import generate_calibration_diagnostics
from app.simulations.evidence_quality import (
    required_fx_pairs_for_scenarios,
    scenario_eligibility,
    validate_evidence_package,
    validate_provider_coverage,
)
from app.simulations.metrics import compute_simulation_metrics
from app.simulations.replay import (
    COMMON_EXPOSURE,
    POLICY_ORIGINATION,
    HistoricalReplayEngine,
    StressOverlay,
)
from app.simulations.reporting import (
    SIMULATION_CONFIG_VERSION,
    generate_evidence_package,
)
from app.simulations.scenarios.official_portfolios import official_portfolio_scenarios


def parse_date(v):
    return date.fromisoformat(v)


def _parse_timestamp(value):
    if isinstance(value, (date, datetime)):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise ValueError(f"Unsupported timestamp value: {value!r}")


def _bar_from_payload(payload: dict) -> HistoricalBar:
    return HistoricalBar(
        instrument=payload.get("instrument")
        or payload.get("symbol")
        or payload.get("asset_id"),
        timestamp=_parse_timestamp(
            payload.get("timestamp") or payload.get("date") or payload.get("t")
        ),
        open=float(
            payload.get(
                "open", payload.get("o", payload.get("close", payload.get("c", 0)))
            )
        ),
        high=float(
            payload.get(
                "high", payload.get("h", payload.get("close", payload.get("c", 0)))
            )
        ),
        low=float(
            payload.get(
                "low", payload.get("l", payload.get("close", payload.get("c", 0)))
            )
        ),
        close=float(payload.get("close", payload.get("c", 0))),
        adjusted_close=payload.get("adjusted_close"),
        volume=float(payload.get("volume", payload.get("v", 0)) or 0),
        value_traded=payload.get("value_traded"),
        currency=payload.get("currency", "USD"),
        source=payload.get("source", "historical_provider"),
        provider_name=payload.get("provider_name", payload.get("provider", "cache")),
        data_quality_score=float(payload.get("data_quality_score", 1.0)),
        warnings=list(payload.get("warnings", [])),
        raw_metadata=dict(payload.get("raw_metadata", {})),
    )


def _load_replay_inputs(
    manifest: dict,
) -> tuple[
    dict[str, list[HistoricalBar]], dict[tuple[str, str], list[HistoricalFXRate]]
]:
    bars_by_symbol: dict[str, list[HistoricalBar]] = {}
    fx_rates: dict[tuple[str, str], list[HistoricalFXRate]] = {}
    for cache_path in manifest.get("cache_paths", []):
        path = Path(cache_path)
        if not path.exists():
            raise FileNotFoundError(
                f"dataset manifest cache path does not exist: {path}"
            )
        envelope = json.loads(path.read_text())
        if envelope.get("checksum") != content_hash(envelope.get("data")):
            raise ValueError(f"normalized cache checksum mismatch: {path}")
        payload = envelope.get("data")
        series_items = payload if isinstance(payload, list) else [payload]
        for item in series_items:
            if not isinstance(item, dict):
                continue
            canonical_fx = all(
                k in item
                for k in (
                    "from_currency",
                    "to_currency",
                    "rates",
                    "provider_name",
                    "retrieved_at",
                    "start_date",
                    "end_date",
                    "warnings",
                    "data_quality_summary",
                )
            )
            canonical_bars = all(
                k in item
                for k in (
                    "instrument",
                    "bars",
                    "provider_name",
                    "retrieved_at",
                    "start_date",
                    "end_date",
                    "warnings",
                    "data_quality_summary",
                )
            )
            if not (canonical_fx or canonical_bars):
                message = f"Skipped provider-native cache payload (not official replay evidence): {path}"
                warnings.warn(message, RuntimeWarning)
                manifest.setdefault("qa_cache_warnings", []).append(message)
                continue
            if "rates" in item:
                frm = item.get("from_currency")
                to = item.get("to_currency")
                rates = item.get("rates") or []
                if frm and to and rates:
                    fx_rates[(frm, to)] = [
                        HistoricalFXRate(
                            frm,
                            to,
                            float(r.get("rate", 0) or 0),
                            _parse_timestamp(r.get("timestamp") or r.get("date")),
                            provider_name=item.get(
                                "provider_name", item.get("provider", "cache")
                            ),
                            quality_score=float(r.get("quality_score", 1.0)),
                        )
                        for r in rates
                        if float(r.get("rate", 0) or 0) > 0
                    ]
                continue
            bars = item.get("bars")
            if isinstance(bars, dict):
                iterable = [(sym, rows) for sym, rows in bars.items()]
            elif isinstance(bars, list):
                iterable = [(item.get("instrument"), bars)]
            else:
                iterable = []
            for symbol, rows in iterable:
                if not symbol or not isinstance(rows, list):
                    continue
                parsed = []
                for row in rows:
                    if isinstance(row, dict):
                        row = {**row, "instrument": row.get("instrument") or symbol}
                        parsed.append(_bar_from_payload(row))
                bars_by_symbol.setdefault(symbol, []).extend(parsed)
    for symbol in list(bars_by_symbol):
        bars_by_symbol[symbol].sort(key=lambda b: b.timestamp)
    return bars_by_symbol, {k: v for k, v in fx_rates.items() if v}


def _synthetic_thin_bars(
    start: date | None, end: date | None, seed: int
) -> list[HistoricalBar]:
    import random

    rng = random.Random(seed)  # nosec B311
    start = start or date(2020, 1, 1)
    end = end or date(2020, 1, 10)
    price = 10.0
    rows = []
    d = start
    from datetime import timedelta

    while d <= end:
        price = max(1.0, price * (1 + rng.uniform(-0.03, 0.03)))
        rows.append(
            HistoricalBar(
                "THIN",
                d,
                price * 0.99,
                price * 1.01,
                price * 0.98,
                price,
                volume=500 + rng.randint(0, 50),
                currency="USD",
                source="synthetic",
                provider_name="synthetic",
                data_quality_score=0.7,
                warnings=["synthetic_thin_liquidity"],
            )
        )
        d += timedelta(days=1)
    return rows


def main():
    p = argparse.ArgumentParser(description="Run v0.5B official validation replay")
    p.add_argument("--dataset-manifest")
    p.add_argument("--start-date", type=parse_date)
    p.add_argument("--end-date", type=parse_date)
    p.add_argument("--scenario", default="all")
    p.add_argument("--output-dir")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--flat-ltv", type=float, default=0.70)
    p.add_argument("--static-haircut-profile", default="standard")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--qa", action="store_true")
    p.add_argument("--calibration", action="store_true")
    p.add_argument("--strict-coverage", action="store_true")
    p.add_argument("--allow-synthetic", action="store_true")
    p.add_argument("--max-provider-calls", type=int)
    p.add_argument("--stress", choices=["all", "baseline", "severe"], default="all")
    p.add_argument("--write-artifacts", choices=["true", "false"], default="true")
    a = p.parse_args()
    scenarios = official_portfolio_scenarios()
    selected = list(scenarios) if a.scenario == "all" else [a.scenario]
    missing = [s for s in selected if s not in scenarios]
    if missing:
        raise SystemExit(f"Unknown scenario(s): {', '.join(missing)}")
    manifest = {}
    if a.dataset_manifest:
        manifest = json.loads(Path(a.dataset_manifest).read_text())
        expected_checksum = manifest.get("checksum")
        if expected_checksum and expected_checksum != content_hash(
            {key: value for key, value in manifest.items() if key != "checksum"}
        ):
            raise SystemExit("Dataset manifest checksum verification failed")
    out = Path(a.output_dir or "simulation_outputs")
    out.mkdir(parents=True, exist_ok=True)
    config = {
        "seed": a.seed,
        "flat_ltv": a.flat_ltv,
        "static_haircut_profile": a.static_haircut_profile,
        "scenario": a.scenario,
        "start_date": str(a.start_date) if a.start_date else None,
        "end_date": str(a.end_date) if a.end_date else None,
        "manifest_checksum": content_hash(manifest) if manifest else None,
        "dataset_manifest_identity": manifest.get("checksum") or content_hash(manifest)
        if manifest
        else None,
        "dataset_manifest": manifest,
        "simulation_config_version": SIMULATION_CONFIG_VERSION,
        "run_timestamp": datetime.now(UTC).isoformat(),
    }
    stress_overlays = {
        "baseline": StressOverlay(),
        "price_gap": StressOverlay(price_gap=0.25),
        "fx_devaluation": StressOverlay(fx_devaluation=0.25),
        "volume_collapse": StressOverlay(volume_collapse=0.8),
        "spread_widening": StressOverlay(spread_widening=4.0),
        "order_book_thinning": StressOverlay(order_book_thinning=0.8),
        "trading_halt": StressOverlay(trading_halt=True),
        "stale_market_data": StressOverlay(market_data_stale=True),
        "missing_fx": StressOverlay(missing_fx=True),
        "single_name_crash": StressOverlay(
            single_name_crash={"AAPL": 0.35, "THIN": 0.50}
        ),
        "correlated_portfolio_selloff": StressOverlay(correlated_selloff=0.30),
        "combined_severe": StressOverlay(
            price_gap=0.30,
            fx_devaluation=0.25,
            volume_collapse=0.8,
            spread_widening=4.0,
            order_book_thinning=0.8,
        ),
    }
    eligibility = scenario_eligibility(
        manifest, selected, allow_synthetic=a.allow_synthetic, stress=a.stress
    )
    if a.strict_coverage and manifest:
        req = sorted(
            {
                h.asset_id
                for s in selected
                for h in scenarios[s].holdings
                if h.asset_id != "THIN"
            }
        )
        coverage = validate_provider_coverage(
            manifest, req, required_fx_pairs_for_scenarios(selected)
        )
        if coverage["blocking_errors"]:
            raise SystemExit(
                "Strict coverage failed: " + "; ".join(coverage["blocking_errors"])
            )
    if a.dry_run:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "scenarios": selected,
                    "stress_overlays": list(stress_overlays),
                    "manifest_loaded": bool(manifest),
                    "output_dir": str(out),
                    "config": config,
                    "scenario_eligibility": eligibility,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    bars_by_symbol, fx_rates = _load_replay_inputs(manifest)
    if not bars_by_symbol:
        raise SystemExit(
            "No cached historical bars found in dataset manifest cache_paths; run the dataset builder first or pass a manifest with normalized caches."
        )
    if eligibility["ineligible_scenarios"]:
        reasons = [
            f"{item['scenario']}: {', '.join(item['reasons'])}"
            for item in eligibility["ineligible_scenarios"]
        ]
        raise SystemExit("Scenario eligibility failed: " + "; ".join(reasons))
    engine = HistoricalReplayEngine(manifest, a.seed)
    results = []
    if a.stress == "baseline":
        stress_overlays = {"baseline": stress_overlays["baseline"]}
    elif a.stress == "severe":
        stress_overlays = {
            k: v
            for k, v in stress_overlays.items()
            if k in {"combined_severe", "price_gap", "fx_devaluation"}
        }
    for s in selected:
        scenario = scenarios[s]
        scenario_bars = {
            h.asset_id: bars_by_symbol[h.asset_id]
            for h in scenario.holdings
            if h.asset_id in bars_by_symbol
        }
        missing = [
            h.asset_id for h in scenario.holdings if h.asset_id not in scenario_bars
        ]
        synthetic_used = False
        if "THIN" in missing and a.allow_synthetic:
            scenario_bars["THIN"] = _synthetic_thin_bars(
                a.start_date, a.end_date, a.seed
            )
            missing = [m for m in missing if m != "THIN"]
            synthetic_used = True
        if missing:
            raise SystemExit(
                f"Missing cached bars for scenario {s}: {', '.join(missing)}"
            )

        for stress_name, overlay in stress_overlays.items():
            for regime in (COMMON_EXPOSURE, POLICY_ORIGINATION):
                r = engine.replay(
                    scenario,
                    scenario_bars,
                    fx_rates=fx_rates,
                    start_date=a.start_date,
                    end_date=a.end_date,
                    stress=overlay,
                    flat_ltv=a.flat_ltv,
                    comparison_regime=regime,
                )
                base_scenario = r.get("scenario") or scenario.name
                r["base_scenario"] = base_scenario
                r["stress_name"] = stress_name
                r["scenario"] = (
                    base_scenario
                    if stress_name == "baseline"
                    else f"{base_scenario}::{stress_name}"
                )
                r["synthetic_data_used"] = synthetic_used
                results.append(r)
    metrics = [
        compute_simulation_metrics(r, a.flat_ltv, manifest=manifest) for r in results
    ]
    if a.write_artifacts == "false":
        print(
            json.dumps(
                {"result_count": len(results), "metric_count": len(metrics)},
                indent=2,
                sort_keys=True,
            )
        )
        return
    files = generate_evidence_package(results, metrics, str(out), config)
    if a.qa:
        qa = validate_evidence_package(files)
        (out / "official_validation_qa.json").write_text(
            json.dumps(qa, indent=2, sort_keys=True)
        )
        (out / "official_validation_qa_report.md").write_text(
            "# Official Validation QA Report\n\n"
            + ("PASS" if qa["passed"] else "FAIL")
            + "\n\n## Blocking Errors\n"
            + "\n".join(f"- {e}" for e in qa["blocking_errors"])
            + "\n\n## Warnings\n"
            + "\n".join(f"- {w}" for w in qa["warnings"])
        )
        files["official_validation_qa.json"] = str(out / "official_validation_qa.json")
        files["official_validation_qa_report.md"] = str(
            out / "official_validation_qa_report.md"
        )
        if not qa["passed"]:
            raise SystemExit("Evidence QA failed: " + "; ".join(qa["blocking_errors"]))
    if a.calibration:
        generate_calibration_diagnostics(metrics, str(out))
        files["calibration_diagnostics.json"] = str(
            out / "calibration_diagnostics.json"
        )
        files["calibration_diagnostics.md"] = str(out / "calibration_diagnostics.md")
    print(json.dumps(files, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
