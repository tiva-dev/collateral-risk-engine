"""Private, checksummed research datasets with an enforced information boundary.

Build (in the publishing workflow, from an Official Provider Validation Run):

    python -m app.learning.datasets build <evidence manifest> --market US \
        --source-run 45873299276 --snapshot-id cfa1e9c2... --out dataset/

* Reads the provider replay inputs with the same verified loader the official
  validation uses (per-file content checksums).
* **Physically cuts** every series at the examined-through boundary
  (2026-11-25): nothing from RC1's locked evaluation period is written.
* Writes ``bars.csv.gz``, ``fx.csv.gz`` and ``DATASET.json`` (sha256 and size
  of every file, row and instrument counts, first/last dates, provenance,
  licence terms, boundary).

Load (in research and training jobs): ``load_research_dataset(dir)`` refuses a
dataset whose files do not match the manifest, whose manifest boundary is later
than the examined-through date, or that contains any observation after it.
In GCP the jobs identity can read only the ``examined/`` prefix of the private
research-data bucket, so the boundary is enforced by access control as well as
by validation.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from app.platform.governance.data_lock import EXAMINED_THROUGH

LICENCE = {
    "US": "Alpaca market data (SIP feed) obtained under the provider's terms for Tiva's internal research; "
          "not for redistribution; stored privately.",
    "NGX": "NGX end-of-day data obtained under the provider's terms for Tiva's internal research; "
           "not for redistribution; stored privately.",
}
BAR_FIELDS = ["symbol", "date", "open", "high", "low", "close", "adj_close", "volume", "value_traded",
              "currency", "trade_status", "range_reported", "volume_reported", "quality"]


class DatasetError(ValueError):
    pass


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _d(ts: Any) -> date:
    return ts.date() if isinstance(ts, datetime) else ts


def _gz_csv(path: Path, header: list[str], rows: list[list[Any]]) -> None:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    # mtime=0 keeps the archive byte-identical for identical content
    with path.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        gz.write(buf.getvalue().encode())


def build_research_dataset(evidence_manifest: Path, market: str, out: Path, *, source_run: str,
                           snapshot_id: str, boundary: date = EXAMINED_THROUGH) -> dict[str, Any]:
    from app.simulations.run_official_validation import _load_replay_inputs

    from app.simulations.config.official_validation_universe import NGX_UNIVERSE, US_UNIVERSE

    manifest = json.loads(evidence_manifest.read_text())
    bars_by_symbol, fx = _load_replay_inputs(manifest)
    # Provider artifacts can contain several markets: keep only the requested
    # market's universe (and its listing currency), so rows are never mislabelled.
    universe = {"US": set(US_UNIVERSE), "NGX": set(NGX_UNIVERSE)}[market]
    currency = {"US": "USD", "NGX": "NGN"}[market]
    excluded = sorted(s for s in bars_by_symbol if s not in universe)
    bars_by_symbol = {s: [b for b in v if (b.currency or currency) == currency]
                      for s, v in bars_by_symbol.items() if s in universe}
    out.mkdir(parents=True, exist_ok=True)
    rows: list[list[Any]] = []
    cut_rows = 0
    first: date | None = None
    last: date | None = None
    for sym in sorted(bars_by_symbol):
        seen: set[date] = set()
        for b in sorted(bars_by_symbol[sym], key=lambda x: _d(x.timestamp)):
            d = _d(b.timestamp)
            if d > boundary:
                cut_rows += 1
                continue
            if d in seen:
                continue
            seen.add(d)
            prov = b.field_provenance or {}
            rows.append([sym, d.isoformat(), b.open, b.high, b.low, b.close,
                         "" if b.adjusted_close is None else b.adjusted_close, b.volume,
                         "" if b.value_traded is None else b.value_traded, b.currency, b.trade_status,
                         int(prov.get("high", "reported") == "reported" and prov.get("low", "reported") == "reported"),
                         int(prov.get("volume", "reported") == "reported"), b.data_quality_score])
            first = d if first is None or d < first else first
            last = d if last is None or d > last else last
    fx_rows: list[list[Any]] = []
    for (frm, to), rates in sorted(fx.items()):
        for r in rates:
            d = _d(r.timestamp)
            if d <= boundary:
                fx_rows.append([frm, to, d.isoformat(), r.rate])
            else:
                cut_rows += 1
    if not rows:
        raise DatasetError("no bars on or before the boundary")
    _gz_csv(out / "bars.csv.gz", BAR_FIELDS, rows)
    _gz_csv(out / "fx.csv.gz", ["from", "to", "date", "rate"], fx_rows)
    files = [{"name": n, "sha256": _sha256(out / n), "bytes": (out / n).stat().st_size}
             for n in ("bars.csv.gz", "fx.csv.gz")]
    ds_id = hashlib.sha256("".join(str(f["sha256"]) for f in files).encode()).hexdigest()[:16]
    meta = {"dataset_id": f"{market.lower()}-{ds_id}", "market": market, "evidence_class": "public_historical",
            "tier": "examined", "boundary": boundary.isoformat(), "first_date": first.isoformat() if first else None,
            "last_date": last.isoformat() if last else None, "rows": len(rows), "fx_rows": len(fx_rows),
            "instruments": len({r[0] for r in rows}), "rows_removed_after_boundary": cut_rows,
            "excluded_other_market_symbols": excluded,
            "source": {"provider_validation_run": source_run, "snapshot_id": snapshot_id,
                       "evidence_manifest": evidence_manifest.name},
            "licence": LICENCE.get(market, "provider terms; internal research only"),
            "files": files, "created_at": datetime.now(UTC).isoformat()}
    (out / "DATASET.json").write_text(json.dumps(meta, indent=2, sort_keys=True))
    return meta


@dataclass
class ResearchDataset:
    meta: dict[str, Any]
    bars: dict[str, list[dict[str, Any]]]          # symbol -> chronologically ordered rows
    fx: list[dict[str, Any]]


def load_research_dataset(directory: Path, boundary: date = EXAMINED_THROUGH) -> ResearchDataset:
    meta = json.loads((directory / "DATASET.json").read_text())
    if meta.get("tier") != "examined":
        raise DatasetError(f"dataset tier {meta.get('tier')!r} is not readable by research")
    if date.fromisoformat(meta["boundary"]) > boundary:
        raise DatasetError(f"dataset boundary {meta['boundary']} is later than {boundary}")
    for f in meta["files"]:
        p = directory / f["name"]
        if not p.exists() or _sha256(p) != f["sha256"] or p.stat().st_size != f["bytes"]:
            raise DatasetError(f"{f['name']} does not match the dataset manifest")
    bars: dict[str, list[dict[str, Any]]] = {}
    with gzip.open(directory / "bars.csv.gz", "rt") as fh:
        for r in csv.DictReader(fh):
            d = date.fromisoformat(r["date"])
            if d > boundary:
                raise DatasetError(f"observation {r['symbol']} {d} is after the boundary {boundary}")
            row: dict[str, Any] = {"date": d, "currency": r["currency"], "trade_status": r["trade_status"],
                                   "range_reported": r["range_reported"] == "1",
                                   "volume_reported": r["volume_reported"] == "1", "quality": float(r["quality"])}
            for k in ("open", "high", "low", "close", "volume"):
                row[k] = float(r[k])
            row["adj_close"] = float(r["adj_close"]) if r["adj_close"] else row["close"]
            row["value_traded"] = float(r["value_traded"]) if r["value_traded"] else None
            bars.setdefault(r["symbol"], []).append(row)
    fx: list[dict[str, Any]] = []
    with gzip.open(directory / "fx.csv.gz", "rt") as fh:
        for r in csv.DictReader(fh):
            d = date.fromisoformat(r["date"])
            if d > boundary:
                raise DatasetError(f"fx observation {d} is after the boundary {boundary}")
            fx.append({"from": r["from"], "to": r["to"], "date": d, "rate": float(r["rate"])})
    for rows in bars.values():
        rows.sort(key=lambda x: x["date"])
    return ResearchDataset(meta, bars, fx)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("manifest")
    b.add_argument("--market", required=True, choices=["US", "NGX"])
    b.add_argument("--source-run", required=True)
    b.add_argument("--snapshot-id", required=True)
    b.add_argument("--out", required=True)
    v = sub.add_parser("verify")
    v.add_argument("dir")
    a = ap.parse_args()
    if a.cmd == "build":
        meta = build_research_dataset(Path(a.manifest), a.market, Path(a.out), source_run=a.source_run,
                                      snapshot_id=a.snapshot_id)
    else:
        meta = load_research_dataset(Path(a.dir)).meta
    print(json.dumps({k: meta[k] for k in ("dataset_id", "market", "rows", "instruments", "first_date",
                                            "last_date", "boundary", "rows_removed_after_boundary")}))


if __name__ == "__main__":
    main()
