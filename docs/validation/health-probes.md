# Health probe validation

2026-11-02: local Python 3.11, Windows. Base d5770627c6e7e2b0e60260f0a036f9604c2d26a9.

`python -m pytest tests -q --junitxml=recovery-test-results.xml`: 49 passed, 1 Starlette deprecation warning, 4.64s.

Probe unit tests mount the existing health router in an isolated FastAPI app. Production startup validation is unchanged. Readiness covers both dependencies healthy and each of three outage combinations. Database and Redis responses are simulated; production startup, database migrations, actual payments and Telegram integrations were not validated by this run.
