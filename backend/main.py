"""FastAPI メインアプリケーション

いとうさんフォトマネージャーのバックエンドAPI。
"""

from __future__ import annotations

import csv
import io
import logging
import mimetypes
from datetime import date
from typing import Annotated, List, Optional, Union
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

import equipment_master
import master_config as mc
import reference_data as ref_data
import storage
import user_storage
from auth import get_current_user, require_admin
from excel_export import generate_excel
from image_utils import resize_image
from models import (
    DocumentResponse,
    DocumentType,
    ErrorResponse,
    ImportResult,
    LoginRequest,
    MasterConfig,
    MasterConfigDocType,
    MasterConfigStatus,
    PhotoUploadResponse,
    ProjectCreate,
    ProjectResponse,
    ProjectUpdate,
    RetakeInstructionUpdate,
    TimelogForceUpdate,
    TokenResponse,
    UserCreate,
    UserRole,
    UserResponse,
    ValidationResult,
)

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

COMMON_ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "不正なリクエスト"},
    401: {"model": ErrorResponse, "description": "認証エラー"},
    403: {"model": ErrorResponse, "description": "権限エラー"},
    404: {"model": ErrorResponse, "description": "リソース未検出"},
    422: {"model": ErrorResponse, "description": "入力バリデーションエラー"},
    500: {"model": ErrorResponse, "description": "サーバー内部エラー"},
}

app = FastAPI(
    title="いとうさんフォトマネージャー API",
    description="現場撮影管理・Excel自動化システム",
    version="4.0.0",
    responses=COMMON_ERROR_RESPONSES,
)


def _to_user_response(user: dict) -> UserResponse:
    """内部ユーザー辞書（hashed_password 含む）を公開レスポンス形式へ変換する。"""
    return UserResponse(
        user_id=user["user_id"],
        username=user["username"],
        display_name=user["display_name"],
        role=UserRole(user["role"]),
        created_at=user["created_at"],
    )


@app.exception_handler(HTTPException)
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(_request: Request, exc: Union[HTTPException, StarletteHTTPException]):
    if isinstance(exc.detail, dict):
        code = exc.detail.get("code")
        message = exc.detail.get("message")
        if isinstance(code, str) and isinstance(message, str):
            return JSONResponse(
                status_code=exc.status_code,
                content=ErrorResponse(code=code, message=message).model_dump(),
                headers=exc.headers,
            )
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            code=f"HTTP_{exc.status_code}",
            message=str(exc.detail),
        ).model_dump(),
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(_request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content=ErrorResponse(
            code="VALIDATION_ERROR",
            message=str(exc.errors()),
        ).model_dump(),
    )


@app.exception_handler(Exception)
async def unexpected_exception_handler(_request: Request, _exc: Exception):
    logger.exception("Unhandled server error")
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(
            code="INTERNAL_SERVER_ERROR",
            message="Internal server error",
        ).model_dump(),
    )


@app.on_event("startup")
def on_startup():
    user_storage.ensure_default_admin()

# CORS設定（Next.jsフロントエンドからのアクセスを許可）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 許可する画像MIMEタイプ
ALLOWED_PHOTO_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_PHOTO_SIZE = 20 * 1024 * 1024  # 20MB

# 許可する書類MIMEタイプ
ALLOWED_DOCUMENT_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "image/jpeg",
    "image/png",
}
MAX_DOCUMENT_SIZE = 50 * 1024 * 1024  # 50MB


# --- 機器マスター ---

@app.get("/api/equipment")
def list_equipment():
    """機器マスター一覧を返す"""
    return equipment_master.get_all_equipment()


# --- 案件 ---

@app.get("/api/projects", response_model=List[ProjectResponse])
def list_projects(
    status: Optional[str] = Query(None, description="ステータスで絞り込み"),
    worker_name: Optional[str] = Query(None, description="作業員名で絞り込み"),
    scheduled_date: Optional[str] = Query(None, description="予定日（YYYY-MM-DD）で絞り込み"),
    _user: Annotated[dict, Depends(get_current_user)] = None,
):
    """案件一覧を返す（フィルタリング対応）"""
    return storage.list_projects(
        status=status,
        worker_name=worker_name,
        scheduled_date=scheduled_date,
    )


@app.post("/api/projects", response_model=ProjectResponse)
def create_project(body: ProjectCreate, _user: Annotated[dict, Depends(get_current_user)]):
    """案件を新規作成する"""
    try:
        project = storage.create_project(
            site_id=body.site_id,
            work_date=body.work_date,
            worker_name=body.worker_name,
            equipment_ids=body.equipment_ids,
            project_name=body.project_name,
            project_number=body.project_number,
            address=body.address,
            status=body.status,
            memo=body.memo,
            description=body.description,
            work_start_time=body.work_start_time,
            work_end_time=body.work_end_time,
            scheduled_date=body.scheduled_date,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"code": "BAD_REQUEST", "message": str(e)})
    return project


@app.get("/api/projects/{project_id}", response_model=ProjectResponse)
def get_project(project_id: str, _user: Annotated[dict, Depends(get_current_user)] = None):
    """案件データを取得する"""
    project = storage.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Project not found"})
    return project


@app.patch("/api/projects/{project_id}", response_model=ProjectResponse)
def update_project(project_id: str, body: ProjectUpdate, _user: Annotated[dict, Depends(get_current_user)]):
    """案件データを部分更新する（ステータス、メモ等）"""
    updates = body.model_dump(exclude_none=True)
    project = storage.update_project(project_id, updates)
    if project is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Project not found"})
    return project


# --- 写真 ---

@app.post("/api/projects/{project_id}/photos", response_model=PhotoUploadResponse)
async def upload_photo(
    project_id: str,
    equipment_id: str = Form(...),
    slot_id: str = Form(...),
    file: UploadFile = File(...),
    _user: Annotated[dict, Depends(get_current_user)] = None,
):
    """写真をアップロードする"""
    if file.content_type not in ALLOWED_PHOTO_MIME_TYPES:
        raise HTTPException(
            status_code=400,
            detail={"code": "UNSUPPORTED_MEDIA_TYPE", "message": f"Unsupported file type: {file.content_type}. "
                   f"Allowed: {', '.join(ALLOWED_PHOTO_MIME_TYPES)}"},
        )

    contents = await file.read()
    if len(contents) > MAX_PHOTO_SIZE:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "PAYLOAD_TOO_LARGE",
                "message": f"File too large. Maximum size: {MAX_PHOTO_SIZE // (1024*1024)}MB",
            },
        )
    if len(contents) == 0:
        raise HTTPException(status_code=400, detail={"code": "BAD_REQUEST", "message": "Empty file"})

    try:
        processed = resize_image(contents)
    except Exception:
        logger.exception("Image processing failed")
        raise HTTPException(status_code=400, detail={"code": "BAD_REQUEST", "message": "Invalid image file"})

    try:
        result = storage.save_photo(
            project_id=project_id,
            equipment_id=equipment_id,
            slot_id=slot_id,
            file_bytes=processed,
            original_filename=file.filename or "photo.jpg",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"code": "BAD_REQUEST", "message": str(e)})

    return result


@app.delete("/api/projects/{project_id}/photos")
def delete_photo(project_id: str, equipment_id: str, slot_id: str, _user: Annotated[dict, Depends(get_current_user)]):
    """写真を削除する"""
    deleted = storage.delete_photo(project_id, equipment_id, slot_id)
    if not deleted:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Photo not found"})
    return {"status": "deleted"}


@app.patch("/api/projects/{project_id}/photos/{equipment_id}/{slot_id}/retake",
           response_model=ProjectResponse)
def set_retake_instruction(
    project_id: str,
    equipment_id: str,
    slot_id: str,
    body: RetakeInstructionUpdate,
    _user: Annotated[dict, Depends(require_admin)],
):
    """写真スロットへ再撮影指示をセット/解除する

    reason が設定された場合は project.status を「図書修正待ち」へ更新する。
    reason=null で指示解除。
    """
    project = storage.set_retake_instruction(
        project_id=project_id,
        equipment_id=equipment_id,
        slot_id=slot_id,
        reason=body.reason,
    )
    if project is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Project or slot not found"})
    return project


# --- 書類 ---

@app.get("/api/projects/{project_id}/documents", response_model=List[DocumentResponse])
def list_documents(
    project_id: str,
    document_type: Optional[str] = Query(None, description="書類種別で絞り込み"),
    _user: Annotated[dict, Depends(get_current_user)] = None,
):
    """案件の書類一覧を返す"""
    project = storage.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Project not found"})
    return storage.list_documents(project_id, document_type=document_type)


@app.post("/api/projects/{project_id}/documents", response_model=DocumentResponse)
async def upload_document(
    project_id: str,
    document_type: str = Form(...),
    file: UploadFile = File(...),
    _user: Annotated[dict, Depends(get_current_user)] = None,
):
    """書類をアップロードする"""
    # document_type バリデーション
    valid_types = {e.value for e in DocumentType}
    if document_type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "BAD_REQUEST",
                "message": f"Invalid document_type: {document_type}. Allowed: {', '.join(valid_types)}",
            },
        )

    # MIMEタイプ検証（拡張子ベースでフォールバック）
    content_type = file.content_type or ""
    if content_type not in ALLOWED_DOCUMENT_MIME_TYPES:
        # 拡張子でも判定
        guessed, _ = mimetypes.guess_type(file.filename or "")
        if guessed not in ALLOWED_DOCUMENT_MIME_TYPES:
            raise HTTPException(
                status_code=400,
                detail={"code": "UNSUPPORTED_MEDIA_TYPE", "message": f"Unsupported file type: {content_type}"},
            )

    contents = await file.read()
    if len(contents) > MAX_DOCUMENT_SIZE:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "PAYLOAD_TOO_LARGE",
                "message": f"File too large. Maximum size: {MAX_DOCUMENT_SIZE // (1024*1024)}MB",
            },
        )
    if len(contents) == 0:
        raise HTTPException(status_code=400, detail={"code": "BAD_REQUEST", "message": "Empty file"})

    try:
        result = storage.save_document(
            project_id=project_id,
            document_type=document_type,
            file_bytes=contents,
            original_filename=file.filename or "document",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"code": "BAD_REQUEST", "message": str(e)})

    return result


@app.delete("/api/projects/{project_id}/documents/{document_id}")
def delete_document(project_id: str, document_id: str, _user: Annotated[dict, Depends(get_current_user)]):
    """書類を削除する"""
    deleted = storage.delete_document(project_id, document_id)
    if not deleted:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Document not found"})
    return {"status": "deleted"}


@app.patch(
    "/api/projects/{project_id}/documents/{document_id}/resubmit",
    response_model=ProjectResponse,
)
def set_resubmit_instruction(
    project_id: str,
    document_id: str,
    body: RetakeInstructionUpdate,
    _user: Annotated[dict, Depends(require_admin)],
):
    """書類への再提出指示をセット/解除する"""
    project = storage.set_resubmit_instruction(
        project_id=project_id,
        document_id=document_id,
        reason=body.reason,
    )
    if project is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Project or document not found"})
    return project


@app.patch("/api/projects/{project_id}/timelog", response_model=ProjectResponse)
def force_update_timelog(
    project_id: str,
    data: TimelogForceUpdate,
    _admin: Annotated[dict, Depends(require_admin)],
):
    """打刻を強制上書き（管理者のみ）。手動修正フラグを記録する"""
    project = storage.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Project not found"})
    work_date = project.get("work_date", "")
    iso_time = f"{work_date}T{data.time}:00"
    updated = storage.force_update_timelog(project_id, data.field, iso_time)
    if updated is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Project not found"})
    return updated


@app.get("/api/documents/{project_id}/{stored_filename}")
def get_document(project_id: str, stored_filename: str, _user: Annotated[dict, Depends(get_current_user)] = None):
    """書類ファイルを返す"""
    doc_path = storage.get_document_path(project_id, stored_filename)
    if doc_path is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Document not found"})
    media_type, _ = mimetypes.guess_type(stored_filename)
    return FileResponse(doc_path, media_type=media_type or "application/octet-stream")


# --- バリデーション ---

@app.get("/api/projects/{project_id}/validate", response_model=ValidationResult)
def validate_project(project_id: str, _user: Annotated[dict, Depends(get_current_user)] = None):
    """案件の撮影完了状態を検証する"""
    try:
        result = storage.validate_project(project_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail={"code": "BAD_REQUEST", "message": str(e)})
    return result


# --- Excel出力 ---

@app.get("/api/projects/{project_id}/export")
def export_excel(project_id: str, _user: Annotated[dict, Depends(get_current_user)] = None):
    """案件のExcel報告書を生成してダウンロードする"""
    project = storage.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Project not found"})

    # 未撮影スロットが残っている場合はサーバー側で拒否
    validation = storage.validate_project(project_id)
    if not validation["is_complete"]:
        missing = validation["total_slots"] - validation["filled_slots"]
        raise HTTPException(
            status_code=400,
            detail={"code": "BAD_REQUEST", "message": f"撮影が未完了です。残り {missing} スロットが未撮影です。"},
        )

    try:
        excel_bytes = generate_excel(project, storage.PHOTOS_DIR)
    except Exception:
        logger.exception("Excel generation failed")
        raise HTTPException(
            status_code=500,
            detail={"code": "INTERNAL_SERVER_ERROR", "message": "Excel generation failed"},
        )

    filename = f"撮影報告書_{project['site_id']}_{project['work_date']}.xlsx"
    encoded_filename = quote(filename)
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
        },
    )


# --- 写真ファイル配信 ---

@app.get("/api/photos/{filename}")
def get_photo(filename: str, _user: Annotated[dict, Depends(get_current_user)] = None):
    """写真ファイルを返す"""
    photo_path = storage.get_photo_path(filename)
    if photo_path is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Photo not found"})
    return FileResponse(photo_path, media_type="image/jpeg")


# --- Phase 4: 認証 ---

@app.post("/api/auth/login", response_model=TokenResponse)
def login(body: LoginRequest):
    """ユーザーログイン → JWT トークンを返す"""
    from auth import create_access_token
    user = user_storage.authenticate_user(body.username, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "Invalid username or password"})
    role = UserRole(user["role"])
    token = create_access_token({"sub": user["user_id"], "role": role.value, "display_name": user["display_name"]})
    return TokenResponse(access_token=token, role=role, display_name=user["display_name"])


@app.get("/api/auth/me", response_model=UserResponse)
def get_me(current_user: Annotated[dict, Depends(get_current_user)]):
    """現在のログインユーザー情報を返す"""
    u = user_storage.get_user_by_username_or_id(current_user["sub"])
    if u is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "User not found"})
    return _to_user_response(u)


# --- Phase 4: ユーザー管理 ---

@app.get("/api/users", response_model=List[UserResponse])
def list_users(_admin: Annotated[dict, Depends(require_admin)]):
    """ユーザー一覧（管理者のみ）"""
    return [_to_user_response(u) for u in user_storage.list_users()]


@app.post("/api/users", response_model=UserResponse)
def create_user(body: UserCreate, _admin: Annotated[dict, Depends(require_admin)]):
    """ユーザー作成（管理者のみ）"""
    if user_storage.get_user_by_username(body.username):
        raise HTTPException(status_code=400, detail={"code": "BAD_REQUEST", "message": "Username already exists"})
    created = user_storage.create_user(
        username=body.username,
        display_name=body.display_name,
        password=body.password,
        role=body.role.value,
    )
    return _to_user_response(created)


@app.delete("/api/users/{user_id}")
def delete_user(user_id: str, admin: Annotated[dict, Depends(require_admin)]):
    """ユーザー削除（管理者のみ、自分自身は不可）"""
    if admin["sub"] == user_id:
        raise HTTPException(status_code=400, detail={"code": "BAD_REQUEST", "message": "Cannot delete yourself"})
    deleted = user_storage.delete_user(user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "User not found"})
    return {"status": "deleted"}


@app.post("/api/users/import-csv", response_model=ImportResult)
async def import_users_csv(file: UploadFile = File(...), _admin: Annotated[dict, Depends(require_admin)] = None):
    """ユーザーCSVインポート（管理者のみ）
    列: username, display_name, role, password
    """
    contents = await file.read()
    reader = csv.DictReader(io.StringIO(contents.decode("utf-8-sig")))
    created = 0
    errors: list[str] = []
    for i, row in enumerate(reader, start=2):
        username = (row.get("username") or "").strip()
        display_name = (row.get("display_name") or "").strip()
        role = (row.get("role") or "worker").strip()
        password = (row.get("password") or "").strip()
        if not username or not display_name or not password:
            errors.append(f"行 {i}: username/display_name/password は必須です")
            continue
        if role not in ("admin", "worker"):
            errors.append(f"行 {i}: role は admin または worker のみ有効です")
            continue
        if user_storage.get_user_by_username(username):
            errors.append(f"行 {i}: username '{username}' は既に存在します")
            continue
        user_storage.create_user(username=username, display_name=display_name, password=password, role=role)
        created += 1
    return ImportResult(created=created, errors=errors)


# --- Phase 4: 案件承認・CSV ---

@app.post("/api/projects/{project_id}/approve", response_model=ProjectResponse)
def approve_project(project_id: str, _admin: Annotated[dict, Depends(require_admin)]):
    """案件を承認し「案件終了」にする（管理者のみ）"""
    project = storage.approve_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Project not found"})
    return project


@app.post("/api/projects/import-csv", response_model=ImportResult)
async def import_projects_csv(file: UploadFile = File(...), _admin: Annotated[dict, Depends(require_admin)] = None):
    """案件CSVインポート（管理者のみ）
    列: project_name, project_number, site_id, worker_name, address,
        scheduled_date (YYYY-MM-DD), work_date (YYYY-MM-DD),
        work_start_time (HH:MM), work_end_time (HH:MM),
        equipment_ids (;区切り), memo, status
    """
    contents = await file.read()
    reader = csv.DictReader(io.StringIO(contents.decode("utf-8-sig")))
    created = 0
    errors: list[str] = []
    for i, row in enumerate(reader, start=2):
        try:
            site_id = (row.get("site_id") or "").strip()
            worker_name = (row.get("worker_name") or "").strip()
            work_date_str = (row.get("work_date") or "").strip()
            if not site_id or not worker_name or not work_date_str:
                errors.append(f"行 {i}: site_id/worker_name/work_date は必須です")
                continue
            work_date = date.fromisoformat(work_date_str)
            equipment_ids_raw = (row.get("equipment_ids") or "").strip()
            equipment_ids = [e.strip() for e in equipment_ids_raw.split(";") if e.strip()] if equipment_ids_raw else []
            if not equipment_ids:
                errors.append(f"行 {i}: equipment_ids は必須です（;区切り）")
                continue

            sched_raw = (row.get("scheduled_date") or "").strip()
            scheduled_date = date.fromisoformat(sched_raw) if sched_raw else None

            from datetime import datetime as dt
            wst_raw = (row.get("work_start_time") or "").strip()
            wet_raw = (row.get("work_end_time") or "").strip()
            work_start_time = dt.fromisoformat(f"{work_date_str}T{wst_raw}") if wst_raw else None
            work_end_time = dt.fromisoformat(f"{work_date_str}T{wet_raw}") if wet_raw else None

            status = (row.get("status") or "対応前").strip() or "対応前"

            storage.create_project(
                site_id=site_id,
                work_date=work_date,
                worker_name=worker_name,
                equipment_ids=equipment_ids,
                project_name=(row.get("project_name") or "").strip() or None,
                project_number=(row.get("project_number") or "").strip() or None,
                address=(row.get("address") or "").strip() or None,
                status=status,
                memo=(row.get("memo") or "").strip() or None,
                scheduled_date=scheduled_date,
                work_start_time=work_start_time,
                work_end_time=work_end_time,
            )
            created += 1
        except Exception as exc:
            errors.append(f"行 {i}: {exc}")
    return ImportResult(created=created, errors=errors)


@app.get("/api/projects/export-csv")
def export_projects_csv(
    status: Optional[str] = Query(None),
    worker_name: Optional[str] = Query(None),
    scheduled_date: Optional[str] = Query(None),
    _admin: Annotated[dict, Depends(require_admin)] = None,
):
    """案件一覧をCSVエクスポート（管理者のみ）"""
    projects = storage.list_projects(status=status, worker_name=worker_name, scheduled_date=scheduled_date)
    output = io.StringIO()
    fieldnames = [
        "project_id", "project_name", "project_number", "site_id", "worker_name",
        "address", "status", "scheduled_date", "work_date", "work_start_time",
        "work_end_time", "departure_time", "arrival_time", "checkout_time",
        "approved_at", "created_at", "memo", "equipment_count", "filled_slots", "total_slots",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for p in projects:
        total = sum(len(eq.get("slots", [])) for eq in p.get("equipment", []))
        filled = sum(
            1 for eq in p.get("equipment", [])
            for sl in eq.get("slots", [])
            if sl.get("photo_filename")
        )
        writer.writerow({
            **{k: p.get(k, "") for k in fieldnames},
            "equipment_count": len(p.get("equipment", [])),
            "filled_slots": filled,
            "total_slots": total,
        })
    csv_bytes = output.getvalue().encode("utf-8-sig")
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''projects.csv"},
    )


# --- マスター設定 ---

@app.get("/api/master-config", response_model=MasterConfig)
def get_master_config(_user: Annotated[dict, Depends(get_current_user)]):
    """ステータス・書類種別のマスター設定を取得する（認証済みユーザー）"""
    return mc.load_config()


@app.put("/api/admin/master-config/statuses", response_model=MasterConfig)
def update_statuses(
    statuses: list[MasterConfigStatus],
    _admin: Annotated[dict, Depends(require_admin)],
):
    """ステータス一覧を更新する（管理者のみ）"""
    config = mc.load_config()
    config["statuses"] = [s.model_dump() for s in statuses]
    mc.save_config(config)
    return config


@app.put("/api/admin/master-config/document-types", response_model=MasterConfig)
def update_document_types(
    document_types: list[MasterConfigDocType],
    _admin: Annotated[dict, Depends(require_admin)],
):
    """書類種別一覧を更新する（管理者のみ）"""
    config = mc.load_config()
    config["document_types"] = [d.model_dump() for d in document_types]
    mc.save_config(config)
    return config


# --- 参照データ（拠点マスタ / 依頼シートテンプレ） ---

@app.get("/api/reference/site-master")
def get_site_master(_user: Annotated[dict, Depends(get_current_user)]):
    """拠点マスタ（NCR）を取得する"""
    return ref_data.load_site_master()


@app.get("/api/reference/request-sheet-template")
def get_request_sheet_template(_user: Annotated[dict, Depends(get_current_user)]):
    """依頼シートテンプレートを取得する"""
    return ref_data.load_request_sheet_template()
