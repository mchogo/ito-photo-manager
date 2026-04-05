# =============================================================================
# ito-photo-manager Makefile
#
# Usage:
#   make run                         # フロントとバックエンドを同時に起動
#   make down                        # docker等を使用している場合（現在はローカルプロセス想定）
#
# 依存インストール:
#   make install                     # backend, frontend を一括インストール
#   make install TARGET=frontend     # frontendでnpm install
#   make install TARGET=backend      # backendでpip install
#   make install TARGET=e2e          # e2e依存をインストール
#
# ローカルCI（PR前に実行必須）:
#   make check                       # frontend + backend を一括チェック
#   make check TARGET=frontend       # lint + typecheck (必要に応じてtest)
#   make check TARGET=backend        # pytest 等（Pydanticバリデーション等含む）
#
# E2Eテスト:
#   make e2e                         # E2Eテスト(Playwright等)を実行
# =============================================================================

FRONTEND_DIR := frontend
BACKEND_DIR := backend
E2E_DIR := e2e

PM ?= npm
PYTHON ?= python3

help: ## コマンド一覧を表示
	@echo ""
	@echo "Usage: make <target>"
	@echo ""
	@awk 'BEGIN {FS = ":.*##"; printf "\nAvailable targets:\n"} /^[a-zA-Z0-9_.-]+:.*##/ {printf "  %-28s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo ""

# ==== Install ====
install: ## frontend/backend/e2e を一括インストール
	@if [ -n "$(TARGET)" ]; then \
		$(MAKE) install-$(TARGET); \
	else \
		echo "[INSTALL] all (frontend, backend, e2e)"; \
		$(MAKE) install-frontend; \
		$(MAKE) install-backend; \
		$(MAKE) install-e2e; \
	fi

install-frontend:
	@echo "[INSTALL] frontend -> $(FRONTEND_DIR)"
	@cd $(FRONTEND_DIR) && $(PM) install

install-backend:
	@echo "[INSTALL] backend -> $(BACKEND_DIR)"
	@cd $(BACKEND_DIR) && $(PYTHON) -m pip install -r requirements.txt

install-e2e:
	@if [ -d "$(E2E_DIR)" ]; then \
		echo "[INSTALL] e2e -> $(E2E_DIR)"; \
		cd $(E2E_DIR) && $(PM) install && npx playwright install; \
	else \
		echo "[INSTALL] e2e dir not found. Skipping."; \
	fi

# ==== Run ====
run: ## frontend と backend をローカルで同時起動 (Ctrl+Cで両方停止)
	@echo "[RUN] Starting frontend and backend..."
	@trap 'kill 0' SIGINT; \
	(cd $(BACKEND_DIR) && venv/bin/python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000) & \
	(cd $(FRONTEND_DIR) && $(PM) run dev) & \
	wait

down: ## docker環境等があれば停止 (現状はプレースホルダ)
	@echo "[DOWN] local processes are stopped via Ctrl+C."

# ==== Check (Lint/Typecheck/Test) ====
check: ## frontend + backend を一括チェック
	@if [ -n "$(TARGET)" ]; then \
		$(MAKE) check-$(TARGET); \
	else \
		echo "[CHECK] all (frontend + backend)"; \
		$(MAKE) check-frontend; \
		$(MAKE) check-backend; \
	fi

check-frontend:
	@echo "[CHECK] frontend: lint + type-check"
	@cd $(FRONTEND_DIR) && $(PM) run lint --if-present
	@cd $(FRONTEND_DIR) && npx tsc --noEmit

check-backend:
	@echo "[CHECK] backend: ruff lint"
	@cd $(BACKEND_DIR) && $(PYTHON) -m ruff check .
	@echo "[CHECK] backend: pytest"
	@cd $(BACKEND_DIR) && $(PYTHON) -m pytest -q tests/

e2e: ## Playwright E2Eテストを実行
	@echo "[E2E] Running e2e tests..."
	@if [ -d "$(E2E_DIR)" ]; then \
		cd $(E2E_DIR) && $(PM) install --legacy-peer-deps && npx playwright test; \
	else \
		echo "[E2E] e2e dir not found."; \
	fi

.PHONY: help install install-frontend install-backend install-e2e run down check check-frontend check-backend e2e
