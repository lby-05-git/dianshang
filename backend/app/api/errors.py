# -*- coding: utf-8 -*-
"""统一错误格式（docs/06 ）：{"code","message"}，不泄露堆栈与内部细节。"""
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.domain.state_machine import StateViolation

logger = logging.getLogger("api.errors")


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(StateViolation)
    async def state_violation_handler(_req: Request, exc: StateViolation):
        return JSONResponse(status_code=409, content={"code": "STATE_VIOLATION", "message": str(exc)})

    @app.exception_handler(IntegrityError)
    async def integrity_handler(_req: Request, exc: IntegrityError):
        # 唯一约束冲突（如幂等键重复）—— 不向客户端泄露表结构细节
        logger.warning("integrity error: %s", exc.orig)
        return JSONResponse(status_code=409, content={"code": "CONFLICT", "message": "数据冲突，请检查幂等键或重试"})

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_req: Request, exc: RequestValidationError):
        return JSONResponse(status_code=400, content={"code": "VALIDATION_ERROR", "message": "参数校验失败"})

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(_req: Request, exc: StarletteHTTPException):
        if exc.status_code == 401:
            return JSONResponse(status_code=401, content={"code": "UNAUTHORIZED", "message": "未认证或凭证无效"})
        if exc.status_code == 403:
            return JSONResponse(status_code=403, content={"code": "FORBIDDEN", "message": "权限不足"})
        if exc.status_code == 404:
            return JSONResponse(status_code=404, content={"code": "NOT_FOUND", "message": "资源不存在"})
        if exc.status_code == 429:
            # 结构化 detail（如语音容量 429 capacity_full）原样透出；
            # 纯文本 detail 保持限流语义（既有限流行为不变）。
            if isinstance(exc.detail, dict):
                return JSONResponse(status_code=429, content=exc.detail)
            return JSONResponse(status_code=429, content={"code": "RATE_LIMITED", "message": "请求过于频繁"})
        if exc.status_code == 409:
            return JSONResponse(status_code=409, content={"code": "CONFLICT", "message": str(exc.detail)})
        return JSONResponse(status_code=exc.status_code, content={"code": "HTTP_ERROR", "message": str(exc.detail)})

    @app.exception_handler(Exception)
    async def unhandled_handler(_req: Request, exc: Exception):
        logger.exception("unhandled error")
        return JSONResponse(status_code=500, content={"code": "INTERNAL_ERROR", "message": "服务器内部错误"})
