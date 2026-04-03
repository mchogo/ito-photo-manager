#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/backend/venv}"

log() { echo "[maintenance] $*"; }

usage() {
  cat <<USAGE
使い方: bash scripts/setup-maintenance.sh [--clean]

オプション:
  --clean   backend/frontend の依存を入れ直します
USAGE
}

CLEAN=false
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
elif [[ "${1:-}" == "--clean" ]]; then
  CLEAN=true
elif [[ -n "${1:-}" ]]; then
  echo "不明なオプション: $1" >&2
  usage
  exit 1
fi

if [[ "$CLEAN" == true ]]; then
  log "クリーンアップを実施します"
  rm -rf "$ROOT_DIR/frontend/node_modules" "$ROOT_DIR/frontend/.next"
  rm -rf "$VENV_DIR"
fi

log "通常セットアップを実行します"
bash "$ROOT_DIR/scripts/setup.sh"

log "バックエンドテストを実行します"
# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"
cd "$ROOT_DIR/backend"
python -m pytest tests/ -q

log "フロントエンドのビルド確認を実行します"
cd "$ROOT_DIR/frontend"
npm run build >/tmp/ito-photo-manager-frontend-build.log 2>&1 || {
  echo "[maintenance][error] frontend build に失敗しました。ログ: /tmp/ito-photo-manager-frontend-build.log" >&2
  exit 1
}

log "メンテナンスセットアップが完了しました"
