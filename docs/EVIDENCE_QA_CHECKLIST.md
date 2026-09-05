# Evidence QA Checklist (v0.7.0)

The QA pass writes `official_validation_qa_report.md` and `official_validation_qa.json`.

QA checks that metrics JSON/CSV, official report, provider coverage, data methodology, interest methodology, and simulation assumptions exist. It also checks scenario results, stress rows, dynamic/static/flat comparisons, FX missing counts, interest metrics, provider coverage, model versions, run timestamp, and manifest checksum.

A pass means evidence is reviewable. A fail means blocking evidence files or fields are missing. Intentional unknowns should be marked `N/A`, not left blank.

v0.7.0 QA does not alter model parameters, perform broker execution, connect a production database, or use live WebSocket streams.

## v0.7.0 blocking checks
QA blocks empty dynamic/flat/static outcome tables, missing baseline columns, placeholder metrics, empty provider coverage in provider-backed evidence, and scenarios for which no result row can be proven. A genuinely inapplicable value must be encoded as `{"status":"not_applicable","reason":"..."}`.
