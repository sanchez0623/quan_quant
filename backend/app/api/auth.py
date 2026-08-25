# -*- coding: utf-8 -*-
"""认证接口"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import auth, db

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(req: LoginRequest):
    user = db.get_user(req.username)
    if not user or not auth.verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    from .. import config
    return {"token": auth.create_token(req.username),
            "expires_in": config.TOKEN_EXPIRE_SECONDS,
            "username": req.username}
