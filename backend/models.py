"""Pydantic データモデル

案件・写真・書類データの入出力モデルを定義する。
"""

from datetime import date, datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


# --- ステータス Enum ---

class ProjectStatus(str, Enum):
    対応前 = "対応前"
    客連絡待ち = "客連絡待ち"
    N連絡待ち = "N連絡待ち"
    調整完了 = "調整完了"
    Pコメ待ち = "Pコメ待ち"
    再架電 = "再架電"
    荷電待機中 = "荷電待機中"
    仮押さえ = "仮押さえ"
    ファーストコール済み = "ファーストコール済み"
    日程確定済み = "日程確定済み"
    対応中 = "対応中"
    案件終了 = "案件終了"
    対応不可 = "対応不可"
    未発注 = "未発注"
    キャンセル = "キャンセル"
    杉本調整中 = "杉本調整中"
    成果物提出待ち = "成果物提出待ち"
    図書提出待ち = "図書提出待ち"
    図書修正待ち = "図書修正待ち"
    統制移行 = "統制移行"


# --- 書類 Enum ---

class DocumentType(str, Enum):
    依頼シート = "依頼シート"
    ID通知書 = "ID通知書"
    コンフィグ = "コンフィグ"
    チェックリスト = "チェックリスト"
    現地調査報告 = "現地調査報告"
    完成図書_調査 = "完成図書_調査"
    完成図書_設置 = "完成図書_設置"
    その他 = "その他"


# --- リクエストモデル ---

class ProjectCreate(BaseModel):
    """案件作成リクエスト"""
    site_id: str = Field(..., min_length=1, max_length=100, description="現場ID")
    work_date: date = Field(..., description="作業日")
    worker_name: str = Field(..., min_length=1, max_length=100, description="作業員名")
    equipment_ids: List[str] = Field(..., min_length=1, description="選択した機器IDリスト")
    # 拡張フィールド（任意）
    project_name: Optional[str] = Field(None, max_length=200, description="案件名")
    project_number: Optional[str] = Field(None, max_length=100, description="案件番号")
    address: Optional[str] = Field(None, max_length=500, description="住所")
    status: ProjectStatus = Field(ProjectStatus.対応前, description="ステータス")
    memo: Optional[str] = Field(None, description="調整メモ")
    description: Optional[str] = Field(None, description="案件内容")
    work_start_time: Optional[datetime] = Field(None, description="作業開始時間")
    work_end_time: Optional[datetime] = Field(None, description="作業終了時間")
    scheduled_date: Optional[date] = Field(None, description="予定日")


class ProjectUpdate(BaseModel):
    """案件更新リクエスト（部分更新）"""
    project_name: Optional[str] = Field(None, max_length=200)
    project_number: Optional[str] = Field(None, max_length=100)
    address: Optional[str] = Field(None, max_length=500)
    status: Optional[ProjectStatus] = None
    memo: Optional[str] = None
    description: Optional[str] = None
    work_start_time: Optional[datetime] = None
    work_end_time: Optional[datetime] = None
    scheduled_date: Optional[date] = None
    worker_name: Optional[str] = Field(None, max_length=100)
    survey_notes: Optional[str] = None
    # Phase 3 打刻フィールド
    departure_time: Optional[datetime] = None
    arrival_time: Optional[datetime] = None
    checkout_time: Optional[datetime] = None


class RetakeInstructionUpdate(BaseModel):
    """再撮影指示の更新リクエスト（reason=None で解除）"""
    reason: Optional[str] = Field(None, description="再撮影理由。Null で指示解除")


# --- レスポンスモデル ---

class PhotoSlotResponse(BaseModel):
    """撮影スロットの状態"""
    slot_id: str
    label: str
    photo_filename: Optional[str] = None
    uploaded_at: Optional[datetime] = None
    retake_instruction: Optional[str] = None
    retake_requested_at: Optional[datetime] = None


class EquipmentStatusResponse(BaseModel):
    """機器ごとの撮影状態"""
    equipment_id: str
    name: str
    slots: List[PhotoSlotResponse]


class DocumentResponse(BaseModel):
    """書類レスポンス"""
    document_id: str
    project_id: str
    document_type: DocumentType
    original_filename: str
    stored_filename: str
    size_bytes: int
    uploaded_at: datetime
    resubmit_instruction: Optional[str] = None
    resubmit_requested_at: Optional[datetime] = None


class ProjectResponse(BaseModel):
    """案件レスポンス"""
    project_id: str
    site_id: str
    work_date: date
    worker_name: str
    created_at: datetime
    equipment: List[EquipmentStatusResponse]
    # Phase 1 拡張フィールド
    project_name: Optional[str] = None
    project_number: Optional[str] = None
    address: Optional[str] = None
    status: ProjectStatus = ProjectStatus.対応前
    memo: Optional[str] = None
    description: Optional[str] = None
    work_start_time: Optional[datetime] = None
    work_end_time: Optional[datetime] = None
    scheduled_date: Optional[date] = None
    # Phase 2 拡張フィールド
    survey_notes: Optional[str] = None
    documents: List[DocumentResponse] = []
    # Phase 3 打刻フィールド
    departure_time: Optional[datetime] = None
    arrival_time: Optional[datetime] = None
    checkout_time: Optional[datetime] = None
    # Phase 4 承認フィールド
    approved_at: Optional[datetime] = None


class ValidationResult(BaseModel):
    """バリデーション結果"""
    is_complete: bool
    missing_slots: List[dict]  # [{"equipment_name": ..., "slot_label": ...}]
    total_slots: int
    filled_slots: int


class PhotoUploadResponse(BaseModel):
    """写真アップロードレスポンス"""
    filename: str
    equipment_id: str
    slot_id: str
    uploaded_at: datetime


# --- Phase 4 認証・ユーザー管理モデル ---

class UserRole(str, Enum):
    admin = "admin"
    worker = "worker"


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: UserRole
    display_name: str


class UserCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    display_name: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=6)
    role: UserRole = UserRole.worker


class UserResponse(BaseModel):
    user_id: str
    username: str
    display_name: str
    role: UserRole
    created_at: datetime


class ImportResult(BaseModel):
    created: int
    errors: List[str]
