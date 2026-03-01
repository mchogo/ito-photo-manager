"use client";

import { useEffect, useState, useCallback, Suspense } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import type { Project, ValidationResult } from "@/types";
import { getProject, validateProject, getExcelExportUrl } from "@/lib/api";
import PreviewGrid from "@/components/PreviewGrid";

function PreviewPageContent() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const projectId = searchParams.get("projectId");

  const [project, setProject] = useState<Project | null>(null);
  const [validation, setValidation] = useState<ValidationResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    if (!projectId) return;
    try {
      const [proj, val] = await Promise.all([
        getProject(projectId),
        validateProject(projectId),
      ]);
      setProject(proj);
      setValidation(val);
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "データ取得に失敗しました");
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  if (!projectId) {
    return (
      <div className="text-center py-16">
        <p className="text-gray-500/70 mb-3 font-medium">案件IDが指定されていません。</p>
        <button
          onClick={() => router.push("/")}
          className="btn-liquid-primary px-6 py-2.5 text-sm"
        >
          トップへ戻る
        </button>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center py-28 gap-4">
        <div className="spinner-glass" />
        <p className="text-sm text-gray-500/60 font-medium">読み込み中...</p>
      </div>
    );
  }

  if (error || !project || !validation) {
    return (
      <div className="text-center py-16">
        <div className="liquid-glass-red px-5 py-4 mb-4 inline-block text-red-700 font-semibold text-sm">
          {error || "データが見つかりません"}
        </div>
        <br />
        <button
          onClick={() => router.push("/")}
          className="btn-liquid-primary px-6 py-2.5 text-sm mt-2"
        >
          トップへ戻る
        </button>
      </div>
    );
  }

  const handleExport = () => {
    window.open(getExcelExportUrl(projectId), "_blank");
  };

  return (
    <div className="space-y-5 animate-fade-in-up">
      {/* Project Summary — Glass Panel */}
      <div className="liquid-glass p-5">
        <div className="flex items-center gap-2.5 mb-4">
          <div
            className="w-7 h-7 rounded-xl flex items-center justify-center text-sm"
            style={{
              background: "rgba(99,102,241,0.12)",
              border: "1px solid rgba(255,255,255,0.4)",
              boxShadow: "inset 0 1px 0 rgba(255,255,255,0.5)",
            }}
          >
            <svg className="w-4 h-4 text-indigo-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
          </div>
          <h2 className="font-extrabold text-gray-900 text-[15px]">撮影内容の確認</h2>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div
            className="p-3"
            style={{
              background: "rgba(255,255,255,0.15)",
              borderRadius: "12px",
              border: "1px solid rgba(255,255,255,0.3)",
              boxShadow: "inset 0 1px 0 rgba(255,255,255,0.4)",
            }}
          >
            <div className="text-[10px] text-gray-600/80 font-extrabold uppercase tracking-widest">
              現場ID
            </div>
            <div className="text-sm font-extrabold text-gray-900 mt-0.5">
              {project.site_id}
            </div>
          </div>
          <div
            className="p-3"
            style={{
              background: "rgba(255,255,255,0.15)",
              borderRadius: "12px",
              border: "1px solid rgba(255,255,255,0.3)",
              boxShadow: "inset 0 1px 0 rgba(255,255,255,0.4)",
            }}
          >
            <div className="text-[10px] text-gray-600/80 font-extrabold uppercase tracking-widest">
              作業日
            </div>
            <div className="text-sm font-extrabold text-gray-900 mt-0.5">
              {project.work_date}
            </div>
          </div>
          <div
            className="p-3"
            style={{
              background: "rgba(255,255,255,0.15)",
              borderRadius: "12px",
              border: "1px solid rgba(255,255,255,0.3)",
              boxShadow: "inset 0 1px 0 rgba(255,255,255,0.4)",
            }}
          >
            <div className="text-[10px] text-gray-600/80 font-extrabold uppercase tracking-widest">
              作業員
            </div>
            <div className="text-sm font-extrabold text-gray-900 mt-0.5">
              {project.worker_name}
            </div>
          </div>
          <div
            className="p-3"
            style={{
              background: validation.is_complete
                ? "rgba(16, 185, 129, 0.1)"
                : "rgba(249, 115, 22, 0.08)",
              borderRadius: "12px",
              border: validation.is_complete
                ? "1px solid rgba(16, 185, 129, 0.25)"
                : "1px solid rgba(249, 115, 22, 0.2)",
              boxShadow: "inset 0 1px 0 rgba(255,255,255,0.4)",
            }}
          >
            <div className="text-[10px] text-gray-600/80 font-extrabold uppercase tracking-widest">
              撮影状況
            </div>
            <div
              className={`text-sm font-extrabold mt-0.5 ${
                validation.is_complete ? "text-emerald-600" : "text-orange-600"
              }`}
            >
              {validation.filled_slots}/{validation.total_slots}枚完了
            </div>
          </div>
        </div>
      </div>

      {/* Missing Slots Warning */}
      {!validation.is_complete && (
        <div className="animate-slide-down liquid-glass-red p-5">
          <div className="flex items-center gap-2 font-bold text-red-700 mb-2">
            <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
              <path
                fillRule="evenodd"
                d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z"
                clipRule="evenodd"
              />
            </svg>
            未撮影の項目があります
          </div>
          <ul className="text-sm text-red-600/80 space-y-1 ml-7 font-medium">
            {validation.missing_slots.map((m) => (
              <li key={`${m.equipment_id}-${m.slot_id}`}>
                {m.equipment_name} — {m.slot_label}
              </li>
            ))}
          </ul>
          <button
            onClick={() => router.push(`/shoot?projectId=${projectId}`)}
            className="mt-3 ml-7 text-sm text-indigo-600 font-bold hover:text-indigo-700 transition-colors flex items-center gap-1"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M11 17l-5-5m0 0l5-5m-5 5h12" />
            </svg>
            撮影画面に戻る
          </button>
        </div>
      )}

      {/* Preview Grid */}
      <PreviewGrid equipment={project.equipment} />

      {/* Fixed Bottom Bar — Glass + safe-area */}
      <div className="bottom-bar-glass">
        <div className="max-w-4xl mx-auto flex gap-3">
          <button
            onClick={() => router.push(`/shoot?projectId=${projectId}`)}
            className="flex-1 py-3.5 btn-liquid flex items-center justify-center gap-1.5"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M11 17l-5-5m0 0l5-5m-5 5h12" />
            </svg>
            撮影画面
          </button>
          <button
            onClick={handleExport}
            disabled={!validation.is_complete}
            className="flex-[2] py-3.5 text-base btn-liquid-success flex items-center justify-center gap-2"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
              />
            </svg>
            Excel出力
          </button>
        </div>
      </div>
    </div>
  );
}

export default function PreviewPage() {
  return (
    <Suspense
      fallback={
        <div className="flex flex-col items-center justify-center py-28 gap-4">
          <div className="spinner-glass" />
          <p className="text-sm text-gray-500/60 font-medium">読み込み中...</p>
        </div>
      }
    >
      <PreviewPageContent />
    </Suspense>
  );
}
