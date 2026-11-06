#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
bash scripts/observability-setup.sh
docker compose --project-name tgmate-observability \
  --env-file .local/observability/.env \
  -f docker-compose.observability.yml up -d
echo "Waiting for local observability endpoints..."
python3 scripts/observability-smoke.py --wait-only
