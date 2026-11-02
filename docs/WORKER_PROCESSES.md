# Commercial worker process

Run the API and commercial background worker separately:

- API: `python -m uvicorn app.main:app`
- Billing worker: `python -m app.workers.billing`

`RUN_BACKGROUND_WORKERS` now defaults to `false`. Existing single-process deployments must start the new worker service or explicitly set this flag to `true` as a temporary compatibility measure. Docker Compose includes a `billing` service and forces API background execution off. Do not treat the commercial worker as the full Telegram Bot/MTProto process implementation; those runtime integrations remain separate work.

The worker reuses current database leases, renewal/reminder/email/Telegram notification processing, and the optional TRON watcher. It changes neither sending policy nor retry/reconciliation policy. It owns a PostgreSQL pool, handles SIGINT/SIGTERM, signals both loops before waiting, and closes resources even if a loop fails. Unexpected loop termination produces a nonzero exit. Graceful stopping waits for current operations; container stop grace is 60 seconds, which is not a guarantee for every stalled external operation.

Required configuration is DATABASE_URL and SMTP_HOST (billing always processes the durable email outbox); production SMTP must use TLS. Platform Telegram settings must be provided together when enabled. No API JWT secret, Session encryption key, or Redis connection is required by this entry point. TRON remains disabled by default. When enabled, an account xpub is required for its deposit-address model. The TronGrid API key is optional: existing request construction uses an empty string rather than a None header when no key is set. Provider limits and external access still require actual deployment validation.

Validation uses simulated workers and pools: production secrets, payment actions and Telegram sends are never invoked by the worker lifecycle tests. Full deployment still requires actual PostgreSQL/SMTP/provider integration and shutdown testing in the target environment.

Local validation (2026-11-02, Python 3.11, Windows): `python -m pytest tests -q --junitxml=worker-test-results.xml` completed with 97 passed, 2 opt-in database tests skipped, and 1 existing Starlette deprecation warning in 5.53s. Lifecycle/signal/worker tests use simulated resources, including Windows signal fallback. This result does not claim a live Docker deployment, real chain scan, SMTP delivery or Telegram send.
