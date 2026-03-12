"use client";

import { useEffect, useState, useCallback } from "react";
import Link from "next/link";
import { listProjects } from "@/lib/api";
import type { Project, ProjectStatus } from "@/types";
import { useMasterConfig } from "@/lib/useMasterConfig";

const LS_KEY_WORKER = "pm_workerName";

function formatTime(iso: string | null): string {
  if (!iso) return "";
  // ISO datetime → HH:MM
  const match = iso.match(/T(\d{2}:\d{2})/);
  if (match) return match[1];
  // time-only string HH:MM:SS
  return iso.slice(0, 5);
}

function formatDate(iso: string | null): string {
  if (!iso) return "";
  return iso.replace(/-/g, "/");
}

function PhotoProgress({ project }: { project: Project }) {
  const total = project.equipment.reduce((s, eq) => s + eq.slots.length, 0);
  const filled = project.equipment.reduce(
    (s, eq) => s + eq.slots.filter((sl) => sl.photo_filename).length,
    0,
  );
  if (total === 0) return null;
  return (
    <span className="text-xs text-gray-500 font-medium">
      📷 {filled}/{total}
    </span>
  );
}

export default function WorkerPage() {
  const { config, colorOf } = useMasterConfig();
  const [workerName, setWorkerName] = useState("");
  const [inputValue, setInputValue] = useState("");
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<ProjectStatus | "">("");

  // Restore worker name from localStorage
  useEffect(() => {
    try {
      const saved = localStorage.getItem(LS_KEY_WORKER);
      if (saved) {
        setInputValue(saved);
        setWorkerName(saved);
      }
    } catch {}
  }, []);

  const fetchProjects = useCallback(async (name: string, status: string) => {
    setLoading(true);
    setError(null);
    try {
      const data = await listProjects({
        worker_name: name || undefined,
        status: status ? (status as ProjectStatus) : undefined,
      });
      setProjects(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "読み込みに失敗しました");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchProjects(workerName, statusFilter);
  }, [workerName, statusFilter, fetchProjects]);

  const handleSearch = () => {
    try { localStorage.setItem(LS_KEY_WORKER, inputValue); } catch {}
    setWorkerName(inputValue);
  };

  return (
    <div className="space-y-5 animate-fade-in-up">
      {/* Header */}
      <div className="px-1">
        <h2 className="text-2xl font-extrabold text-gray-800 tracking-tight">作業員ダッシュボード</h2>
        <p className="text-sm text-gray-500/70 mt-1 font-medium">スケジュール・案件一覧</p>
      </div>

      {/* Filters */}
      <div className="liquid-glass p-4 space-y-3">
        <div className="flex gap-2">
          <input
            type="text"
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleSearch()}
            placeholder="作業員名で絞り込み"
            className="input-glass flex-1"
          />
          <button
            onClick={handleSearch}
            className="px-4 py-2 rounded-xl text-sm font-bold text-white"
            style={{
              background: "linear-gradient(135deg, rgba(99,102,241,0.85), rgba(139,92,246,0.85))",
              border: "1px solid rgba(255,255,255,0.3)",
              boxShadow: "inset 0 1px 0 rgba(255,255,255,0.35), 0 4px 12px rgba(99,102,241,0.2)",
            }}
          >
            検索
          </button>
        </div>
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value as ProjectStatus | "")}
          className="input-glass w-full"
        >
          <option value="">すべてのステータス</option>
          {config.statuses.map((s) => (
            <option key={s.value} value={s.value}>{s.value}</option>
          ))}
        </select>
      </div>

      {/* Results */}
      {loading && (
        <div className="flex flex-col items-center justify-center py-16 gap-3">
          <div className="spinner-glass" />
          <p className="text-sm text-gray-500/60 font-medium">読み込み中...</p>
        </div>
      )}

      {error && (
        <div className="liquid-glass-red px-4 py-3 text-red-700 text-sm font-semibold">
          {error}
        </div>
      )}

      {!loading && !error && projects.length === 0 && (
        <div className="liquid-glass px-5 py-10 text-center text-gray-400 text-sm font-medium">
          該当する案件がありません
        </div>
      )}

      {!loading && projects.length > 0 && (
        <div className="space-y-3">
          <p className="text-xs text-gray-400 font-medium px-1">{projects.length} 件</p>
          {projects.map((project) => (
            <Link key={project.project_id} href={`/projects/${project.project_id}`}>
              <div className="liquid-glass p-4 space-y-3 hover:shadow-lg transition-shadow cursor-pointer">
                {/* Top row: date + status badge */}
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-bold text-gray-500">
                      📅 {formatDate(project.scheduled_date || project.work_date)}
                    </span>
                    {project.work_start_time && (
                      <span className="text-xs text-gray-400 font-medium">
                        {formatTime(project.work_start_time)}
                        {project.work_end_time && `〜${formatTime(project.work_end_time)}`}
                      </span>
                    )}
                  </div>
                  <span
                    className={`text-[11px] font-bold px-2.5 py-0.5 rounded-full ${colorOf(project.status)}`}
                  >
                    {project.status}
                  </span>
                </div>

                {/* Project name + number */}
                <div>
                  <p className="font-bold text-gray-800 text-[15px] leading-snug">
                    {project.project_name || project.site_id}
                  </p>
                  {project.project_number && (
                    <p className="text-xs text-gray-500 font-medium mt-0.5">
                      #{project.project_number}
                    </p>
                  )}
                </div>

                {/* Address */}
                {project.address && (
                  <p className="text-xs text-gray-500 font-medium flex items-start gap-1">
                    <span className="mt-px">📍</span>
                    <span>{project.address}</span>
                  </p>
                )}

                {/* Bottom row: worker + photo progress */}
                <div className="flex items-center justify-between pt-1 border-t border-white/30">
                  <span className="text-xs text-gray-500 font-medium">👷 {project.worker_name}</span>
                  <PhotoProgress project={project} />
                </div>
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
