# Architectural and Methodology Decision Log

Each entry records a decision, who owns it, its status and the evidence behind it.
Status values: `proposed`, `accepted`, `rejected`, `superseded`.
Material financial-methodology decisions require independent review (ChatGPT
challenger) and Tiva approval before they change the operational engine.

| ID | Date | Decision | Status | Owner | Evidence |
|---|---|---|---|---|---|
| ADR-001 | 2026-11-25 | `main` (5898756) is the v0.6.2 baseline. PR #21 (`agent/cri-v070-recoverability`, 2b47314) holds v0.7.0 and is frozen: no merge until a reviewed methodology and fresh provider-backed validation exist. | accepted | Tiva | Repository audit, 25 Nov 2026 |
| ADR-002 | 2026-11-25 | The provider-backed evidence from GitHub Actions run 30455533912 applies only to v0.6.2 code (`app/` identical at f7cce7f, 3e14b49 and 5898756). It must never be cited as validation of v0.7.0. | accepted | Tiva | `docs/governance/VALIDATION_RECORD.md` |
| ADR-003 | 2026-11-25 | CI runs pytest (which collects both `unittest.TestCase` classes and pytest functions), Ruff, mypy, Bandit (medium/high fail), pip-audit, byte-compilation and a package build. Developer tools are pinned in `requirements-dev.txt`. | accepted | Engineering | Audit finding C5 |
| ADR-004 | 2026-11-25 | Known financial defects found by the audit are recorded as strict `xfail` characterization tests. They document current behaviour without changing it, and fail loudly (XPASS) once a reviewed fix lands, so the marker must be removed deliberately. | accepted | Engineering | `app/tests/test_audit_characterization.py` |
| ADR-005 | 2026-11-25 | Methodology research is carried out in `research/`, isolated from `app/`. Nothing in `research/` is imported by the operational engine. | accepted | Engineering | Post-audit instructions §1 |
| ADR-006 | 2026-11-25 | Replacement credit methodology (Approach A/B/C or hybrid) | proposed | Tiva + challenger | Methodology document (in review) |
