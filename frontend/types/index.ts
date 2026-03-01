/** 撮影スロット定義 */
export interface PhotoSlotDef {
  slot_id: string;
  label: string;
}

/** 機器定義 */
export interface EquipmentDef {
  equipment_id: string;
  name: string;
  photo_slots: PhotoSlotDef[];
}

/** 撮影スロットの状態 */
export interface PhotoSlotStatus {
  slot_id: string;
  label: string;
  photo_filename: string | null;
  uploaded_at: string | null;
  retake_instruction: string | null;
  retake_requested_at: string | null;
}

/** 機器ごとの撮影状態 */
export interface EquipmentStatus {
  equipment_id: string;
  name: string;
  slots: PhotoSlotStatus[];
}

/** 案件ステータス */
export type ProjectStatus =
  | "対応前"
  | "客連絡待ち"
  | "N連絡待ち"
  | "調整完了"
  | "Pコメ待ち"
  | "再架電"
  | "荷電待機中"
  | "仮押さえ"
  | "ファーストコール済み"
  | "日程確定済み"
  | "対応中"
  | "案件終了"
  | "対応不可"
  | "未発注"
  | "キャンセル"
  | "杉本調整中"
  | "成果物提出待ち"
  | "図書提出待ち"
  | "図書修正待ち"
  | "統制移行";

export const PROJECT_STATUSES: ProjectStatus[] = [
  "対応前",
  "客連絡待ち",
  "N連絡待ち",
  "調整完了",
  "Pコメ待ち",
  "再架電",
  "荷電待機中",
  "仮押さえ",
  "ファーストコール済み",
  "日程確定済み",
  "対応中",
  "案件終了",
  "対応不可",
  "未発注",
  "キャンセル",
  "杉本調整中",
  "成果物提出待ち",
  "図書提出待ち",
  "図書修正待ち",
  "統制移行",
];

/** ステータスに対応するバッジカラー */
export const STATUS_COLORS: Record<ProjectStatus, string> = {
  対応前: "bg-gray-100 text-gray-600",
  客連絡待ち: "bg-yellow-100 text-yellow-700",
  N連絡待ち: "bg-orange-100 text-orange-700",
  調整完了: "bg-green-100 text-green-700",
  Pコメ待ち: "bg-blue-100 text-blue-700",
  再架電: "bg-orange-100 text-orange-700",
  荷電待機中: "bg-purple-100 text-purple-700",
  仮押さえ: "bg-cyan-100 text-cyan-700",
  ファーストコール済み: "bg-teal-100 text-teal-700",
  日程確定済み: "bg-emerald-100 text-emerald-700",
  対応中: "bg-indigo-100 text-indigo-700",
  案件終了: "bg-gray-200 text-gray-500",
  対応不可: "bg-red-100 text-red-700",
  未発注: "bg-amber-100 text-amber-700",
  キャンセル: "bg-red-200 text-red-800",
  杉本調整中: "bg-violet-100 text-violet-700",
  成果物提出待ち: "bg-sky-100 text-sky-700",
  図書提出待ち: "bg-blue-100 text-blue-700",
  図書修正待ち: "bg-rose-100 text-rose-700",
  統制移行: "bg-slate-100 text-slate-700",
};

/** 書類種別 */
export type DocumentType =
  | "依頼シート"
  | "ID通知書"
  | "コンフィグ"
  | "チェックリスト"
  | "現地調査報告"
  | "完成図書_調査"
  | "完成図書_設置"
  | "その他";

export const DOCUMENT_TYPES: DocumentType[] = [
  "依頼シート",
  "ID通知書",
  "コンフィグ",
  "チェックリスト",
  "現地調査報告",
  "完成図書_調査",
  "完成図書_設置",
  "その他",
];

/** 書類カテゴリ */
export const DOCUMENT_CATEGORIES = {
  管理共有: ["依頼シート", "ID通知書", "コンフィグ", "チェックリスト"] as DocumentType[],
  現地調査: ["現地調査報告", "完成図書_調査"] as DocumentType[],
  設置: ["完成図書_設置"] as DocumentType[],
};

/** 書類データ */
export interface ProjectDocument {
  document_id: string;
  project_id: string;
  document_type: DocumentType;
  original_filename: string;
  stored_filename: string;
  size_bytes: number;
  uploaded_at: string;
  resubmit_instruction: string | null;
  resubmit_requested_at: string | null;
}

/** Phase 4 認証 */
export type UserRole = "admin" | "worker";

export interface AuthUser {
  user_id: string;
  username: string;
  display_name: string;
  role: UserRole;
  created_at?: string;
}

/** 案件データ */
export interface Project {
  project_id: string;
  site_id: string;
  work_date: string;
  worker_name: string;
  created_at: string;
  equipment: EquipmentStatus[];
  // 拡張フィールド
  project_name: string | null;
  project_number: string | null;
  address: string | null;
  status: ProjectStatus;
  memo: string | null;
  description: string | null;
  work_start_time: string | null;
  work_end_time: string | null;
  scheduled_date: string | null;
  // Phase 2 拡張
  survey_notes: string | null;
  documents: ProjectDocument[];
  // Phase 3 打刻
  departure_time: string | null;
  arrival_time: string | null;
  checkout_time: string | null;
  // Phase 4 承認
  approved_at: string | null;
}

/** 案件更新リクエスト */
export interface ProjectUpdateRequest {
  project_name?: string | null;
  project_number?: string | null;
  address?: string | null;
  status?: ProjectStatus;
  memo?: string | null;
  description?: string | null;
  work_start_time?: string | null;
  work_end_time?: string | null;
  scheduled_date?: string | null;
  worker_name?: string;
  survey_notes?: string | null;
  departure_time?: string | null;
  arrival_time?: string | null;
  checkout_time?: string | null;
}

/** 案件一覧フィルター */
export interface ProjectListFilter {
  status?: ProjectStatus;
  worker_name?: string;
  scheduled_date?: string;
}

/** バリデーション結果 */
export interface ValidationResult {
  is_complete: boolean;
  missing_slots: {
    equipment_id: string;
    equipment_name: string;
    slot_id: string;
    slot_label: string;
  }[];
  total_slots: number;
  filled_slots: number;
}
