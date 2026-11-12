# -*- coding: utf-8 -*-
"""认证接口：POST /api/v1/auth/login（含 IP 固定窗口限流，防暴力破解）。

限流实现说明（任务四审计修复）：原实现用进程内存 dict —— uvicorn 多 worker 下
各进程独立计数，10 次/60s 会被稀释成 worker 数 × 10；改为 Redis INCR 原子计数，
多进程共享同一窗口。成功登录不计数（避免误伤正常用户）。
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.schemas import LoginRequest, LoginResponse
from app.core.config import settings
from app.core.security import create_access_token, verify_password
from app.domain.models import User
from app.infrastructure.db import get_db
from app.infrastructure.redis_client import redis_client

router = APIRouter(prefix="/auth", tags=["auth"])

# 登录限流：固定窗口 60s / 10 次失败尝试（Redis 原子计数，多 worker 共享）
_LOGIN_WINDOW_SECONDS = 60
_LOGIN_MAX_ATTEMPTS = 10


def _rate_limited(ip: str) -> bool:
    """仅检查是否超限（不计数）；计数由失败分支负责。"""
    n = redis_client.get(f"login:fail:{ip}")
    return n is not None and int(n) >= _LOGIN_MAX_ATTEMPTS


def _record_failure(ip: str) -> None:
    """失败尝试计数（INCR 原子；首次设置窗口 TTL）。"""
    key = f"login:fail:{ip}"
    n = redis_client.incr(key)
    if n == 1:
        redis_client.expire(key, _LOGIN_WINDOW_SECONDS)


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    ip = request.client.host if request.client else "unknown"
    if _rate_limited(ip):
        raise HTTPException(status_code=429, detail="too many attempts")
    user = db.query(User).filter_by(username=body.username).first()
    if user is None or not verify_password(body.password, user.password_hash):
        # 仅失败尝试计数（防爆破语义；成功登录不占额度，避免误伤正常用户）
        _record_failure(ip)
        raise HTTPException(status_code=401, detail="invalid credentials")
    token = create_access_token(user.id, user.role, user.data_scope)
    return LoginResponse(
        token=token,
        expires_in=settings.JWT_EXPIRE_MINUTES * 60,
        username=user.username,
        role=user.role,
    )
