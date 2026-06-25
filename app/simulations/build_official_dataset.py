from __future__ import annotations
import argparse
from datetime import date
from app.simulations.data_builder import OfficialDatasetBuilder
from app.simulations.config.official_validation_universe import START_DATE

def parse_date(v): return date.fromisoformat(v)
def main():
    p=argparse.ArgumentParser(); p.add_argument("--start-date",type=parse_date,default=START_DATE); p.add_argument("--end-date",type=parse_date); p.add_argument("--force-refresh",action="store_true"); p.add_argument("--providers",default="alpaca,ngnmarket,alpha_vantage"); p.add_argument("--output-dir"); p.add_argument("--dry-run",action="store_true"); p.add_argument("--quota-override",action="store_true"); p.add_argument("--max-provider-calls",type=int)
    a=p.parse_args(); providers=["alpaca","ngnmarket","alpha_vantage"] if a.providers=="all" else [x.strip() for x in a.providers.split(",") if x.strip()]; b=OfficialDatasetBuilder(providers,a.output_dir)
    if a.dry_run:
        for call in b.plan_calls(): print(call)
    m=b.build(a.start_date,a.end_date,a.force_refresh,a.dry_run,a.quota_override,a.max_provider_calls); path=b.write_manifest(m); print(path)
if __name__ == "__main__": main()
