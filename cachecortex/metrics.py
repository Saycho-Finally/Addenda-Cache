"""缓存效果口径：按工作负载拆分 + 两个业界主指标（C1 / C2）。

**为什么必须按工作负载拆开看**：聚合命中率会把稳定负载的高命中与不稳定负载的低命中
平均掉。业界一致的工程结论是——某条主负载命中率低于 40% 时，几乎总是"前缀里混进了
动态内容"（时间戳、用户 ID、增长的工作记忆），而聚合数字会把这条信号稀释掉。
所以本模块默认**同时输出聚合与拆分两份**，并把低命中负载显式标出来。

四个口径：

    cached_read_share   缓存读取 token / 输入 token —— 判"缓存边界有没有盖住正文"
    ttft_p50 / p95      首 token 延迟分位数 —— 命中应把 p50/p95 一起压低
    ttft_shift          启用缓存前后 p50/p95 的位移（正数 = 变快）
    block_hit_rate      复用 cache_metric 的块级口径（若调用方提供块级数据）

零依赖、纯确定性。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 低命中告警阈值的工程经验值：稳定负载低于此值通常意味着前缀里有动态内容
LOW_HIT_THRESHOLD = 0.4


@dataclass
class CacheSample:
    """一次调用的缓存相关计量。"""
    workload: str                  # 工作负载标识（如 "stable_agent" / "research_agent"）
    input_tokens: int
    cached_read_tokens: int = 0
    ttft_ms: float = 0.0


@dataclass
class CacheReport:
    overall: dict = field(default_factory=dict)
    by_workload: dict = field(default_factory=dict)
    low_hit_workloads: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"overall": self.overall, "by_workload": self.by_workload,
                "low_hit_workloads": self.low_hit_workloads}


def percentile(values: list[float], p: float) -> float:
    """线性插值分位数（与常见库的默认口径一致）。"""
    if not values:
        return 0.0
    xs = sorted(values)
    if len(xs) == 1:
        return round(float(xs[0]), 3)
    k = (len(xs) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 3)


def _agg(samples: list[CacheSample]) -> dict:
    n = len(samples)
    in_tok = sum(s.input_tokens for s in samples)
    hit_tok = sum(s.cached_read_tokens for s in samples)
    ttfts = [s.ttft_ms for s in samples if s.ttft_ms > 0]
    return {
        "n": n,
        "input_tokens": in_tok,
        "cached_read_tokens": hit_tok,
        "cached_read_share": round(hit_tok / in_tok, 4) if in_tok else 0.0,
        "ttft_p50_ms": percentile(ttfts, 50),
        "ttft_p95_ms": percentile(ttfts, 95),
    }


def report(samples: list[CacheSample],
           low_hit_threshold: float = LOW_HIT_THRESHOLD) -> CacheReport:
    """聚合口径 + 按工作负载拆分口径；并标出低命中负载。"""
    groups: dict[str, list[CacheSample]] = {}
    for s in samples:
        groups.setdefault(s.workload, []).append(s)
    by_wl = {w: _agg(v) for w, v in sorted(groups.items())}
    overall = _agg(samples)
    low = sorted(w for w, a in by_wl.items()
                 if a["cached_read_share"] < low_hit_threshold)
    return CacheReport(overall=overall, by_workload=by_wl, low_hit_workloads=low)


def ttft_shift(before: list[float], after: list[float]) -> dict:
    """启用缓存前后的 TTFT 位移。正数表示变快。"""
    if not before or not after:
        return {"error": "两组 TTFT 样本都不能为空"}
    b50, a50 = percentile(before, 50), percentile(after, 50)
    b95, a95 = percentile(before, 95), percentile(after, 95)
    return {
        "p50_before_ms": b50, "p50_after_ms": a50,
        "p50_shift_ms": round(b50 - a50, 3),
        "p50_shift_pct": round((b50 - a50) / b50, 4) if b50 else 0.0,
        "p95_before_ms": b95, "p95_after_ms": a95,
        "p95_shift_ms": round(b95 - a95, 3),
        "p95_shift_pct": round((b95 - a95) / b95, 4) if b95 else 0.0,
    }


def diagnose(rep: CacheReport) -> list[str]:
    """把口径翻译成可执行的判断（只在有信号时给结论）。"""
    notes: list[str] = []
    o = rep.overall
    if o["n"] == 0:
        return ["无样本"]
    if rep.low_hit_workloads:
        notes.append(
            f"低命中负载：{rep.low_hit_workloads}——优先检查其前缀是否混入动态内容"
            f"（时间戳 / 用户 ID / 增长的工作记忆）")
    agg = o["cached_read_share"]
    if rep.by_workload:
        best = max(a["cached_read_share"] for a in rep.by_workload.values())
        worst = min(a["cached_read_share"] for a in rep.by_workload.values())
        if best - worst > 0.3:
            notes.append(
                f"负载间差异显著（{worst:.2f} ~ {best:.2f}）："
                f"聚合值 {agg:.2f} 掩盖了差异，应按负载分别优化")
    if not notes:
        notes.append("各负载命中率接近且不低于阈值，聚合口径未掩盖问题")
    return notes
