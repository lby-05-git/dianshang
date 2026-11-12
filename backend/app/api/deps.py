# -*- coding: utf-8 -*-
"""认证与授权依赖：get_current_user / require_role / require_supervisor。

角色矩阵（3.2.2 权限模型）：
  customer_service : 客服，数据范围=本人(self)，可建单/认领/审批自己承接单
  supervisor       : 主管，数据范围=team/store，可审批
  leader           : 班组长(新增)，数据范围=team，可审批/导出/策略配置基础操作
  admin            : 管理员，数据范围=all，全量

所有守卫基于 JWT 内的 role/data_scope（登录时签发）做快速拒绝；
数据范围在具体查询上用 query_service.data_scope_filter 落库过滤。
"""
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy.orm import Session

from app.core.security import decode_access_token
from app.domain.models import ROLE_SUPERVISOR, User
from app.infrastructure.db import get_db

bearer = HTTPBearer(auto_error=False)

# 角色枚举（与 models 常量一致 + admin 扩展）
ROLE_CUSTOMER_SERVICE = "customer_service"
ROLE_SUPERVISOR = "supervisor"
ROLE_LEADER = "leader"
ROLE_ADMIN = "admin"
ALL_ROLES = (ROLE_CUSTOMER_SERVICE, ROLE_SUPERVISOR, ROLE_LEADER, ROLE_ADMIN)

# 角色层级（用于“至少 need_min 级”判断）：数字越大权限越高
_ROLE_LEVEL = {
    ROLE_CUSTOMER_SERVICE: 1,
    ROLE_SUPERVISOR: 2,
    ROLE_LEADER: 3,
    ROLE_ADMIN: 4,
}


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status_code=401, detail="missing token")
    try:
        payload = decode_access_token(credentials.credentials)
    except JWTError:
        raise HTTPException(status_code=401, detail="invalid token")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid token")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="user not found")
    return user


def require_roles(*allowed: str):
    """校验当前用户角色属于 allowed 之一；否则 403。用法：require_roles(ROLE_LEADER, ROLE_ADMIN)"""
    def _dep(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(status_code=403, detail="forbidden")
        return user
    return _dep


def require_min_role(min_role: str):
    """校验当前角色 >= min_role（按 _ROLE_LEVEL 层级）；否则 403。"""
    required = _ROLE_LEVEL.get(min_role, 99)
    def _dep(user: User = Depends(get_current_user)) -> User:
        if _ROLE_LEVEL.get(user.role, 0) < required:
            raise HTTPException(status_code=403, detail="forbidden")
        return user
    return _dep


def require_supervisor(user: User = Depends(get_current_user)) -> User:
    if user.role != ROLE_SUPERVISOR:
        raise HTTPException(status_code=403, detail="forbidden")
    return user