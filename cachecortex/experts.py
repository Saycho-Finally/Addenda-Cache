"""ExpertRegistry：MoE 侧专家注册表（与 addenda-decide 的 DecisionCore 对接的共享组件）。

ExpertSpec 按能力契约注册（capabilities/cost_tier/tools），不按"人格化角色"——
路由器对 capability 做分类，有具体物可推理（programmer.ie 模式）。
路由分布统计（熵/利用率/饿死检测）= MoE 负载均衡的运行时输入。
"""

from __future__ import annotations

import math
import time
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class ExpertSpec:
    """专家能力契约。"""
    name: str
    capabilities: set[str]
    cost_tier: int            # 1(最便宜)~4(最贵)
    latency_tier: int = 1
    tools: set[str] = field(default_factory=set)
    deterministic: bool = False   # 程序性专家（calculator/SQL/parser）——可靠即优先


@dataclass
class RouteDecision:
    """路由返回：置信度参与控制流（低置信 → 多专家并行/升级/拆任务）。"""
    expert: str
    confidence: float
    required_capabilities: list[str]
    alternatives: list[str]
    reason: str


class ExpertRegistry:
    def __init__(self):
        self.experts: dict[str, ExpertSpec] = {}
        self.route_history: Counter = Counter()   # 路由分布（熵/饿死检测）

    def register(self, spec: ExpertSpec) -> None:
        self.experts[spec.name] = spec

    def route(self, required: set[str], prefer_deterministic: bool = True) -> RouteDecision:
        """按所需能力路由：required ⊆ capabilities 的候选中，deterministic 优先，
        再按 cost_tier 最低。空集 → 低置信 RouteDecision（confidence=0）。"""
        cands = [e for e in self.experts.values() if required <= e.capabilities]
        if not cands:
            return RouteDecision(expert="", confidence=0.0,
                                 required_capabilities=sorted(required),
                                 alternatives=[],
                                 reason="no expert covers required capabilities")
        if prefer_deterministic:
            det = [e for e in cands if e.deterministic]
            if det:
                cands = det
        cands.sort(key=lambda e: e.cost_tier)
        best = cands[0]
        alts = [e.name for e in cands[1:]]
        conf = 1.0 if best.deterministic else round(1.0 / (1 + 0.3 * len(alts)), 3)
        self.route_history[best.name] += 1
        return RouteDecision(expert=best.name, confidence=conf,
                             required_capabilities=sorted(required),
                             alternatives=alts,
                             reason=f"capability match {sorted(required)}"
                             + (" (deterministic preferred)" if best.deterministic else ""))

    # ---- 负载均衡与健康监控（MoE 模式导入）----

    def route_entropy(self) -> float:
        """路由分布熵：过低 = 路由坍缩（全涌向单专家），报警阈值由调用方定。"""
        total = sum(self.route_history.values())
        if total == 0:
            return 0.0
        ent = 0.0
        for c in self.route_history.values():
            p = c / total
            if p > 0:
                ent -= p * math.log(p, 2)
        return round(ent, 4)

    def starved_experts(self, threshold: int = 1) -> list[str]:
        """专家饿死检测：路由次数 < threshold 的已注册专家。"""
        return [name for name in self.experts
                if self.route_history.get(name, 0) < threshold]

    def utilization_report(self) -> dict:
        total = sum(self.route_history.values())
        return {name: {"count": self.route_history.get(name, 0),
                       "pct": round(self.route_history.get(name, 0) / total * 100, 1)
                       if total else 0.0}
                for name in self.experts}

    def snapshot(self) -> dict:
        return {"n_experts": len(self.experts),
                "route_entropy": self.route_entropy(),
                "starved": self.starved_experts(),
                "utilization": self.utilization_report(),
                "ts": time.time()}
