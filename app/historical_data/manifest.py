from __future__ import annotations
import json
from dataclasses import asdict
from pathlib import Path
from .cache import content_hash, _json_default
from .models import HistoricalDatasetManifest

def write_manifest(manifest: HistoricalDatasetManifest, output_dir: str) -> Path:
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    data=asdict(manifest); data["checksum"]=content_hash({k:v for k,v in data.items() if k!="checksum"})
    path=Path(output_dir)/f"{manifest.dataset_id}.manifest.json"
    path.write_text(json.dumps(data,indent=2,sort_keys=True,default=_json_default))
    return path
