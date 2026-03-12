#!/bin/bash
# いとうさんフォトマネージャー — 本番起動スクリプト
# 初回または --build オプション付きで next build を実行してから起動します
# 使い方:
#   bash start-prod.sh           # ビルド済みなら即起動
#   bash start-prod.sh --build   # 再ビルドしてから起動

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
echo "       本番モード"
echo "============================="
echo ""

# ビルドが必要か判定
NEED_BUILD=false
if [ "$1" = "--build" ]; then
  NEED_BUILD=true
elif [ ! -d "frontend/.next" ]; then
  echo "[Frontend] ビルドが見つかりません。ビルドを実行します..."
  NEED_BUILD=true
fi

if [ "$NEED_BUILD" = true ]; then
  echo "[Frontend] ビルド中... (しばらくかかります)"
  cd frontend
  npm run build
  if [ $? -ne 0 ]; then
    echo "[エラー] ビルドに失敗しました"
    exit 1
  fi
  cd ..
  echo "[Frontend] ビルド完了"
  echo ""
fi

# Backend 起動
echo "[Backend]  http://localhost:8000 で起動中..."
cd backend
source venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!
cd ..

# Frontend 起動
echo "[Frontend] http://localhost:3000 で起動中..."
cd frontend
npm run start &
FRONTEND_PID=$!
cd ..

echo ""
echo "Ctrl+C で両方のサーバーを停止できます"
echo ""

wait $BACKEND_PID $FRONTEND_PID
