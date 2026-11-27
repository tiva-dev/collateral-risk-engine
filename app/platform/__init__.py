"""Production platform layer: persistence, tenancy, auth, controls and operations.

The financial engine (app.core, app.liquidation, app.monitoring) stays unaware of
storage and tenancy. This package supplies durable adapters for its repository
contracts and wraps the existing API with authentication, tenant isolation,
permanent idempotency, audit trails and administrative controls.
"""
