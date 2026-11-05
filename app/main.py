from contextlib import AsyncExitStack, asynccontextmanager
import asyncio

import asyncpg
from fastapi import FastAPI
from redis.asyncio import Redis

from app.auth.router import router as auth_router
from app.billing.router import router as billing_router
from app.billing.commerce import router as commerce_router
from app.billing.automation import router as billing_automation_router, run_billing_automation
from app.billing.payments import router as payments_router
from app.billing.tron_watcher import run_tron_watcher
from app.config import settings, validate_startup_settings
from app.health import router as health_router
from app.telegram.router import router as telegram_resources_router
from app.telegram.login_router import router as telegram_login_router
from app.telegram.gateway_router import router as gateway_router
from app.telegram.login_manager import TelegramLoginManager
from app.integration.router import router as business_reads_router
from app.integration.relay_control import router as relay_control_router
from app.integration.catalog import router as catalog_router
from app.observability import configure_request_metrics, configure_request_tracing


async def _stop_workers(app):
    # Signal every worker before awaiting either, including during partial startup.
    app.state.billing_automation_stop.set()
    app.state.tron_watcher_stop.set()
    tasks = [task for task in (
        app.state.billing_automation_task, app.state.tron_watcher_task
    ) if task is not None]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result


async def run_telegram_cleanup(manager, stop):
    while not stop.is_set():
        await manager.cleanup_expired()
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass


async def stop_telegram(manager, stop, task):
    stop.set()
    try:
        await task
    finally:
        await manager.shutdown()


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncExitStack() as cleanup:
        # Flush telemetry last, after workers and application resources have stopped.
        metrics_provider = getattr(app.state, "metrics_provider", None)
        if metrics_provider is not None:
            cleanup.callback(metrics_provider.shutdown)
        tracing_provider = getattr(app.state, "tracing_provider", None)
        if tracing_provider is not None:
            cleanup.callback(tracing_provider.shutdown)
        validate_startup_settings()
        app.state.db_pool = await asyncpg.create_pool(
            dsn=settings.database_url, min_size=1, max_size=10, command_timeout=10
        )
        cleanup.push_async_callback(app.state.db_pool.close)
        app.state.redis = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
        cleanup.push_async_callback(app.state.redis.aclose)
        app.state.tron_watcher_stop = asyncio.Event()
        app.state.billing_automation_stop = asyncio.Event()
        app.state.tron_watcher_task = None
        app.state.billing_automation_task = None
        cleanup.push_async_callback(_stop_workers, app)
        if settings.run_background_workers:
            app.state.billing_automation_task = asyncio.create_task(
                run_billing_automation(app.state.db_pool, app.state.billing_automation_stop)
            )
            if settings.tron_watcher_enabled:
                app.state.tron_watcher_task = asyncio.create_task(
                    run_tron_watcher(app.state.db_pool, app.state.tron_watcher_stop)
                )
        app.state.telegram_login_manager = TelegramLoginManager(
            max_pending=getattr(settings,'telegram_login_max_pending',100),
            max_per_tenant=getattr(settings,'telegram_login_max_per_tenant',20),
            max_per_user=getattr(settings,'telegram_login_max_per_user',3),
            max_attempts=getattr(settings,'telegram_login_max_attempts',5),
            network_timeout=getattr(settings,'telegram_login_network_timeout',30),
        )
        stop = asyncio.Event()
        task = asyncio.create_task(run_telegram_cleanup(app.state.telegram_login_manager, stop))
        cleanup.push_async_callback(stop_telegram, app.state.telegram_login_manager, stop, task)
        yield


app = FastAPI(
    title=settings.app_name,
    version="1.0.0-foundation",
    lifespan=lifespan,
)
app.include_router(health_router)
app.include_router(telegram_resources_router)
app.include_router(telegram_login_router)
app.include_router(gateway_router)
app.include_router(business_reads_router)
app.include_router(relay_control_router)
app.include_router(catalog_router)
app.include_router(auth_router)
app.include_router(billing_router)
app.include_router(commerce_router)
app.include_router(payments_router)
app.include_router(billing_automation_router)


@app.get("/")
async def root():
    return {
        "service": settings.app_name,
        "version": "1.0.0-foundation",
        "status": "running",
        "modules": ["health", "authentication", "membership_entitlements", "wallet_commerce", "payment_orchestration", "tron_usdt_watcher", "auto_renewal", "billing_notifications", "telegram_notifications"],
        "note": "Telegram integrations are not enabled until deployment secrets and integration tests are complete.",
    }


# Tracing is opt-in: no exporter or telemetry network traffic exists by default.
configure_request_tracing(
    app,
    service_name=settings.app_name,
    environment=settings.app_env,
)


# Metrics are independently opt-in and use bounded route/method/status labels.
configure_request_metrics(
    app,
    service_name=settings.app_name,
    environment=settings.app_env,
)
