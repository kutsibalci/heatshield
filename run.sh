#!/usr/bin/env bash
# Tek tuşla demo (Linux/macOS/Git Bash): ./run.sh [fixture|simulator]
MODE=${1:-simulator}; API_PORT=${API_PORT:-8000}; SIM_PORT=${SIM_PORT:-8081}
cd "$(dirname "$0")"
export NAC_MODE=$MODE NAC_FIXTURE_PATH="$PWD/fixtures/profiles.json"
if [ "$MODE" = "simulator" ]; then
  export NAC_BASE_URL="http://127.0.0.1:$SIM_PORT"
  python -m uvicorn apps.simulator.main:app --port $SIM_PORT &
  sleep 1
fi
echo "API: http://127.0.0.1:$API_PORT (demo: /demo) mode=$MODE"
python -m uvicorn apps.api.main:app --port $API_PORT --reload
