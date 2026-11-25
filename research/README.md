# Research workspace

Isolated quantitative research for CRI. Nothing here is imported by `app/`, and
nothing here changes the operational engine's methodology (ADR-005).

* `audit_probes/` — numerical probes from the November 2026 audit. They call
  the engine in the current checkout and print JSON:
  `python -m research.audit_probes.probes`.
