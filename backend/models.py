"""Pydantic データモデル

案件・写真・書類データの入出力モデルを定義する。
"""

from datetime import date, datetime
from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, ConfigDict


# --- 共通エラーレスポンス ---

class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: str = Field(..., description="エラーコード")
    message: str = Field(..., description="ユーザ表示メッセージ")


# --- ステータス（動的マスター管理対応のため plain str に変更） ---

# ProjectStatus は単純な文字列エイリアス。Pydantic の列挙バリデーションを廃止し、
# master_config.json の内容を正として扱う。
ProjectStatus = str


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
    model_config = ConfigDict(extra="forbid")
    site_id: str = Field(..., min_length=1, max_length=100, description="現場ID")
    work_date: date = Field(..., description="作業日")
    worker_name: str = Field(..., min_length=1, max_length=100, description="作業員名")
    equipment_ids: List[str] = Field(..., min_length=1, description="選択した機器IDリスト")
    # 拡張フィールド（任意）
    project_name: Optional[str] = Field(None, max_length=200, description="案件名")
    project_number: Optional[str] = Field(None, max_length=100, description="案件番号")
    address: Optional[str] = Field(None, max_length=500, description="住所")
    status: ProjectStatus = Field("対応前", description="ステータス")
    memo: Optional[str] = Field(None, description="調整メモ")
    description: Optional[str] = Field(None, description="案件内容")
    work_start_time: Optional[datetime] = Field(None, description="作業開始時間")
    work_end_time: Optional[datetime] = Field(None, description="作業終了時間")
    scheduled_date: Optional[date] = Field(None, description="予定日")


class ProjectUpdate(BaseModel):
    """案件更新リクエスト（部分更新）"""
    model_config = ConfigDict(extra="forbid")
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
    model_config = ConfigDict(extra="forbid")
    reason: Optional[str] = Field(None, description="再撮影理由。Null で指示解除")


class TimelogForceUpdate(BaseModel):
    """管理者向け打刻強制更新リクエスト"""
    model_config = ConfigDict(extra="forbid")
    field: Literal["departure_time", "arrival_time", "checkout_time"]
    time: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="HH:MM 形式の時刻")


# --- レスポンスモデル ---

class PhotoSlotResponse(BaseModel):
    """撮影スロットの状態"""
    model_config = ConfigDict(extra="forbid")
    slot_id: str
    label: str
    photo_filename: Optional[str] = None
    uploaded_at: Optional[datetime] = None
    retake_instruction: Optional[str] = None
    retake_requested_at: Optional[datetime] = None


class EquipmentStatusResponse(BaseModel):
    """機器ごとの撮影状態"""
    model_config = ConfigDict(extra="forbid")
    equipment_id: str
    name: str
    slots: List[PhotoSlotResponse]


class DocumentResponse(BaseModel):
    """書類レスポンス"""
    model_config = ConfigDict(extra="forbid")
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
    model_config = ConfigDict(extra="forbid")
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
    status: ProjectStatus = "対応前"
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
    departure_time_manual: bool = False
    arrival_time_manual: bool = False
    checkout_time_manual: bool = False
    # Phase 4 承認フィールド
    approved_at: Optional[datetime] = None


class ValidationResult(BaseModel):
    """バリデーション結果"""
    model_config = ConfigDict(extra="forbid", strict=True)
    is_complete: bool
    missing_slots: List[dict]  # [{"equipment_name": ..., "slot_label": ...}]
    total_slots: int
    filled_slots: int


class PhotoUploadResponse(BaseModel):
    """写真アップロードレスポンス"""
    model_config = ConfigDict(extra="forbid")
    filename: str
    equipment_id: str
    slot_id: str
    uploaded_at: datetime


# --- Phase 4 認証・ユーザー管理モデル ---

class UserRole(str, Enum):
    admin = "admin"
    worker = "worker"


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str
    password: str


class TokenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    access_token: str
    token_type: str = "bearer"
    role: UserRole
    display_name: str


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(..., min_length=1, max_length=50)
    display_name: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=6)
    role: UserRole = UserRole.worker


class UserResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    username: str
    display_name: str
    role: UserRole
    created_at: datetime


class ImportResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    created: int
    errors: List[str]


# --- マスター設定モデル ---

class MasterConfigStatus(BaseModel):
    """ステータス定義"""
    model_config = ConfigDict(extra="forbid", strict=True)
    value: str = Field(..., min_length=1, max_length=50)
    color: str = Field("gray", description="パレットキー (gray/red/blue...)")


class MasterConfigDocType(BaseModel):
    """書類種別定義"""
    model_config = ConfigDict(extra="forbid", strict=True)
    value: str = Field(..., min_length=1, max_length=100)
    category: Literal["管理共有", "現地調査", "設置"]


class MasterConfig(BaseModel):
    """マスター設定全体"""
    model_config = ConfigDict(extra="forbid", strict=True)
    statuses: List[MasterConfigStatus]
    document_types: List[MasterConfigDocType]
