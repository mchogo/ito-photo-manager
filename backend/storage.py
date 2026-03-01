"""ローカルファイルストレージ

案件データ（JSON）と写真ファイルをローカルディスクに保存・読み込みする。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

from equipment_master import get_equipment_by_id

logger = logging.getLogger(__name__)

# データディレクトリ（main.py から設定可能）
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PROJECTS_DIR = DATA_DIR / "projects"
PHOTOS_DIR = DATA_DIR / "photos"


def _ensure_dirs() -> None:
    """データディレクトリを作成する"""
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)


def _project_path(project_id: str) -> Path:
    return PROJECTS_DIR / f"{project_id}.json"


def _serialize_date(obj: Any) -> str:
    """JSON シリアライズ用のカスタムハンドラ"""
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} is not JSON serializable")


# --- 案件 CRUD ---

def create_project(
    site_id: str,
    work_date: date,
    worker_name: str,
    equipment_ids: list[str],
) -> dict:
    """案件を新規作成し、選択された機器の撮影スロットを初期化する"""
    _ensure_dirs()

    project_id = uuid.uuid4().hex[:12]

    equipment_data: list[dict] = []
    for eq_id in equipment_ids:
        eq_def = get_equipment_by_id(eq_id)
        if eq_def is None:
            raise ValueError(f"Unknown equipment_id: {eq_id}")
        equipment_data.append({
            "equipment_id": eq_def.equipment_id,
            "name": eq_def.name,
            "slots": [
                {
                    "slot_id": slot.slot_id,
                    "label": slot.label,
                    "photo_filename": None,
                    "uploaded_at": None,
                }
                for slot in eq_def.photo_slots
            ],
        })

    project = {
        "project_id": project_id,
        "site_id": site_id,
        "work_date": work_date.isoformat(),
        "worker_name": worker_name,
        "created_at": datetime.now().isoformat(),
        "equipment": equipment_data,
    }

    _project_path(project_id).write_text(
        json.dumps(project, ensure_ascii=False, indent=2, default=_serialize_date),
        encoding="utf-8",
    )
    logger.info("Project created: %s", project_id)
    return project


def get_project(project_id: str) -> dict | None:
    """案件データを取得する"""
    path = _project_path(project_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _save_project(project: dict) -> None:
    """案件データを保存する"""
    _project_path(project["project_id"]).write_text(
        json.dumps(project, ensure_ascii=False, indent=2, default=_serialize_date),
        encoding="utf-8",
    )


# --- 写真 ---

def save_photo(
    project_id: str,
    equipment_id: str,
    slot_id: str,
    file_bytes: bytes,
    original_filename: str,
) -> dict:
    """写真を保存し、案件データを更新する

    ファイル名規則: {機器名}_{現場ID}_{YYYYMMDD_HHMMSS}.jpg
    """
    _ensure_dirs()

    project = get_project(project_id)
    if project is None:
        raise ValueError(f"Project not found: {project_id}")

    # 対象スロットを探す
    target_eq = None
    target_slot = None
    for eq in project["equipment"]:
        if eq["equipment_id"] == equipment_id:
            target_eq = eq
            for slot in eq["slots"]:
                if slot["slot_id"] == slot_id:
                    target_slot = slot
                    break
            break

    if target_eq is None or target_slot is None:
        raise ValueError(
            f"Slot not found: equipment_id={equipment_id}, slot_id={slot_id}"
        )

    # 既存の写真があれば削除
    if target_slot["photo_filename"]:
        old_path = PHOTOS_DIR / target_slot["photo_filename"]
        if old_path.exists():
            old_path.unlink()
            logger.info("Deleted old photo: %s", old_path.name)

    # ファイル名生成（マイクロ秒まで含めて一意性を確保）
    now = datetime.now()
    timestamp = now.strftime("%Y%m%d_%H%M%S_%f")
    # 拡張子を判定（デフォルト .jpg）
    ext = Path(original_filename).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    safe_eq_name = target_eq["name"].replace(" ", "_").replace("/", "_")
    safe_site_id = project["site_id"].replace(" ", "_").replace("/", "_")
    filename = f"{safe_eq_name}_{safe_site_id}_{timestamp}{ext}"

    # ファイル保存
    photo_path = PHOTOS_DIR / filename
    photo_path.write_bytes(file_bytes)

    # 案件データ更新
    target_slot["photo_filename"] = filename
    target_slot["uploaded_at"] = now.isoformat()
    _save_project(project)

    logger.info("Photo saved: %s", filename)
    return {
        "filename": filename,
        "equipment_id": equipment_id,
        "slot_id": slot_id,
        "uploaded_at": now.isoformat(),
    }


def delete_photo(project_id: str, equipment_id: str, slot_id: str) -> bool:
    """写真を削除し、案件データを更新する"""
    project = get_project(project_id)
    if project is None:
        return False

    for eq in project["equipment"]:
        if eq["equipment_id"] == equipment_id:
            for slot in eq["slots"]:
                if slot["slot_id"] == slot_id and slot["photo_filename"]:
                    photo_path = PHOTOS_DIR / slot["photo_filename"]
                    if photo_path.exists():
                        photo_path.unlink()
                    slot["photo_filename"] = None
                    slot["uploaded_at"] = None
                    _save_project(project)
                    logger.info(
                        "Photo deleted: project=%s, eq=%s, slot=%s",
                        project_id, equipment_id, slot_id,
                    )
                    return True
    return False


def validate_project(project_id: str) -> dict:
    """案件の撮影完了状態を検証する"""
    project = get_project(project_id)
    if project is None:
        raise ValueError(f"Project not found: {project_id}")

    missing: list[dict] = []
    total = 0
    filled = 0

    for eq in project["equipment"]:
        for slot in eq["slots"]:
            total += 1
            if slot["photo_filename"]:
                filled += 1
            else:
                missing.append({
                    "equipment_id": eq["equipment_id"],
                    "equipment_name": eq["name"],
                    "slot_id": slot["slot_id"],
                    "slot_label": slot["label"],
                })

    return {
        "is_complete": len(missing) == 0,
        "missing_slots": missing,
        "total_slots": total,
        "filled_slots": filled,
    }


def get_photo_path(filename: str) -> Path | None:
    """写真ファイルのパスを返す"""
    path = PHOTOS_DIR / filename
    if path.exists():
        return path
    return None
