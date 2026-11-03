"""Run commercial background work separately from the HTTP API."""
import asyncio
import signal
from contextlib import AsyncExitStack

import asyncpg
from app.billing.automation import run_billing_automation
from app.billing.tron_watcher import run_tron_watcher
from app.config import settings


def validate_worker_settings():
    if not settings.database_url:
        raise RuntimeError('DATABASE_URL is required')
    # The existing billing loop always processes its durable email outbox.
    if not settings.smtp_host:
        raise RuntimeError('SMTP_HOST is required for billing email delivery')
    if settings.app_env.lower() == 'production' and not (settings.smtp_starttls or settings.smtp_use_tls):
        raise RuntimeError('Production SMTP must use TLS')
    telegram = (settings.platform_telegram_bot_token, settings.platform_telegram_bot_username,
                settings.platform_telegram_webhook_secret)
    if any(telegram) and not all(telegram):
        raise RuntimeError('Configure all platform Telegram bot settings together')
    if settings.tron_watcher_enabled and not settings.tron_account_xpub:
        raise RuntimeError('TRON_ACCOUNT_XPUB is required when the TRON watcher is enabled')


def install_signal_handlers(stop):
    loop = asyncio.get_running_loop()
    restorers = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous = signal.getsignal(signum)
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:  # Windows event loops lack add_signal_handler.
            signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
            restorers.append(lambda s=signum, p=previous: signal.signal(s, p))
        else:
            def restore(s=signum, p=previous):
                loop.remove_signal_handler(s)
                signal.signal(s, p)
            restorers.append(restore)
    return restorers


async def serve(stop=None):
    validate_worker_settings()
    stop = stop or asyncio.Event()
    async with AsyncExitStack() as cleanup:
        pool = await asyncpg.create_pool(dsn=settings.database_url, min_size=1,
                                        max_size=10, command_timeout=10)
        cleanup.push_async_callback(pool.close)
        for restore in install_signal_handlers(stop):
            cleanup.callback(restore)
        tasks = []
        async def finish():
            stop.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
        cleanup.push_async_callback(finish)
        tasks.append(asyncio.create_task(run_billing_automation(pool, stop)))
        if settings.tron_watcher_enabled:
            tasks.append(asyncio.create_task(run_tron_watcher(pool, stop)))
        stopping = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait([*tasks, stopping], return_when=asyncio.FIRST_COMPLETED)
            for task in tasks:
                if task in done:
                    if task.cancelled():
                        raise RuntimeError('Background worker was unexpectedly cancelled')
                    task.result()
                    if not stop.is_set():
                        raise RuntimeError('Background worker exited before shutdown')
        finally:
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)


def main():
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
