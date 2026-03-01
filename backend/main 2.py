"""FastAPI メインアプリケーション

いとうさんフォトマネージャーのバックエンドAPI。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

import equipment_master
import storage
from excel_export import generate_excel
from image_utils import resize_image
from models import (
    PhotoUploadResponse,
    ProjectCreate,
    ProjectResponse,
    ValidationResult,
)

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="いとうさんフォトマネージャー API",
    description="現場撮影管理・Excel自動化システム",
    version="1.0.0",
)

# CORS設定（Next.jsフロントエンドからのアクセスを許可）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 許可する画像MIMEタイプ
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB


# --- 機器マスター ---

@app.get("/api/equipment")
def list_equipment():
    """機器マスター一覧を返す"""
    return equipment_master.get_all_equipment()


# --- 案件 ---

@app.post("/api/projects", response_model=ProjectResponse)
def create_project(body: ProjectCreate):
    """案件を新規作成する"""
    try:
        project = storage.create_project(
            site_id=body.site_id,
            work_date=body.work_date,
            worker_name=body.worker_name,
            equipment_ids=body.equipment_ids,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return project


@app.get("/api/projects/{project_id}", response_model=ProjectResponse)
def get_project(project_id: str):
    """案件データを取得する"""
    project = storage.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


# --- 写真 ---

@app.post("/api/projects/{project_id}/photos", response_model=PhotoUploadResponse)
async def upload_photo(
    project_id: str,
    equipment_id: str = Form(...),
    slot_id: str = Form(...),
    file: UploadFile = File(...),
):
    """写真をアップロードする"""
    # MIMEタイプ検証
    if file.content_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {file.content_type}. "
                   f"Allowed: {', '.join(ALLOWED_MIME_TYPES)}",
        )

    # ファイルサイズ検証
    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"File too large. Maximum size: {MAX_FILE_SIZE // (1024*1024)}MB",
        )

    if len(contents) == 0:
        raise HTTPException(status_code=400, detail="Empty file")

    # 画像リサイズ・圧縮
    try:
        processed = resize_image(contents)
    except Exception:
        logger.exception("Image processing failed")
        raise HTTPException(status_code=400, detail="Invalid image file")

    # 保存
    try:
        result = storage.save_photo(
            project_id=project_id,
            equipment_id=equipment_id,
            slot_id=slot_id,
            file_bytes=processed,
            original_filename=file.filename or "photo.jpg",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return result


@app.delete("/api/projects/{project_id}/photos")
def delete_photo(project_id: str, equipment_id: str, slot_id: str):
    """写真を削除する"""
    deleted = storage.delete_photo(project_id, equipment_id, slot_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Photo not found")
    return {"status": "deleted"}


# --- バリデーション ---

@app.get("/api/projects/{project_id}/validate", response_model=ValidationResult)
def validate_project(project_id: str):
    """案件の撮影完了状態を検証する"""
    try:
        result = storage.validate_project(project_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return result


# --- Excel出力 ---

@app.get("/api/projects/{project_id}/export")
def export_excel(project_id: str):
    """案件のExcel報告書を生成してダウンロードする"""
    project = storage.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        excel_bytes = generate_excel(project, storage.PHOTOS_DIR)
    except Exception:
        logger.exception("Excel generation failed")
        raise HTTPException(status_code=500, detail="Excel generation failed")

    filename = f"撮影報告書_{project['site_id']}_{project['work_date']}.xlsx"
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


# --- 写真ファイル配信 ---

@app.get("/api/photos/{filename}")
def get_photo(filename: str):
    """写真ファイルを返す"""
    photo_path = storage.get_photo_path(filename)
    if photo_path is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    return FileResponse(photo_path, media_type="image/jpeg")
