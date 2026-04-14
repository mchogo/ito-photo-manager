"use client";

import Image from "next/image";
import { useRef, useState } from "react";
import type { PhotoSlotStatus } from "@/types";
import { getPhotoUrl, uploadPhoto, deletePhoto } from "@/lib/api";

interface Props {
  projectId: string;
  equipmentId: string;
  equipmentName: string;
  slot: PhotoSlotStatus;
  onUpdated: () => void;
}

export default function PhotoSlotCard({
  projectId,
  equipmentId,
  equipmentName,
  slot,
  onUpdated,
}: Props) {
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const cameraInputRef = useRef<HTMLInputElement>(null);

  const hasPhoto = slot.photo_filename !== null;

  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    setError(null);
    try {
      await uploadPhoto(projectId, equipmentId, slot.slot_id, file);
      onUpdated();
    } catch (err: unknown) {
      setError(
        err instanceof Error ? err.message : "アップロードに失敗しました",
      );
    } finally {
      setUploading(false);
      e.target.value = "";
    }
  };

  const handleDelete = async () => {
    if (!confirm("この写真を削除しますか？")) return;
    try {
      await deletePhoto(projectId, equipmentId, slot.slot_id);
      onUpdated();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "削除に失敗しました");
    }
  };

  return (
    <div
      className="overflow-hidden"
      style={{
        background: hasPhoto
          ? "rgba(16, 185, 129, 0.1)"
          : "rgba(255, 255, 255, 0.18)",
        backdropFilter: "blur(20px) saturate(180%)",
        WebkitBackdropFilter: "blur(20px) saturate(180%)",
        border: hasPhoto
          ? "1.5px solid rgba(16, 185, 129, 0.35)"
          : "1px solid rgba(255, 255, 255, 0.4)",
        boxShadow: hasPhoto
          ? "inset 0 1px 0 rgba(255,255,255,0.5), 0 4px 16px rgba(16,185,129,0.1)"
          : "inset 0 1px 0 rgba(255,255,255,0.5), 0 4px 16px rgba(0,0,0,0.04)",
        borderRadius: "18px",
        transition: "all 0.3s cubic-bezier(0.22, 1, 0.36, 1)",
      }}
    >
      {/* Header */}
      <div
        className="px-3 py-2.5 flex items-center justify-between"
        style={{
          background: hasPhoto
            ? "rgba(16, 185, 129, 0.18)"
            : "rgba(239, 68, 68, 0.1)",
          borderBottom: hasPhoto
            ? "1px solid rgba(16, 185, 129, 0.2)"
            : "1px solid rgba(255, 255, 255, 0.25)",
        }}
      >
        <div className={`text-[13px] font-extrabold ${hasPhoto ? "text-emerald-800" : "text-gray-900"}`}>
          {slot.label}
        </div>
        <div
          className="w-5 h-5 rounded-lg flex items-center justify-center"
          style={{
            background: hasPhoto
              ? "linear-gradient(135deg, rgba(5,150,105,0.9), rgba(4,120,87,0.9))"
              : "rgba(255,255,255,0.25)",
            border: hasPhoto
              ? "1px solid rgba(255,255,255,0.4)"
              : "1.5px solid rgba(0,0,0,0.12)",
            boxShadow: hasPhoto
              ? "inset 0 1px 0 rgba(255,255,255,0.4), 0 2px 4px rgba(16,185,129,0.2)"
              : "inset 0 1px 0 rgba(255,255,255,0.3)",
            transition: "all 0.3s cubic-bezier(0.34, 1.56, 0.64, 1)",
            transform: hasPhoto ? "scale(1.1)" : "scale(1)",
          }}
        >
          {hasPhoto && (
            <svg className="w-3 h-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
            </svg>
          )}
        </div>
      </div>

      {/* Content */}
      <div className="p-3">
        {error && (
          <div
            className="text-xs text-red-700 mb-2 px-2.5 py-1.5 font-bold"
            style={{
              background: "rgba(239, 68, 68, 0.1)",
              border: "1px solid rgba(239, 68, 68, 0.2)",
              borderRadius: "10px",
            }}
          >
            {error}
          </div>
        )}

        {hasPhoto ? (
          <div>
            <div
              className="relative overflow-hidden mb-2 group h-36"
              style={{ borderRadius: "14px" }}
            >
              <Image
                src={getPhotoUrl(slot.photo_filename!)}
                alt={`${equipmentName} - ${slot.label}`}
                fill
                sizes="(max-width: 640px) 50vw, 33vw"
                className="object-cover transition-transform duration-300"
                style={{ transition: "transform 0.4s cubic-bezier(0.22, 1, 0.36, 1)" }}
              />
              <div
                className="absolute inset-0 opacity-0 group-hover:opacity-100"
                style={{
                  background: "linear-gradient(to top, rgba(0,0,0,0.25), transparent)",
                  transition: "opacity 0.3s ease",
                }}
              />
            </div>
            <div className="flex gap-1.5">
              <button
                onClick={() => fileInputRef.current?.click()}
                disabled={uploading}
                className="btn-glass-sm flex-1 text-xs py-1.5 font-extrabold"
                style={{
                  background: "rgba(255,255,255,0.3)",
                  backdropFilter: "blur(12px)",
                  border: "1px solid rgba(255,255,255,0.45)",
                  borderRadius: "10px",
                  color: "#1e293b",
                  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.5)",
                }}
              >
                撮り直す
              </button>
              <button
                onClick={handleDelete}
                className="btn-glass-sm text-xs py-1.5 px-3 font-extrabold"
                style={{
                  background: "rgba(239, 68, 68, 0.12)",
                  border: "1px solid rgba(239, 68, 68, 0.25)",
                  borderRadius: "10px",
                  color: "#b91c1c",
                  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.3)",
                }}
              >
                削除
              </button>
            </div>
          </div>
        ) : (
          <div className="space-y-2">
            {/* Placeholder — skeleton shimmer when uploading */}
            <div
              className={`h-28 flex flex-col items-center justify-center ${uploading ? "skeleton-shimmer" : ""}`}
              style={{
                background: uploading
                  ? "rgba(99, 102, 241, 0.06)"
                  : "rgba(255,255,255,0.12)",
                border: uploading
                  ? "2px solid rgba(99, 102, 241, 0.2)"
                  : "2px dashed rgba(0,0,0,0.1)",
                borderRadius: "14px",
                transition: "all 0.3s ease",
              }}
            >
              {uploading ? (
                <div className="flex flex-col items-center gap-2 upload-pulse">
                  <div
                    className="w-10 h-10 rounded-2xl flex items-center justify-center"
                    style={{
                      background: "linear-gradient(135deg, rgba(99,102,241,0.2), rgba(139,92,246,0.2))",
                      border: "1px solid rgba(255,255,255,0.4)",
                    }}
                  >
                    <div className="w-5 h-5 border-2 border-indigo-300/60 border-t-indigo-600 rounded-full animate-spin" />
                  </div>
                  <span className="text-xs text-indigo-700/80 font-bold">アップロード中...</span>
                </div>
              ) : (
                <>
                  <svg className="w-8 h-8 text-gray-400/50 mb-1" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z" />
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M15 13a3 3 0 11-6 0 3 3 0 016 0z" />
                  </svg>
                  <span className="text-xs text-gray-500/70 font-bold">未撮影</span>
                </>
              )}
            </div>

            {/* Buttons — using CSS class for tap feedback */}
            <div className="flex gap-1.5">
              <button
                onClick={() => cameraInputRef.current?.click()}
                disabled={uploading}
                className="btn-glass-sm flex-1 py-2.5 text-white text-xs font-extrabold flex items-center justify-center gap-1 disabled:opacity-40"
                style={{
                  background: "linear-gradient(135deg, rgba(79,70,229,0.92), rgba(124,58,237,0.92))",
                  border: "1px solid rgba(255,255,255,0.3)",
                  borderRadius: "12px",
                  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.35), 0 4px 12px rgba(99,102,241,0.3)",
                }}
              >
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z" />
                  <path strokeLinecap="round" strokeLinejoin="round" d="M15 13a3 3 0 11-6 0 3 3 0 016 0z" />
                </svg>
                カメラ
              </button>
              <button
                onClick={() => fileInputRef.current?.click()}
                disabled={uploading}
                className="btn-glass-sm flex-1 py-2.5 text-xs font-extrabold flex items-center justify-center gap-1 disabled:opacity-40"
                style={{
                  background: "rgba(255,255,255,0.3)",
                  backdropFilter: "blur(12px)",
                  border: "1px solid rgba(255,255,255,0.45)",
                  borderRadius: "12px",
                  color: "#1e293b",
                  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.5)",
                }}
              >
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
                </svg>
                選択
              </button>
            </div>
          </div>
        )}

        {/* Hidden file inputs */}
        <input
          ref={cameraInputRef}
          type="file"
          accept="image/*"
          capture="environment"
          onChange={handleFileChange}
          className="hidden"
        />
        <input
          ref={fileInputRef}
          type="file"
          accept="image/jpeg,image/png,image/webp"
          onChange={handleFileChange}
          className="hidden"
        />
      </div>
    </div>
  );
}
