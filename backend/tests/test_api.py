"""FastAPI 統合テスト"""

import io
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import storage
import reference_data as ref_data
from auth import get_current_user, require_admin
from main import app


@pytest.fixture(autouse=True)
def temp_data_dir(tmp_path):
    """テスト用の一時データディレクトリ"""
    projects_dir = tmp_path / "projects"
    photos_dir = tmp_path / "photos"
    site_master_path = tmp_path / "site_master_ncr.json"
    request_template_path = tmp_path / "request_sheet_template.json"
    projects_dir.mkdir()
    photos_dir.mkdir()
    site_master_path.write_text(
        '{"source_sheet":"拠点マスタ（NCR）","record_count":1,"records":[{"営業所":"テスト営業所"}]}',
        encoding="utf-8",
    )
    request_template_path.write_text(
        '{"source_sheet":"依頼シート","template":{"title":"テストテンプレ","sections":[]}}',
        encoding="utf-8",
    )
    with mock.patch.object(storage, "PROJECTS_DIR", projects_dir), \
         mock.patch.object(storage, "PHOTOS_DIR", photos_dir), \
         mock.patch.object(ref_data, "SITE_MASTER_PATH", site_master_path), \
         mock.patch.object(ref_data, "REQUEST_TEMPLATE_PATH", request_template_path):
        yield tmp_path


@pytest.fixture
def client():
    """認証済みテストクライアント（依存関係をモックで上書き）"""
    def mock_user():
        return {"sub": "test-admin", "role": "admin", "display_name": "テスト管理者"}

    app.dependency_overrides[get_current_user] = mock_user
    app.dependency_overrides[require_admin] = mock_user
    tc = TestClient(app)
    yield tc
    app.dependency_overrides.clear()


def _make_jpeg() -> bytes:
    img = Image.new("RGB", (200, 150), color="red")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


class TestEquipmentAPI:
    def test_list_equipment(self, client):
        res = client.get("/api/equipment")
        assert res.status_code == 200
        data = res.json()
        assert len(data) == 5
        assert data[0]["equipment_id"] == "pos_register"


class TestReferenceDataAPI:
    def test_get_site_master(self, client):
        res = client.get("/api/reference/site-master")
        assert res.status_code == 200
        data = res.json()
        assert data["source_sheet"] == "拠点マスタ（NCR）"
        assert data["record_count"] == 1

    def test_get_request_sheet_template(self, client):
        res = client.get("/api/reference/request-sheet-template")
        assert res.status_code == 200
        data = res.json()
        assert data["source_sheet"] == "依頼シート"
        assert data["template"]["title"] == "テストテンプレ"


class TestProjectAPI:
    def test_create_project(self, client):
        res = client.post("/api/projects", json={
            "site_id": "SITE-001",
            "work_date": "2025-06-01",
            "worker_name": "テスト太郎",
            "equipment_ids": ["pos_register", "router"],
        })
        assert res.status_code == 200
        data = res.json()
        assert data["site_id"] == "SITE-001"
        assert len(data["equipment"]) == 2

    def test_create_project_invalid_equipment(self, client):
        res = client.post("/api/projects", json={
            "site_id": "X",
            "work_date": "2025-06-01",
            "worker_name": "X",
            "equipment_ids": ["nonexistent"],
        })
        assert res.status_code == 400

    def test_create_project_empty_fields(self, client):
        res = client.post("/api/projects", json={
            "site_id": "",
            "work_date": "2025-06-01",
            "worker_name": "X",
            "equipment_ids": ["pos_register"],
        })
        assert res.status_code == 422  # Validation error

    def test_get_project(self, client):
        create_res = client.post("/api/projects", json={
            "site_id": "SITE-002",
            "work_date": "2025-07-01",
            "worker_name": "花子",
            "equipment_ids": ["cash_drawer"],
        })
        pid = create_res.json()["project_id"]
        res = client.get(f"/api/projects/{pid}")
        assert res.status_code == 200
        assert res.json()["project_id"] == pid

    def test_get_nonexistent_project(self, client):
        res = client.get("/api/projects/doesnotexist")
        assert res.status_code == 404
        assert res.json()["detail"] == {"code": "NOT_FOUND", "message": "Project not found"}


class TestPhotoAPI:
    def _create_project(self, client) -> str:
        res = client.post("/api/projects", json={
            "site_id": "PHOTO-TEST",
            "work_date": "2025-08-01",
            "worker_name": "太郎",
            "equipment_ids": ["pos_register"],
        })
        return res.json()["project_id"]

    def test_upload_photo(self, client):
        pid = self._create_project(client)
        jpeg = _make_jpeg()
        res = client.post(
            f"/api/projects/{pid}/photos",
            data={"equipment_id": "pos_register", "slot_id": "pos_front"},
            files={"file": ("test.jpg", jpeg, "image/jpeg")},
        )
        assert res.status_code == 200
        assert res.json()["slot_id"] == "pos_front"

    def test_upload_invalid_mime(self, client):
        pid = self._create_project(client)
        res = client.post(
            f"/api/projects/{pid}/photos",
            data={"equipment_id": "pos_register", "slot_id": "pos_front"},
            files={"file": ("test.txt", b"not an image", "text/plain")},
        )
        assert res.status_code == 400

    def test_delete_photo(self, client):
        pid = self._create_project(client)
        jpeg = _make_jpeg()
        client.post(
            f"/api/projects/{pid}/photos",
            data={"equipment_id": "pos_register", "slot_id": "pos_front"},
            files={"file": ("test.jpg", jpeg, "image/jpeg")},
        )
        res = client.delete(
            f"/api/projects/{pid}/photos",
            params={"equipment_id": "pos_register", "slot_id": "pos_front"},
        )
        assert res.status_code == 200


class TestDocumentAPI:
    def test_list_documents_requires_authentication(self):
        # client fixture は認証を上書きするため、このケースでは生の app を使う
        tc = TestClient(app)
        res = tc.get("/api/projects/dummy-project/documents")
        assert res.status_code == 401
        assert res.json()["detail"] == {"code": "UNAUTHORIZED", "message": "Not authenticated"}


class TestAuthErrorFormat:
    def test_auth_me_with_invalid_token_returns_standard_error(self):
        tc = TestClient(app)
        res = tc.get("/api/auth/me", headers={"Authorization": "Bearer invalid-token"})
        assert res.status_code == 401
        assert res.json()["detail"] == {"code": "UNAUTHORIZED", "message": "Invalid or expired token"}


class TestValidationAPI:
    def test_validation_incomplete(self, client):
        res = client.post("/api/projects", json={
            "site_id": "VAL-TEST",
            "work_date": "2025-09-01",
            "worker_name": "次郎",
            "equipment_ids": ["router"],
        })
        pid = res.json()["project_id"]
        val_res = client.get(f"/api/projects/{pid}/validate")
        assert val_res.status_code == 200
        data = val_res.json()
        assert data["is_complete"] is False
        assert data["total_slots"] == 2
        assert data["filled_slots"] == 0

    def test_validation_complete(self, client):
        res = client.post("/api/projects", json={
            "site_id": "VAL-COMP",
            "work_date": "2025-09-01",
            "worker_name": "三郎",
            "equipment_ids": ["router"],
        })
        pid = res.json()["project_id"]
        jpeg = _make_jpeg()
        for slot_id in ["router_front", "router_wiring"]:
            client.post(
                f"/api/projects/{pid}/photos",
                data={"equipment_id": "router", "slot_id": slot_id},
                files={"file": ("test.jpg", jpeg, "image/jpeg")},
            )
        val_res = client.get(f"/api/projects/{pid}/validate")
        data = val_res.json()
        assert data["is_complete"] is True


class TestExcelExportAPI:
    def test_export_excel(self, client):
        res = client.post("/api/projects", json={
            "site_id": "EXCEL-TEST",
            "work_date": "2025-10-01",
            "worker_name": "四郎",
            "equipment_ids": ["lan_cabling"],
        })
        pid = res.json()["project_id"]
        # exportはサーバー側で撮影完了を必須とするため、全スロットを先にアップロード
        jpeg = _make_jpeg()
        for slot_id in ["lan_overview", "lan_point"]:
            client.post(
                f"/api/projects/{pid}/photos",
                data={"equipment_id": "lan_cabling", "slot_id": slot_id},
                files={"file": ("test.jpg", jpeg, "image/jpeg")},
            )
        export_res = client.get(f"/api/projects/{pid}/export")
        assert export_res.status_code == 200
        assert "spreadsheetml" in export_res.headers["content-type"]
        assert len(export_res.content) > 0
