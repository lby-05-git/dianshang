# -*- coding: utf-8 -*-
"""订单核验 Provider（：订单真实性 / 商品一致性 / 价格一致性）。

V1.0 使用完全确定性的内置 Mock 订单数据（合成），保证评测可复现：
- `OrderRecord` 是受信任订单源返回的结构（存在性、归属、交易状态、可退款资格、商品、金额）。
- `MockOrderProvider.lookup(order_id)` 按 order_id 返回订单或 None；
- 归属核验：order.user_username 与案件提交人（RefundCase.submitter_id 对应用户 username）比对；
- Provider 不可用（超时/网络错）必须如实抛 `OrderProviderTimeout` / `OrderProviderUnavailable`，
  由上游核验节点转 UNKNOWN 并挂起转人工——**绝不允许伪造默认值当真实**。

对外主类型：
  OrderItem           单个商品（SKU/名称/规格/数量）
  OrderRecord         订单快照（OrderItem 列表 + 金额）
  ProviderResult      lookup 的结果（ok / timed_out / unavailable + order 或 error）
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.infrastructure.redis_client import redis_client

# ---- 结果/原因统一（与 workflow 核验节点共用，见 app/workflow/state.py）----
MATCH = "MATCH"
MISMATCH = "MISMATCH"
UNKNOWN = "UNKNOWN"

# SKU 判定的“规格不匹配”容差（如颜色/尺码允许的文字近似，MVP 精确匹配）
SKU_TOLERANCE: float = 0.0
# 价格允许误差（元）：优惠/运费/四舍五入带来的极小差
PRICE_TOLERANCE_YUAN: float = 0.01
EXTERNAL_ORDER_PREFIX = "order:external:"


@dataclass
class OrderItem:
    sku: str = ""
    name: str = ""
    spec: str = ""          # 规格：如 500ml、红色、XL
    quantity: int = 0


@dataclass
class OrderRecord:
    order_id: str
    order_no: str
    user_username: str      # 归属：该订单所属用户
    status: str             # PAID / CLOSED / REFUNDING / REFUNDED 等（交易状态）
    total_price_yuan: float
    paid_amount_yuan: float
    refunded_amount_yuan: float          # 已退金额（历史）
    refundable_amount_yuan: float        # 当前可退金额
    items: list[OrderItem] = field(default_factory=list)


@dataclass
class ProviderResult:
    ok: bool = False
    order: OrderRecord | None = None
    error: str = ""
    timed_out: bool = False
    available: bool = True


def _record_payload(record: OrderRecord) -> dict[str, Any]:
    return {
        "order_id": record.order_id,
        "order_no": record.order_no,
        "user_username": record.user_username,
        "status": record.status,
        "total_price_yuan": record.total_price_yuan,
        "paid_amount_yuan": record.paid_amount_yuan,
        "refunded_amount_yuan": record.refunded_amount_yuan,
        "refundable_amount_yuan": record.refundable_amount_yuan,
        "items": [
            {"sku": i.sku, "name": i.name, "spec": i.spec, "quantity": i.quantity}
            for i in record.items
        ],
    }


def register_external_order(record: OrderRecord) -> bool:
    """把外部商城订单写入 Redis，供 API 与独立 Worker 进程共同核验。

    相同快照可幂等重试；同一订单号出现不同快照则拒绝覆盖，避免在提交退款前
    篡改金额、商品或归属。返回 True 表示首次写入，False 表示幂等命中。
    """
    key = f"{EXTERNAL_ORDER_PREFIX}{record.order_id}"
    encoded = json.dumps(_record_payload(record), ensure_ascii=False, sort_keys=True)
    existing = redis_client.get(key)
    if existing is not None:
        if existing != encoded:
            raise ValueError("同一订单号已存在不同的可信快照")
        redis_client.expire(key, settings.EXTERNAL_ORDER_TTL_SECONDS)
        return False
    created = redis_client.set(
        key, encoded, nx=True, ex=settings.EXTERNAL_ORDER_TTL_SECONDS)
    if created:
        return True
    # 并发写入：再次读取并按相同快照判定幂等。
    existing = redis_client.get(key)
    if existing == encoded:
        return False
    raise ValueError("订单快照并发冲突")


def load_external_order(order_id: str) -> OrderRecord | None:
    """读取外部订单快照。损坏数据按 Provider 不可用处理，绝不伪造 MATCH。"""
    raw = redis_client.get(f"{EXTERNAL_ORDER_PREFIX}{order_id}")
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return OrderRecord(
            order_id=str(data["order_id"]),
            order_no=str(data["order_no"]),
            user_username=str(data["user_username"]),
            status=str(data["status"]),
            total_price_yuan=float(data["total_price_yuan"]),
            paid_amount_yuan=float(data["paid_amount_yuan"]),
            refunded_amount_yuan=float(data["refunded_amount_yuan"]),
            refundable_amount_yuan=float(data["refundable_amount_yuan"]),
            items=[OrderItem(**item) for item in data.get("items", [])],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OrderProviderUnavailable(f"外部订单快照损坏: {order_id}") from exc


class OrderProviderError(Exception):
    """订单源粒度错误（底层）。"""


class OrderProviderTimeout(OrderProviderError):
    """订单源超时。"""


class OrderProviderUnavailable(OrderProviderError):
    """订单源不可用/网络错。"""


# ---- 确定性内置订单表（合成数据；真实订单源留待后续接入）----
_BUILTIN_ORDERS: dict[str, OrderRecord] = {}


def _item(sku: str, name: str, spec: str, qty: int) -> OrderItem:
    return OrderItem(sku=sku, name=name, spec=spec, quantity=qty)


def _build_orders() -> dict[str, OrderRecord]:
    """内置订单：owner 均为 customer_service1（评测账号），金额/状态/可退资格各不同。"""
    orders: dict[str, OrderRecord] = {}
    # 低风险：小额、全可退（G01 自动秒退场景）
    orders["ORD-1001"] = OrderRecord(
        order_id="ORD-1001", order_no="NO1001", user_username="customer_service1",
        status="PAID", total_price_yuan=99.00, paid_amount_yuan=99.00,
        refunded_amount_yuan=0.00, refundable_amount_yuan=99.00,
        items=[_item("SKU-AA", "保温杯", "500ml", 1)],
    )
    # 中高风险：高额、可退（G02 挂起-审批）
    orders["ORD-2001"] = OrderRecord(
        order_id="ORD-2001", order_no="NO2001", user_username="customer_service1",
        status="PAID", total_price_yuan=888.00, paid_amount_yuan=888.00,
        refunded_amount_yuan=0.00, refundable_amount_yuan=888.00,
        items=[_item("SKU-BB", "空气炸锅", "5L", 1), _item("SKU-CC", "炸锅配件", "通用", 1)],
    )
    # 部分退款历史（可退<原始实付）
    orders["ORD-2002"] = OrderRecord(
        order_id="ORD-2002", order_no="NO2002", user_username="customer_service1",
        status="REFUNDING", total_price_yuan=500.00, paid_amount_yuan=500.00,
        refunded_amount_yuan=200.00, refundable_amount_yuan=300.00,
        items=[_item("SKU-DD", "跑步机", "智能款", 1)],
    )
    # 归属他人（G05 归属不一致）
    orders["ORD-3001"] = OrderRecord(
        order_id="ORD-3001", order_no="NO3001", user_username="supervisor1",
        status="PAID", total_price_yuan=120.00, paid_amount_yuan=120.00,
        refunded_amount_yuan=0.00, refundable_amount_yuan=120.00,
        items=[_item("SKU-EE", "机械键盘", "87键", 1)],
    )
    # 商品规格与原单不同（G06）
    orders["ORD-3002"] = OrderRecord(
        order_id="ORD-3002", order_no="NO3002", user_username="customer_service1",
        status="PAID", total_price_yuan=200.00, paid_amount_yuan=200.00,
        refunded_amount_yuan=0.00, refundable_amount_yuan=200.00,
        items=[_item("SKU-FF", "蓝牙音箱", "黑色", 1)],
    )
    # 价格：可退金额低于申请（G07）
    orders["ORD-3003"] = OrderRecord(
        order_id="ORD-3003", order_no="NO3003", user_username="customer_service1",
        status="REFUNDING", total_price_yuan=1000.00, paid_amount_yuan=1000.00,
        refunded_amount_yuan=700.00, refundable_amount_yuan=300.00,
        items=[_item("SKU-GG", "冰箱", "双门", 1)],
    )
    return orders


_BUILTIN_ORDERS = _build_orders()

# 条件开关：`supervisor1` 固定返回超时，用于评测 G08（订单 Provider 超时 -> UNKNOWN）
_SIMULATE_TIMEOUT_USERNAME: str | None = "supervisor1"


class OrderProvider:
    """订单源基类：子类实现 lookup()。提供归属校验与结果打包。"""

    name = "base"

    def lookup(self, order_id: str) -> ProviderResult:
        raise NotImplementedError

    def verify_ownership(self, order: OrderRecord, requester_username: str) -> tuple[str, list[str]]:
        """校验订单归属。返回 (verdict, reasons)。"""
        if order.user_username == requester_username:
            return MATCH, ["订单归属一致"]
        return MISMATCH, [f"订单归属不一致: 订单属于 {order.user_username}，申请人是 {requester_username}"]


class MockOrderProvider(OrderProvider):
    """确定性订单源：内置合成订单 + 可注入超时/不可用故障 + 测试动态注册。"""

    name = "mock"

    def __init__(
        self,
        timeout_for_username: str | None = _SIMULATE_TIMEOUT_USERNAME,
        base_delay: float = 0.0,
    ) -> None:
        self._timeout_for = timeout_for_username
        self._base_delay = base_delay
        self._extra: dict[str, OrderRecord] = {}

    def register_order(self, record: OrderRecord) -> None:
        """测试动态注册一张订单（避免多用例共用内置订单触发 fraud 历史串扰）。"""
        self._extra[record.order_id] = record

    def lookup(self, order_id: str) -> ProviderResult:
        time.sleep(self._base_delay)
        # 故障注入：指定用户的所有请求都超时（G08 目标用例可复现）
        if self._timeout_for and order_id.startswith("ORD-TIMEOUT"):
            return ProviderResult(ok=False, error=f"订单源超时 (order_id={order_id})",
                                  timed_out=True, available=True)
        rec = self._extra.get(order_id) or _BUILTIN_ORDERS.get(order_id)
        if rec is None:
            rec = load_external_order(order_id)
        if rec is None:
            return ProviderResult(ok=False, error=f"订单不存在: {order_id}", available=True)
        return ProviderResult(ok=True, order=rec, available=True)


def verify_order(
    provider: OrderProvider,
    order_id: str,
    requester_username: str,
) -> dict:
    """订单真实性 + 归属 + 交易状态 + 可退款资格 -> 结构化核验结果（供流程节点写库/输出）。

    返回：
      {"order_verification": MATCH|MISMATCH|UNKNOWN,
       "reasons": [...],
       "order_status": 交易状态或 None,
       "refundable_amount_yuan": 可退金额或 None}
    """
    # 订单存在性是真实性的第一前提：不存在 => MISMATCH（挂起，不退款）
    pr = provider.lookup(order_id)
    if not pr.ok:
        if pr.timed_out or not pr.available:
            return {
                "order_verification": UNKNOWN,
                "reasons": [pr.error],
                "order_status": None,
                "refundable_amount_yuan": None,
            }
        return {
            "order_verification": MISMATCH,
            "reasons": [f"订单不存在: {order_id}"],
            "order_status": None,
            "refundable_amount_yuan": None,
        }
    order = pr.order
    verdict, reasons = provider.verify_ownership(order, requester_username)
    # 交易状态：PAID 才可退；CLOSED/REFUNDED 视为不可退（归属 MATCH 但状态异常 -> MISMATCH）
    if order.status not in ("PAID", "REFUNDING"):
        verdict = MISMATCH
        reasons.append(f"订单交易状态异常，不可退款: {order.status}")
    if order.refundable_amount_yuan <= 0:
        verdict = MISMATCH
        reasons.append("订单无可退金额")
    return {
        "order_verification": verdict,
        "reasons": reasons,
        "order_status": order.status,
        "refundable_amount_yuan": order.refundable_amount_yuan,
        "order": order,
    }


_provider: OrderProvider | None = None


def get_order_provider(
    timeout_for_username: str | None = None,
) -> OrderProvider:
    """全局单例（可注入故障）。测试可传子类覆盖。"""
    global _provider
    if _provider is not None and timeout_for_username is None:
        return _provider
    # 给评测/测试传不同 timeout 用户时，新建独立实例（不污染全局缓存）
    return MockOrderProvider(timeout_for_username=timeout_for_username or _SIMULATE_TIMEOUT_USERNAME)


def reset_order_provider() -> None:
    """重置全局单例（测试用）。"""
    global _provider
    _provider = None


_multi_provider: "MultiSourceOrderProvider | None" = None


def get_multi_source_provider() -> "MultiSourceOrderProvider":
    """全局默认多来源聚合单例：mock-a / mock-b 双源（信任权重读 config）。

    默认双源同为确定性 Mock（便于评测复现）；测试如需矛盾/熔断场景自行
    register_source 覆盖业务实例（模块级注入）。
    """
    global _multi_provider
    if _multi_provider is None:
        mp = MultiSourceOrderProvider()
        trust = dict(settings.ORDER_SOURCE_TRUST or {"mock-a": 1.0, "mock-b": 0.6})
        mp.register_source("mock-a",
                           MockOrderProvider(timeout_for_username=None),
                           trust.get("mock-a", 1.0))
        mp.register_source("mock-b",
                           MockOrderProvider(timeout_for_username=None),
                           trust.get("mock-b", 0.6))
        _multi_provider = mp
    return _multi_provider


def reset_multi_source_provider() -> None:
    """重置多来源聚合单例（测试用）。"""
    global _multi_provider
    _multi_provider = None


# =====================================================================
# V3.0：多来源统一核验契约 + 熔断降级
# =====================================================================

# 多来源聚合判定：EXIST 订单存在 / MISSING 订单不存在 / UNKNOWN 来源不可用或来源间矛盾
EXIST = "EXIST"
MISSING = "MISSING"
SOURCE_UNKNOWN = "UNKNOWN"


class SourceBreaker:
    """单来源熔断器：连续超时/不可用 -> 断开 -> 冷却后半开放行探针。

    与 infra/llm.py CircuitBreaker 思路一致，但语义是「来源可用性」：
      - record(ok=False) 累计连续失败；达到 threshold 置 OPEN（拒绝调用）；
      - cooldown 过后自动进入 half-open：下一次 allow() 放行探针，成功则闭合；
      - 只影响来源选取，绝不让"缺来源"伪装成真实订单（UNKNOWN 安全挂起红线保持）。
    """

    def __init__(self, threshold: int = 2, cooldown_seconds: float = 5.0):
        self.threshold = max(1, threshold)
        self.cooldown = cooldown_seconds
        self._consecutive_fail = 0
        self._opened_at: float | None = None

    @property
    def open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.time() - self._opened_at >= self.cooldown:
            return False                     # 冷却结束 -> 半开（下次调用放行探针）
        return True

    def allow(self) -> bool:
        return not self.open

    def record(self, ok: bool) -> None:
        if ok:
            self._consecutive_fail = 0
            self._opened_at = None
        else:
            self._consecutive_fail += 1
            if self._consecutive_fail >= self.threshold:
                self._opened_at = time.time()

    def state(self) -> str:
        if self.open:
            return "OPEN"
        if self._opened_at is not None:
            return "HALF_OPEN"
        return "CLOSED"


class MultiSourceOrderProvider(OrderProvider):
    """多来源订单核验。

    - 注册 N 个 `OrderProvider` 子来源（各自独立故障注入，供评测）；
    - 每个来源按 `trust` 加权。聚合共识：
        * EXIST   —— 至少一个信任来源返回订单，且无信任来源返回"不存在"；
        * MISSING —— 至少一个信任来源返回"不存在"，且无信任来源返回订单；
        * UNKNOWN —— 无信任来源可用（全超时/熔断/不可用）或来源间互相矛盾（应转人工，绝不自动退）。
    - 熔断降级：某来源连续失败 -> SourceBreaker OPEN，跳过该来源并降级到其余来源；
      `degraded=True` 指本次至少一个来源未被采用（熔断/超时），审计留痕。
    - 判定绝不伪造：所有来源不可用时明确 UNKNOWN（上游转人工/安全挂起）。

    `lookup()` 实现 OrderProvider 单源契约（给 verify_order 用的兼容入口）；
    `multi_lookup()` 返回结构化聚合结果（含每个来源的 verdicts，供 reconciliation / 审计）。
    """

    name = "multi"

    def __init__(self):
        self._sources: list[tuple[str, OrderProvider, float]] = []
        self._breakers: dict[str, SourceBreaker] = {}
        self._trust: dict[str, float] = {}
        self.last_verdicts: list[dict] = []

    def register_source(self, name: str, provider: OrderProvider,
                        trust: float = 1.0) -> None:
        """注册一个子来源（同名覆盖）。"""
        self._sources = [(n, p, t) for (n, p, t) in self._sources if n != name]
        self._sources.append((name, provider, float(trust)))
        self._breakers[name] = SourceBreaker()
        self._trust[name] = float(trust)

    @property
    def source_names(self) -> list[str]:
        return [n for (n, _, _) in self._sources]

    def source_state(self) -> dict[str, str]:
        return {n: self._breakers[n].state() for (n, _, _) in self._sources}

    def _opinion(self, name: str, pr: ProviderResult) -> tuple[str, bool, list[dict]]:
        """单个来源的判定：返回 (opinion, degraded, source_verdict)。

        opinion: EXIST / MISSING / UNKNOWN；degraded: 来源被熔断或本次超时；
        source_verdict：落 OrderSourceVerdict 的字段。
        """
        if name in self._breakers and not self._breakers[name].allow():
            v = {"source_id": name, "trusted": False, "verdict": SOURCE_UNKNOWN,
                 "reason": "circuit open (degraded)", "ok": False, "degraded": True}
            return SOURCE_UNKNOWN, True, v
        if pr.ok and pr.order is not None:
            v = {"source_id": name, "trusted": True, "verdict": EXIST,
                 "reason": pr.order.status, "ok": True, "degraded": False}
            return EXIST, False, v
        if pr.timed_out or not pr.available:
            v = {"source_id": name, "trusted": False, "verdict": SOURCE_UNKNOWN,
                 "reason": pr.error, "ok": False, "degraded": True}
            return SOURCE_UNKNOWN, True, v
        v = {"source_id": name, "trusted": True, "verdict": MISSING,
             "reason": pr.error, "ok": False, "degraded": False}
        return MISSING, False, v

    def multi_lookup(self, order_id: str) -> dict:
        """按信任加权聚合多来源判定（确定性，可评测）。

        共识规则（信任权重=注册 trust；被熔断/超时的来源 trusted=False 不参与表决）：
          - 有任一信任来源 EXIST 且无任一信任来源 MISSING  -> EXIST；
          - 有任一信任来源 MISSING 且无任一信任来源 EXIST  -> MISSING；
          - 其余（无可用信任来源 / 来源间相反）            -> UNKNOWN。
        返回：
          {"order_id", "verdict": EXIST|MISSING|UNKNOWN,
           "order": OrderRecord|None,   # EXIST 时取信任度最高来源的订单
           "consensus": bool,           # 来源一致（无矛盾）
           "degraded": bool,            # 至少一个来源未被采用
           "trust_used": float,         # 采用来源信任和 / 注册信任和（0~1）
           "sources": [verdict dict...]}
        """
        self.last_verdicts = []
        reachable: list[tuple[str, str, OrderRecord | None, float, bool]] = []
        total_trust = sum(self._trust.values()) or 1.0
        used_trust = 0.0
        degraded = False

        for name, prov, t in self._sources:
            pr = prov.lookup(order_id)
            opinion, src_degraded, v = self._opinion(name, pr)
            if src_degraded:
                degraded = True
            if v["trusted"]:
                used_trust += t if opinion != SOURCE_UNKNOWN else 0.0
            self.last_verdicts.append(v)
            if v["trusted"]:
                reachable.append((name, opinion, pr.order, t, pr))

        founded = [r for r in reachable if r[1] == EXIST]
        missing = [r for r in reachable if r[1] == MISSING]
        unknown = [r for r in reachable if r[1] == SOURCE_UNKNOWN]

        if founded and not missing:
            best = max(founded, key=lambda r: r[3])
            return {
                "order_id": order_id, "verdict": EXIST, "order": best[2],
                "consensus": True, "degraded": degraded,
                "trust_used": round(used_trust / total_trust, 4),
                "sources": list(self.last_verdicts),
            }
        if missing and not founded:
            return {
                "order_id": order_id, "verdict": MISSING, "order": None,
                "consensus": True, "degraded": degraded,
                "trust_used": round(used_trust / total_trust, 4),
                "sources": list(self.last_verdicts),
            }
        # 无可信来源（全熔断/超时）或来源间矛盾：UNKNOWN（安全挂起，绝不伪造默认值）
        if not reachable:
            return {
                "order_id": order_id, "verdict": SOURCE_UNKNOWN, "order": None,
                "consensus": len(self.last_verdicts) == 0, "degraded": True,
                "trust_used": 0.0, "sources": list(self.last_verdicts),
            }
        return {
            "order_id": order_id, "verdict": SOURCE_UNKNOWN, "order": None,
            "consensus": False, "degraded": True,
            "trust_used": round(used_trust / total_trust, 4),
            "sources": list(self.last_verdicts),
        }

    def lookup(self, order_id: str) -> ProviderResult:
        """OrderProvider 单源兼容：EXIST -> ok/order；MISSING -> 订单不存在；
        UNKNOWN -> timed_out（上游安全挂起，绝不伪装默认值）。"""
        res = self.multi_lookup(order_id)
        if res["verdict"] == EXIST:
            return ProviderResult(ok=True, order=res["order"], available=True)
        if res["verdict"] == MISSING:
            return ProviderResult(ok=False, error=f"订单不存在: {order_id}", available=True)
        return ProviderResult(ok=False,
                              error=f"多来源聚合不可判定 (order_id={order_id})",
                              timed_out=True, available=True)
