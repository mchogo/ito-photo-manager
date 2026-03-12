"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { approveProject, downloadExportCSV, importProjectsCSV, listProjects } from "@/lib/api";
import type { Project } from "@/types";
import { useRequireAuth } from "@/lib/useAuth";
import { useMasterConfig } from "@/lib/useMasterConfig";

type DateTab = "today" | "tomorrow";

function toDateStr(offsetDays: number): string {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return d.toISOString().slice(0, 10);
}

function extractHHMM(iso: string | null): string {
  if (!iso) return "—";
  return iso.match(/T(\d{2}:\d{2})/)?.[1] ?? iso.slice(11, 16);
}

function getAlerts(p: Project): string[] {
  const now = Date.now();
  const alerts: string[] = [];
  if (p.work_start_time && !p.arrival_time) {
    if (new Date(p.work_start_time).getTime() < now) {
      alerts.push("入店遅れ");
    }
  }
  if (p.checkout_time) {
    const hasKansho = (p.documents ?? []).some(
      (d) => d.document_type === "完成図書_調査" || d.document_type === "完成図書_設置",
    );
    if (!hasKansho) alerts.push("図書未提出");
  }
  return alerts;
}

export default function AdminPage() {
  const { isAdmin } = useRequireAuth();
  const { colorOf } = useMasterConfig();
  const [tab, setTab] = useState<DateTab>("today");
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [reminded, setReminded] = useState<Set<string>>(new Set());
  const [approving, setApproving] = useState<Set<string>>(new Set());
  const importRef = useRef<HTMLInputElement>(null);

  const showToast = (msg: string) => {
    setToast(msg);
    setTimeout(() => setToast(null), 3000);
  };

  const fetchProjects = useCallback(async (offset: number) => {
    setLoading(true);
    try {
      const data = await listProjects({ scheduled_date: toDateStr(offset) });
      setProjects(data);
    } catch {
      setProjects([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchProjects(tab === "today" ? 0 : 1);
  }, [tab, fetchProjects]);

  const handleRemind = (projectId: string, projectName: string) => {
    setReminded((prev) => new Set(prev).add(projectId));
    showToast(`${projectName} にリマインドを送信しました`);
  };

  const handleApprove = async (projectId: string, projectName: string) => {
    setApproving((prev) => new Set(prev).add(projectId));
    try {
      const updated = await approveProject(projectId);
      setProjects((prev) => prev.map((p) => (p.project_id === projectId ? updated : p)));
      showToast(`${projectName} を承認しました`);
    } catch {
      showToast("承認に失敗しました");
    } finally {
      setApproving((prev) => {
        const next = new Set(prev);
        next.delete(projectId);
        return next;
      });
    }
  };

  const handleExport = async () => {
    try {
      await downloadExportCSV();
      showToast("CSVエクスポートを開始しました");
    } catch {
      showToast("エクスポートに失敗しました");
    }
  };

  const handleImportChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const result = await importProjectsCSV(file);
      showToast(`${result.created} 件インポートしました${result.errors.length > 0 ? `（エラー ${result.errors.length} 件）` : ""}`);
      fetchProjects(tab === "today" ? 0 : 1);
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

  const sorted = [...projects].sort((a, b) => {
    const aAlerts = getAlerts(a).length;
    const bAlerts = getAlerts(b).length;
    if (bAlerts !== aAlerts) return bAlerts - aAlerts;
    return (a.work_start_time ?? "").localeCompare(b.work_start_time ?? "");
  });

  const TABS: { key: DateTab; label: string }[] = [
    { key: "today", label: `本日 (${toDateStr(0)})` },
    { key: "tomorrow", label: `明日 (${toDateStr(1)})` },
  ];

  return (
    <div className="space-y-5 animate-fade-in-up">
      {/* Header */}
      <div className="px-1 flex items-start justify-between">
        <div>
          <h2 className="text-2xl font-extrabold text-gray-800 tracking-tight">管理者ボード</h2>
          <p className="text-sm text-gray-500/70 mt-1 font-medium">本日・翌日の案件状況を確認</p>
        </div>
        <div className="flex items-center gap-2">
          <Link
            href="/admin/users"
            className="text-xs font-bold px-3 py-1.5 rounded-xl"
            style={{ border: "1px solid var(--c-border)", color: "var(--c-text-secondary)" }}
          >
            👥 ユーザー管理
          </Link>
          <Link
            href="/admin/settings"
            className="text-xs font-bold px-3 py-1.5 rounded-xl"
            style={{ border: "1px solid var(--c-border)", color: "var(--c-text-secondary)" }}
          >
            ⚙️ マスター設定
          </Link>
          <button
            onClick={handleExport}
            className="text-xs font-bold px-3 py-1.5 rounded-xl"
            style={{ border: "1px solid var(--c-border)", color: "var(--c-text-secondary)" }}
          >
            ↓ CSV出力
          </button>
          <button
            onClick={() => importRef.current?.click()}
            className="text-xs font-bold px-3 py-1.5 rounded-xl"
            style={{ border: "1px solid var(--c-border)", color: "var(--c-text-secondary)" }}
          >
            ↑ CSV入力
          </button>
          <input
            ref={importRef}
            type="file"
            accept=".csv"
            className="hidden"
            onChange={handleImportChange}
          />
        </div>
      </div>

      {/* Tab toggle */}
      <div
        className="flex rounded-2xl p-1 gap-1"
        style={{
          background: "var(--c-tab-bg)",
          border: "1px solid var(--c-tab-border)",
          backdropFilter: "blur(12px)",
        }}
      >
        {TABS.map(({ key, label }) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className="flex-1 py-2 rounded-xl text-xs font-bold transition-all"
            style={
              tab === key
                ? {
                    background: "linear-gradient(135deg, rgba(99,102,241,0.85), rgba(139,92,246,0.85))",
                    color: "white",
                    boxShadow: "inset 0 1px 0 rgba(255,255,255,0.3), 0 2px 8px rgba(99,102,241,0.3)",
                  }
                : { color: "var(--c-text-secondary)" }
            }
          >
            {label}
          </button>
        ))}
      </div>

      {/* Project cards */}
      {loading ? (
        <div className="flex items-center justify-center py-16">
          <div className="spinner-glass" />
        </div>
      ) : sorted.length === 0 ? (
        <div className="liquid-glass px-5 py-12 text-center text-gray-400 text-sm font-medium">
          該当する案件がありません
        </div>
      ) : (
        <div className="space-y-3">
          {sorted.map((p) => {
            const alerts = getAlerts(p);
            const name = p.project_name || p.site_id;
            const hasAlert = alerts.length > 0;
            const isReminded = reminded.has(p.project_id);
            const isApprovable = p.status === "成果物提出待ち";
            const isApproving = approving.has(p.project_id);

            return (
              <div
                key={p.project_id}
                className="liquid-glass p-4 space-y-3"
                style={hasAlert ? { borderColor: "rgba(239,68,68,0.35)" } : {}}
              >
                {/* Alert badges */}
                {hasAlert && (
                  <div className="flex gap-2 flex-wrap">
                    {alerts.map((a) => (
                      <span
                        key={a}
                        className="text-[11px] font-bold px-2.5 py-0.5 rounded-full"
                        style={{
                          background: "rgba(239,68,68,0.08)",
                          color: "rgba(239,68,68,0.9)",
                          border: "1px solid rgba(239,68,68,0.2)",
                        }}
                      >
                        ⚠ {a}
                      </span>
                    ))}
                  </div>
                )}

                {/* Project info link */}
                <Link href={`/projects/${p.project_id}`} className="block">
                  <div className="flex items-center justify-between">
                    <p className="font-bold text-gray-800 text-[15px] truncate">{name}</p>
                    <span
                      className={`text-[11px] font-bold px-2.5 py-0.5 rounded-full shrink-0 ml-2 ${colorOf(p.status)}`}
                    >
                      {p.status}
                    </span>
                  </div>
                  <p className="text-xs text-gray-500 font-medium mt-1">
                    👷 {p.worker_name}
                    {p.work_start_time && (
                      <> · 予定 {extractHHMM(p.work_start_time)}〜{extractHHMM(p.work_end_time)}</>
                    )}
                  </p>
                </Link>

                {/* Timelog summary */}
                <div
                  className="grid grid-cols-3 gap-2 rounded-xl p-2.5 text-[11px] font-medium text-gray-600"
                  style={{ background: "var(--c-surface-subtle)", border: "1px solid var(--c-border-subtle)" }}
                >
                  <div className="text-center">
                    <div className="text-[10px] text-gray-400 font-bold uppercase tracking-wider mb-0.5">出発</div>
                    <div className={p.departure_time ? "text-indigo-600 font-bold" : "text-gray-300"}>
                      {extractHHMM(p.departure_time)}
                    </div>
                  </div>
                  <div className="text-center border-x border-gray-100">
                    <div className="text-[10px] text-gray-400 font-bold uppercase tracking-wider mb-0.5">到着</div>
                    <div className={p.arrival_time ? "text-green-600 font-bold" : "text-gray-300"}>
                      {extractHHMM(p.arrival_time)}
                    </div>
                  </div>
                  <div className="text-center">
                    <div className="text-[10px] text-gray-400 font-bold uppercase tracking-wider mb-0.5">退店</div>
                    <div className={p.checkout_time ? "text-orange-500 font-bold" : "text-gray-300"}>
                      {extractHHMM(p.checkout_time)}
                    </div>
                  </div>
                </div>

                {/* Approve button (成果物提出待ち のみ) */}
                {isApprovable && (
                  <button
                    onClick={() => handleApprove(p.project_id, name)}
                    disabled={isApproving}
                    className="w-full py-2 rounded-xl text-xs font-bold transition-all"
                    style={{
                      background: "linear-gradient(135deg, rgba(34,197,94,0.85), rgba(16,185,129,0.85))",
                      color: "white",
                      boxShadow: "inset 0 1px 0 rgba(255,255,255,0.3), 0 2px 8px rgba(34,197,94,0.3)",
                      opacity: isApproving ? 0.7 : 1,
                    }}
                  >
                    {isApproving ? "処理中..." : "✓ 承認して案件終了"}
                  </button>
                )}

                {/* Remind button */}
                <button
                  onClick={() => handleRemind(p.project_id, name)}
                  disabled={isReminded}
                  className="w-full py-2 rounded-xl text-xs font-bold transition-all"
                  style={
                    isReminded
                      ? {
                          background: "rgba(34,197,94,0.08)",
                          border: "1px solid rgba(34,197,94,0.2)",
                          color: "rgba(34,197,94,0.8)",
                          cursor: "default",
                        }
                      : {
                          background: "linear-gradient(135deg, rgba(99,102,241,0.1), rgba(139,92,246,0.1))",
                          border: "1px solid rgba(99,102,241,0.2)",
                          color: "rgba(99,102,241,0.85)",
                        }
                  }
                >
                  {isReminded ? "✓ 送信済み" : "📨 リマインド送信"}
                </button>
              </div>
            );
          })}
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
