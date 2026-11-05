#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -f .env ]]; then
  echo '.env nao encontrado.' >&2
  exit 1
fi
image="$(sed -n 's/^APP_IMAGE=//p' .env | tail -n 1)"
if [[ -z "$image" ]]; then
  echo 'APP_IMAGE nao definida em .env.' >&2
  exit 1
fi
docker build -t "$image" .
docker compose -f docker-compose.prod.yml up -d --force-recreate
docker compose -f docker-compose.prod.yml ps
