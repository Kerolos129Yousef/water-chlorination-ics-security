#!/usr/bin/env bash
# Phase 10B - repeatable deploy of ICS Guardian onto the EC2 instance.
#
# Runs ON the instance, invoked through SSM (Session Manager / RunCommand) as
# root. It is idempotent and contains NO secrets. The application images are
# PUBLIC on Docker Hub, so no `docker login` is needed.
#
# What it does:
#   1. Verifies Docker + the compose plugin and the mounted /data volume.
#   2. Fetches the cloud compose file pinned to the deploy commit SHA.
#   3. Pulls the commit-SHA-tagged images and starts the stack.
#   4. Waits for health and prints verification output.
#
# Usage (on the instance, via SSM):
#   sudo ICS_IMAGE_TAG=<commit-sha> \
#        ICS_RAW_BASE=https://raw.githubusercontent.com/Kerolos129Yousef/water-chlorination-ics-security/<commit-sha> \
#        bash deploy.sh
#
# Rollback: re-run with ICS_IMAGE_TAG set to a PREVIOUS commit SHA (and the
# matching ICS_RAW_BASE). Images are immutable per-SHA, so rollback is exact.
set -euo pipefail

: "${ICS_IMAGE_TAG:?set ICS_IMAGE_TAG to the commit-SHA image tag to deploy}"
: "${ICS_RAW_BASE:?set ICS_RAW_BASE to the raw repo URL at the same commit SHA}"

APP_DIR=/opt/ics-guardian
COMPOSE_FILE="$APP_DIR/docker-compose.aws.yml"
DATA_MNT=/data

echo "== 1. Preflight: Docker, compose, /data =="
docker --version
docker compose version
systemctl is-active --quiet docker || { echo "docker not running"; exit 1; }
mountpoint -q "$DATA_MNT" || echo "WARN: $DATA_MNT is not a separate mountpoint (data would live on the root volume)"
# Must be writable by the non-root backend uid (10001).
own="$(stat -c '%u:%g' "$DATA_MNT")"
[ "$own" = "10001:10001" ] || { echo "fixing $DATA_MNT ownership ($own -> 10001:10001)"; chown 10001:10001 "$DATA_MNT"; }

echo "== 2. Fetch pinned compose file =="
mkdir -p "$APP_DIR"
curl -fsSL "$ICS_RAW_BASE/deploy/docker-compose.aws.yml" -o "$COMPOSE_FILE"

echo "== 3. Pull + start (tag: $ICS_IMAGE_TAG) =="
export ICS_IMAGE_TAG
docker compose -f "$COMPOSE_FILE" pull
docker compose -f "$COMPOSE_FILE" up -d

echo "== 4. Wait for health =="
for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1/healthz >/dev/null 2>&1 \
     && curl -fsS http://127.0.0.1/api/health >/dev/null 2>&1; then
    echo "frontend + proxied backend healthy"; break
  fi
  echo "  waiting for health ($i/30)..."; sleep 5
done

echo "== 5. Verify =="
echo "--- container status ---"; docker compose -f "$COMPOSE_FILE" ps
echo "--- backend /health (via nginx /api) ---"; curl -fsS http://127.0.0.1/api/health | head -c 400; echo
echo "--- backend /status (via nginx /api) ---"; curl -fsS http://127.0.0.1/api/status | head -c 400; echo
echo "--- confirm port 8000 is NOT published on the host ---"
if ss -ltn 2>/dev/null | grep -q ':8000 '; then echo "WARN: something is listening on host :8000"; else echo "OK: nothing on host :8000"; fi
echo "Deploy complete. Dashboard: http://<public-ip>/  (API proxied at /api/)."
