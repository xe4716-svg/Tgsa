import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
import app.main as main


def configure(monkeypatch, *, enabled=True, redis_error=False, worker_error=False, close_error=False):
    db = SimpleNamespace(close=AsyncMock())
    redis = SimpleNamespace(aclose=AsyncMock())
    if close_error:
        redis.aclose.side_effect = RuntimeError('redis close failed')
    monkeypatch.setattr(main, 'validate_startup_settings', lambda: None)
    monkeypatch.setattr(main, 'settings', SimpleNamespace(database_url='unused', redis_url='unused', tron_watcher_enabled=enabled))
    monkeypatch.setattr(main.asyncpg, 'create_pool', AsyncMock(return_value=db))
    def make_redis(*args, **kwargs):
        if redis_error:
            raise RuntimeError('redis startup failed')
        return redis
    monkeypatch.setattr(main.Redis, 'from_url', make_redis)
    app = FastAPI()
    async def billing(pool, stop):
        await stop.wait()
        if enabled:
            assert app.state.tron_watcher_stop.is_set()
        if worker_error:
            raise RuntimeError('billing failed')
    async def tron(pool, stop):
        await stop.wait()
        assert app.state.billing_automation_stop.is_set()
    monkeypatch.setattr(main, 'run_billing_automation', billing)
    monkeypatch.setattr(main, 'run_tron_watcher', tron)
    return app, db, redis


@pytest.mark.parametrize('enabled', [True, False])
def test_shutdown_signals_all_workers_and_closes_resources(monkeypatch, enabled):
    app, db, redis = configure(monkeypatch, enabled=enabled)
    async def run():
        async with main.lifespan(app):
            await asyncio.sleep(0)
    asyncio.run(asyncio.wait_for(run(), 2))
    redis.aclose.assert_awaited_once()
    db.close.assert_awaited_once()


def test_worker_failure_still_closes_both_resources(monkeypatch):
    app, db, redis = configure(monkeypatch, worker_error=True)
    async def run():
        async with main.lifespan(app):
            await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match='billing failed'):
        asyncio.run(run())
    redis.aclose.assert_awaited_once()
    db.close.assert_awaited_once()
    assert app.state.tron_watcher_task.done()


def test_partial_startup_closes_database(monkeypatch):
    app, db, redis = configure(monkeypatch, redis_error=True)
    async def run():
        async with main.lifespan(app):
            pytest.fail('startup must fail')
    with pytest.raises(RuntimeError, match='redis startup failed'):
        asyncio.run(run())
    db.close.assert_awaited_once()
    redis.aclose.assert_not_awaited()


def test_redis_close_failure_still_closes_database(monkeypatch):
    app, db, redis = configure(monkeypatch, close_error=True)
    async def run():
        async with main.lifespan(app):
            pass
    with pytest.raises(RuntimeError, match='redis close failed'):
        asyncio.run(run())
    db.close.assert_awaited_once()


def test_worker_startup_failure_stops_started_worker(monkeypatch):
    app, db, redis = configure(monkeypatch)
    def broken_tron(pool, stop):
        raise RuntimeError('tron startup failed')
    monkeypatch.setattr(main, 'run_tron_watcher', broken_tron)
    async def run():
        async with main.lifespan(app):
            pytest.fail('startup must fail')
    with pytest.raises(RuntimeError, match='tron startup failed'):
        asyncio.run(run())
    assert app.state.billing_automation_task.done()
    assert app.state.billing_automation_stop.is_set()
    redis.aclose.assert_awaited_once()
    db.close.assert_awaited_once()


def test_validation_failure_does_not_open_resources(monkeypatch):
    app, db, redis = configure(monkeypatch)
    def reject():
        raise RuntimeError('invalid deployment settings')
    monkeypatch.setattr(main, 'validate_startup_settings', reject)
    async def run():
        async with main.lifespan(app):
            pytest.fail('startup must fail')
    with pytest.raises(RuntimeError, match='invalid deployment settings'):
        asyncio.run(run())
    main.asyncpg.create_pool.assert_not_awaited()
    redis.aclose.assert_not_awaited()
