from __future__ import annotations

import argparse
import json
from datetime import date

from app.simulations.config.official_validation_universe import START_DATE
from app.simulations.data_builder import OfficialDatasetBuilder


def parse_date(v):
    return date.fromisoformat(v)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start-date", type=parse_date, default=START_DATE)
    p.add_argument("--end-date", type=parse_date)
    p.add_argument("--force-refresh", action="store_true")
    p.add_argument("--providers", default="alpaca,ngnmarket,alpha_vantage")
    p.add_argument("--output-dir")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--quota-override", action="store_true")
    p.add_argument("--max-provider-calls", type=int)
    a = p.parse_args()
    providers = (
        ["alpaca", "ngnmarket", "alpha_vantage"]
        if a.providers == "all"
        else [x.strip() for x in a.providers.split(",") if x.strip()]
    )
    b = OfficialDatasetBuilder(providers, a.output_dir)
    if a.dry_run:
        for call in b.plan_calls():
            print(call)
    m = b.build(
        a.start_date,
        a.end_date,
        a.force_refresh,
        a.dry_run,
        a.quota_override,
        a.max_provider_calls,
    )
    path = b.write_manifest(m)
    print(path)
    available_equities = sorted(
        symbol
        for symbol in m.instruments
        if symbol in m.earliest_available_date_by_symbol
    )
    print(
        json.dumps(
            {
                "dataset_id": m.dataset_id,
                "cache_path_count": len(m.cache_paths),
                "raw_response_path_count": len(m.raw_response_paths),
                "available_equities": available_equities,
                "provider_coverage_summary": m.provider_coverage_summary,
                "missing_symbol_reasons": m.missing_symbol_reasons,
                "warnings": m.warnings,
            },
            indent=2,
            default=str,
        )
    )
    if not a.dry_run and not available_equities:
        raise SystemExit(
            "Provider dataset build produced no usable equity histories; "
            "see the sanitized manifest summary above."
        )


if __name__ == "__main__":
    main()
