"use client";

import { useEffect, useState, useCallback } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { getProject, updateProject, validateProject } from "@/lib/api";
import type { Project, ProjectStatus, ProjectUpdateRequest, ValidationResult } from "@/types";
import { PROJECT_STATUSES, STATUS_COLORS } from "@/types";

type Tab = "info" | "shoot" | "docs";

// --- Sub-components ---

function StatusBadge({ status }: { status: ProjectStatus }) {
  return (
    <span className={`text-xs font-bold px-2.5 py-0.5 rounded-full ${STATUS_COLORS[status] ?? "bg-gray-100 text-gray-600"}`}>
      {status}
    </span>
  );
}

function FieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <label className="block text-[11px] font-bold text-gray-500/80 mb-1.5 uppercase tracking-widest">
      {children}
    </label>
  );
}

function SaveIndicator({ saving, saved }: { saving: boolean; saved: boolean }) {
  if (saving) return <span className="text-xs text-indigo-400 font-medium">保存中...</span>;
  if (saved) return <span className="text-xs text-green-500 font-medium">✓ 保存済み</span>;
  return null;
}

// --- Info Tab ---

function InfoTab({
  project,
  onUpdate,
}: {
  project: Project;
  onUpdate: (updates: ProjectUpdateRequest) => Promise<void>;
}) {
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  const save = useCallback(async (updates: ProjectUpdateRequest) => {
    setSaving(true);
    setSaved(false);
    try {
      await onUpdate(updates);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } finally {
      setSaving(false);
    }
  }, [onUpdate]);

  const handleBlur = (field: keyof ProjectUpdateRequest, value: string) => {
    const trimmed = value.trim();
    // Only save if value changed from current project data
    const current = (project as unknown as Record<string, unknown>)[field];
    if (trimmed !== (current ?? "")) {
      save({ [field]: trimmed || null });
    }
  };

  const handleStatusChange = (status: ProjectStatus) => {
    save({ status });
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between px-1">
        <h3 className="font-bold text-gray-700 text-sm">案件情報</h3>
        <SaveIndicator saving={saving} saved={saved} />
      </div>

      {/* Status */}
      <div className="liquid-glass p-4 space-y-3">
        <FieldLabel>ステータス</FieldLabel>
        <select
          defaultValue={project.status}
          onChange={(e) => handleStatusChange(e.target.value as ProjectStatus)}
          className="input-glass w-full"
        >
          {PROJECT_STATUSES.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
      </div>

      {/* Basic Info */}
      <div className="liquid-glass p-4 space-y-3">
        <div>
          <FieldLabel>案件名</FieldLabel>
          <input
            type="text"
            defaultValue={project.project_name ?? ""}
            onBlur={(e) => handleBlur("project_name", e.target.value)}
            placeholder="例: ○○株式会社 POSレジ導入"
            className="input-glass w-full"
          />
        </div>
        <div>
          <FieldLabel>案件番号</FieldLabel>
          <input
            type="text"
            defaultValue={project.project_number ?? ""}
            onBlur={(e) => handleBlur("project_number", e.target.value)}
            placeholder="例: A-2024-001"
            className="input-glass w-full"
          />
        </div>
        <div>
          <FieldLabel>住所</FieldLabel>
          <input
            type="text"
            defaultValue={project.address ?? ""}
            onBlur={(e) => handleBlur("address", e.target.value)}
            placeholder="例: 東京都渋谷区..."
            className="input-glass w-full"
          />
        </div>
      </div>

      {/* Schedule */}
      <div className="liquid-glass p-4 space-y-3">
        <div>
          <FieldLabel>予定日</FieldLabel>
          <input
            type="date"
            defaultValue={project.scheduled_date ?? ""}
            onBlur={(e) => handleBlur("scheduled_date", e.target.value)}
            className="input-glass w-full"
          />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <FieldLabel>作業開始時間</FieldLabel>
            <input
              type="time"
              defaultValue={
                project.work_start_time
                  ? project.work_start_time.match(/T(\d{2}:\d{2})/)?.[1] ??
                    project.work_start_time.slice(0, 5)
                  : ""
              }
              onBlur={(e) => {
                const val = e.target.value;
                if (!val) return;
                // Store as ISO datetime using scheduled_date or work_date
                const base = project.scheduled_date || project.work_date;
                save({ work_start_time: `${base}T${val}:00` });
              }}
              className="input-glass w-full"
            />
          </div>
          <div>
            <FieldLabel>作業終了時間</FieldLabel>
            <input
              type="time"
              defaultValue={
                project.work_end_time
                  ? project.work_end_time.match(/T(\d{2}:\d{2})/)?.[1] ??
                    project.work_end_time.slice(0, 5)
                  : ""
              }
              onBlur={(e) => {
                const val = e.target.value;
                if (!val) return;
                const base = project.scheduled_date || project.work_date;
                save({ work_end_time: `${base}T${val}:00` });
              }}
              className="input-glass w-full"
            />
          </div>
        </div>
      </div>

      {/* Memo & Description */}
      <div className="liquid-glass p-4 space-y-3">
        <div>
          <FieldLabel>調整メモ</FieldLabel>
          <textarea
            defaultValue={project.memo ?? ""}
            onBlur={(e) => handleBlur("memo", e.target.value)}
            rows={3}
            placeholder="担当者名、連絡先、特記事項など"
            className="input-glass w-full resize-none"
          />
        </div>
        <div>
          <FieldLabel>案件内容</FieldLabel>
          <textarea
            defaultValue={project.description ?? ""}
            onBlur={(e) => handleBlur("description", e.target.value)}
            rows={4}
            placeholder="作業内容の詳細"
            className="input-glass w-full resize-none"
          />
        </div>
      </div>

      {/* Read-only info */}
      <div className="liquid-glass p-4 space-y-2">
        <div className="flex justify-between text-xs text-gray-500 font-medium">
          <span>現場ID</span>
          <span className="font-mono">{project.site_id}</span>
        </div>
        <div className="flex justify-between text-xs text-gray-500 font-medium">
          <span>作業員</span>
          <span>{project.worker_name}</span>
        </div>
        <div className="flex justify-between text-xs text-gray-500 font-medium">
          <span>作業日</span>
          <span>{project.work_date}</span>
        </div>
        <div className="flex justify-between text-xs text-gray-500 font-medium">
          <span>案件ID</span>
          <span className="font-mono text-gray-400">{project.project_id}</span>
        </div>
      </div>
    </div>
  );
}

// --- Shoot Tab ---

function ShootTab({
  project,
  validation,
}: {
  project: Project;
  validation: ValidationResult | null;
}) {
  const totalSlots = project.equipment.reduce((s, eq) => s + eq.slots.length, 0);
  const filledSlots = project.equipment.reduce(
    (s, eq) => s + eq.slots.filter((sl) => sl.photo_filename).length,
    0,
  );
  const isComplete = totalSlots > 0 && filledSlots === totalSlots;

  return (
    <div className="space-y-4">
      {/* Progress summary */}
      <div className="liquid-glass p-4">
        <div className="flex items-center justify-between mb-3">
          <h3 className="font-bold text-gray-700 text-sm">撮影進捗</h3>
          {isComplete && (
            <span className="text-xs font-bold text-green-600 bg-green-50 px-2.5 py-0.5 rounded-full">
              ✓ 完了
            </span>
          )}
        </div>
        <div className="flex items-center gap-3">
          <div className="flex-1 h-2 bg-gray-100 rounded-full overflow-hidden">
            <div
              className="h-full rounded-full transition-all"
              style={{
                width: totalSlots > 0 ? `${(filledSlots / totalSlots) * 100}%` : "0%",
                background: isComplete
                  ? "linear-gradient(90deg, #22c55e, #16a34a)"
                  : "linear-gradient(90deg, rgba(99,102,241,0.8), rgba(139,92,246,0.8))",
              }}
            />
          </div>
          <span className="text-sm font-bold text-gray-700 min-w-[3.5rem] text-right">
            {filledSlots}/{totalSlots}
          </span>
        </div>
        {validation && validation.missing_slots.length > 0 && (
          <div className="mt-3 space-y-1">
            {validation.missing_slots.map((slot) => (
              <p key={`${slot.equipment_id}-${slot.slot_id}`} className="text-xs text-red-500 font-medium">
                ✗ {slot.equipment_name} — {slot.slot_label}
              </p>
            ))}
          </div>
        )}
      </div>

      {/* Link to shoot page */}
      <Link href={`/shoot?projectId=${project.project_id}`}>
        <div
          className="liquid-glass p-5 flex items-center justify-between cursor-pointer hover:shadow-lg transition-shadow"
          style={{ borderColor: "rgba(99,102,241,0.2)" }}
        >
          <div className="flex items-center gap-3">
            <div
              className="w-11 h-11 rounded-2xl flex items-center justify-center text-xl"
              style={{
                background: "linear-gradient(135deg, rgba(99,102,241,0.15), rgba(139,92,246,0.15))",
                border: "1px solid rgba(255,255,255,0.4)",
              }}
            >
              📷
            </div>
            <div>
              <p className="font-bold text-gray-800 text-sm">撮影画面を開く</p>
              <p className="text-xs text-gray-500 font-medium mt-0.5">写真の撮影・アップロード</p>
            </div>
          </div>
          <svg className="w-5 h-5 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
          </svg>
        </div>
      </Link>

      {/* Equipment slots list */}
      {project.equipment.map((eq) => (
        <div key={eq.equipment_id} className="liquid-glass p-4 space-y-2">
          <p className="font-bold text-gray-700 text-sm">{eq.name}</p>
          {eq.slots.map((slot) => (
            <div key={slot.slot_id} className="flex items-center justify-between text-xs font-medium">
              <span className="text-gray-600">{slot.label}</span>
              {slot.photo_filename ? (
                <span className="text-green-600">✓ 撮影済み</span>
              ) : (
                <span className="text-red-400">未撮影</span>
              )}
            </div>
          ))}
        </div>
      ))}

      {/* Excel export */}
      {isComplete && (
        <a
          href={`/api/projects/${project.project_id}/export`}
          download
          className="block w-full py-3.5 text-center text-sm font-bold text-white rounded-2xl"
          style={{
            background: "linear-gradient(135deg, rgba(34,197,94,0.85), rgba(16,163,74,0.85))",
            border: "1px solid rgba(255,255,255,0.3)",
            boxShadow: "inset 0 1px 0 rgba(255,255,255,0.35), 0 4px 12px rgba(34,197,94,0.25)",
          }}
        >
          📊 Excelレポートをダウンロード
        </a>
      )}
    </div>
  );
}

// --- Docs Tab (placeholder for Phase 2) ---

function DocsTab() {
  return (
    <div className="liquid-glass p-8 text-center space-y-3">
      <div className="text-4xl">📁</div>
      <p className="font-bold text-gray-600 text-sm">書類管理</p>
      <p className="text-xs text-gray-400 font-medium">Phase 2 で実装予定</p>
    </div>
  );
}

// --- Main Page ---

export default function ProjectDetailPage() {
  const params = useParams();
  const router = useRouter();
  const projectId = params.id as string;

  const [project, setProject] = useState<Project | null>(null);
  const [validation, setValidation] = useState<ValidationResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("info");

  useEffect(() => {
    Promise.all([
      getProject(projectId),
      validateProject(projectId),
    ])
      .then(([proj, val]) => {
        setProject(proj);
        setValidation(val);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "読み込みに失敗しました"))
      .finally(() => setLoading(false));
  }, [projectId]);

  const handleUpdate = useCallback(async (updates: ProjectUpdateRequest) => {
    const updated = await updateProject(projectId, updates);
    setProject(updated);
    // Re-fetch validation if equipment-related (shouldn't happen here but defensive)
  }, [projectId]);

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
      <div className="space-y-4 animate-fade-in-up">
        <div className="liquid-glass-red px-4 py-3 text-red-700 text-sm font-semibold">
          {error ?? "案件が見つかりません"}
        </div>
        <button onClick={() => router.back()} className="text-sm text-indigo-500 font-bold px-2">
          ← 戻る
        </button>
      </div>
    );
  }

  const tabs: { key: Tab; label: string; icon: string }[] = [
    { key: "info", label: "案件情報", icon: "📋" },
    { key: "shoot", label: "撮影管理", icon: "📷" },
    { key: "docs", label: "書類管理", icon: "📁" },
  ];

  return (
    <div className="space-y-4 animate-fade-in-up">
      {/* Page header */}
      <div className="flex items-start gap-3 px-1">
        <button onClick={() => router.back()} className="mt-1 text-gray-400 hover:text-gray-600 transition-colors">
          <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M15 19l-7-7 7-7" />
          </svg>
        </button>
        <div className="flex-1 min-w-0">
          <h2 className="text-xl font-extrabold text-gray-800 tracking-tight truncate">
            {project.project_name || project.site_id}
          </h2>
          <div className="flex items-center gap-2 mt-1">
            <StatusBadge status={project.status} />
            {project.project_number && (
              <span className="text-xs text-gray-400 font-medium">#{project.project_number}</span>
            )}
          </div>
        </div>
      </div>

      {/* Tabs */}
      <div
        className="flex rounded-2xl p-1 gap-1"
        style={{
          background: "rgba(255,255,255,0.35)",
          border: "1px solid rgba(255,255,255,0.5)",
          boxShadow: "inset 0 1px 0 rgba(255,255,255,0.6)",
          backdropFilter: "blur(12px)",
        }}
      >
        {tabs.map(({ key, label, icon }) => (
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
                : { color: "rgba(75,85,99,0.8)" }
            }
          >
            <span className="mr-1">{icon}</span>
            {label}
          </button>
        ))}
      </div>

      {/* Tab content */}
      {tab === "info" && <InfoTab project={project} onUpdate={handleUpdate} />}
      {tab === "shoot" && <ShootTab project={project} validation={validation} />}
      {tab === "docs" && <DocsTab />}
    </div>
  );
}
