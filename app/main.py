import hmac
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.version import API_VERSION

app = FastAPI(
    title="Collateral Risk Engine",
    version=API_VERSION,
    description="Dynamic collateral risk and liquidation intelligence engine for investment-backed lending.",
)

app.include_router(router)


def _configured_api_keys() -> dict[str, str]:
    configured: dict[str, str] = {}
    for entry in os.getenv("CRI_API_KEYS", "").split(","):
        tenant, separator, key = entry.strip().partition(":")
        if separator and tenant and key:
            configured[tenant] = key
    return configured


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    """Require a tenant API key when production keys are configured."""
    configured = _configured_api_keys()
    if not configured or request.url.path == "/health":
        return await call_next(request)
    supplied = request.headers.get("X-CRI-API-Key", "")
    tenant = next(
        (
            name
            for name, expected in configured.items()
            if hmac.compare_digest(supplied, expected)
        ),
        None,
    )
    if tenant is None:
        return JSONResponse({"detail": "invalid or missing API key"}, status_code=401)
    request.state.tenant_id = tenant
    return await call_next(request)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
