#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/backend/venv}"
FRONTEND_INSTALL_CMD="${FRONTEND_INSTALL_CMD:-npm ci}"

log() { echo "[setup] $*"; }
warn() { echo "[setup][warn] $*"; }
err() { echo "[setup][error] $*" >&2; }

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    err "'$1' が見つかりません。インストールして再実行してください。"
    exit 1
  fi
}

log "リポジトリ: $ROOT_DIR"
require_cmd "$PYTHON_BIN"
require_cmd npm
require_cmd node

log "Python 仮想環境を作成します: $VENV_DIR"
"$PYTHON_BIN" -m venv "$VENV_DIR"
# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"

log "backend 依存パッケージをインストールします"
pip install --upgrade pip
pip install -r "$ROOT_DIR/backend/requirements.txt"

log "frontend 依存パッケージをインストールします (${FRONTEND_INSTALL_CMD})"
cd "$ROOT_DIR/frontend"
# shellcheck disable=SC2086
$FRONTEND_INSTALL_CMD

log "データディレクトリを初期化します"
mkdir -p "$ROOT_DIR/data/projects" "$ROOT_DIR/data/photos" "$ROOT_DIR/data/documents"

if [[ ! -f "$ROOT_DIR/data/users.json" ]]; then
  warn "data/users.json が未作成です。バックエンド初回起動時にデフォルト管理者が自動作成されます。"
fi

cat <<MSG

セットアップが完了しました。

次のコマンドで起動できます:
  bash start.sh

または個別起動:
  cd backend && source venv/bin/activate && uvicorn main:app --reload --host 0.0.0.0 --port 8000
  cd frontend && npm run dev
MSG
