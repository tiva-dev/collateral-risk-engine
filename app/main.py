from fastapi import FastAPI

from app.api.routes import router

app = FastAPI(
    title="Collateral Risk Engine",
    version="0.1.0",
    description="Dynamic collateral risk and liquidation intelligence engine for investment-backed lending.",
)

app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
