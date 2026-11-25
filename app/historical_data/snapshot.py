"""Content-addressed, checksummed snapshots of a built historical dataset.

A snapshot freezes exactly what a validation run consumed: the manifest, every
normalized series, and the raw provider responses, each stored under its
SHA-256. ``SNAPSHOT.json`` lists every file with its role, provider, size and
hash, plus a snapshot id that is the hash of that listing. Re-running
validation against a verified snapshot reproduces the inputs bit for bit.

Licensing. Provider data is licensed, not owned. The snapshot never goes into
git. Only the metadata ledger (ids, hashes, coverage verdicts; no prices) is
committed. Each provider's terms are recorded in PROVIDER_LICENCES and copied
into the snapshot so whoever restores it sees the constraints. Where to keep
the snapshot durably (beyond the 90-day Actions artifact) is a Tiva decision.

Usage::

    python -m app.historical_data.snapshot create MANIFEST OUT_DIR
    python -m app.historical_data.snapshot verify SNAPSHOT_DIR
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SNAPSHOT_SCHEMA = "cri-historical-snapshot/v1"

# Summary of each provider's terms as they bear on storing data. These are
# engineering notes, not legal advice; "review_required" means Tiva must
# confirm the plan in use permits this use before evidence relies on it.
PROVIDER_LICENCES: dict[str, dict[str, str]] = {
    "cbn": {
        "status": "public",
        "storage": "public official statistics",
        "redistribution": "attribute the Central Bank of Nigeria",
        "note": "Official CBN exchange rates from the public JSON feed.",
    },
    "ngnmarket": {
        "status": "review_required",
        "storage": "internal storage of retrieved data for validation",
        "redistribution": "not permitted without agreement",
        "note": "Confirm NGNMarket API terms for the active plan permit retaining "
        "history after the subscription lapses.",
    },
    "alpaca": {
        "status": "review_required",
        "storage": "internal storage for the account holder",
        "redistribution": "not permitted; exchange (IEX/SIP) terms apply",
        "note": "Market data passes through exchange licence terms.",
    },
    "alpha_vantage": {
        "status": "review_required",
        "storage": "internal storage for the key holder",
        "redistribution": "not permitted",
        "note": "Free keys are for non-commercial use under Alpha Vantage terms; "
        "commercial validation evidence likely needs a premium plan.",
    },
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _provider_of(path: Path, manifest: dict[str, Any]) -> str:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return "unknown"
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict) and data.get("provider_name"):
        return str(data["provider_name"])
    providers = str(manifest.get("provider") or "").split(",")
    return providers[0] if len(providers) == 1 else "mixed"


def create_snapshot(manifest_path: str | Path, out_dir: str | Path) -> Path:
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    dataset_id = manifest["dataset_id"]
    root = Path(out_dir) / dataset_id
    files_dir = root / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    roles = (
        [("manifest", str(manifest_path))]
        + [("normalized", p) for p in manifest.get("cache_paths", [])]
        + [("normalized_alternate", p) for p in manifest.get("alternate_fx_cache_paths", [])]
        + [("raw", p) for p in manifest.get("raw_response_paths", [])]
    )
    entries = []
    seen: set[tuple[str, str]] = set()
    for role, original in roles:
        src = Path(original)
        if (role, str(src)) in seen:
            continue
        seen.add((role, str(src)))
        if not src.exists():
            entries.append(
                {"role": role, "original_path": str(src), "missing": True}
            )
            continue
        digest = _sha256(src)
        dest = files_dir / f"{digest}{src.suffix}"
        if not dest.exists():
            shutil.copyfile(src, dest)
        provider = "cri" if role == "manifest" else _provider_of(src, manifest)
        entries.append(
            {
                "role": role,
                "original_path": str(src),
                "sha256": digest,
                "bytes": src.stat().st_size,
                "provider": provider,
            }
        )
    entries.sort(key=lambda e: (e["role"], e.get("sha256", ""), e["original_path"]))
    listing = json.dumps(entries, sort_keys=True).encode()
    providers = sorted({str(e["provider"]) for e in entries if e.get("provider")} - {"cri"})
    snapshot = {
        "schema": SNAPSHOT_SCHEMA,
        "snapshot_id": hashlib.sha256(listing).hexdigest(),
        "dataset_id": dataset_id,
        "manifest_checksum": manifest.get("checksum"),
        "window": [manifest.get("start_date"), manifest.get("end_date")],
        "created_at": datetime.now(UTC).isoformat(),
        "file_count": len([e for e in entries if not e.get("missing")]),
        "missing_files": [e["original_path"] for e in entries if e.get("missing")],
        "total_bytes": sum(e.get("bytes", 0) for e in entries),
        "coverage_summary": (manifest.get("coverage_validation") or {}).get("summary"),
        "licences": {p: PROVIDER_LICENCES.get(p, {"status": "unknown"}) for p in providers},
        "entries": entries,
    }
    (root / "SNAPSHOT.json").write_text(json.dumps(snapshot, indent=2, sort_keys=True))
    return root


def verify_snapshot(snapshot_dir: str | Path) -> dict[str, Any]:
    root = Path(snapshot_dir)
    snap = json.loads((root / "SNAPSHOT.json").read_text())
    problems = []
    for e in snap["entries"]:
        if e.get("missing"):
            continue
        f = root / "files" / f"{e['sha256']}{Path(e['original_path']).suffix}"
        if not f.exists():
            problems.append(f"missing file for {e['original_path']}")
        elif _sha256(f) != e["sha256"]:
            problems.append(f"checksum mismatch for {e['original_path']}")
    listing = json.dumps(snap["entries"], sort_keys=True).encode()
    if hashlib.sha256(listing).hexdigest() != snap["snapshot_id"]:
        problems.append("snapshot id does not match its entry listing")
    return {"snapshot_id": snap["snapshot_id"], "ok": not problems, "problems": problems}


def ledger_entry(snapshot_dir: str | Path) -> dict[str, Any]:
    """Metadata-only record suitable for committing (no market data)."""
    snap = json.loads((Path(snapshot_dir) / "SNAPSHOT.json").read_text())
    return {
        k: snap[k]
        for k in (
            "snapshot_id",
            "dataset_id",
            "manifest_checksum",
            "window",
            "created_at",
            "file_count",
            "total_bytes",
            "coverage_summary",
        )
    } | {"licence_status": {p: v.get("status") for p, v in snap["licences"].items()}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("manifest")
    c.add_argument("out_dir")
    v = sub.add_parser("verify")
    v.add_argument("snapshot_dir")
    args = ap.parse_args(argv)
    if args.cmd == "create":
        root = create_snapshot(args.manifest, args.out_dir)
        result = verify_snapshot(root)
        print(json.dumps({"snapshot_dir": str(root), **ledger_entry(root), **result}, indent=2))
        return 0 if result["ok"] else 1
    result = verify_snapshot(args.snapshot_dir)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
