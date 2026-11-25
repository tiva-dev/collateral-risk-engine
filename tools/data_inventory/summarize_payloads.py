"""Summarise provider payload files without reproducing their data.

Prints, for every JSON file under the given roots: path, size, sha256, and a
structural outline (keys, value types, list lengths, first/last dates). At most
one sample row per list is shown, so the output is a diagnostic description of
the payload rather than a copy of licensed market data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

DATE_KEYS = ("date", "timestamp", "time", "t", "datetime")


def _date_of(row: Any) -> str | None:
    if isinstance(row, dict):
        for k in DATE_KEYS:
            if k in row and row[k] is not None:
                return str(row[k])[:10]
    if isinstance(row, list) and row:
        return str(row[0])[:19]
    return None


def outline(value: Any, depth: int = 0, max_depth: int = 4) -> list[str]:
    pad = "  " * depth
    out: list[str] = []
    if isinstance(value, dict):
        if depth >= max_depth:
            return [f"{pad}{{...{len(value)} keys}}"]
        for k, v in list(value.items())[:25]:
            if isinstance(v, (dict, list)):
                out.append(f"{pad}{k}: {type(v).__name__}"
                           + (f"[{len(v)}]" if isinstance(v, list) else ""))
                out.extend(outline(v, depth + 1, max_depth))
            else:
                out.append(f"{pad}{k}: {type(v).__name__} = {str(v)[:60]!r}")
    elif isinstance(value, list):
        if not value:
            return [f"{pad}(empty list)"]
        first, last = value[0], value[-1]
        fd, ld = _date_of(first), _date_of(last)
        if fd or ld:
            out.append(f"{pad}rows={len(value)} first={fd} last={ld}")
        nulls: dict[str, int] = {}
        for r in value:
            if isinstance(r, dict):
                for k, v in r.items():
                    if v is None or v == "":
                        nulls[k] = nulls.get(k, 0) + 1
        if nulls:
            out.append(f"{pad}null_counts={nulls}")
        out.append(f"{pad}sample[0]:")
        out.extend(outline(first, depth + 1, max_depth))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="+")
    args = ap.parse_args(argv)
    for root in args.roots:
        for p in sorted(Path(root).rglob("*")):
            if not p.is_file():
                continue
            blob = p.read_bytes()
            print(f"\n=== {p}  bytes={len(blob)}  sha256={hashlib.sha256(blob).hexdigest()}")
            if p.suffix != ".json":
                continue
            try:
                payload = json.loads(blob)
            except json.JSONDecodeError as exc:
                print(f"  invalid json: {exc}")
                continue
            for line in outline(payload, 1):
                print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
