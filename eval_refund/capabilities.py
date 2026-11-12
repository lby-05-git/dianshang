# -*- coding: utf-8 -*-
"""插件契约（M21 V3.0）：能力声明 + Metric 注册，不改核心表/核心字段。

契约目标：
- 数据集声明 required_capabilities（核心能力）；预留能力（security/intent/voice/
  coaching）未实现时为 unavailable —— 用例带预留标签必须 SKIPPED_UNSUPPORTED，
  绝不显示通过。
- 被测 Target 可声明 audio/streaming 等能力 -> 报告 environment.capabilities 可读；
  未实现能力仍标 unavailable，不伪造可用。
- 插件可注册新 Metric 维度 / 新能力，只改注册表（内存/JSON detail），
  绝不触碰核心表结构与核心字段（不新增列、不修改 schema_version）。

用法（示例，插件内调用）：
    from eval_refund.capabilities import register_capability, register_metric
    register_capability("voice_qa", modality="audio", streaming=True,
                        metric_names=("voice_similarity",))
    register_metric("voice_similarity", capability="voice_qa",
                    dim_type="score", source="voice-plugin")
"""
from __future__ import annotations


# ---- 核心能力（数据集已声明，评审用例服务）----
CORE_CAPABILITIES = ("refund_workflow", "order_verification", "hitl")

# ---- 预留能力（未实现，不许伪 pass）----
_RESERVED_DEFAULT = {
    "security": {"availability": "unavailable", "modality": None, "streaming": False},
    "intent": {"availability": "unavailable", "modality": None, "streaming": False},
    "voice": {"availability": "unavailable", "modality": "audio", "streaming": True},
    "coaching": {"availability": "unavailable", "modality": None, "streaming": False},
}

# 注册表：插件可在此声明能力 / Metric（进程内起效，不落核心表）
_CAPABILITY_REGISTRY: dict[str, dict] = {
    name: dict(meta) for name, meta in _RESERVED_DEFAULT.items()
}
_METRIC_REGISTRY: dict[str, dict] = {}


def register_capability(name: str, *, availability: str = "available",
                        modality: str | None = None, streaming: bool = False,
                        metric_names: tuple[str, ...] = ()) -> None:
    """插件声明一个能力（不落库，只扩展注册表）。

    实现方插件把 availability 从 unavailable -> available；未实现时保持
    unavailable，对应预留标签用例仍走 SKIPPED_UNSUPPORTED。
    """
    if availability not in ("available", "unavailable", "declared"):
        raise ValueError(f"availability 须为 available/unavailable/declared: {availability}")
    _CAPABILITY_REGISTRY[name] = {
        "availability": availability,
        "modality": modality,
        "streaming": bool(streaming),
        "metric_names": list(metric_names),
    }
    for m in metric_names:
        _METRIC_REGISTRY.setdefault(m, {"capability": name})


def register_metric(name: str, *, capability: str | None = None,
                    dim_type: str = "score", source: str = "plugin") -> None:
    """插件注册一个 Metric 维度（不新增核心表列；结果写入 dimensions/detail JSON）。"""
    _METRIC_REGISTRY[name] = {"capability": capability, "dim_type": dim_type,
                              "source": source}


def capability_table() -> dict:
    """当前能力注册表（报告/API 可读）：每项带 implemented 语义。"""
    return {
        name: {**meta, "implemented": meta.get("availability") == "available"}
        for name, meta in sorted(_CAPABILITY_REGISTRY.items())
    }


def metric_table() -> dict:
    return {name: dict(meta) for name, meta in sorted(_METRIC_REGISTRY.items())}


def unimplemented_metrics() -> list[str]:
    """列出声明了 metric 但能力未实现（unavailable）的度量 —— 报告须标未测。"""
    out = []
    for name, meta in _METRIC_REGISTRY.items():
        cap = meta.get("capability")
        if cap and _CAPABILITY_REGISTRY.get(cap, {}).get("availability") != "available":
            out.append(name)
    return out


def target_declaration(audio: bool = False, streaming: bool = False) -> dict:
    """被测 Target 的能力声明：audio/streaming 等（未实现时为 False）。

    报告 environment.capabilities 读取本声明：只反映"声明了什么"，绝不自证可用。
    """
    base = capability_table()
    base["audio"] = {"availability": "available" if audio else "unavailable",
                     "modality": "audio", "streaming": False,
                     "implemented": bool(audio)}
    base["streaming"] = {"availability": "available" if streaming else "unavailable",
                         "modality": None, "streaming": True,
                         "implemented": bool(streaming)}
    return base