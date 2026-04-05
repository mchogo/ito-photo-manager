"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useMasterConfig } from "@/lib/useMasterConfig";
import { useAdminMode } from "@/lib/useAdminMode";
import type { MasterConfigDocType, MasterConfigStatus } from "@/types";
import { COLOR_PALETTE, DOCUMENT_CATEGORY_TITLES } from "@/types";

const PALETTE_KEYS = Object.keys(COLOR_PALETTE) as (keyof typeof COLOR_PALETTE)[];
const DOC_CATEGORIES = ["管理共有", "現地調査", "設置"] as const;

// --- Color swatch picker ---
function ColorPicker({
  value,
  onChange,
}: {
  value: string;
  onChange: (color: string) => void;
}) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {PALETTE_KEYS.map((key) => (
        <button
          key={key}
          type="button"
          onClick={() => onChange(key)}
          title={key}
          className={`w-5 h-5 rounded-full border-2 transition-all ${
            value === key ? "border-gray-600 scale-125" : "border-transparent"
          } ${COLOR_PALETTE[key].split(" ")[0]}`}
        />
      ))}
    </div>
  );
}

// --- Status row ---
function StatusRow({
  status,
  onDelete,
  onColorChange,
}: {
  status: MasterConfigStatus;
  onDelete: () => void;
  onColorChange: (color: string) => void;
}) {
  return (
    <div className="flex items-center gap-3 py-2 border-b border-black/5 last:border-0">
      <span
        className={`text-xs font-bold px-2.5 py-0.5 rounded-full shrink-0 ${
          COLOR_PALETTE[status.color] ?? COLOR_PALETTE.gray
        }`}
      >
        {status.value}
      </span>
      <div className="flex-1">
        <ColorPicker value={status.color} onChange={onColorChange} />
      </div>
      <button
        type="button"
        onClick={onDelete}
        className="text-[10px] font-bold px-2 py-0.5 rounded-full text-red-500 bg-red-50 border border-red-200 hover:bg-red-100 shrink-0"
      >
        削除
      </button>
    </div>
  );
}

// --- Doc type row ---
function DocTypeRow({
  doc,
  onDelete,
}: {
  doc: MasterConfigDocType;
  onDelete: () => void;
}) {
  return (
    <div className="flex items-center gap-3 py-2 border-b border-black/5 last:border-0">
      <span className="text-xs font-medium text-gray-700 flex-1">{doc.value}</span>
      <span className="text-[10px] text-gray-400 font-medium shrink-0">{doc.category}</span>
      <button
        type="button"
        onClick={onDelete}
        className="text-[10px] font-bold px-2 py-0.5 rounded-full text-red-500 bg-red-50 border border-red-200 hover:bg-red-100 shrink-0"
      >
        削除
      </button>
    </div>
  );
}

// --- Main Page ---
export default function AdminSettingsPage() {
  const router = useRouter();
  const [isAdmin] = useAdminMode();
  const { config, saveStatuses, saveDocumentTypes } = useMasterConfig();

  // Status state
  const [statuses, setStatuses] = useState<MasterConfigStatus[]>(() => config.statuses);
  const [newStatusName, setNewStatusName] = useState("");
  const [newStatusColor, setNewStatusColor] = useState("gray");
  const [statusSaving, setStatusSaving] = useState(false);
  const [statusError, setStatusError] = useState<string | null>(null);

  // Doc type state
  const [docTypes, setDocTypes] = useState<MasterConfigDocType[]>(() => config.document_types);
  const [newDocName, setNewDocName] = useState("");
  const [newDocCategory, setNewDocCategory] = useState<"管理共有" | "現地調査" | "設置">("管理共有");
  const [docSaving, setDocSaving] = useState(false);
  const [docError, setDocError] = useState<string | null>(null);

  if (!isAdmin) {
    return (
      <div className="liquid-glass-red px-4 py-3 text-red-700 text-sm font-semibold">
        管理者のみアクセスできます
      </div>
    );
  }

  // --- Status handlers ---
  const handleAddStatus = () => {
    const name = newStatusName.trim();
    if (!name) return;
    if (statuses.some((s) => s.value === name)) {
      setStatusError("同名のステータスがすでに存在します");
      return;
    }
    setStatuses((prev) => [...prev, { value: name, color: newStatusColor }]);
    setNewStatusName("");
    setNewStatusColor("gray");
    setStatusError(null);
  };

  const handleDeleteStatus = (idx: number) => {
    setStatuses((prev) => prev.filter((_, i) => i !== idx));
  };

  const handleColorChange = (idx: number, color: string) => {
    setStatuses((prev) => prev.map((s, i) => (i === idx ? { ...s, color } : s)));
  };

  const handleSaveStatuses = async () => {
    setStatusSaving(true);
    setStatusError(null);
    try {
      await saveStatuses(statuses);
    } catch (e) {
      setStatusError(e instanceof Error ? e.message : "保存に失敗しました");
    } finally {
      setStatusSaving(false);
    }
  };

  // --- Doc type handlers ---
  const handleAddDocType = () => {
    const name = newDocName.trim();
    if (!name) return;
    if (docTypes.some((d) => d.value === name)) {
      setDocError("同名の書類種別がすでに存在します");
      return;
    }
    setDocTypes((prev) => [...prev, { value: name, category: newDocCategory }]);
    setNewDocName("");
    setDocError(null);
  };

  const handleDeleteDocType = (idx: number) => {
    setDocTypes((prev) => prev.filter((_, i) => i !== idx));
  };

  const handleSaveDocTypes = async () => {
    setDocSaving(true);
    setDocError(null);
    try {
      await saveDocumentTypes(docTypes);
    } catch (e) {
      setDocError(e instanceof Error ? e.message : "保存に失敗しました");
    } finally {
      setDocSaving(false);
    }
  };

  return (
    <div className="space-y-6 animate-fade-in-up">
      {/* Header */}
      <div className="flex items-center gap-3 px-1">
        <button
          onClick={() => router.back()}
          className="text-gray-400 hover:text-gray-600 transition-colors"
        >
          <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M15 19l-7-7 7-7" />
          </svg>
        </button>
        <h2 className="text-xl font-extrabold text-gray-800 tracking-tight">マスター設定</h2>
      </div>

      {/* ===== Status section ===== */}
      <div className="liquid-glass p-4 space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="font-bold text-gray-700 text-sm">ステータス管理</h3>
          <button
            onClick={handleSaveStatuses}
            disabled={statusSaving}
            className="text-xs font-bold px-3 py-1.5 rounded-xl text-white disabled:opacity-50"
            style={{
              background: "linear-gradient(135deg, rgba(99,102,241,0.85), rgba(139,92,246,0.85))",
            }}
          >
            {statusSaving ? "保存中..." : "保存"}
          </button>
        </div>

        {statusError && (
          <p className="text-xs font-medium text-red-600">{statusError}</p>
        )}

        {/* Status list */}
        <div>
          {statuses.map((s, idx) => (
            <StatusRow
              key={`${s.value}-${idx}`}
              status={s}
              onDelete={() => handleDeleteStatus(idx)}
              onColorChange={(color) => handleColorChange(idx, color)}
            />
          ))}
        </div>

        {/* Add new status */}
        <div className="pt-2 space-y-2">
          <p className="text-[11px] font-bold text-gray-500 uppercase tracking-wider">新しいステータスを追加</p>
          <div className="flex gap-2">
            <input
              type="text"
              value={newStatusName}
              onChange={(e) => setNewStatusName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleAddStatus()}
              placeholder="ステータス名"
              className="input-glass flex-1 text-sm py-1.5"
            />
            <button
              type="button"
              onClick={handleAddStatus}
              className="text-xs font-bold px-3 py-1.5 rounded-xl shrink-0"
              style={{
                background: "linear-gradient(135deg, rgba(34,197,94,0.15), rgba(16,163,74,0.15))",
                border: "1px solid rgba(34,197,94,0.3)",
                color: "rgba(34,197,94,0.9)",
              }}
            >
              ＋ 追加
            </button>
          </div>
          <ColorPicker value={newStatusColor} onChange={setNewStatusColor} />
          {newStatusName && (
            <div className="flex items-center gap-2">
              <span className="text-[10px] text-gray-500">プレビュー:</span>
              <span
                className={`text-xs font-bold px-2.5 py-0.5 rounded-full ${
                  COLOR_PALETTE[newStatusColor] ?? COLOR_PALETTE.gray
                }`}
              >
                {newStatusName}
              </span>
            </div>
          )}
        </div>
      </div>

      {/* ===== Doc type section ===== */}
      <div className="liquid-glass p-4 space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="font-bold text-gray-700 text-sm">書類種別管理</h3>
          <button
            onClick={handleSaveDocTypes}
            disabled={docSaving}
            className="text-xs font-bold px-3 py-1.5 rounded-xl text-white disabled:opacity-50"
            style={{
              background: "linear-gradient(135deg, rgba(99,102,241,0.85), rgba(139,92,246,0.85))",
            }}
          >
            {docSaving ? "保存中..." : "保存"}
          </button>
        </div>

        {docError && (
          <p className="text-xs font-medium text-red-600">{docError}</p>
        )}

        {/* Doc type list grouped by category */}
        {DOC_CATEGORIES.map((cat) => {
          const items = docTypes.filter((d) => d.category === cat);
          return (
            <div key={cat}>
              <p className="text-[11px] font-bold text-gray-400 uppercase tracking-wider mb-1">
                {DOCUMENT_CATEGORY_TITLES[cat]}
              </p>
              {items.length === 0 ? (
                <p className="text-xs text-gray-300 py-1 pl-1">（なし）</p>
              ) : (
                items.map((doc) => {
                  const idx = docTypes.findIndex((d) => d.value === doc.value && d.category === doc.category);
                  return (
                    <DocTypeRow
                      key={`${doc.value}-${cat}`}
                      doc={doc}
                      onDelete={() => handleDeleteDocType(idx)}
                    />
                  );
                })
              )}
            </div>
          );
        })}

        {/* Add new doc type */}
        <div className="pt-2 space-y-2">
          <p className="text-[11px] font-bold text-gray-500 uppercase tracking-wider">新しい書類種別を追加</p>
          <div className="flex gap-2">
            <input
              type="text"
              value={newDocName}
              onChange={(e) => setNewDocName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleAddDocType()}
              placeholder="書類種別名"
              className="input-glass flex-1 text-sm py-1.5"
            />
            <select
              value={newDocCategory}
              onChange={(e) => setNewDocCategory(e.target.value as typeof newDocCategory)}
              className="input-glass text-sm py-1.5 shrink-0"
            >
              {DOC_CATEGORIES.map((c) => (
                <option key={c} value={c}>{DOCUMENT_CATEGORY_TITLES[c]}</option>
              ))}
            </select>
            <button
              type="button"
              onClick={handleAddDocType}
              className="text-xs font-bold px-3 py-1.5 rounded-xl shrink-0"
              style={{
                background: "linear-gradient(135deg, rgba(34,197,94,0.15), rgba(16,163,74,0.15))",
                border: "1px solid rgba(34,197,94,0.3)",
                color: "rgba(34,197,94,0.9)",
              }}
            >
              ＋ 追加
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
