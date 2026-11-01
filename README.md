# TG SaaS V1.0

This private repository contains the initial TG SaaS V1.0 foundation package.

## Source package

The initial source snapshot is provided as `tg_saas_v1_foundation.zip`. Extract it to obtain the FastAPI application, PostgreSQL migrations, Telegram account/session security modules, membership and feature entitlement logic, tests, Docker configuration, and implementation documentation.

## Security

- Never commit `.env`, Telegram session files, API credentials, private keys, database dumps, or production secrets.
- `.env.example` contains placeholders only. Copy it to `.env` locally and generate fresh secrets before running.
- The repository is private.

## Status

Foundation snapshot; not a production deployment. Review `docs/ROADMAP.md` and `README.md` inside the source archive before deployment.
