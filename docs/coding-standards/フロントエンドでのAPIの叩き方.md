# フロントエンドでの API の叩き方

## 原則

フロントエンドからバックエンド API を呼び出す際は、**必ず `lib/api.ts` の共通ラッパー関数を経由**する。

### NG パターン

```typescript
// ❌ コンポーネント内で直接 fetch
const res = await fetch("/api/projects");
```

### OK パターン

```typescript
// ✅ 共通ラッパーを使用
import { fetchProjects } from "@/lib/api";
const projects = await fetchProjects();
```

## `lib/api.ts` の責務

1. **認証ヘッダーの自動付与**: localStorage からトークンを取り出し `Authorization` ヘッダーに設定。
2. **エラーハンドリングの統一**: バックエンドの `ErrorResponse` (`{code, message}`) をパースし、人が読めるメッセージを `throw new Error(message)` する。
3. **401 時の自動リダイレクト**: トークン切れ等で 401 が返った場合、自動的にログインページへリダイレクトする。

## 新しいエンドポイントを追加する場合

1. バックエンドに Pydantic モデル付きのエンドポイントを追加する。
2. `lib/api.ts` に対応する関数を追加する。
3. コンポーネントからはその関数を `import` して利用する。
