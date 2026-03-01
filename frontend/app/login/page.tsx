"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { login } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      await login(username, password);
      router.push("/worker");
    } catch (err) {
      setError(err instanceof Error ? err.message : "ログインに失敗しました");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center px-4">
      <div className="w-full max-w-sm space-y-6 animate-fade-in-up">
        {/* Logo */}
        <div className="text-center space-y-2">
          <div
            className="w-16 h-16 rounded-3xl flex items-center justify-center text-3xl mx-auto"
            style={{
              background: "linear-gradient(135deg, rgba(99,102,241,0.2), rgba(139,92,246,0.2))",
              border: "1px solid rgba(255,255,255,0.5)",
              boxShadow: "inset 0 1px 0 rgba(255,255,255,0.6)",
            }}
          >
            📷
          </div>
          <h1 className="text-xl font-extrabold text-gray-900 tracking-tight">フォトマネージャー</h1>
          <p className="text-xs text-gray-500 font-medium">現場撮影管理システム</p>
        </div>

        {/* Login form */}
        <form onSubmit={handleSubmit} className="liquid-glass p-6 space-y-4">
          <h2 className="text-base font-bold text-gray-700 text-center">ログイン</h2>

          <div className="space-y-1">
            <label className="text-xs font-bold text-gray-500 uppercase tracking-wider">
              ユーザー名
            </label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
              autoComplete="username"
              placeholder="username"
              className="input-glass w-full px-3 py-2 text-sm font-medium"
            />
          </div>

          <div className="space-y-1">
            <label className="text-xs font-bold text-gray-500 uppercase tracking-wider">
              パスワード
            </label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              autoComplete="current-password"
              placeholder="••••••••"
              className="input-glass w-full px-3 py-2 text-sm font-medium"
            />
          </div>

          {error && (
            <p
              className="text-xs font-bold px-3 py-2 rounded-xl text-center"
              style={{
                background: "rgba(239,68,68,0.08)",
                color: "rgba(239,68,68,0.9)",
                border: "1px solid rgba(239,68,68,0.2)",
              }}
            >
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={loading}
            className="w-full py-2.5 rounded-xl text-sm font-bold transition-all"
            style={{
              background: "linear-gradient(135deg, rgba(99,102,241,0.85), rgba(139,92,246,0.85))",
              color: "white",
              boxShadow: "inset 0 1px 0 rgba(255,255,255,0.3), 0 2px 8px rgba(99,102,241,0.3)",
              opacity: loading ? 0.7 : 1,
            }}
          >
            {loading ? "ログイン中..." : "ログイン"}
          </button>
        </form>
      </div>
    </div>
  );
}
