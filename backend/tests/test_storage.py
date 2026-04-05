"""ストレージのテスト"""

from datetime import date
from unittest import mock

import pytest

import storage


@pytest.fixture(autouse=True)
def temp_data_dir(tmp_path):
    """テスト用の一時データディレクトリを使用する"""
    projects_dir = tmp_path / "projects"
    photos_dir = tmp_path / "photos"
    projects_dir.mkdir()
    photos_dir.mkdir()

    with mock.patch.object(storage, "PROJECTS_DIR", projects_dir), \
         mock.patch.object(storage, "PHOTOS_DIR", photos_dir):
        yield tmp_path


def _create_test_project() -> dict:
    return storage.create_project(
        site_id="TEST-001",
        work_date=date(2025, 1, 15),
        worker_name="テスト太郎",
        equipment_ids=["pos_register", "router"],
    )


class TestCreateProject:
    def test_creates_project_with_id(self):
        project = _create_test_project()
        assert "project_id" in project
        assert len(project["project_id"]) == 12

    def test_stores_project_info(self):
        project = _create_test_project()
        assert project["site_id"] == "TEST-001"
        assert project["work_date"] == "2025-01-15"
        assert project["worker_name"] == "テスト太郎"

    def test_initializes_equipment_slots(self):
        project = _create_test_project()
        assert len(project["equipment"]) == 2
        # POSレジは3スロット
        pos = project["equipment"][0]
        assert pos["equipment_id"] == "pos_register"
        assert len(pos["slots"]) == 3
        # 全スロットが空
        for slot in pos["slots"]:
            assert slot["photo_filename"] is None

    def test_unknown_equipment_raises(self):
        with pytest.raises(ValueError, match="Unknown equipment_id"):
            storage.create_project(
                site_id="X",
                work_date=date(2025, 1, 1),
                worker_name="X",
                equipment_ids=["nonexistent"],
            )


class TestGetProject:
    def test_get_existing_project(self):
        created = _create_test_project()
        loaded = storage.get_project(created["project_id"])
        assert loaded is not None
        assert loaded["project_id"] == created["project_id"]

    def test_get_nonexistent_returns_none(self):
        assert storage.get_project("doesnotexist") is None


class TestSavePhoto:
    def test_saves_photo_and_updates_project(self):
        project = _create_test_project()
        pid = project["project_id"]
        # ダミー画像データ（1x1 JPEG）
        dummy_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        result = storage.save_photo(
            project_id=pid,
            equipment_id="pos_register",
            slot_id="pos_front",
            file_bytes=dummy_jpeg,
            original_filename="test.jpg",
        )
        assert result["filename"].endswith(".jpg")
        assert result["equipment_id"] == "pos_register"
        assert result["slot_id"] == "pos_front"

        # 案件データが更新されている
        updated = storage.get_project(pid)
        pos = updated["equipment"][0]
        front_slot = pos["slots"][0]
        assert front_slot["photo_filename"] == result["filename"]
        assert front_slot["uploaded_at"] is not None

    def test_replaces_existing_photo(self):
        project = _create_test_project()
        pid = project["project_id"]
        dummy = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        r1 = storage.save_photo(pid, "pos_register", "pos_front", dummy, "a.jpg")
        r2 = storage.save_photo(pid, "pos_register", "pos_front", dummy, "b.jpg")
        assert r1["filename"] != r2["filename"]
        # 旧ファイルは削除されている
        assert not (storage.PHOTOS_DIR / r1["filename"]).exists()

    def test_invalid_project_raises(self):
        with pytest.raises(ValueError, match="Project not found"):
            storage.save_photo("nope", "pos_register", "pos_front", b"x", "x.jpg")

    def test_invalid_slot_raises(self):
        project = _create_test_project()
        with pytest.raises(ValueError, match="Slot not found"):
            storage.save_photo(
                project["project_id"], "pos_register", "bad_slot", b"x", "x.jpg",
            )


class TestDeletePhoto:
    def test_delete_existing_photo(self):
        project = _create_test_project()
        pid = project["project_id"]
        dummy = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        storage.save_photo(pid, "pos_register", "pos_front", dummy, "t.jpg")
        assert storage.delete_photo(pid, "pos_register", "pos_front") is True
        # スロットが空に戻っている
        updated = storage.get_project(pid)
        assert updated["equipment"][0]["slots"][0]["photo_filename"] is None

    def test_delete_nonexistent_returns_false(self):
        project = _create_test_project()
        assert storage.delete_photo(project["project_id"], "pos_register", "pos_front") is False


class TestValidateProject:
    def test_empty_project_not_complete(self):
        project = _create_test_project()
        result = storage.validate_project(project["project_id"])
        assert result["is_complete"] is False
        assert result["total_slots"] == 5  # POS(3) + Router(2)
        assert result["filled_slots"] == 0
        assert len(result["missing_slots"]) == 5

    def test_complete_project(self):
        project = _create_test_project()
        pid = project["project_id"]
        dummy = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        # 全スロットに写真を登録
        for eq in project["equipment"]:
            for slot in eq["slots"]:
                storage.save_photo(
                    pid, eq["equipment_id"], slot["slot_id"], dummy, "x.jpg",
                )
        result = storage.validate_project(pid)
        assert result["is_complete"] is True
        assert result["missing_slots"] == []
        assert result["filled_slots"] == 5

    def test_nonexistent_project_raises(self):
        with pytest.raises(ValueError, match="Project not found"):
            storage.validate_project("nope")
