# いとうさんフォトマネージャー (ito-photo-manager)

## 概要
撮影写真・書類等の管理を一元化する業務アプリケーションです。
現場での撮影報告、書類アップロード、Excel 出力、ユーザー管理などの機能を提供します。

## 技術スタック
| レイヤー | 技術 |
|---|---|
| Frontend | Next.js 15 (App Router), React 19, TailwindCSS |
| Backend | FastAPI, Pydantic v2 |
| テスト | Pytest (Backend), Playwright (E2E) |
| Linter | Ruff (Python), ESLint (Frontend) |
| CI | GitHub Actions (`ci-pr.yml`) |

## ディレクトリ構成

```
ito-photo-manager/
├── backend/              # FastAPI バックエンド
│   ├── main.py           # エントリーポイント
│   ├── models.py         # Pydantic モデル
│   ├── storage.py        # ファイルストレージ
│   ├── auth.py           # JWT 認証
│   ├── tests/            # Pytest テスト群
│   ├── requirements.txt
│   ├── pytest.ini
│   └── .ruff.toml
├── frontend/             # Next.js フロントエンド
│   ├── app/              # App Router ページ群
│   ├── components/       # UI コンポーネント
│   └── lib/api.ts        # API クライアント (共通ラッパー)
├── e2e/                  # Playwright E2E テスト
│   ├── tests/
│   └── playwright.config.ts
├── data/                 # ランタイムデータ (gitignore 対象)
├── docs/
│   ├── coding-standards/ # コーディング規約
│   └── feature_requests/ # 機能要望
├── .github/workflows/    # GitHub Actions CI
├── Makefile              # 開発コマンド一元管理
├── AGENTS.md             # AI エージェント向けガイドライン
├── start.sh              # 開発用起動スクリプト
└── start-prod.sh         # 本番用起動スクリプト
```

## セットアップ手順

### 前提条件
- Python 3.13+
- Node.js 22+
- npm

### セキュリティ環境変数（推奨）

```bash
# JWT署名鍵（本番では必須）
export ITO_PM_SECRET_KEY="十分に長いランダム文字列"

# 初期管理者パスワード（初回起動時のadmin作成に使用）
export ITO_PM_DEFAULT_ADMIN_PASSWORD="強固な初期パスワード"
```

`ITO_PM_SECRET_KEY` 未設定時は `data/.jwt_secret_key` を自動生成して使用します。  
`ITO_PM_DEFAULT_ADMIN_PASSWORD` 未設定時はランダムな初期管理者パスワードを自動生成し、バックエンドログに出力します。

### インストール

```bash
# 全体の依存関係をインストール
make install

# 個別にインストールする場合
make install TARGET=backend    # pip install -r requirements.txt
make install TARGET=frontend   # npm install
make install TARGET=e2e        # npm install + playwright install
```

### 起動

```bash
# フロントエンド・バックエンドを同時起動 (開発用)
make run

# 本番用起動
./start-prod.sh
```

バックエンド: `http://localhost:8000`
フロントエンド: `http://localhost:3000`

## テスト・バリデーション

PR 作成前には必ず以下を実行してください:

```bash
# 全体チェック (Ruff lint + Pytest + ESLint + TypeScript)
make check

# バックエンドのみ
make check TARGET=backend

# フロントエンドのみ
make check TARGET=frontend

# E2E テスト (要: フロントエンド・バックエンド起動状態)
make e2e
```

## 開発ガイドライン
- [AI エージェント向けガイドライン (AGENTS.md)](./AGENTS.md)
- [基本的なコーディング規約](./docs/coding-standards/基本的なコーディング規約.md)
- [FastAPI エンドポイント設計ポリシー](./docs/coding-standards/FastAPIエンドポイント設計ポリシー.md)
- [フロントエンドでの API の叩き方](./docs/coding-standards/フロントエンドでのAPIの叩き方.md)
