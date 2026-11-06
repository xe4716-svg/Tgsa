#!/usr/bin/env bash
# Source this file in the shell that starts a local TGMate worker:
#   source scripts/observability-env.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.local/observability/.env"
CA_FILE="$ROOT/.local/observability/tls/ca.crt"

if [[ ! -s "$ENV_FILE" || ! -s "$CA_FILE" ]]; then
  echo "Run scripts/observability-setup.sh first." >&2
  return 1 2>/dev/null || exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

export OTEL_EXPORTER_OTLP_ENDPOINT="https://localhost:4318"
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer $OTEL_COLLECTOR_BEARER_TOKEN"
export OTEL_EXPORTER_OTLP_CERTIFICATE="$CA_FILE"
export OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:-tgmate-gateway-worker}"
export OTEL_TRACES_SAMPLER_ARG="${OTEL_TRACES_SAMPLER_ARG:-0.1}"

echo "OTLP trace/metrics export configured for https://localhost:4318."
