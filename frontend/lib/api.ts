import type { EquipmentDef, Project, ValidationResult } from "@/types";

const API_BASE = "/api";

async function fetchJSON<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${url}`, init);
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`API Error ${res.status}: ${body}`);
  }
  return res.json();
}

/** 機器マスター一覧を取得 */
export async function getEquipmentList(): Promise<EquipmentDef[]> {
  return fetchJSON<EquipmentDef[]>("/equipment");
}

/** 案件を作成 */
export async function createProject(data: {
  site_id: string;
  work_date: string;
  worker_name: string;
  equipment_ids: string[];
}): Promise<Project> {
  return fetchJSON<Project>("/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

/** 案件データを取得 */
export async function getProject(projectId: string): Promise<Project> {
  return fetchJSON<Project>(`/projects/${projectId}`);
}

/** 写真をアップロード */
export async function uploadPhoto(
  projectId: string,
  equipmentId: string,
  slotId: string,
  file: File,
): Promise<{ filename: string; equipment_id: string; slot_id: string; uploaded_at: string }> {
  const formData = new FormData();
  formData.append("equipment_id", equipmentId);
  formData.append("slot_id", slotId);
  formData.append("file", file);
  return fetchJSON(`/projects/${projectId}/photos`, {
    method: "POST",
    body: formData,
  });
}

/** 写真を削除 */
export async function deletePhoto(
  projectId: string,
  equipmentId: string,
  slotId: string,
): Promise<void> {
  const params = new URLSearchParams({ equipment_id: equipmentId, slot_id: slotId });
  await fetchJSON(`/projects/${projectId}/photos?${params}`, {
    method: "DELETE",
  });
}

/** バリデーション */
export async function validateProject(projectId: string): Promise<ValidationResult> {
  return fetchJSON<ValidationResult>(`/projects/${projectId}/validate`);
}

/** Excel出力ダウンロードURL */
export function getExcelExportUrl(projectId: string): string {
  return `${API_BASE}/projects/${projectId}/export`;
}

/** 写真URLを取得 */
export function getPhotoUrl(filename: string): string {
  return `${API_BASE}/photos/${encodeURIComponent(filename)}`;
}
