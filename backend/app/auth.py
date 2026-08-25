# -*- coding: utf-8 -*-
"""JWT 认证：签发/校验 + 密码哈希（pbkdf2_hmac，避免 bcrypt Windows 编译问题）"""
import hashlib
import hmac
import secrets
import time
from typing import Optional

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import config

_pbkdf2_iters = 100_000
_bearer = HTTPBearer(auto_error=False)


# ---------------- 密码哈希 ----------------

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt), _pbkdf2_iters).hex()
    return f"pbkdf2${_pbkdf2_iters}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt, digest = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                   bytes.fromhex(salt), int(iters)).hex()
        return hmac.compare_digest(calc, digest)
    except (ValueError, AttributeError):
        return False


# ---------------- JWT ----------------

def create_token(username: str) -> str:
    now = int(time.time())
    payload = {"sub": username, "iat": now, "exp": now + config.TOKEN_EXPIRE_SECONDS}
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def decode_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])
        return payload.get("sub")
    except jwt.PyJWTError:
        return None


# ---------------- FastAPI 依赖 ----------------

def get_current_user(credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> str:
    if credentials is None:
        raise HTTPException(status_code=401, detail="未认证，请先登录")
    username = decode_token(credentials.credentials)
    if not username:
        raise HTTPException(status_code=401, detail="token 无效或已过期")
    return username
