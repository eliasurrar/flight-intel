#!/bin/bash
# worker_runner_launcher.sh — Loads env and runs the Python worker loop.
set -u
cd /Users/openclaw/projects/flight-intel
set -a
. worker/.env
set +a
export FLIGHT_INTEL_WORKER_URL="https://flight-intel-worker.eliasurrar.workers.dev"
export FLIGHT_INTEL_BACKEND_TOKEN="$BACKEND_TOKEN"
echo "[$(date)] launcher: token_len=${#FLIGHT_INTEL_BACKEND_TOKEN}, prefix=${FLIGHT_INTEL_BACKEND_TOKEN:0:8}, url=$FLIGHT_INTEL_WORKER_URL"
exec .venv/bin/python backend/worker_runner.py

