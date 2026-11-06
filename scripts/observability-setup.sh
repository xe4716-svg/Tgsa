#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="$ROOT/.local/observability"
TLS="$BASE/tls"
SECRETS="$BASE/secrets"
ENV_FILE="$BASE/.env"

command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

umask 077
mkdir -p "$TLS" "$SECRETS"

if [[ ! -s "$ENV_FILE" ]]; then
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  ADMIN_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  cat > "$ENV_FILE" <<EOF
GRAFANA_ADMIN_USER=admin
GRAFANA_ADMIN_PASSWORD=$ADMIN_PASSWORD
OTEL_COLLECTOR_BEARER_TOKEN=$TOKEN
EOF
  chmod 600 "$ENV_FILE"
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

if [[ ! -s "$TLS/ca.crt" || ! -s "$TLS/ca.key" ]]; then
  openssl req -x509 -newkey rsa:4096 -sha256 -nodes -days 365 \
    -keyout "$TLS/ca.key" -out "$TLS/ca.crt" \
    -subj "/CN=TGMate Local Observability CA"
  chmod 600 "$TLS/ca.key"
fi

if [[ ! -s "$TLS/server.crt" || ! -s "$TLS/server.key" ]]; then
  openssl req -new -newkey rsa:2048 -nodes \
    -keyout "$TLS/server.key" -out "$TLS/server.csr" \
    -subj "/CN=localhost"
  cat > "$TLS/server.ext" <<'EOF'
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:localhost,DNS:otel-collector,IP:127.0.0.1
EOF
  openssl x509 -req -in "$TLS/server.csr" -CA "$TLS/ca.crt" \
    -CAkey "$TLS/ca.key" -CAcreateserial -out "$TLS/server.crt" \
    -days 365 -sha256 -extfile "$TLS/server.ext"
  chmod 600 "$TLS/server.key"
fi

printf '%s\n' "$OTEL_COLLECTOR_BEARER_TOKEN" > "$SECRETS/otlp-bearer-token"
chmod 600 "$SECRETS/otlp-bearer-token"

echo "Local observability secrets and TLS material are ready under .local/observability."
echo "No generated private key, password or bearer token is tracked by Git."
