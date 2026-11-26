"""Frozen snapshots of candidate CRI engine versions, run only in shadow mode.

Each snapshot is an unmodified copy of a tagged engine (imports rewritten to
live under this package) so that several engine versions can evaluate the same
portfolio in one process without sharing state. Shadow outputs are persisted
for comparison and never drive lending, margin actions or liquidation.
"""
