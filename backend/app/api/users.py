# -*- coding: utf-8 -*-
"""用户管理接口（仅管理员）：创建账号 / 改密 / 删除。回测等功能全体用户公用。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import config, db
from ..auth import get_current_user, hash_password

router = APIRouter(prefix="/api/users", tags=["users"])


def require_admin(user: str = Depends(get_current_user)) -> str:
    if user != config.ADMIN_USERNAME:
        raise HTTPException(status_code=403, detail="仅管理员可操作")
    return user


@router.get("")
def users_list(_admin: str = Depends(require_admin)):
    return db.list_users()


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_]+$")
    password: str = Field(min_length=6, max_length=64)


@router.post("")
def user_create(req: UserCreate, _admin: str = Depends(require_admin)):
    if db.get_user(req.username):
        raise HTTPException(status_code=400, detail="用户名已存在")
    db.create_user(req.username, hash_password(req.password))
    return {"status": "ok", "username": req.username}


class PasswordUpdate(BaseModel):
    password: str = Field(min_length=6, max_length=64)


@router.put("/{username}/password")
def user_password(username: str, req: PasswordUpdate,
                  _admin: str = Depends(require_admin)):
    if not db.get_user(username):
        raise HTTPException(status_code=404, detail="用户不存在")
    db.update_user_password(username, hash_password(req.password))
    return {"status": "ok"}


@router.delete("/{username}")
def user_delete(username: str, _admin: str = Depends(require_admin)):
    if username == config.ADMIN_USERNAME:
        raise HTTPException(status_code=400, detail="不能删除管理员账号")
    if not db.delete_user(username):
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"status": "ok"}
