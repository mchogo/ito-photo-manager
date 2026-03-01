"""Pydantic データモデル

案件・写真データの入出力モデルを定義する。
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


# --- レスポンスモデル ---

class PhotoSlotResponse(BaseModel):
    """撮影スロットの状態"""
    slot_id: str
    label: str
    photo_filename: Optional[str] = None
    uploaded_at: Optional[datetime] = None


class EquipmentStatusResponse(BaseModel):
    """機器ごとの撮影状態"""
    equipment_id: str
    name: str
    slots: List[PhotoSlotResponse]


class ProjectResponse(BaseModel):
    """案件レスポンス"""
    project_id: str
    site_id: str
    work_date: date
    worker_name: str
    created_at: datetime
    equipment: List[EquipmentStatusResponse]
    # 拡張フィールド
    project_name: Optional[str] = None
    project_number: Optional[str] = None
    address: Optional[str] = None
    status: ProjectStatus = ProjectStatus.対応前
    memo: Optional[str] = None
    description: Optional[str] = None
    work_start_time: Optional[datetime] = None
    work_end_time: Optional[datetime] = None
    scheduled_date: Optional[date] = None


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
