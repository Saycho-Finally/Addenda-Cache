"""CacheOptimizeStack：场景自适应缓存优化栈。

目标：在全场景背景下，让每个场景的缓存命中都尽可能逼近各自的理论上限。

理论极限（先说清，再逼近）：
  输入命中率上限 = 1 − 动态尾巴/总输入（滞后窗可预热摊薄，动态尾巴是结构性的）
  - 多轮累积场景：分母随轮数增长 → 上限趋近 100%，实测 92-99.8%
  - 独立长说明场景：上限 = 说明/(说明+尾巴) → 前缀做大逼近
  - 独立全新输入场景（实时流）：输入几乎全新 → 输入缓存无解，
    正确策略 = 响应缓存（重复查询）/ 批处理合并 / 输出侧降本 / off-peak

四层栈（每层可独立开关，Auto 模式按 Scene 自动选择）：
  S1 审计器   PollutionAudit    前缀污染检测（断崖式杀伤的第一防线）
  S2 前缀库   PrefixBank        三区结构 + 指纹（已实现）
  S3 压缩器   TailCompressor    动态尾巴压缩接口（接 LLMLingua/检索裁剪）
  S4 调度器   BatchScheduler    同前缀聚簇 + TTL 保活 + 批处理合并
  S5 探测层   CacheTiers        provider 能力探测与降级（已实现）

本模块提供：AutoStack（场景 → 杠杆组合 → 预期命中率）+ BatchMerger（实时流解法）
+ WarmupScheduler（预热保活骨架）。单测覆盖策略选择与合并数学。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from cachecortex.hitrate_budget import Scene, predict


# ---------------------------------------------------------------- 策略选择

@dataclass
class StackPlan:
    """一个场景的最优杠杆组合与预期收益。"""
    scene: str
    levers: list[str]                    # 启用的杠杆（L1-L4 子集）
    expected_hit_rate: float             # 叠加后的预期输入命中率
    expected_cost_ratio: float           # 相对无缓存基线的输入成本比（hit 价按 2% 计）
    unreachable_reason: str = ""         # 输入命中不可达时的替代策略说明
    notes: list[str] = field(default_factory=list)


def plan(scene: Scene, tail_compression: float = 1.0) -> StackPlan:
    """Auto 模式：给定场景参数与压缩率（1.0=不压缩），产出最优杠杆组合。

    tail_compression：L2 压缩后尾巴的保留比例（0.3 表示压到 30%）。
    """
    levers: list[str] = []
    s = scene
    if s.pollution_per_turn > 0:
        levers.append("S1 审计器：前缀污染检测告警（断崖式杀伤，必须先归零）")
    if s.multi_turn_shared:
        levers.append("S2 前缀库：三区结构 + 指纹回归（PrefixBank）")
        if s.history_per_turn > 0:
            levers.append("S4 调度器：append-only 纪律 + 同前缀聚簇 + TTL 保活")
    else:
        levers.append("S4 调度器：批处理合并（BatchMerger，把独立请求拼成共享前缀的批量调用）")

    # L2 压缩后的场景副本
    compressed = Scene(
        name=s.name + "(compressed)",
        stable_prefix=s.stable_prefix,
        history_per_turn=s.history_per_turn,
        dynamic_tail_per_turn=max(int(s.dynamic_tail_per_turn * tail_compression), 64),
        turns=s.turns, pollution_per_turn=s.pollution_per_turn,
        pollution_mode=s.pollution_mode, lag_window=s.lag_window,
        multi_turn_shared=s.multi_turn_shared)
    v = predict(compressed)

    if v.hit_rate >= 0.95:
        levers.append("目标达成：预期输入命中率 %.1f%%" % (v.hit_rate * 100))
        return StackPlan(s.name, levers, v.hit_rate,
                         round(1 - v.hit_rate * 0.98, 3),  # hit 价按 2% miss 价
                         notes=[v.breakdown])
    if s.multi_turn_shared or s.stable_prefix > 0:
        levers.append("L1 前缀做大：仍需增长（见 budget 杠杆）")
    # 输入命中仍不可达 → 替代策略
    reason = ("输入结构性全新（动态尾巴/总输入过高），输入缓存上限 %.1f%%。"
              % (v.hit_rate * 100))
    if s.dynamic_tail_per_turn >= 2000:
        reason += ("替代：①批处理合并（BatchMerger 把 N 个独立请求拼成一次调用，"
                   "分摊前缀）；②Tier 3 响应缓存（重复查询免调用）；"
                   "③输出侧降本（thinking=False/短输出）+ off-peak 定价。")
    else:
        reason += "替代：Tier 3 响应缓存 + off-peak 定价。"
    levers.append("S5 探测层降级：" + reason)
    return StackPlan(s.name, levers, v.hit_rate,
                     round(1 - v.hit_rate * 0.98, 3),
                     unreachable_reason=reason, notes=[v.breakdown])


# ---------------------------------------------------------------- 批处理合并

class BatchMerger:
    """实时流/独立短输入场景的结构性解法：把 N 个独立请求合并为一次调用。

    数学：独立调用时输入命中 = prefix/(prefix+tail)；合并后 =
    prefix/(prefix + N*tail)。前缀越长、单条尾巴越短，合并收益越大。
    若 prefix=0 则合并无收益（无共享前缀可命中），此时收益来自
    off-peak 定价与一次性输出的调用费节省，需实测定夺。
    """

    def __init__(self, shared_system: str, max_items: int = 10):
        self.shared_system = shared_system
        self.max_items = max_items
        self._buffer: list[str] = []

    def add(self, item: str) -> None:
        self._buffer.append(item)

    def should_flush(self) -> bool:
        return len(self._buffer) >= self.max_items

    def flush(self, adapter, **gen_kw) -> tuple[str, list[str]]:
        """合并为一次调用。返回 (原始回复, 解析出的答案列表)。
        解析协议：要求模型对每条输入输出 '### <i> <answer>' 行。"""
        from cachecortex.core import GenRequest
        numbered = "\n".join(f"[{i+1}] {q}" for i, q in enumerate(self._buffer))
        messages = [
            {"role": "system",
             "content": self.shared_system +
             "\n\n对下面编号列表的每一项分别作答，输出格式：\n### <编号> <答案>\n逐行输出，不要遗漏。"},
            {"role": "user", "content": numbered},
        ]
        g = adapter.generate(GenRequest(
            messages=messages, max_tokens=max(256 * len(self._buffer), 512),
            thinking=False, temperature=0, **gen_kw))
        answers = _parse_numbered(g.text, len(self._buffer))
        self._buffer.clear()
        return g.text, answers


def _parse_numbered(text: str, n: int) -> list[str]:
    import re
    found: dict[int, str] = {}
    for m in re.finditer(r"###\s*(\d+)\)?[.、：: ]\s*(.+)", text):
        found[int(m.group(1))] = m.group(2).strip()
    return [found.get(i, "") for i in range(1, n + 1)]


# ---------------------------------------------------------------- 预热保活

class WarmupScheduler:
    """TTL 保活骨架：对活跃前缀定期发送最小请求，防止 LRU 驱逐。
    设计要点（实测/文献依据）：
      - 间隔取 TTL 的 1/2 以下（DeepSeek ~30min 不活动口径 → 4-10 分钟）
      - 保活请求本身产生费用：max_tokens=1 + thinking=False 压到最低
      - 只对"将被再次使用"的前缀保活（LRU 反面：保活太多冷前缀反而费钱）
    实现：调度回调由宿主环境注入（cron / automation / agent loop），
    本类只维护前缀注册表与预算上限。"""

    def __init__(self, max_warm_prefixes: int = 8):
        self.registry: dict[str, str] = {}   # fingerprint -> prefix text
        self.max_warm_prefixes = max_warm_prefixes
        self.spent_calls = 0

    def register(self, prefix: str) -> str:
        fp = hashlib.sha256(prefix.encode("utf-8")).hexdigest()[:16]
        self.registry[fp] = prefix
        # 超额时淘汰最旧（简化 LRU：dict 保序）
        while len(self.registry) > self.max_warm_prefixes:
            self.registry.pop(next(iter(self.registry)))
        return fp

    def keepalive_request(self, adapter, fingerprint: str) -> dict:
        prefix = self.registry.get(fingerprint)
        if prefix is None:
            return {"ok": False, "reason": "unknown fingerprint"}
        from cachecortex.core import GenRequest
        g = adapter.generate(GenRequest(
            messages=[{"role": "system", "content": prefix},
                      {"role": "user", "content": "1"}],
            max_tokens=1, thinking=False, temperature=0))
        self.spent_calls += 1
        raw = g.raw or {}
        hit = raw.get("prompt_cache_hit_tokens") or 0
        return {"ok": True, "fingerprint": fingerprint,
                "hit": hit, "miss": raw.get("prompt_cache_miss_tokens")}


def _parse_numbered_fallback(text: str, n: int) -> list[str]:  # pragma: no cover
    return BatchMerger._parse_numbered if False else _parse_numbered  # 占位防误用


# ---------------------------------------------------------------- Auto 汇总

def auto_plan_all(scenes: dict[str, Scene], tail_compression: float = 1.0) -> dict:
    return {name: plan(s, tail_compression) for name, s in scenes.items()}


def _serialize_plan(p: StackPlan) -> dict:
    return {"scene": p.scene, "levers": p.levers,
            "expected_hit_rate": p.expected_hit_rate,
            "expected_cost_ratio": p.expected_cost_ratio,
            "unreachable_reason": p.unreachable_reason, "notes": p.notes}


if __name__ == "__main__":
    from cachecortex.hitrate_budget import SCENARIOS
    plans = auto_plan_all(SCENARIOS, tail_compression=0.5)
    for name, p in plans.items():
        mark = "[是]" if p.expected_hit_rate >= 0.95 else "[否]"
        print(f"[{mark}] {name:18s} expected_hit={p.expected_hit_rate:.1%} "
              f"cost_ratio={p.expected_cost_ratio:.2f}")
        for lv in p.levers:
            print(f"      ↳ {lv}")
    with open("results/cache_optimize_stack_plans.json", "w", encoding="utf-8") as f:
        json.dump({k: _serialize_plan(v) for k, v in plans.items()},
                  f, ensure_ascii=False, indent=2)
    print("SAVED results/cache_optimize_stack_plans.json")
