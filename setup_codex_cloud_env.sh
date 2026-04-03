#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MODE="${1:-setup}"

case "$MODE" in
  setup)
    echo "[codex-cloud-setup] Running project setup script..."
    bash "$ROOT_DIR/scripts/setup.sh"
    ;;
  maintenance)
    echo "[codex-cloud-setup] Running maintenance setup script..."
    bash "$ROOT_DIR/scripts/setup-maintenance.sh"
    ;;
  maintenance-clean)
    echo "[codex-cloud-setup] Running maintenance setup script with --clean..."
    bash "$ROOT_DIR/scripts/setup-maintenance.sh" --clean
    ;;
  *)
    cat <<USAGE >&2
Usage:
  ./setup_codex_cloud_env.sh [setup|maintenance|maintenance-clean]
USAGE
    exit 1
    ;;
esac

echo "[codex-cloud-setup] Done."
