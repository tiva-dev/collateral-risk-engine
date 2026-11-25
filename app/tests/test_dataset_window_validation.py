"""The official dataset builder must reject an inverted or empty date window."""

from __future__ import annotations

from datetime import date

import pytest

from app.simulations.data_builder import OfficialDatasetBuilder


@pytest.mark.parametrize(
    ("start", "end"),
    [(date(2025, 9, 5), date(2025, 9, 4)), (date(2025, 9, 5), date(2025, 9, 5))],
)
def test_builder_rejects_end_not_after_start(tmp_path, start, end) -> None:
    builder = OfficialDatasetBuilder(["alpaca"], str(tmp_path))
    with pytest.raises(ValueError, match="must be after start date"):
        builder.build(start, end, dry_run=True)
