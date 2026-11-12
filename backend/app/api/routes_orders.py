# -*- coding: utf-8 -*-
"""外部商城订单同步 API。

同步发生在退款建单之前。订单归属强制绑定当前 JWT 用户，快照写入 Redis，
从而让独立退款 Worker 能完成订单真实性、商品一致性和价格一致性核验。
"""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import get_current_user
from app.domain.models import User
from app.infrastructure.order_provider import (
    OrderItem,
    OrderRecord,
    register_external_order,
)

router = APIRouter(prefix="/orders", tags=["orders"])


class ExternalOrderItemIn(BaseModel):
    sku: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=200)
    spec: str = Field(default="", max_length=200)
    quantity: int = Field(ge=1, le=1000)


class ExternalOrderSyncIn(BaseModel):
    order_id: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]+$")
    order_no: str | None = Field(default=None, max_length=64)
    total_price_yuan: Decimal = Field(gt=0, le=Decimal("100000.00"))
    items: list[ExternalOrderItemIn] = Field(min_length=1, max_length=50)


@router.post("/external", status_code=201)
def sync_external_order(body: ExternalOrderSyncIn,
                        user: User = Depends(get_current_user)):
    amount = float(body.total_price_yuan)
    record = OrderRecord(
        order_id=body.order_id,
        order_no=body.order_no or body.order_id,
        # 不接受调用方传 owner，防止伪造他人订单；与后续退款 JWT 保持同一主体。
        user_username=user.username,
        status="PAID",
        total_price_yuan=amount,
        paid_amount_yuan=amount,
        refunded_amount_yuan=0.0,
        refundable_amount_yuan=amount,
        items=[
            OrderItem(sku=i.sku, name=i.name, spec=i.spec, quantity=i.quantity)
            for i in body.items
        ],
    )
    try:
        created = register_external_order(record)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "order_id": body.order_id,
        "owner": user.username,
        "created": created,
        "status": "SYNCED",
    }
