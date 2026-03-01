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
}

/** 機器ごとの撮影状態 */
export interface EquipmentStatus {
  equipment_id: string;
  name: string;
  slots: PhotoSlotStatus[];
}

/** 案件データ */
export interface Project {
  project_id: string;
  site_id: string;
  work_date: string;
  worker_name: string;
  created_at: string;
  equipment: EquipmentStatus[];
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
