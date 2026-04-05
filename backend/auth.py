import os
import secrets
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 8
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SECRET_KEY_FILE = DATA_DIR / ".jwt_secret_key"

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


@lru_cache(maxsize=1)
def get_secret_key() -> str:
    """JWT 秘密鍵を取得する。

    優先順位:
    1) 環境変数 ITO_PM_SECRET_KEY
    2) data/.jwt_secret_key (なければ自動生成して永続化)
    """
    env_key = (os.getenv("ITO_PM_SECRET_KEY") or "").strip()
    if env_key:
        return env_key

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not SECRET_KEY_FILE.exists():
        generated = secrets.token_urlsafe(64)
        try:
            fd = os.open(SECRET_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(generated)
        except FileExistsError:
            pass

    key = SECRET_KEY_FILE.read_text(encoding="utf-8").strip() if SECRET_KEY_FILE.exists() else ""
    if key:
        return key

    # ファイルが空だった場合の救済
    key = secrets.token_urlsafe(64)
    SECRET_KEY_FILE.write_text(key, encoding="utf-8")
    try:
        os.chmod(SECRET_KEY_FILE, 0o600)
    except OSError:
        pass
    return key


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(data: dict) -> str:
    payload = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload["exp"] = expire
    return jwt.encode(payload, get_secret_key(), algorithm=ALGORITHM)


def verify_token(token: str) -> dict:
    try:
        return jwt.decode(token, get_secret_key(), algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_current_user(token: Annotated[str, Depends(oauth2_scheme)]) -> dict:
    return verify_token(token)


async def require_admin(user: Annotated[dict, Depends(get_current_user)]) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return user
