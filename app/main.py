from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI
from redis.asyncio import Redis

from app.auth.router import router as auth_router
from app.billing.router import router as billing_router
from app.billing.commerce import router as commerce_router
from app.billing.payments import router as payments_router
from app.config import settings, validate_startup_settings
from app.health import router as health_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_startup_settings()
    app.state.db_pool = await asyncpg.create_pool(
        dsn=settings.database_url, min_size=1, max_size=10, command_timeout=10
    )
    app.state.redis = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
    )
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await app.state.db_pool.close()


app = FastAPI(
    title=settings.app_name,
    version="1.0.0-foundation",
    lifespan=lifespan,
)
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(billing_router)
app.include_router(commerce_router)
app.include_router(payments_router)


@app.get("/")
async def root():
    return {
        "service": settings.app_name,
        "version": "1.0.0-foundation",
        "status": "running",
        "modules": ["health", "authentication", "membership_entitlements", "wallet_commerce", "payment_orchestration"],
        "note": "Telegram integrations are not enabled until deployment secrets and integration tests are complete.",
    }
