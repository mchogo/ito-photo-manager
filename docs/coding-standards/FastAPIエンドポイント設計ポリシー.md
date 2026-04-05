# FastAPI エンドポイント設計ポリシー

## リクエスト / レスポンスの定義

- すべてのエンドポイントは **Pydantic BaseModel** でリクエスト Body とレスポンスを定義する。
- リクエストモデルには `ConfigDict(extra="forbid")` を設定し、未知フィールドを拒否する。
- レスポンスモデルにも `extra="forbid"` を設定し、スキーマ外のフィールドが混入しないようにする。

> **注意**: `strict=True` は JSON の文字列→date/datetime 変換を阻害するため、リクエスト/レスポンスモデルでは使用しない。

## エラーハンドリング

すべての `HTTPException` は以下の統一フォーマットで `detail` を返す:

```python
raise HTTPException(
    status_code=400,
    detail={"code": "BAD_REQUEST", "message": "具体的なエラー内容"},
)
```

### 使用するエラーコード

| HTTP Status | code | 用途 |
|---|---|---|
| 400 | `BAD_REQUEST` | バリデーションエラー、不正な入力 |
| 400 | `UNSUPPORTED_MEDIA_TYPE` | 非対応ファイル形式 |
| 400 | `PAYLOAD_TOO_LARGE` | ファイルサイズ超過 |
| 401 | `UNAUTHORIZED` | 認証失敗 |
| 404 | `NOT_FOUND` | リソース未検出 |
| 500 | `INTERNAL_SERVER_ERROR` | サーバー内部エラー |

## 認証・認可

- 認証は JWT ベースで `Depends(get_current_user)` を利用。
- 管理者専用エンドポイントには `Depends(require_admin)` を付与。
- エンドポイント内でロール判定を直接行わない。

## パフォーマンス

- ファイル I/O を伴う処理は非同期 (`async`) で実装する。
- 大量データの一覧取得にはページネーション (将来対応) を検討する。
