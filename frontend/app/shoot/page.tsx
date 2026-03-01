"use client";

import { useEffect, useState, useCallback, Suspense } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import type { Project } from "@/types";
import { getProject, validateProject } from "@/lib/api";
import PhotoSlotCard from "@/components/PhotoSlotCard";

const EQUIPMENT_ICONS: Record<string, string> = {
  pos_register: "🖥️",
  cash_drawer: "💰",
  receipt_printer: "🖨️",
  router: "📡",
  lan_cabling: "🔌",
};

function ShootPageContent() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const projectId = searchParams.get("projectId");

  const [project, setProject] = useState<Project | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filledCount, setFilledCount] = useState(0);
  const [totalCount, setTotalCount] = useState(0);

  const loadProject = useCallback(async () => {
    if (!projectId) return;
    try {
      const [proj, validation] = await Promise.all([
        getProject(projectId),
        validateProject(projectId),
      ]);
      setProject(proj);
      setFilledCount(validation.filled_slots);
      setTotalCount(validation.total_slots);
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "データ取得に失敗しました");
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    loadProject();
  }, [loadProject]);

  // Warn on browser back / tab close while shooting is in progress
  useEffect(() => {
    const handleBeforeUnload = (e: BeforeUnloadEvent) => {
      if (project && totalCount > 0) {
        e.preventDefault();
      }
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [project, totalCount]);

  if (!projectId) {
    return (
      <div className="text-center py-16">
        <p className="text-gray-500/70 mb-3 font-medium">案件IDが指定されていません。</p>
        <button onClick={() => router.push("/")} className="btn-liquid-primary px-6 py-2.5 text-sm">
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

  if (error || !project) {
    return (
      <div className="text-center py-16">
        <div className="liquid-glass-red px-5 py-4 mb-4 inline-block text-red-700 font-semibold text-sm">
          {error || "案件が見つかりません"}
        </div>
        <br />
        <button onClick={() => router.push("/")} className="btn-liquid-primary px-6 py-2.5 text-sm mt-2">
          トップへ戻る
        </button>
      </div>
    );
  }

  const isComplete = filledCount === totalCount;
  const percent = totalCount > 0 ? Math.round((filledCount / totalCount) * 100) : 0;

  return (
    <div className="space-y-5 animate-fade-in-up">
      {/* Status Bar — Glass Panel */}
      <div className="liquid-glass p-5">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-3">
            <div
              className="w-10 h-10 rounded-2xl flex items-center justify-center text-white text-xs font-bold"
              style={{
                background: isComplete
                  ? "linear-gradient(135deg, rgba(16,185,129,0.85), rgba(5,150,105,0.85))"
                  : "linear-gradient(135deg, rgba(99,102,241,0.85), rgba(139,92,246,0.85))",
                border: "1px solid rgba(255,255,255,0.3)",
                boxShadow: isComplete
                  ? "inset 0 1px 0 rgba(255,255,255,0.35), 0 4px 12px rgba(16,185,129,0.25)"
                  : "inset 0 1px 0 rgba(255,255,255,0.35), 0 4px 12px rgba(99,102,241,0.25)",
              }}
            >
              {isComplete ? (
                <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
                  <path fillRule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clipRule="evenodd" />
                </svg>
              ) : (
                <span className="text-sm">{percent}%</span>
              )}
            </div>
            <div>
              <div className="text-[15px] font-extrabold text-gray-900">
                {project.site_id}
              </div>
              <div className="text-xs text-gray-600/80 font-semibold">
                {project.worker_name} / {project.work_date}
              </div>
            </div>
          </div>
          <div className="text-right">
            <div className={`text-3xl font-extrabold ${isComplete ? "text-emerald-700" : "text-indigo-700"}`}>
              {filledCount}
              <span className="text-sm text-gray-400 font-bold ml-0.5">/{totalCount}</span>
            </div>
          </div>
        </div>

        {/* Progress Bar — Glass */}
        <div
          className="w-full h-3 overflow-hidden"
          style={{
            background: "rgba(0,0,0,0.05)",
            borderRadius: "8px",
            border: "1px solid rgba(255,255,255,0.3)",
            boxShadow: "inset 0 1px 2px rgba(0,0,0,0.06)",
          }}
        >
          <div
            className="h-full progress-glass"
            style={{
              width: `${percent}%`,
              background: isComplete
                ? "linear-gradient(90deg, rgba(16,185,129,0.8), rgba(52,211,153,0.8))"
                : "linear-gradient(90deg, rgba(99,102,241,0.8), rgba(167,139,250,0.8))",
              borderRadius: "7px",
              boxShadow: isComplete
                ? "inset 0 1px 0 rgba(255,255,255,0.4), 0 0 8px rgba(16,185,129,0.3)"
                : "inset 0 1px 0 rgba(255,255,255,0.4), 0 0 8px rgba(99,102,241,0.3)",
            }}
          />
        </div>
      </div>

      {/* Equipment Sections */}
      {project.equipment.map((eq) => {
        const icon = EQUIPMENT_ICONS[eq.equipment_id] || "📦";
        const eqFilled = eq.slots.filter((s) => s.photo_filename).length;
        const eqTotal = eq.slots.length;
        const eqComplete = eqFilled === eqTotal;
        return (
          <div key={eq.equipment_id} className="animate-fade-in">
            {/* Equipment Header — Glass */}
            <div className="flex items-center gap-2.5 mb-3 px-1">
              <div
                className="w-8 h-8 rounded-xl flex items-center justify-center text-lg shrink-0"
                style={{
                  background: eqComplete
                    ? "rgba(16, 185, 129, 0.12)"
                    : "rgba(99, 102, 241, 0.1)",
                  border: "1px solid rgba(255,255,255,0.4)",
                  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.5)",
                }}
              >
                {icon}
              </div>
              <h3 className="text-[15px] font-extrabold text-gray-900 flex-1">
                {eq.name}
              </h3>
              <span
                className="text-xs font-bold px-2.5 py-1 rounded-full"
                style={{
                  background: eqComplete
                    ? "rgba(16, 185, 129, 0.15)"
                    : "rgba(249, 115, 22, 0.12)",
                  color: eqComplete ? "#047857" : "#c2410c",
                  border: eqComplete
                    ? "1px solid rgba(16, 185, 129, 0.25)"
                    : "1px solid rgba(249, 115, 22, 0.2)",
                }}
              >
                {eqFilled}/{eqTotal}
              </span>
            </div>

            {/* Slot Grid */}
            <div className="grid grid-cols-2 gap-3 stagger">
              {eq.slots.map((slot) => (
                <PhotoSlotCard
                  key={`${eq.equipment_id}-${slot.slot_id}`}
                  projectId={projectId}
                  equipmentId={eq.equipment_id}
                  equipmentName={eq.name}
                  slot={slot}
                  onUpdated={loadProject}
                />
              ))}
            </div>
          </div>
        );
      })}

      {/* Fixed Bottom Bar — Glass + safe-area */}
      <div className="bottom-bar-glass">
        <div className="max-w-4xl mx-auto">
          <button
            onClick={() => router.push(`/preview?projectId=${projectId}`)}
            disabled={!isComplete}
            className={`w-full py-4 text-[17px] ${isComplete ? "btn-liquid-success" : "btn-liquid-success"}`}
          >
            {isComplete ? (
              <span className="flex items-center justify-center gap-2">
                プレビュー / 提出へ
                <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M13 7l5 5m0 0l-5 5m5-5H6" />
                </svg>
              </span>
            ) : (
              <span className="flex items-center justify-center gap-2">
                残り {totalCount - filledCount} 枚 — 全て撮影してください
              </span>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function ShootPage() {
  return (
    <Suspense
      fallback={
        <div className="flex flex-col items-center justify-center py-28 gap-4">
          <div className="spinner-glass" />
          <p className="text-sm text-gray-500/60 font-medium">読み込み中...</p>
        </div>
      }
    >
      <ShootPageContent />
    </Suspense>
  );
}
