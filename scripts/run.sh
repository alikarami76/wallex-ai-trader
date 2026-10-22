#!/usr/bin/env bash
# Load .env if present, then run one trader mode.
#   ./scripts/run.sh shadow --duration 900 --fresh
#   ./scripts/run.sh live
#   ./scripts/run.sh backtest
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
MODE="${1:-shadow}"
shift || true
exec python3 -m trader --mode "$MODE" --config config/default.yaml "$@"
