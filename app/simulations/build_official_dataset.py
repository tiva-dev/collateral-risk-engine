from __future__ import annotations
import argparse
from datetime import date
from app.simulations.data_builder import OfficialDatasetBuilder
from app.simulations.config.official_validation_universe import START_DATE

def parse_date(v): return date.fromisoformat(v)
def main():
    p=argparse.ArgumentParser(); p.add_argument("--start-date",type=parse_date,default=START_DATE); p.add_argument("--end-date",type=parse_date); p.add_argument("--force-refresh",action="store_true"); p.add_argument("--providers",default="alpaca,ngnmarket,alpha_vantage"); p.add_argument("--output-dir"); p.add_argument("--dry-run",action="store_true")
    a=p.parse_args(); b=OfficialDatasetBuilder([x.strip() for x in a.providers.split(",") if x.strip()],a.output_dir)
    if a.dry_run:
        for call in b.plan_calls(): print(call)
    m=b.build(a.start_date,a.end_date,a.force_refresh,a.dry_run); path=b.write_manifest(m); print(path)
if __name__ == "__main__": main()
