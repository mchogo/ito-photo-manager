"""Pydantic データモデル

案件・写真データの入出力モデルを定義する。
"""

from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel, Field


# --- リクエストモデル ---

class ProjectCreate(BaseModel):
    """案件作成リクエスト"""
    site_id: str = Field(..., min_length=1, max_length=100, description="現場ID")
    work_date: date = Field(..., description="作業日")
    worker_name: str = Field(..., min_length=1, max_length=100, description="作業員名")
    equipment_ids: List[str] = Field(..., min_length=1, description="選択した機器IDリスト")


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
