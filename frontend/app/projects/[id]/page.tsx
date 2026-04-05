"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import {
  getProject,
  updateProject,
  validateProject,
  setRetakeInstruction,
  uploadDocument,
  deleteDocument,
  getDocumentUrl,
  setResubmitInstruction,
  forceUpdateTimelog,
} from "@/lib/api";
import type {
  Project,
  ProjectDocument,
  ProjectStatus,
  ProjectUpdateRequest,
  ValidationResult,
} from "@/types";
import { useAdminMode } from "@/lib/useAdminMode";
import { useMasterConfig } from "@/lib/useMasterConfig";

type Tab = "info" | "shoot" | "docs";

// --- Sub-components ---

function StatusBadge({ status, colorClass }: { status: ProjectStatus; colorClass?: string }) {
  return (
    <span className={`text-xs font-bold px-2.5 py-0.5 rounded-full ${colorClass ?? "bg-gray-100 text-gray-600"}`}>
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

// --- Timelog Section ---

const TIMELOG_ACTIONS = [
  { field: "departure_time" as const, label: "出発", emoji: "🚗", color: "99,102,241" },
  { field: "arrival_time" as const,   label: "到着", emoji: "📍", color: "34,197,94" },
  { field: "checkout_time" as const,  label: "退店", emoji: "🏁", color: "249,115,22" },
] as const;

type TimelogField = "departure_time" | "arrival_time" | "checkout_time";

function TimelogSection({
  project,
  onUpdate,
  onProjectUpdated,
}: {
  project: Project;
  onUpdate: (updates: ProjectUpdateRequest) => Promise<void>;
  onProjectUpdated: (project: Project) => void;
}) {
  const [isAdmin] = useAdminMode();
  const [loading, setLoading] = useState<TimelogField | null>(null);
  // Pre-stamp picker state (all users)
  const [pendingField, setPendingField] = useState<TimelogField | null>(null);
  const [pendingTime, setPendingTime] = useState("");
  // Admin edit picker state
  const [editingField, setEditingField] = useState<TimelogField | null>(null);
  const [editingTime, setEditingTime] = useState("");

  const nowHHMM = () => {
    const d = new Date();
    return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  };

  const handleButtonClick = (field: TimelogField, recorded: string | null) => {
    if (recorded) {
      if (!isAdmin) return;
      const hhmm = recorded.match(/T(\d{2}:\d{2})/)?.[1] ?? recorded.slice(11, 16);
      setEditingField(field);
      setEditingTime(hhmm);
      setPendingField(null);
    } else {
      setPendingField(field);
      setPendingTime(nowHHMM());
      setEditingField(null);
    }
  };

  const handleStampConfirm = async () => {
    if (!pendingField) return;
    setLoading(pendingField);
    try {
      await onUpdate({ [pendingField]: `${project.work_date}T${pendingTime}:00` });
      setPendingField(null);
    } finally {
      setLoading(null);
    }
  };

  const handleEditConfirm = async () => {
    if (!editingField) return;
    setLoading(editingField);
    try {
      const updated = await forceUpdateTimelog(project.project_id, editingField, editingTime);
      onProjectUpdated(updated);
      setEditingField(null);
    } finally {
      setLoading(null);
    }
  };

  const activeField = pendingField ?? editingField;
  const isEditing = editingField !== null;

  return (
    <div className="liquid-glass p-4 space-y-3">
      <FieldLabel>打刻</FieldLabel>
      <div className="grid grid-cols-3 gap-2">
        {TIMELOG_ACTIONS.map(({ field, label, emoji, color }) => {
          const recorded = project[field] ?? null;
          const isActive = activeField === field;
          const isLoading = loading === field;
          const isManual =
            field === "departure_time" ? project.departure_time_manual
            : field === "arrival_time" ? project.arrival_time_manual
            : project.checkout_time_manual;
          const hhmm = recorded
            ? recorded.match(/T(\d{2}:\d{2})/)?.[1] ?? recorded.slice(11, 16)
            : null;
          return (
            <button
              key={field}
              disabled={(!recorded && isLoading) || (!!recorded && !isAdmin)}
              onClick={() => handleButtonClick(field, recorded)}
              className="flex flex-col items-center gap-1 py-3 rounded-2xl text-xs font-bold transition-all"
              style={
                recorded
                  ? {
                      background: `rgba(${color},0.08)`,
                      border: `1px solid rgba(${color},${isActive ? "0.6" : "0.3"})`,
                      color: `rgba(${color},0.8)`,
                      cursor: isAdmin ? "pointer" : "default",
                      boxShadow: isActive ? `0 0 0 2px rgba(${color},0.25)` : "none",
                    }
                  : {
                      background: isActive
                        ? `rgba(${color},0.15)`
                        : `linear-gradient(135deg, rgba(${color},0.12), rgba(${color},0.07))`,
                      border: `1px solid rgba(${color},${isActive ? "0.4" : "0.2"})`,
                      color: `rgba(${color},0.9)`,
                      boxShadow: isActive ? `0 0 0 2px rgba(${color},0.2)` : "none",
                    }
              }
            >
              <span className="text-lg">{emoji}</span>
              <span>{label}</span>
              {hhmm ? (
                <span className="text-[10px] font-medium opacity-75">
                  {hhmm}{isAdmin && " ✎"}
                </span>
              ) : null}
              {isManual && (
                <span className="text-[9px] font-bold opacity-50">手動修正</span>
              )}
              {isLoading && <span className="text-[10px] opacity-50">送信中...</span>}
            </button>
          );
        })}
      </div>

      {/* Inline time picker — appears when pre-stamp or admin edit is active */}
      {activeField && (
        <div
          className="flex items-center gap-2 rounded-xl px-3 py-2.5"
          style={{ background: "rgba(0,0,0,0.03)", border: "1px solid rgba(0,0,0,0.06)" }}
        >
          <span className="text-sm shrink-0">
            {TIMELOG_ACTIONS.find((a) => a.field === activeField)?.emoji}
          </span>
          <span className="text-xs font-bold text-gray-600 shrink-0">
            {TIMELOG_ACTIONS.find((a) => a.field === activeField)?.label}
            {isEditing && (
              <span className="ml-1 text-[10px] text-amber-600 font-bold">修正</span>
            )}
          </span>
          <input
            type="time"
            value={isEditing ? editingTime : pendingTime}
            onChange={(e) =>
              isEditing ? setEditingTime(e.target.value) : setPendingTime(e.target.value)
            }
            className="input-glass flex-1 text-sm py-1"
          />
          <button
            onClick={isEditing ? handleEditConfirm : handleStampConfirm}
            disabled={loading !== null}
            className="text-xs font-bold px-3 py-1.5 rounded-xl text-white disabled:opacity-50 shrink-0"
            style={{
              background: "linear-gradient(135deg, rgba(99,102,241,0.85), rgba(139,92,246,0.85))",
            }}
          >
            {isEditing ? "更新" : "打刻"}
          </button>
          <button
            onClick={() => { setPendingField(null); setEditingField(null); }}
            className="text-xs text-gray-400 hover:text-gray-600 px-1 shrink-0"
          >
            ✕
          </button>
        </div>
      )}
    </div>
  );
}

// --- Info Tab ---

function InfoTab({
  project,
  onUpdate,
  onProjectUpdated,
  statuses,
}: {
  project: Project;
  onUpdate: (updates: ProjectUpdateRequest) => Promise<void>;
  onProjectUpdated: (project: Project) => void;
  statuses: string[];
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

      {/* 打刻 */}
      <TimelogSection project={project} onUpdate={onUpdate} onProjectUpdated={onProjectUpdated} />

      {/* Status */}
      <div className="liquid-glass p-4 space-y-3">
        <FieldLabel>ステータス</FieldLabel>
        <select
          defaultValue={project.status}
          onChange={(e) => handleStatusChange(e.target.value as ProjectStatus)}
          className="input-glass w-full"
        >
          {statuses.map((s) => (
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

const RETAKE_PRESETS = ["不鮮明", "角度不良", "範囲外", "その他"];

function RetakeInstructionPanel({
  projectId,
  equipmentId,
  slotId,
  currentReason,
  onUpdated,
}: {
  projectId: string;
  equipmentId: string;
  slotId: string;
  currentReason: string | null;
  onUpdated: (project: Project) => void;
}) {
  const [open, setOpen] = useState(false);
  const [preset, setPreset] = useState(RETAKE_PRESETS[0]);
  const [custom, setCustom] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSet = async () => {
    const reason = preset === "その他" ? custom.trim() : preset;
    if (!reason) return;
    setLoading(true);
    try {
      const updated = await setRetakeInstruction(projectId, equipmentId, slotId, reason);
      onUpdated(updated);
      setOpen(false);
    } finally {
      setLoading(false);
    }
  };

  const handleClear = async () => {
    setLoading(true);
    try {
      const updated = await setRetakeInstruction(projectId, equipmentId, slotId, null);
      onUpdated(updated);
    } finally {
      setLoading(false);
    }
  };

  if (currentReason) {
    return (
      <button
        onClick={handleClear}
        disabled={loading}
        className="text-[10px] font-bold px-2 py-0.5 rounded-full text-orange-600 bg-orange-50 border border-orange-200 hover:bg-orange-100 transition-colors"
      >
        指示解除
      </button>
    );
  }

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="text-[10px] font-bold px-2 py-0.5 rounded-full text-gray-500 bg-gray-50 border border-gray-200 hover:bg-gray-100 transition-colors"
      >
        再撮影指示
      </button>
    );
  }

  return (
    <div className="flex items-center gap-1.5 flex-wrap justify-end">
      <select
        value={preset}
        onChange={(e) => setPreset(e.target.value)}
        className="text-[10px] rounded-lg border border-gray-200 bg-white px-1.5 py-0.5"
      >
        {RETAKE_PRESETS.map((p) => <option key={p} value={p}>{p}</option>)}
      </select>
      {preset === "その他" && (
        <input
          type="text"
          value={custom}
          onChange={(e) => setCustom(e.target.value)}
          placeholder="理由を入力"
          className="text-[10px] rounded-lg border border-gray-200 bg-white px-1.5 py-0.5 w-24"
        />
      )}
      <button
        onClick={handleSet}
        disabled={loading || (preset === "その他" && !custom.trim())}
        className="text-[10px] font-bold px-2 py-0.5 rounded-full text-white bg-orange-500 hover:bg-orange-600 disabled:opacity-40 transition-colors"
      >
        送信
      </button>
      <button
        onClick={() => setOpen(false)}
        className="text-[10px] text-gray-400 hover:text-gray-600"
      >
        ✕
      </button>
    </div>
  );
}

function ShootTab({
  project,
  validation,
  onProjectUpdated,
}: {
  project: Project;
  validation: ValidationResult | null;
  onProjectUpdated: (project: Project) => void;
}) {
  const [isAdmin] = useAdminMode();
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
          {eq.slots.map((slot) => {
            const hasRetake = !!slot.retake_instruction;
            return (
              <div
                key={slot.slot_id}
                className="rounded-xl p-2.5 transition-all"
                style={
                  hasRetake
                    ? { border: "1.5px solid rgba(239,68,68,0.4)", background: "rgba(254,226,226,0.3)" }
                    : {}
                }
              >
                <div className="flex items-center justify-between text-xs font-medium">
                  <span className="text-gray-600">{slot.label}</span>
                  <div className="flex items-center gap-2">
                    {slot.photo_filename ? (
                      <span className="text-green-600">✓ 撮影済み</span>
                    ) : (
                      <span className="text-red-400">未撮影</span>
                    )}
                    {isAdmin && slot.photo_filename && (
                      <RetakeInstructionPanel
                        projectId={project.project_id}
                        equipmentId={eq.equipment_id}
                        slotId={slot.slot_id}
                        currentReason={slot.retake_instruction ?? null}
                        onUpdated={onProjectUpdated}
                      />
                    )}
                  </div>
                </div>
                {hasRetake && (
                  <p className="text-[11px] font-bold text-red-600 mt-1.5">
                    ⚠ {slot.retake_instruction}
                  </p>
                )}
              </div>
            );
          })}
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

// --- Docs Tab ---

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function DocumentRow({
  doc,
  projectId,
  isAdmin,
  onDelete,
  onResubmit,
}: {
  doc: ProjectDocument;
  projectId: string;
  isAdmin: boolean;
  onDelete: (docId: string) => void;
  onResubmit: (docId: string, reason: string | null) => void;
}) {
  const [resubmitOpen, setResubmitOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [loading, setLoading] = useState(false);

  const handleResubmitSet = async () => {
    const r = reason.trim();
    if (!r) return;
    setLoading(true);
    try {
      await onResubmit(doc.document_id, r);
      setResubmitOpen(false);
    } finally {
      setLoading(false);
    }
  };

  const handleResubmitClear = async () => {
    setLoading(true);
    try {
      await onResubmit(doc.document_id, null);
    } finally {
      setLoading(false);
    }
  };

  const handleDelete = async () => {
    if (!confirm(`「${doc.original_filename}」を削除しますか？`)) return;
    setLoading(true);
    try {
      await onDelete(doc.document_id);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      className="rounded-xl p-3 space-y-1.5"
      style={
        doc.resubmit_instruction
          ? { border: "1.5px solid rgba(239,68,68,0.4)", background: "rgba(254,226,226,0.3)" }
          : { border: "1px solid rgba(0,0,0,0.06)", background: "rgba(255,255,255,0.5)" }
      }
    >
      <div className="flex items-start gap-2">
        <a
          href={getDocumentUrl(projectId, doc.stored_filename)}
          target="_blank"
          rel="noopener noreferrer"
          className="flex-1 min-w-0"
        >
          <p className="text-xs font-bold text-indigo-600 truncate hover:underline">
            📄 {doc.original_filename}
          </p>
          <p className="text-[10px] text-gray-400 font-medium mt-0.5">
            {formatBytes(doc.size_bytes)} · {doc.uploaded_at.slice(0, 10)}
          </p>
        </a>
        {isAdmin && (
          <div className="flex items-center gap-1 shrink-0">
            {doc.resubmit_instruction ? (
              <button
                onClick={handleResubmitClear}
                disabled={loading}
                className="text-[10px] font-bold px-2 py-0.5 rounded-full text-orange-600 bg-orange-50 border border-orange-200 hover:bg-orange-100"
              >
                指示解除
              </button>
            ) : (
              <button
                onClick={() => setResubmitOpen(!resubmitOpen)}
                className="text-[10px] font-bold px-2 py-0.5 rounded-full text-gray-500 bg-gray-50 border border-gray-200 hover:bg-gray-100"
              >
                再提出指示
              </button>
            )}
            <button
              onClick={handleDelete}
              disabled={loading}
              className="text-[10px] font-bold px-2 py-0.5 rounded-full text-red-500 bg-red-50 border border-red-200 hover:bg-red-100"
            >
              削除
            </button>
          </div>
        )}
      </div>
      {doc.resubmit_instruction && (
        <p className="text-[11px] font-bold text-red-600">⚠ {doc.resubmit_instruction}</p>
      )}
      {resubmitOpen && (
        <div className="flex items-center gap-1.5 pt-1">
          <input
            type="text"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="再提出理由を入力"
            className="flex-1 text-[10px] rounded-lg border border-gray-200 bg-white px-2 py-1"
          />
          <button
            onClick={handleResubmitSet}
            disabled={loading || !reason.trim()}
            className="text-[10px] font-bold px-2 py-1 rounded-full text-white bg-orange-500 hover:bg-orange-600 disabled:opacity-40"
          >
            送信
          </button>
          <button onClick={() => setResubmitOpen(false)} className="text-[10px] text-gray-400">✕</button>
        </div>
      )}
    </div>
  );
}

function DocSection({
  title,
  docTypes,
  project,
  isAdmin,
  onProjectUpdated,
}: {
  title: string;
  docTypes: string[];
  project: Project;
  isAdmin: boolean;
  onProjectUpdated: (p: Project) => void;
}) {
  const docs = (project.documents ?? []).filter((d) => docTypes.includes(d.document_type));
  const [selectedType, setSelectedType] = useState(docTypes[0]);
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  const handleUpload = async (file: File) => {
    setUploading(true);
    setUploadError(null);
    try {
      await uploadDocument(project.project_id, selectedType, file);
      // Refresh project to get updated documents list
      const { getProject } = await import("@/lib/api");
      const updated = await getProject(project.project_id);
      onProjectUpdated(updated);
    } catch (e) {
      setUploadError(e instanceof Error ? e.message : "アップロードに失敗しました");
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const handleDelete = async (docId: string) => {
    await deleteDocument(project.project_id, docId);
    const { getProject } = await import("@/lib/api");
    const updated = await getProject(project.project_id);
    onProjectUpdated(updated);
  };

  const handleResubmit = async (docId: string, reason: string | null) => {
    const updated = await setResubmitInstruction(project.project_id, docId, reason);
    onProjectUpdated(updated);
  };

  return (
    <div className="liquid-glass p-4 space-y-3">
      <h4 className="font-bold text-gray-700 text-sm">{title}</h4>

      {/* Upload UI */}
      <div className="flex items-center gap-2">
        {docTypes.length > 1 && (
          <select
            value={selectedType}
            onChange={(e) => setSelectedType(e.target.value)}
            className="input-glass text-xs py-1.5 flex-1"
          >
            {docTypes.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        )}
        <label
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-bold cursor-pointer transition-all"
          style={{
            background: uploading
              ? "rgba(99,102,241,0.05)"
              : "linear-gradient(135deg, rgba(99,102,241,0.12), rgba(139,92,246,0.12))",
            border: "1px solid rgba(99,102,241,0.2)",
            color: "rgba(99,102,241,0.9)",
            opacity: uploading ? 0.6 : 1,
          }}
        >
          {uploading ? "アップロード中..." : "＋ ファイルを追加"}
          <input
            ref={fileRef}
            type="file"
            accept=".pdf,.docx,.xlsx,.xls,.jpg,.jpeg,.png"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) handleUpload(f);
            }}
          />
        </label>
      </div>

      {uploadError && (
        <p className="text-[11px] font-medium text-red-600">{uploadError}</p>
      )}

      {/* Document list */}
      {docs.length === 0 ? (
        <p className="text-xs text-gray-400 font-medium text-center py-2">書類なし</p>
      ) : (
        <div className="space-y-2">
          {docs.map((doc) => (
            <DocumentRow
              key={doc.document_id}
              doc={doc}
              projectId={project.project_id}
              isAdmin={isAdmin}
              onDelete={handleDelete}
              onResubmit={handleResubmit}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function DocsTab({
  project,
  onUpdate,
  onProjectUpdated,
  docTypesByCategory,
}: {
  project: Project;
  onUpdate: (updates: ProjectUpdateRequest) => Promise<void>;
  onProjectUpdated: (p: Project) => void;
  docTypesByCategory: (cat: "管理共有" | "現地調査" | "設置") => string[];
}) {
  const [isAdmin] = useAdminMode();
  const [surveyNotes, setSurveyNotes] = useState(project.survey_notes ?? "");
  const [notesSaving, setNotesSaving] = useState(false);

  const handleNotesBlur = async () => {
    const trimmed = surveyNotes.trim();
    if (trimmed === (project.survey_notes ?? "")) return;
    setNotesSaving(true);
    try {
      await onUpdate({ survey_notes: trimmed || null });
    } finally {
      setNotesSaving(false);
    }
  };

  return (
    <div className="space-y-4">
      {/* Section 1: 統制からの資料 */}
      <DocSection
        title="統制からの資料"
        docTypes={docTypesByCategory("管理共有")}
        project={project}
        isAdmin={isAdmin}
        onProjectUpdated={onProjectUpdated}
      />

      {/* Section 2: 現地調査 */}
      <DocSection
        title="現地調査"
        docTypes={docTypesByCategory("現地調査")}
        project={project}
        isAdmin={isAdmin}
        onProjectUpdated={onProjectUpdated}
      />

      {/* 申し送り事項 (Section 2 entry) */}
      <div className="liquid-glass p-4 space-y-2">
        <div className="flex items-center justify-between">
          <FieldLabel>申し送り事項</FieldLabel>
          {notesSaving && <span className="text-[10px] text-indigo-400 font-medium">保存中...</span>}
        </div>
        <textarea
          value={surveyNotes}
          onChange={(e) => setSurveyNotes(e.target.value)}
          onBlur={handleNotesBlur}
          rows={4}
          placeholder="次工程への申し送り事項を記入..."
          className="input-glass w-full resize-none"
        />
      </div>

      {/* Section 3: 設置 */}
      <DocSection
        title="設置"
        docTypes={docTypesByCategory("設置")}
        project={project}
        isAdmin={isAdmin}
        onProjectUpdated={onProjectUpdated}
      />

      {/* 申し送り事項 (Section 3 read-only) */}
      {project.survey_notes && (
        <div className="liquid-glass p-4 space-y-2">
          <FieldLabel>申し送り事項（参照）</FieldLabel>
          <p className="text-xs text-gray-600 font-medium whitespace-pre-wrap">{project.survey_notes}</p>
        </div>
      )}
    </div>
  );
}

// --- Main Page ---

export default function ProjectDetailPage() {
  const params = useParams();
  const router = useRouter();
  const projectId = params.id as string;
  const { colorOf, config, docTypesByCategory } = useMasterConfig();

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
            <StatusBadge status={project.status} colorClass={colorOf(project.status)} />
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
          background: "var(--c-tab-bg)",
          border: "1px solid var(--c-tab-border)",
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
                : { color: "var(--c-text-secondary)" }
            }
          >
            <span className="mr-1">{icon}</span>
            {label}
          </button>
        ))}
      </div>

      {/* Tab content */}
      {tab === "info" && (
        <InfoTab
          project={project}
          onUpdate={handleUpdate}
          onProjectUpdated={setProject}
          statuses={config.statuses.map((s) => s.value)}
        />
      )}
      {tab === "shoot" && (
        <ShootTab project={project} validation={validation} onProjectUpdated={setProject} />
      )}
      {tab === "docs" && (
        <DocsTab
          project={project}
          onUpdate={handleUpdate}
          onProjectUpdated={setProject}
          docTypesByCategory={docTypesByCategory}
        />
      )}
    </div>
  );
}
