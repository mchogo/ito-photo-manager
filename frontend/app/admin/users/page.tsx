"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { createUser, deleteUser, importUsersCSV, listUsers } from "@/lib/api";
import type { AuthUser } from "@/types";
import { useRequireAuth } from "@/lib/useAuth";

export default function UsersPage() {
  const { user: me, isAdmin } = useRequireAuth();
  const [users, setUsers] = useState<AuthUser[]>([]);
  const [loading, setLoading] = useState(false);
  const [toast, setToast] = useState<string | null>(null);

  // New user form
  const [form, setForm] = useState({ username: "", display_name: "", password: "", role: "worker" });
  const [creating, setCreating] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const importRef = useRef<HTMLInputElement>(null);

  const showToast = (msg: string) => {
    setToast(msg);
    setTimeout(() => setToast(null), 3000);
  };

  const fetchUsers = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listUsers();
      setUsers(data);
    } catch {
      setUsers([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isAdmin) fetchUsers();
  }, [isAdmin, fetchUsers]);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormError(null);
    setCreating(true);
    try {
      await createUser(form);
      setForm({ username: "", display_name: "", password: "", role: "worker" });
      await fetchUsers();
      showToast("ユーザーを作成しました");
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "作成に失敗しました");
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async (u: AuthUser) => {
    if (!confirm(`${u.display_name} を削除しますか？`)) return;
    try {
      await deleteUser(u.user_id);
      await fetchUsers();
      showToast(`${u.display_name} を削除しました`);
    } catch {
      showToast("削除に失敗しました");
    }
  };

  const downloadTemplate = () => {
    const csv = "username,display_name,role,password\nworker01,山田 太郎,worker,password123\nadmin01,管理者,admin,password123\n";
    const blob = new Blob(["\uFEFF" + csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "users_template.csv";
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleImport = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const result = await importUsersCSV(file);
      showToast(`${result.created} 件インポートしました${result.errors.length > 0 ? `（エラー ${result.errors.length} 件）` : ""}`);
      await fetchUsers();
    } catch {
      showToast("インポートに失敗しました");
    }
    if (importRef.current) importRef.current.value = "";
  };

  if (!isAdmin) {
    return (
      <div className="liquid-glass p-10 text-center space-y-3 animate-fade-in-up">
        <div className="text-4xl">🔒</div>
        <p className="font-bold text-gray-600 text-sm">管理者権限が必要です</p>
      </div>
    );
  }

  return (
    <div className="space-y-6 animate-fade-in-up">
      {/* Header */}
      <div className="px-1 flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-extrabold text-gray-800 tracking-tight">ユーザー管理</h2>
          <p className="text-sm text-gray-500/70 mt-1 font-medium">作業員・管理者アカウントの管理</p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={downloadTemplate}
            className="text-xs font-bold px-3 py-1.5 rounded-xl"
            style={{ border: "1px solid var(--c-border)", color: "var(--c-text-secondary)" }}
          >
            ↓ フォーマット
          </button>
          <button
            onClick={() => importRef.current?.click()}
            className="text-xs font-bold px-3 py-1.5 rounded-xl"
            style={{ border: "1px solid var(--c-border)", color: "var(--c-text-secondary)" }}
          >
            ↑ CSV入力
          </button>
        </div>
        <input ref={importRef} type="file" accept=".csv" className="hidden" onChange={handleImport} />
      </div>

      {/* Add user form */}
      <form onSubmit={handleCreate} className="liquid-glass p-4 space-y-3">
        <p className="text-xs font-bold text-gray-600 uppercase tracking-wider">＋ ユーザーを追加</p>
        <div className="grid grid-cols-2 gap-2">
          <div className="space-y-1">
            <label className="text-[10px] font-bold text-gray-400 uppercase">ユーザー名</label>
            <input
              type="text"
              required
              value={form.username}
              onChange={(e) => setForm((f) => ({ ...f, username: e.target.value }))}
              placeholder="username"
              className="input-glass w-full px-3 py-2 text-xs font-medium"
            />
          </div>
          <div className="space-y-1">
            <label className="text-[10px] font-bold text-gray-400 uppercase">表示名</label>
            <input
              type="text"
              required
              value={form.display_name}
              onChange={(e) => setForm((f) => ({ ...f, display_name: e.target.value }))}
              placeholder="山田 太郎"
              className="input-glass w-full px-3 py-2 text-xs font-medium"
            />
          </div>
          <div className="space-y-1">
            <label className="text-[10px] font-bold text-gray-400 uppercase">パスワード</label>
            <input
              type="password"
              required
              minLength={6}
              value={form.password}
              onChange={(e) => setForm((f) => ({ ...f, password: e.target.value }))}
              placeholder="••••••"
              className="input-glass w-full px-3 py-2 text-xs font-medium"
            />
          </div>
          <div className="space-y-1">
            <label className="text-[10px] font-bold text-gray-400 uppercase">ロール</label>
            <select
              value={form.role}
              onChange={(e) => setForm((f) => ({ ...f, role: e.target.value }))}
              className="input-glass w-full px-3 py-2 text-xs font-medium"
            >
              <option value="worker">作業員</option>
              <option value="admin">管理者</option>
            </select>
          </div>
        </div>
        {formError && (
          <p
            className="text-xs font-bold px-3 py-2 rounded-xl text-center"
            style={{
              background: "rgba(239,68,68,0.08)",
              color: "rgba(239,68,68,0.9)",
              border: "1px solid rgba(239,68,68,0.2)",
            }}
          >
            {formError}
          </p>
        )}
        <button
          type="submit"
          disabled={creating}
          className="w-full py-2 rounded-xl text-xs font-bold transition-all"
          style={{
            background: "linear-gradient(135deg, rgba(99,102,241,0.85), rgba(139,92,246,0.85))",
            color: "white",
            boxShadow: "inset 0 1px 0 rgba(255,255,255,0.3), 0 2px 8px rgba(99,102,241,0.3)",
            opacity: creating ? 0.7 : 1,
          }}
        >
          {creating ? "作成中..." : "作成"}
        </button>
      </form>

      {/* User list */}
      {loading ? (
        <div className="flex items-center justify-center py-10">
          <div className="spinner-glass" />
        </div>
      ) : (
        <div className="liquid-glass overflow-hidden">
          <table className="w-full text-xs">
            <thead>
              <tr
                className="text-[10px] font-bold text-gray-400 uppercase tracking-wider"
                style={{ borderBottom: "1px solid var(--c-border-subtle)" }}
              >
                <th className="px-4 py-3 text-left">表示名</th>
                <th className="px-4 py-3 text-left">ユーザー名</th>
                <th className="px-4 py-3 text-left">ロール</th>
                <th className="px-4 py-3 text-left">作成日</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr
                  key={u.user_id}
                  className="font-medium text-gray-700"
                  style={{ borderBottom: "1px solid var(--c-border-subtle)" }}
                >
                  <td className="px-4 py-3">
                    {u.display_name}
                    {u.user_id === me?.user_id && (
                      <span className="ml-1.5 text-[10px] text-indigo-500 font-bold">(自分)</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-gray-400">{u.username}</td>
                  <td className="px-4 py-3">
                    <span
                      className="text-[10px] font-bold px-2 py-0.5 rounded-full"
                      style={
                        u.role === "admin"
                          ? { background: "rgba(234,88,12,0.1)", color: "rgba(234,88,12,0.9)" }
                          : { background: "rgba(99,102,241,0.1)", color: "rgba(99,102,241,0.9)" }
                      }
                    >
                      {u.role === "admin" ? "管理者" : "作業員"}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-gray-400">
                    {u.created_at ? new Date(u.created_at).toLocaleDateString("ja-JP") : "—"}
                  </td>
                  <td className="px-4 py-3 text-right">
                    {u.user_id !== me?.user_id && (
                      <button
                        onClick={() => handleDelete(u)}
                        className="text-[10px] font-bold px-2.5 py-1 rounded-lg transition-colors"
                        style={{
                          background: "rgba(239,68,68,0.06)",
                          color: "rgba(239,68,68,0.8)",
                          border: "1px solid rgba(239,68,68,0.15)",
                        }}
                      >
                        削除
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {users.length === 0 && (
                <tr>
                  <td colSpan={5} className="px-4 py-8 text-center text-gray-400">
                    ユーザーが見つかりません
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* Toast */}
      {toast && (
        <div
          className="fixed bottom-6 left-1/2 -translate-x-1/2 px-5 py-3 rounded-2xl text-sm font-bold text-white z-50"
          style={{
            background: "rgba(34,197,94,0.9)",
            boxShadow: "0 4px 16px rgba(34,197,94,0.3)",
          }}
        >
          ✓ {toast}
        </div>
      )}
    </div>
  );
}
