# Methodology lab (synthetic experiments)

Isolated research code for the methodology document "CRI Quantitative Risk
Methodology — Research, Comparative Analysis and Proposed Specification".
Nothing here is imported by `app/`. **All data are synthetic**; results are
relative comparisons, not validation.

| File | Purpose |
|---|---|
| `dgp.py` | Synthetic market generator (GARCH-t, regimes, jumps, halts, FX) and portfolio profiles |
| `methods.py` | Feature estimation; Approaches A, B (incl. B_gap), C; risk appetites |
| `lifecycle.py` | Vectorised loan lifecycle: monitoring, margin call, liquidation, settlement |
| `engine_worker.py` | Runs a checkout's own engine (v0.6.2 or v0.7.0) on identical inputs |
| `v062_factor_worker.py` | Per-factor decomposition of the legacy adjustment product |
| `run_experiments.py` | Rolling-cohort comparison |
| `deterministic.py` | Capacity maps and cure algebra on identical inputs |
| `analyze.py` | Aggregation with Clopper–Pearson intervals |
| `results/` | Summary CSVs from the runs cited in the document, and the exact commands |

Reproduce (two checkouts needed):

```bash
python -m research.methodology_lab.deterministic --v062 <main> --v070 <pr21> --out det.json
python -m research.methodology_lab.run_experiments --out own.json --v062 <main> --v070 <pr21> \
    --reps 5 --cohorts 50 --c-every 5 --trigger own --psi 1.15 --with-bgap
python -m research.methodology_lab.analyze own.json --out-dir own_summary
```

Seeds are fixed (`--seed 20261125`, per-replication streams derived with CRC32),
so runs are reproducible on the same NumPy/SciPy versions.
