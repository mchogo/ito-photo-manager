# いとうさんフォトマネージャー

現場でのPOSレジ・通信機器の撮影漏れを防止し、提出用Excelの作成を自動化するWebアプリケーション。

## 機能

- **機器選択**: チェックボックスで導入機器を選択すると、必要な撮影項目が自動生成される
- **撮影ナビ**: カード形式で撮影スロットを表示。未撮影=赤、完了=緑で一目で状況把握
- **バリデーション**: 全撮影項目が埋まるまでExcel出力を防止
- **Excel報告書自動生成**: 写真をセルに埋め込んだExcelファイルをワンクリックでダウンロード
- **画像自動圧縮**: アップロード時に自動リサイズ・JPEG圧縮（EXIF回転補正対応）

## 対応機器（マスターデータ）

| 機器名 | 撮影項目 |
|---|---|
| POSレジ本体 | 正面, 背面, シリアル番号（3枚） |
| キャッシュドロア | 全体, 接続部（2枚） |
| レシートプリンタ | 正面, シリアル番号（2枚） |
| ルーター | 正面, 接続・配線（2枚） |
| LAN配線 | 全体俯瞰, 接続ポイント（2枚） |

## セットアップ手順

### 前提条件

- Python 3.9以上
- Node.js 18以上
- npm

### 1. バックエンド（FastAPI）

```bash
cd backend

# 依存パッケージのインストール
pip install -r requirements.txt

# サーバー起動（ポート8000）
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### 2. フロントエンド（Next.js）

```bash
cd frontend

# 依存パッケージのインストール
npm install

# 開発サーバー起動（ポート3000）
npm run dev
```

### 3. アクセス

ブラウザで http://localhost:3000 を開く。

> フロントエンドの `next.config.ts` で `/api/*` へのリクエストが自動的にバックエンド（ポート8000）にプロキシされます。

## テスト実行

```bash
cd backend
python -m pytest tests/ -v
```

## 使い方

1. トップ画面で **現場ID**、**作業日**、**作業員名** を入力
2. 導入する **機器をチェック** して「撮影開始」をタップ
3. 撮影ナビ画面で各スロットの **「カメラ」** または **「ファイル選択」** で写真を登録
4. 全スロットが緑になったら **「プレビュー / 提出へ」** をタップ
5. プレビュー画面で内容を確認し、**「Excel出力」** でExcelファイルをダウンロード

## プロジェクト構成

```
ito-photo-manager/
├── backend/                   # Python FastAPI バックエンド
│   ├── main.py                # APIエントリポイント
│   ├── models.py              # Pydanticモデル
│   ├── equipment_master.py    # 機器マスター定義
│   ├── storage.py             # ローカルファイルストレージ
│   ├── excel_export.py        # Excel生成（openpyxl）
│   ├── image_utils.py         # 画像リサイズ・圧縮（Pillow）
│   ├── requirements.txt
│   └── tests/                 # pytest テスト（41件）
├── frontend/                  # Next.js フロントエンド
│   ├── app/                   # App Router ページ
│   │   ├── page.tsx           # 案件作成 + 機器選択
│   │   ├── shoot/page.tsx     # 撮影ナビ
│   │   └── preview/page.tsx   # プレビュー / 提出
│   ├── components/            # UIコンポーネント
│   ├── lib/api.ts             # APIクライアント
│   └── types/index.ts         # TypeScript型定義
├── data/                      # ランタイムデータ（自動生成）
│   ├── projects/              # 案件JSON
│   └── photos/                # 写真ファイル
└── README.md
```

## 主要な設計判断

### 技術スタック
- **Next.js (App Router)** + **FastAPI**: リッチなUIとPython Excel処理を両立
- **ローカルファイルストレージ**: セットアップ不要で即動作。後からクラウド連携を追加可能

### 写真ファイル名
- `{機器名}_{現場ID}_{YYYYMMDD_HHMMSS_ffffff}.jpg` 形式
- 目視でも判別可能な命名規則（仕様書準拠）

### バリデーション
- **APIレベル**: `/validate` エンドポイントで未撮影スロットを返却
- **UIレベル**: 未撮影がある場合、プレビューボタン/Excel出力ボタンを無効化し警告表示

### 画像処理
- アップロード時に最大幅800pxにリサイズ + JPEG品質85%で圧縮
- Excel埋め込み時はさらに300pxにリサイズ
- EXIF回転情報を適用（スマホ写真の向き補正）

### Excel出力
- openpyxlでゼロからワークブックを生成
- ヘッダに案件情報、機器ごとにセクション分け、各スロットに画像を埋め込み

## API一覧

| Method | Path | 説明 |
|---|---|---|
| GET | `/api/equipment` | 機器マスター一覧 |
| POST | `/api/projects` | 案件作成 |
| GET | `/api/projects/{id}` | 案件データ取得 |
| POST | `/api/projects/{id}/photos` | 写真アップロード |
| DELETE | `/api/projects/{id}/photos` | 写真削除 |
| GET | `/api/projects/{id}/validate` | バリデーション |
| GET | `/api/projects/{id}/export` | Excel出力 |
| GET | `/api/photos/{filename}` | 写真ファイル配信 |
