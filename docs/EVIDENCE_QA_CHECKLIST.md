# Evidence QA Checklist (v0.6)

The QA pass writes `official_validation_qa_report.md` and `official_validation_qa.json`.

QA checks that metrics JSON/CSV, official report, provider coverage, data methodology, interest methodology, and simulation assumptions exist. It also checks scenario results, stress rows, dynamic/static/flat comparisons, FX missing counts, interest metrics, provider coverage, model versions, run timestamp, and manifest checksum.

A pass means evidence is reviewable. A fail means blocking evidence files or fields are missing. Intentional unknowns should be marked `N/A`, not left blank.

v0.6 QA does not alter model parameters, perform broker execution, connect a production database, or use live WebSocket streams.
