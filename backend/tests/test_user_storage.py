"""user_storage のセキュリティ関連テスト"""

from __future__ import annotations

import json

import user_storage
from auth import verify_password


def _load_users_from_file() -> list[dict]:
    return json.loads(user_storage.USERS_FILE.read_text(encoding="utf-8"))


def test_ensure_default_admin_uses_env_password(monkeypatch, tmp_path):
    monkeypatch.setattr(user_storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(user_storage, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setenv("ITO_PM_DEFAULT_ADMIN_PASSWORD", "StrongPass!123")

    user_storage.ensure_default_admin()

    users = _load_users_from_file()
    assert len(users) == 1
    assert users[0]["username"] == "admin"
    assert verify_password("StrongPass!123", users[0]["hashed_password"])


def test_ensure_default_admin_generates_random_password(monkeypatch, tmp_path):
    monkeypatch.setattr(user_storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(user_storage, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.delenv("ITO_PM_DEFAULT_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(user_storage.secrets, "token_urlsafe", lambda _n: "generated-in-test")

    user_storage.ensure_default_admin()

    users = _load_users_from_file()
    assert len(users) == 1
    assert verify_password("generated-in-test", users[0]["hashed_password"])
    assert not verify_password("admin1234", users[0]["hashed_password"])
