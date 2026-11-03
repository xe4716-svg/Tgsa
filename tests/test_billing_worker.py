import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
import app.workers.billing as worker


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(worker, 'settings', replace(worker.settings, database_url='postgresql://test', smtp_host='test', app_env='development', tron_watcher_enabled=False, platform_telegram_bot_token=None, platform_telegram_bot_username=None, platform_telegram_webhook_secret=None))
    pool = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(worker.asyncpg, 'create_pool', AsyncMock(return_value=pool))
    restored = []
    monkeypatch.setattr(worker, 'install_signal_handlers', lambda stop: [lambda: restored.append(True)])
    return pool, restored


def test_graceful_stop_and_independent_configuration(setup, monkeypatch):
    pool, restored = setup
    monkeypatch.setattr(worker, 'settings', replace(worker.settings, jwt_signing_secret=None, session_kek_base64=None))
    async def run():
        stop = asyncio.Event()
        async def billing(db, event):
            assert db is pool
            stop.set()
        monkeypatch.setattr(worker, 'run_billing_automation', billing)
        await worker.serve(stop)
    asyncio.run(run())
    pool.close.assert_awaited_once()
    assert restored == [True]


@pytest.mark.parametrize('fails', [True, False])
def test_worker_failure_or_early_exit_closes_pool(setup, monkeypatch, fails):
    pool, restored = setup
    async def billing(db, event):
        if fails:
            raise RuntimeError('worker failed')
    monkeypatch.setattr(worker, 'run_billing_automation', billing)
    with pytest.raises(RuntimeError, match='worker failed|exited before shutdown'):
        asyncio.run(worker.serve())
    pool.close.assert_awaited_once()
    assert restored


def test_failure_signals_other_worker(setup, monkeypatch):
    pool, _ = setup
    monkeypatch.setattr(worker, 'settings', replace(worker.settings, tron_watcher_enabled=True, tron_account_xpub='test', trongrid_api_key=None))
    stopped = []
    async def billing(db, stop):
        raise RuntimeError('failed')
    async def tron(db, stop):
        await stop.wait()
        stopped.append(True)
    monkeypatch.setattr(worker, 'run_billing_automation', billing)
    monkeypatch.setattr(worker, 'run_tron_watcher', tron)
    with pytest.raises(RuntimeError, match='failed'):
        asyncio.run(worker.serve())
    assert stopped == [True]
    pool.close.assert_awaited_once()


@pytest.mark.parametrize('changes,message', [({'smtp_host':None}, 'SMTP_HOST'), ({'database_url':''}, 'DATABASE_URL'), ({'app_env':'production','smtp_starttls':False,'smtp_use_tls':False}, 'TLS'), ({'tron_watcher_enabled':True,'tron_account_xpub':None}, 'XPUB')])
def test_invalid_required_settings_rejected_before_pool(setup, monkeypatch, changes, message):
    monkeypatch.setattr(worker, 'settings', replace(worker.settings, **changes))
    with pytest.raises(RuntimeError, match=message):
        asyncio.run(worker.serve())
    worker.asyncpg.create_pool.assert_not_awaited()


def test_signal_handlers_notify_stop_and_restore(monkeypatch):
    handlers, removed, restored = {}, [], []
    async def run():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, 'add_signal_handler', lambda sig, callback: handlers.update({sig:callback}))
        monkeypatch.setattr(loop, 'remove_signal_handler', lambda sig: removed.append(sig))
        monkeypatch.setattr(worker.signal, 'getsignal', lambda sig: 'previous')
        monkeypatch.setattr(worker.signal, 'signal', lambda sig, callback: restored.append((sig,callback)))
        stop = asyncio.Event()
        cleanup = worker.install_signal_handlers(stop)
        for callback in handlers.values():
            stop.clear()
            callback()
            assert stop.is_set()
        for callback in cleanup:
            callback()
    asyncio.run(run())
    assert set(removed) == {worker.signal.SIGINT, worker.signal.SIGTERM}
    assert all(previous == 'previous' for _, previous in restored)


def test_windows_signal_fallback(monkeypatch):
    handlers = {}
    async def run():
        loop = asyncio.get_running_loop()
        def unsupported(*args):
            raise NotImplementedError
        monkeypatch.setattr(loop, 'add_signal_handler', unsupported)
        monkeypatch.setattr(worker.signal, 'getsignal', lambda sig: None)
        monkeypatch.setattr(worker.signal, 'signal', lambda sig, callback: handlers.update({sig:callback}))
        stop = asyncio.Event()
        cleanup = worker.install_signal_handlers(stop)
        handlers[worker.signal.SIGTERM](worker.signal.SIGTERM, None)
        await asyncio.sleep(0)
        assert stop.is_set()
        for callback in cleanup:
            callback()
        assert all(callback is None for callback in handlers.values())
    asyncio.run(run())
