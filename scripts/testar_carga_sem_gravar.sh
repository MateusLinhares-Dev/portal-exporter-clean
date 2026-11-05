#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
limite="${1:-1000}"
if ! [[ "$limite" =~ ^[1-9][0-9]*$ ]] || (( limite > 1000000 )); then
  echo 'Informe um limite de 1 a 1000000 documentos.' >&2
  exit 2
fi
if [[ ! -f .env ]]; then
  echo 'Arquivo .env nao encontrado.' >&2
  exit 1
fi
key="$(sed -n 's/^APP_API_KEY=//p' .env | tail -n 1)"
if [[ -z "$key" ]]; then
  echo 'APP_API_KEY nao configurada no .env.' >&2
  exit 1
fi
curl --fail-with-body --silent --show-error --max-time 30 \
  -X POST http://127.0.0.1:8080/api/v1/sync \
  -H "X-API-Key: ${key}" -H 'Content-Type: application/json' \
  --data "{\"requested_by\":\"teste-carga-sem-smb\",\"mode\":\"test\",\"dry_run\":true,\"max_reports\":${limite},\"max_portals\":0,\"max_depth\":12}"
echo
