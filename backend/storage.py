"""ローカルファイルストレージ

案件データ（JSON）、写真ファイル、書類ファイルをローカルディスクに保存・読み込みする。
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

# データディレクトリ
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PROJECTS_DIR = DATA_DIR / "projects"
PHOTOS_DIR = DATA_DIR / "photos"
DOCUMENTS_DIR = DATA_DIR / "documents"


def _ensure_dirs() -> None:
    """データディレクトリを作成する"""
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)


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
    project_name: str | None = None,
    project_number: str | None = None,
    address: str | None = None,
    status: str = "対応前",
    memo: str | None = None,
    description: str | None = None,
    work_start_time: datetime | None = None,
    work_end_time: datetime | None = None,
    scheduled_date: date | None = None,
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
                    "retake_instruction": None,
                    "retake_requested_at": None,
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
        # Phase 1 拡張フィールド
        "project_name": project_name,
        "project_number": project_number,
        "address": address,
        "status": status,
        "memo": memo,
        "description": description,
        "work_start_time": work_start_time.isoformat() if work_start_time else None,
        "work_end_time": work_end_time.isoformat() if work_end_time else None,
        "scheduled_date": scheduled_date.isoformat() if scheduled_date else None,
        # Phase 2 拡張フィールド
        "survey_notes": None,
        "documents": [],
        # Phase 3 打刻フィールド
        "departure_time": None,
        "arrival_time": None,
        "checkout_time": None,
        # Phase 4 承認フィールド
        "approved_at": None,
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


def list_projects(
    status: str | None = None,
    worker_name: str | None = None,
    scheduled_date: str | None = None,
) -> list[dict]:
    """案件一覧を取得する（フィルタリング対応）"""
    _ensure_dirs()
    projects: list[dict] = []
    for path in PROJECTS_DIR.glob("*.json"):
        try:
            project = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to load project file: %s", path)
            continue

        if status and project.get("status") != status:
            continue
        if worker_name and project.get("worker_name") != worker_name:
            continue
        if scheduled_date and project.get("scheduled_date") != scheduled_date:
            continue

        projects.append(project)

    def sort_key(p: dict) -> tuple:
        sd = p.get("scheduled_date") or "9999-12-31"
        ca = p.get("created_at") or ""
        return (sd, ca)

    projects.sort(key=sort_key)
    return projects


_TIMELOG_FIELDS = {"departure_time", "arrival_time", "checkout_time"}

_EARLY_STATUSES = {
    "対応前", "客連絡待ち", "N連絡待ち", "調整完了", "Pコメ待ち",
    "再架電", "荷電待機中", "仮押さえ", "ファーストコール済み", "未発注", "杉本調整中",
}


def update_project(project_id: str, updates: dict) -> dict | None:
    """案件データを部分更新する"""
    project = get_project(project_id)
    if project is None:
        return None

    for key, value in updates.items():
        # Phase 3: 打刻フィールドはべき等性保証（既存値は上書き不可）
        if key in _TIMELOG_FIELDS and project.get(key) is not None:
            continue
        # Phase 3: 打刻に連動した自動ステータス更新
        if key == "arrival_time" and project.get("arrival_time") is None:
            project["status"] = "対応中"
        if key == "checkout_time" and project.get("checkout_time") is None:
            project["status"] = "図書提出待ち"
        # 値をセット
        if isinstance(value, datetime):
            project[key] = value.isoformat()
        elif isinstance(value, date):
            project[key] = value.isoformat()
        else:
            project[key] = value

    # Phase 4: scheduled_date + worker_name が揃った時に「日程確定済み」へ自動遷移
    if (project.get("scheduled_date") and project.get("worker_name")
            and project.get("status") in _EARLY_STATUSES):
        project["status"] = "日程確定済み"

    _save_project(project)
    logger.info("Project updated: %s, fields: %s", project_id, list(updates.keys()))
    return project


# --- 写真 ---

def save_photo(
    project_id: str,
    equipment_id: str,
    slot_id: str,
    file_bytes: bytes,
    original_filename: str,
) -> dict:
    """写真を保存し、案件データを更新する。再撮影指示は自動クリアする"""
    _ensure_dirs()

    project = get_project(project_id)
    if project is None:
        raise ValueError(f"Project not found: {project_id}")

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

    now = datetime.now()
    timestamp = now.strftime("%Y%m%d_%H%M%S_%f")
    ext = Path(original_filename).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    safe_eq_name = target_eq["name"].replace(" ", "_").replace("/", "_")
    safe_site_id = project["site_id"].replace(" ", "_").replace("/", "_")
    filename = f"{safe_eq_name}_{safe_site_id}_{timestamp}{ext}"

    photo_path = PHOTOS_DIR / filename
    photo_path.write_bytes(file_bytes)

    target_slot["photo_filename"] = filename
    target_slot["uploaded_at"] = now.isoformat()
    # 再撮影指示をクリア（新写真アップロードで解決扱い）
    target_slot["retake_instruction"] = None
    target_slot["retake_requested_at"] = None
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


def set_retake_instruction(
    project_id: str,
    equipment_id: str,
    slot_id: str,
    reason: str | None,
) -> dict | None:
    """写真スロットへ再撮影指示をセット/解除する

    Args:
        reason: 指示理由。None を渡すと解除。

    Returns:
        更新後のプロジェクト dict。スロットが見つからない場合は None。
    """
    project = get_project(project_id)
    if project is None:
        return None

    found = False
    for eq in project["equipment"]:
        if eq["equipment_id"] == equipment_id:
            for slot in eq["slots"]:
                if slot["slot_id"] == slot_id:
                    slot["retake_instruction"] = reason
                    slot["retake_requested_at"] = datetime.now().isoformat() if reason else None
                    found = True
                    break
            break

    if not found:
        return None

    # 指示セット時はステータスを「図書修正待ち」へ
    if reason:
        project["status"] = "図書修正待ち"

    _save_project(project)
    logger.info(
        "Retake instruction %s: project=%s, eq=%s, slot=%s",
        "set" if reason else "cleared",
        project_id, equipment_id, slot_id,
    )
    return project


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


# --- 書類 ---

def save_document(
    project_id: str,
    document_type: str,
    file_bytes: bytes,
    original_filename: str,
) -> dict:
    """書類を保存し、案件データに追記する

    ファイルは data/documents/{project_id}/{uuid}.{ext} に保存する。
    """
    _ensure_dirs()

    project = get_project(project_id)
    if project is None:
        raise ValueError(f"Project not found: {project_id}")

    doc_dir = DOCUMENTS_DIR / project_id
    doc_dir.mkdir(parents=True, exist_ok=True)

    ext = Path(original_filename).suffix.lower()
    if not ext:
        ext = ".bin"
    doc_id = uuid.uuid4().hex[:12]
    stored_filename = f"{doc_id}{ext}"

    doc_path = doc_dir / stored_filename
    doc_path.write_bytes(file_bytes)

    now = datetime.now()
    doc_record = {
        "document_id": doc_id,
        "project_id": project_id,
        "document_type": document_type,
        "original_filename": original_filename,
        "stored_filename": stored_filename,
        "size_bytes": len(file_bytes),
        "uploaded_at": now.isoformat(),
        "resubmit_instruction": None,
        "resubmit_requested_at": None,
    }

    if "documents" not in project:
        project["documents"] = []
    project["documents"].append(doc_record)

    # Phase 4: 完成図書アップロード時 (状態: 図書提出待ち) → 成果物提出待ちへ自動遷移
    _KANSHO_TYPES = {"完成図書_調査", "完成図書_設置"}
    if document_type in _KANSHO_TYPES and project.get("status") == "図書提出待ち":
        project["status"] = "成果物提出待ち"

    _save_project(project)

    logger.info("Document saved: %s / %s", project_id, stored_filename)
    return doc_record


def list_documents(project_id: str, document_type: str | None = None) -> list[dict]:
    """案件の書類一覧を返す"""
    project = get_project(project_id)
    if project is None:
        return []

    docs = project.get("documents", [])
    if document_type:
        docs = [d for d in docs if d.get("document_type") == document_type]
    return docs


def delete_document(project_id: str, document_id: str) -> bool:
    """書類を削除し、案件データから除去する"""
    project = get_project(project_id)
    if project is None:
        return False

    docs = project.get("documents", [])
    target = next((d for d in docs if d["document_id"] == document_id), None)
    if target is None:
        return False

    # ファイル削除
    doc_path = DOCUMENTS_DIR / project_id / target["stored_filename"]
    if doc_path.exists():
        doc_path.unlink()

    project["documents"] = [d for d in docs if d["document_id"] != document_id]
    _save_project(project)
    logger.info("Document deleted: %s / %s", project_id, document_id)
    return True


def get_document_path(project_id: str, stored_filename: str) -> Path | None:
    """書類ファイルのパスを返す"""
    path = DOCUMENTS_DIR / project_id / stored_filename
    if path.exists():
        return path
    return None


def set_resubmit_instruction(
    project_id: str,
    document_id: str,
    reason: str | None,
) -> dict | None:
    """書類への再提出指示をセット/解除する"""
    project = get_project(project_id)
    if project is None:
        return None

    docs = project.get("documents", [])
    target = next((d for d in docs if d["document_id"] == document_id), None)
    if target is None:
        return None

    target["resubmit_instruction"] = reason
    target["resubmit_requested_at"] = datetime.now().isoformat() if reason else None

    if reason:
        project["status"] = "図書修正待ち"

    _save_project(project)
    logger.info(
        "Resubmit instruction %s: project=%s, doc=%s",
        "set" if reason else "cleared",
        project_id, document_id,
    )
    return project


# --- Phase 4: 承認 ---

def approve_project(project_id: str) -> dict | None:
    """案件を承認し、ステータスを「案件終了」に変更する"""
    project = get_project(project_id)
    if project is None:
        return None
    project["status"] = "案件終了"
    project["approved_at"] = datetime.now().isoformat()
    _save_project(project)
    logger.info("Project approved: %s", project_id)
    return project
