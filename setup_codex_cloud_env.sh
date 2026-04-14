#!/usr/bin/env bash
set -euo pipefail

REQUIRED_UV_VERSION="0.10.9"
E2E_SETUP_OVERWRITE_ENV="${E2E_SETUP_OVERWRITE_ENV:-1}"

log() { printf '[codex-setup] %s\n' "$*"; }

root="$(cd "$(dirname "$0")" && pwd)"
cd "$root"

APT_UPDATED=0

run_as_root_or_sudo() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
    return $?
  fi

  if command -v sudo >/dev/null 2>&1; then
    sudo "$@"
    return $?
  fi

  log "cannot run privileged command (need root or sudo): $*"
  return 1
}

apt_install() {
  if [ "$#" -eq 0 ]; then
    return 0
  fi

  if ! command -v apt-get >/dev/null 2>&1; then
    log "apt-get is not available; skip install: $*"
    return 1
  fi

  if [ "$APT_UPDATED" -eq 0 ]; then
    log "apt-get update"
    run_as_root_or_sudo apt-get update -y
    APT_UPDATED=1
  fi

  run_as_root_or_sudo apt-get install -y "$@"
}

uv_version() {
  local uv_bin="$1"
  "$uv_bin" --version | awk '{print $2}'
}

uv_meets_minimum_version() {
  local actual_version="$1"
  [ "$(printf '%s\n%s\n' "$REQUIRED_UV_VERSION" "$actual_version" | sort -V | tail -n1)" = "$actual_version" ]
}

ensure_uv() {
  local uv_bin=""

  if command -v uv >/dev/null 2>&1; then
    uv_bin="$(command -v uv)"
  elif [ -x "$HOME/.local/bin/uv" ]; then
    export PATH="$HOME/.local/bin:$PATH"
    uv_bin="$HOME/.local/bin/uv"
  fi

  if [ -n "$uv_bin" ]; then
    if uv_meets_minimum_version "$(uv_version "$uv_bin")"; then
      return 0
    fi
    log "upgrade uv to >= ${REQUIRED_UV_VERSION}"
  fi

  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
  export PATH="$HOME/.local/bin:$PATH"
}

ensure_nodejs() {
  if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
    return 0
  fi

  if ! command -v curl >/dev/null 2>&1; then
    log "curl is required to install Node.js"
    return 1
  fi

  if ! command -v apt-get >/dev/null 2>&1; then
    log "apt-get is required to install Node.js"
    return 1
  fi

  log "install Node.js LTS"
  if ! curl -fsSL https://deb.nodesource.com/setup_lts.x | run_as_root_or_sudo env bash -; then
    log "failed to configure NodeSource repository"
    return 1
  fi
  if ! apt_install nodejs; then
    log "failed to install Node.js"
    return 1
  fi
}

write_agent_phase_dependency_report() {
  local report_dir="$root/.codex-logs"
  local report_file="$report_dir/setup-agent-dependencies.txt"
  local missing_required=0

  mkdir -p "$report_dir"
  : > "$report_file"

  check_command() {
    local cmd="$1"
    local required="$2"
    local resolved

    resolved="$(command -v "$cmd" 2>/dev/null || true)"
    if [ -n "$resolved" ]; then
      printf 'command=%s required=%s status=ok path=%s\n' "$cmd" "$required" "$resolved" >> "$report_file"
      return 0
    fi

    printf 'command=%s required=%s status=missing path=-\n' "$cmd" "$required" >> "$report_file"
    if [ "$required" = "required" ]; then
      missing_required=1
    fi

    return 0
  }

  check_file() {
    local file_path="$1"
    local required="$2"

    if [ -e "$file_path" ]; then
      printf 'path=%s required=%s status=ok\n' "$file_path" "$required" >> "$report_file"
      return 0
    fi

    printf 'path=%s required=%s status=missing\n' "$file_path" "$required" >> "$report_file"
    if [ "$required" = "required" ]; then
      missing_required=1
    fi

    return 0
  }

  {
    printf '# setup-phase dependency report for codex agent phase\n'
    printf 'generated_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'note=agent-phase-default-is-offline;setup_and_agent_use_separate_shell_sessions\n'
  } >> "$report_file"

  check_command uv required
  check_command node required
  check_command npm required
  check_command curl required
  check_file "$root/frontend/node_modules" required
  check_file "$root/backend/.venv" required

  if [ "$missing_required" -eq 1 ]; then
    log "WARN: missing required dependencies for agent phase. inspect $report_file"
  else
    log "agent phase dependency check passed. report: $report_file"
  fi
}

log "start"

log "ensure uv"
ensure_uv

ensure_nodejs

log "install uv-managed Python 3.12 and 3.13"
uv python install 3.12 3.13

# 1) .env*.sample -> .env*（必要に応じて上書き）
if [ "$E2E_SETUP_OVERWRITE_ENV" = "1" ]; then
  log "copy .env*.sample -> .env* (overwrite existing)"
  find . \
    -type d \( -name .git -o -name node_modules -o -name .venv -o -name .next -o -name dist -o -name build \) -prune -false -o \
    -type f -name ".env*.sample" -print0 \
    | while IFS= read -r -d '' f; do
      tgt="${f%.sample}"
      cp "$f" "$tgt"
      log "copied: $tgt"
    done
else
  log "skip .env*.sample overwrite (E2E_SETUP_OVERWRITE_ENV=${E2E_SETUP_OVERWRITE_ENV})"
fi

# 2) 依存インストール（Makefile が冪等なので毎回実行）
if [[ -f Makefile ]] && grep -qE '(^|[[:space:]])install:' Makefile; then
  log "run: make install"
  make install
else
  log "no Makefile install target, skip"
fi

# 3) Codex 固有の処理を追加したい場合はここへ
# 例: log "run migrations"; make migrate || true

if ! command -v docker >/dev/null 2>&1; then
  log "docker is not available in this environment"
  log "setup phase finished. non-docker e2e path is disabled; use docker-capable environment for make e2e"
fi

log "note: setup and agent phases run in separate bash sessions; exports here do not persist automatically"
write_agent_phase_dependency_report

log "done"
