"""ユーザーストレージ

ユーザーデータを data/users.json に保存する。
初回起動時にデフォルト管理者 (admin / admin1234) を自動作成する。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from auth import hash_password, verify_password

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
USERS_FILE = DATA_DIR / "users.json"


def _load_users() -> list[dict]:
    if not USERS_FILE.exists():
        return []
    with USERS_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def _save_users(users: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with USERS_FILE.open("w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)


def get_user_by_username(username: str) -> dict | None:
    for u in _load_users():
        if u["username"] == username:
            return u
    return None


def get_user_by_id(user_id: str) -> dict | None:
    for u in _load_users():
        if u["user_id"] == user_id:
            return u
    return None


def get_user_by_username_or_id(identifier: str) -> dict | None:
    """user_id または username でユーザーを検索する"""
    users = _load_users()
    for u in users:
        if u["user_id"] == identifier or u["username"] == identifier:
            return u
    return None


def authenticate_user(username: str, password: str) -> dict | None:
    user = get_user_by_username(username)
    if user is None:
        return None
    if not verify_password(password, user["hashed_password"]):
        return None
    return user


def create_user(username: str, display_name: str, password: str, role: str) -> dict:
    users = _load_users()
    user = {
        "user_id": str(uuid.uuid4()),
        "username": username,
        "display_name": display_name,
        "hashed_password": hash_password(password),
        "role": role,
        "created_at": datetime.now().isoformat(),
    }
    users.append(user)
    _save_users(users)
    return user


def delete_user(user_id: str) -> bool:
    users = _load_users()
    new_users = [u for u in users if u["user_id"] != user_id]
    if len(new_users) == len(users):
        return False
    _save_users(new_users)
    return True


def list_users() -> list[dict]:
    return _load_users()


def ensure_default_admin() -> None:
    users = _load_users()
    if not users:
        create_user(
            username="admin",
            display_name="管理者",
            password="admin1234",
            role="admin",
        )
