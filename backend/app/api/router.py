# -*- coding: utf-8 -*-
"""API 路由聚合。"""
from fastapi import APIRouter

from app.api import (
    routes_auth,
    routes_collab,
    routes_customer_assistant,
    routes_dashboard,
    routes_eval,
    routes_eval_center,
    routes_eval_schedule,
    routes_intent,
    routes_intent_eval,
    routes_observability,
    routes_orders,
    routes_policy,
    routes_query,
    routes_reconciliation,
    routes_refunds,
    routes_release_gate,
    routes_reliability,
    routes_security,
    routes_security_ops,
    routes_version_center,
    routes_voice,
    routes_voice_product,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(routes_auth.router)
api_router.include_router(routes_customer_assistant.router)
api_router.include_router(routes_refunds.router)
api_router.include_router(routes_orders.router)
api_router.include_router(routes_intent.router)
api_router.include_router(routes_intent_eval.router)
api_router.include_router(routes_reliability.intent_router)
api_router.include_router(routes_reliability.dl_router)
api_router.include_router(routes_security.router)
api_router.include_router(routes_security_ops.router)
api_router.include_router(routes_version_center.router)
api_router.include_router(routes_dashboard.router)
api_router.include_router(routes_query.router)
api_router.include_router(routes_collab.router)
api_router.include_router(routes_policy.router)
api_router.include_router(routes_eval.router)
api_router.include_router(routes_eval_center.router)
api_router.include_router(routes_eval_schedule.router)
api_router.include_router(routes_release_gate.router)
api_router.include_router(routes_observability.router)
api_router.include_router(routes_reconciliation.router)
api_router.include_router(routes_voice.router)
api_router.include_router(routes_voice_product.router)
