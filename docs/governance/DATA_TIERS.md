# Data tiers for CRI methodology evidence

Every result states its tier. Results from different tiers are never pooled.

| Tier | Definition | CRI status (27 Nov 2026) |
|---|---|---|
| 1. Development and calibration | Data used to choose methods, defaults or parameters, and the unit-test fixtures | Synthetic fixtures; the research defaults authorised by Tiva (ADR-012) were chosen before the extended validation, but earlier CRI research (generalization, intervention, real-data comparisons) used the same provider dataset |
| 2. Previously examined evaluation | Historical data on which results have already been seen by the project | **All provider data to 24 Nov 2026** (US from 27 Jul 2020, NGX from 2018). Reported in two windows: before 16 Feb 2026 (earlier research) and 16 Feb – 24 Nov 2026 (examined in the 26 Nov lifecycle round). Not holdout evidence. |
| 3. Locked unseen validation | Data that no one has looked at, reserved for a one-time final evaluation of a frozen candidate | Provider data dated after 26 Nov 2026. It is collected by staging ingestion but not used by any experiment until a candidate version is frozen for final validation. |
| 4. Prospective live shadow | Decisions made live on current data, recorded before outcomes are known | Staging shadow runs (`shadow_evaluations`), excluding runs listed in `app/platform/governance/excluded_runs.json` |

Synthetic stress paths are a separate category (scenario evidence): they are
appended after an observed origination date and are never mixed with
observed outcomes.
