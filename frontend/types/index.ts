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

/** 案件ステータス（動的マスター管理対応のため plain string） */
export type ProjectStatus = string;

/** フォールバック用静的ステータス一覧（useMasterConfig が未ロードの間に使用） */
export const PROJECT_STATUSES: string[] = [
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

/** 12色のプリセットパレット（Tailwind クラスを静的定義してパージ回避） */
export const COLOR_PALETTE: Record<string, string> = {
  gray:   "bg-gray-100 text-gray-600",
  red:    "bg-red-100 text-red-700",
  orange: "bg-orange-100 text-orange-700",
  amber:  "bg-amber-100 text-amber-700",
  yellow: "bg-yellow-100 text-yellow-700",
  green:  "bg-green-100 text-green-700",
  teal:   "bg-teal-100 text-teal-700",
  cyan:   "bg-cyan-100 text-cyan-700",
  blue:   "bg-blue-100 text-blue-700",
  indigo: "bg-indigo-100 text-indigo-700",
  violet: "bg-violet-100 text-violet-700",
  rose:   "bg-rose-100 text-rose-700",
};

/** フォールバック用静的ステータスカラーマップ */
export const STATUS_COLORS: Record<string, string> = {
  対応前: COLOR_PALETTE.gray,
  客連絡待ち: COLOR_PALETTE.yellow,
  N連絡待ち: COLOR_PALETTE.orange,
  調整完了: COLOR_PALETTE.green,
  Pコメ待ち: COLOR_PALETTE.blue,
  再架電: COLOR_PALETTE.orange,
  荷電待機中: COLOR_PALETTE.violet,
  仮押さえ: COLOR_PALETTE.cyan,
  ファーストコール済み: COLOR_PALETTE.teal,
  日程確定済み: COLOR_PALETTE.green,
  対応中: COLOR_PALETTE.indigo,
  案件終了: COLOR_PALETTE.gray,
  対応不可: COLOR_PALETTE.red,
  未発注: COLOR_PALETTE.amber,
  キャンセル: COLOR_PALETTE.red,
  杉本調整中: COLOR_PALETTE.violet,
  成果物提出待ち: COLOR_PALETTE.blue,
  図書提出待ち: COLOR_PALETTE.blue,
  図書修正待ち: COLOR_PALETTE.rose,
  統制移行: COLOR_PALETTE.gray,
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

/** 書類カテゴリ（静的フォールバック） */
export const DOCUMENT_CATEGORIES = {
  管理共有: ["依頼シート", "ID通知書", "コンフィグ", "チェックリスト"] as string[],
  現地調査: ["現地調査報告", "完成図書_調査"] as string[],
  設置: ["完成図書_設置"] as string[],
};

/** 書類カテゴリキー → 表示タイトル */
export const DOCUMENT_CATEGORY_TITLES: Record<string, string> = {
  管理共有: "統制からの資料",
  現地調査: "現地調査",
  設置: "設置",
};

/** マスター設定型（API レスポンス） */
export interface MasterConfigStatus {
  value: string;
  color: string;
}

export interface MasterConfigDocType {
  value: string;
  category: "管理共有" | "現地調査" | "設置";
}

export interface MasterConfig {
  statuses: MasterConfigStatus[];
  document_types: MasterConfigDocType[];
}

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
  departure_time_manual?: boolean;
  arrival_time_manual?: boolean;
  checkout_time_manual?: boolean;
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
