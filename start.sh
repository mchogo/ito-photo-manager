#!/bin/bash
# いとうさんフォトマネージャー — 統一起動スクリプト
# Backend (FastAPI) と Frontend (Next.js) を同時に起動します

cd "$(dirname "$0")"

cleanup() {
  echo ""
  echo "サーバーを停止しています..."
  kill $BACKEND_PID $FRONTEND_PID 2>/dev/null
  wait $BACKEND_PID $FRONTEND_PID 2>/dev/null
  echo "停止しました"
  exit 0
}
trap cleanup INT TERM

echo "============================="
echo " いとうさんフォトマネージャー"
echo "============================="
echo ""

# Backend 起動
echo "[Backend]  http://localhost:8000 で起動中..."
cd backend
uvicorn main:app --host 0.0.0.0 --port 8000 --reload &
BACKEND_PID=$!
cd ..

# Frontend 起動
echo "[Frontend] http://localhost:3000 で起動中..."
cd frontend
npm run dev &
FRONTEND_PID=$!
cd ..

echo ""
echo "Ctrl+C で両方のサーバーを停止できます"
echo ""

wait $BACKEND_PID $FRONTEND_PID
