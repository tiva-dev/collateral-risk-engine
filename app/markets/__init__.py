"""Market-aware configuration and cross-market statistical estimation.

Architectural principle (ADR-009): market-agnostic mathematics, asset-specific
intelligence, market-aware execution. Nothing in this package encodes a
country-specific risk model. A market is onboarded by describing its exchange
and execution characteristics (MarketProfile) and validating that the universal
estimators work on its data (onboarding.validate_market).
"""
