import type {
  AuthUser,
  EquipmentDef,
  MasterConfig,
  MasterConfigDocType,
  MasterConfigStatus,
  Project,
  ProjectDocument,
  ProjectListFilter,
  ProjectUpdateRequest,
  ValidationResult,
} from "@/types";
import { clearToken, getToken, setToken } from "./auth";

const API_BASE = "/api";

async function fetchJSON<T>(url: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    ...(init?.headers as Record<string, string>),
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(`${API_BASE}${url}`, { ...init, headers });
  if (res.status === 401) {
    clearToken();
    // /login 上では無限リロードを防ぐためリダイレクトしない
    if (typeof window !== "undefined" && window.location.pathname !== "/login") {
      window.location.href = "/login";
    }
    throw new Error("Unauthorized");
  }
  if (!res.ok) {
    const text = await res.text();
    let errMessage = text;
    try {
      const data = JSON.parse(text);
      if (typeof data.message === "string") {
        errMessage = data.message;
      } else if (data.detail && typeof data.detail === "object" && data.detail.message) {
        errMessage = data.detail.message;
      } else if (data.detail) {
        errMessage = String(data.detail);
      }
    } catch {}
    throw new Error(errMessage);
  }
  return res.json();
}

/** 機器マスター一覧を取得 */
export async function getEquipmentList(): Promise<EquipmentDef[]> {
  return fetchJSON<EquipmentDef[]>("/equipment");
}

/** 案件一覧を取得（フィルタリング対応） */
export async function listProjects(filters?: ProjectListFilter): Promise<Project[]> {
  const params = new URLSearchParams();
  if (filters?.status) params.set("status", filters.status);
  if (filters?.worker_name) params.set("worker_name", filters.worker_name);
  if (filters?.scheduled_date) params.set("scheduled_date", filters.scheduled_date);
  const qs = params.toString();
  return fetchJSON<Project[]>(`/projects${qs ? `?${qs}` : ""}`);
}

/** 案件を作成 */
export async function createProject(data: {
  site_id: string;
  work_date: string;
  worker_name: string;
  equipment_ids: string[];
  project_name?: string;
  project_number?: string;
  address?: string;
  status?: string;
  memo?: string;
  description?: string;
  work_start_time?: string;
  work_end_time?: string;
  scheduled_date?: string;
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

/** 案件データを部分更新 */
export async function updateProject(
  projectId: string,
  data: ProjectUpdateRequest,
): Promise<Project> {
  return fetchJSON<Project>(`/projects/${projectId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
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

/** 再撮影指示をセット/解除（reason=null で解除） */
export async function setRetakeInstruction(
  projectId: string,
  equipmentId: string,
  slotId: string,
  reason: string | null,
): Promise<Project> {
  return fetchJSON<Project>(
    `/projects/${projectId}/photos/${equipmentId}/${slotId}/retake`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason }),
    },
  );
}

/** 書類一覧を取得 */
export async function listDocuments(
  projectId: string,
  documentType?: string,
): Promise<ProjectDocument[]> {
  const params = new URLSearchParams();
  if (documentType) params.set("document_type", documentType);
  const qs = params.toString();
  return fetchJSON<ProjectDocument[]>(
    `/projects/${projectId}/documents${qs ? `?${qs}` : ""}`,
  );
}

/** 書類をアップロード */
export async function uploadDocument(
  projectId: string,
  documentType: string,
  file: File,
): Promise<ProjectDocument> {
  const formData = new FormData();
  formData.append("document_type", documentType);
  formData.append("file", file);
  return fetchJSON<ProjectDocument>(`/projects/${projectId}/documents`, {
    method: "POST",
    body: formData,
  });
}

/** 書類を削除 */
export async function deleteDocument(
  projectId: string,
  documentId: string,
): Promise<void> {
  await fetchJSON(`/projects/${projectId}/documents/${documentId}`, {
    method: "DELETE",
  });
}

/** 書類ファイルのURLを取得 */
export function getDocumentUrl(projectId: string, storedFilename: string): string {
  return `${API_BASE}/documents/${projectId}/${encodeURIComponent(storedFilename)}`;
}

/** 打刻を管理者権限で強制上書き（HH:MM 形式で指定） */
export async function forceUpdateTimelog(
  projectId: string,
  field: "departure_time" | "arrival_time" | "checkout_time",
  time: string,
): Promise<Project> {
  return fetchJSON<Project>(`/projects/${projectId}/timelog`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ field, time }),
  });
}

/** 書類への再提出指示をセット/解除（reason=null で解除） */
export async function setResubmitInstruction(
  projectId: string,
  documentId: string,
  reason: string | null,
): Promise<Project> {
  return fetchJSON<Project>(
    `/projects/${projectId}/documents/${documentId}/resubmit`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason }),
    },
  );
}

// --- Phase 4: 認証・ユーザー管理 API ---

export interface TokenResponse {
  access_token: string;
  token_type: string;
  role: string;
  display_name: string;
}

/** ログイン → トークンを取得しlocalStorageに保存 */
export async function login(username: string, password: string): Promise<TokenResponse> {
  const res = await fetch(`${API_BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    throw new Error("ユーザー名またはパスワードが違います");
  }
  const data: TokenResponse = await res.json();
  setToken(data.access_token);
  return data;
}

/** 現在のログインユーザー情報を取得 */
export async function getMe(): Promise<AuthUser> {
  return fetchJSON<AuthUser>("/auth/me");
}

/** ユーザー一覧（管理者のみ） */
export async function listUsers(): Promise<AuthUser[]> {
  return fetchJSON<AuthUser[]>("/users");
}

/** ユーザー作成（管理者のみ） */
export async function createUser(data: {
  username: string;
  display_name: string;
  password: string;
  role: string;
}): Promise<AuthUser> {
  return fetchJSON<AuthUser>("/users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

/** ユーザー削除（管理者のみ） */
export async function deleteUser(userId: string): Promise<void> {
  await fetchJSON(`/users/${userId}`, { method: "DELETE" });
}

/** ユーザーCSVインポート（管理者のみ） */
export async function importUsersCSV(file: File): Promise<{ created: number; errors: string[] }> {
  const formData = new FormData();
  formData.append("file", file);
  return fetchJSON("/users/import-csv", { method: "POST", body: formData });
}

/** 案件を承認（管理者のみ） */
export async function approveProject(projectId: string): Promise<Project> {
  return fetchJSON<Project>(`/projects/${projectId}/approve`, { method: "POST" });
}

/** 案件CSVインポート（管理者のみ） */
export async function importProjectsCSV(file: File): Promise<{ created: number; errors: string[] }> {
  const formData = new FormData();
  formData.append("file", file);
  return fetchJSON("/projects/import-csv", { method: "POST", body: formData });
}

/** 案件CSVエクスポート（ダウンロード） */
export async function downloadExportCSV(filters?: ProjectListFilter): Promise<void> {
  const params = new URLSearchParams();
  if (filters?.status) params.set("status", filters.status);
  if (filters?.worker_name) params.set("worker_name", filters.worker_name);
  if (filters?.scheduled_date) params.set("scheduled_date", filters.scheduled_date);
  const qs = params.toString();
  const token = getToken();
  const res = await fetch(`${API_BASE}/projects/export-csv${qs ? `?${qs}` : ""}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) throw new Error("CSV export failed");
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "projects.csv";
  a.click();
  URL.revokeObjectURL(url);
}

// --- マスター設定 ---

/** ステータス・書類種別のマスター設定を取得 */
export async function getMasterConfig(): Promise<MasterConfig> {
  return fetchJSON<MasterConfig>("/master-config");
}

/** ステータス一覧を更新（管理者のみ） */
export async function updateStatuses(statuses: MasterConfigStatus[]): Promise<MasterConfig> {
  return fetchJSON<MasterConfig>("/admin/master-config/statuses", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(statuses),
  });
}

/** 書類種別一覧を更新（管理者のみ） */
export async function updateDocumentTypes(docTypes: MasterConfigDocType[]): Promise<MasterConfig> {
  return fetchJSON<MasterConfig>("/admin/master-config/document-types", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(docTypes),
  });
}
