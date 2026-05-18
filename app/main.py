from fastapi import FastAPI

from app.api.routes import router
from app.version import API_VERSION

app = FastAPI(
    title="Collateral Risk Engine",
    version=API_VERSION,
    description="Dynamic collateral risk and liquidation intelligence engine for investment-backed lending.",
)

app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
