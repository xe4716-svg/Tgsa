from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])

@router.get("/health/live")
async def live():
    return {"status": "alive"}

@router.get("/health/ready")
async def ready(request: Request):
    checks = {"postgres": False, "redis": False}
    try:
        async with request.app.state.db_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        checks["postgres"] = True
    except Exception:
        pass
    try:
        await request.app.state.redis.ping()
        checks["redis"] = True
    except Exception:
        pass

    ok = all(checks.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ready" if ok else "not_ready", "checks": checks},
    )
