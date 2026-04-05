"""auth の秘密鍵管理テスト"""

from __future__ import annotations

from pathlib import Path

import auth


def _set_secret_paths(tmp_path: Path, monkeypatch) -> Path:
    secret_file = tmp_path / ".jwt_secret_key"
    monkeypatch.setattr(auth, "DATA_DIR", tmp_path)
    monkeypatch.setattr(auth, "SECRET_KEY_FILE", secret_file)
    auth.get_secret_key.cache_clear()
    return secret_file


def test_get_secret_key_uses_env(monkeypatch, tmp_path):
    secret_file = _set_secret_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("ITO_PM_SECRET_KEY", "env-secret-key")

    key = auth.get_secret_key()

    assert key == "env-secret-key"
    assert not secret_file.exists()


def test_get_secret_key_generates_and_persists(monkeypatch, tmp_path):
    secret_file = _set_secret_paths(tmp_path, monkeypatch)
    monkeypatch.delenv("ITO_PM_SECRET_KEY", raising=False)
    monkeypatch.setattr(auth.secrets, "token_urlsafe", lambda _n: "generated-secret")

    key1 = auth.get_secret_key()
    auth.get_secret_key.cache_clear()
    key2 = auth.get_secret_key()

    assert key1 == "generated-secret"
    assert key2 == "generated-secret"
    assert secret_file.read_text(encoding="utf-8").strip() == "generated-secret"
