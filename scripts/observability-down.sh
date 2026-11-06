#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
docker compose --project-name tgmate-observability \
  --env-file .local/observability/.env \
  -f docker-compose.observability.yml down
echo "Local observability services stopped. Named volumes and local secrets were preserved."
