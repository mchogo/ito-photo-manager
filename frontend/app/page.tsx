"use client";

import { useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import type { EquipmentDef } from "@/types";
import { getEquipmentList, createProject } from "@/lib/api";
import EquipmentSelector from "@/components/EquipmentSelector";

const LS_KEY_SITE_ID = "pm_siteId";
const LS_KEY_WORKER = "pm_workerName";

export default function HomePage() {
  const router = useRouter();
  const [equipment, setEquipment] = useState<EquipmentDef[]>([]);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [siteId, setSiteId] = useState("");
  const [workDate, setWorkDate] = useState(new Date().toISOString().slice(0, 10));
  const [workerName, setWorkerName] = useState("");
  const [selectedEquipment, setSelectedEquipment] = useState<Set<string>>(new Set());

  // Restore saved inputs from localStorage on mount
  useEffect(() => {
    try {
      const savedSiteId = localStorage.getItem(LS_KEY_SITE_ID);
      const savedWorker = localStorage.getItem(LS_KEY_WORKER);
      if (savedSiteId) setSiteId(savedSiteId);
      if (savedWorker) setWorkerName(savedWorker);
    } catch {
      // localStorage unavailable — silently ignore
    }
  }, []);

  // Persist inputs to localStorage on every change
  const handleSiteIdChange = useCallback((value: string) => {
    setSiteId(value);
    try { localStorage.setItem(LS_KEY_SITE_ID, value); } catch {}
  }, []);

  const handleWorkerNameChange = useCallback((value: string) => {
    setWorkerName(value);
    try { localStorage.setItem(LS_KEY_WORKER, value); } catch {}
  }, []);

  useEffect(() => {
    getEquipmentList()
      .then(setEquipment)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  const handleToggle = (id: string) => {
    setSelectedEquipment((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  const canSubmit =
    siteId.trim() && workerName.trim() && workDate && selectedEquipment.size > 0 && !submitting;

  const totalPhotos = equipment
    .filter((eq) => selectedEquipment.has(eq.equipment_id))
    .reduce((sum, eq) => sum + eq.photo_slots.length, 0);

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const project = await createProject({
        site_id: siteId.trim(),
        work_date: workDate,
        worker_name: workerName.trim(),
        equipment_ids: Array.from(selectedEquipment),
      });
      router.push(`/shoot?projectId=${project.project_id}`);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "案件の作成に失敗しました");
      setSubmitting(false);
    }
  };

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center py-28 gap-4">
        <div className="spinner-glass" />
        <p className="text-sm text-gray-500/60 font-medium">読み込み中...</p>
      </div>
    );
  }

  return (
    <div className="space-y-5 animate-fade-in-up">
      {/* Title */}
      <div className="px-1">
        <h2 className="text-2xl font-extrabold text-gray-800 tracking-tight">新規案件作成</h2>
        <p className="text-sm text-gray-500/70 mt-1 font-medium">
          現場情報を入力し、導入機器を選択してください
        </p>
      </div>

      {/* Error */}
      {error && (
        <div className="animate-slide-down liquid-glass-red px-4 py-3 flex items-center gap-2 text-red-700 text-sm font-semibold">
          <svg className="w-5 h-5 shrink-0" fill="currentColor" viewBox="0 0 20 20">
            <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clipRule="evenodd" />
          </svg>
          {error}
        </div>
      )}

      {/* Project Info — Glass Panel */}
      <div className="liquid-glass p-5 space-y-4">
        <div className="flex items-center gap-2 mb-1">
          <div className="w-7 h-7 rounded-xl flex items-center justify-center text-sm"
            style={{
              background: "rgba(99,102,241,0.12)",
              border: "1px solid rgba(255,255,255,0.4)",
              boxShadow: "inset 0 1px 0 rgba(255,255,255,0.5)",
            }}>
            📋
          </div>
          <h3 className="font-bold text-gray-800 text-[15px]">案件情報</h3>
        </div>

        <div>
          <label className="block text-[11px] font-bold text-gray-500/80 mb-1.5 uppercase tracking-widest">
            現場ID <span className="text-red-400">*</span>
          </label>
          <input type="text" value={siteId} onChange={(e) => handleSiteIdChange(e.target.value)}
            placeholder="例: SITE-001" className="input-glass" />
        </div>
        <div>
          <label className="block text-[11px] font-bold text-gray-500/80 mb-1.5 uppercase tracking-widest">
            作業日 <span className="text-red-400">*</span>
          </label>
          <input type="date" value={workDate} onChange={(e) => setWorkDate(e.target.value)}
            className="input-glass" />
        </div>
        <div>
          <label className="block text-[11px] font-bold text-gray-500/80 mb-1.5 uppercase tracking-widest">
            作業員名 <span className="text-red-400">*</span>
          </label>
          <input type="text" value={workerName} onChange={(e) => handleWorkerNameChange(e.target.value)}
            placeholder="例: 田中太郎" className="input-glass" />
        </div>
      </div>

      {/* Equipment Selection — Glass Panel */}
      <div className="liquid-glass p-5">
        <div className="flex items-center gap-2 mb-4">
          <div className="w-7 h-7 rounded-xl flex items-center justify-center text-sm"
            style={{
              background: "rgba(139,92,246,0.12)",
              border: "1px solid rgba(255,255,255,0.4)",
              boxShadow: "inset 0 1px 0 rgba(255,255,255,0.5)",
            }}>
            🔧
          </div>
          <h3 className="font-bold text-gray-800 text-[15px]">導入機器の選択</h3>
        </div>
        <EquipmentSelector equipment={equipment} selected={selectedEquipment} onToggle={handleToggle} />
      </div>

      {/* Summary Pill */}
      {selectedEquipment.size > 0 && (
        <div className="animate-elastic-pop liquid-glass px-5 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-11 h-11 rounded-2xl flex items-center justify-center text-white font-bold text-lg"
              style={{
                background: "linear-gradient(135deg, rgba(99,102,241,0.85), rgba(139,92,246,0.85))",
                border: "1px solid rgba(255,255,255,0.3)",
                boxShadow: "inset 0 1px 0 rgba(255,255,255,0.35), 0 4px 12px rgba(99,102,241,0.25)",
              }}>
              {selectedEquipment.size}
            </div>
            <div>
              <div className="text-sm font-bold text-gray-800">{selectedEquipment.size}台の機器を選択中</div>
              <div className="text-xs text-gray-500/70 font-medium">合計 {totalPhotos} 枚の撮影が必要です</div>
            </div>
          </div>
          <div className="text-3xl font-extrabold text-indigo-600/80">
            {totalPhotos}<span className="text-sm font-bold text-gray-400 ml-0.5">枚</span>
          </div>
        </div>
      )}

      {/* Submit */}
      <button onClick={handleSubmit} disabled={!canSubmit}
        className="w-full py-4 text-[17px] btn-liquid-primary">
        {submitting ? (
          <span className="flex items-center justify-center gap-2">
            <div className="w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
            作成中...
          </span>
        ) : (
          <span className="flex items-center justify-center gap-2">
            撮影開始
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M13 7l5 5m0 0l-5 5m5-5H6" />
            </svg>
          </span>
        )}
      </button>
    </div>
  );
}
