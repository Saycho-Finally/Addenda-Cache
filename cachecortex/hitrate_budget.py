"""命中率预算器：任何场景在 DeepSeek（或同机制 provider）上的缓存命中预测与杠杆建议。

理论（本项目三轮实测 + Reasonix 生产数据支撑）：

    输入命中率 ≈ 1 − (动态尾巴 + 滞后窗) / 每轮总输入

    动态尾巴：每轮必须全新的内容（新消息/检索结果/工具输出），结构性 miss
    滞后窗：  最近 1-3 个 64-tok 块的缓存写入滞后，实测 128-256 tok，固定成本
    分母：    共享前缀 + 累计历史 + 动态尾巴

    ≥95% 的充要条件：总输入 ≥ 20 × (动态尾巴 + 滞后窗)

四条杠杆（按性价比排序）：
    L1 前缀做大：把一切稳定内容（人设/规则/示例/知识/历史）搬进前缀——抬分母
    L2 尾巴做小：动态内容压缩（检索裁剪/结构化抽取）——降分子
    L3 污染归零：确定性序列化 + append-only 纪律 + 动态字段后置——消灭假 miss
    L4 场景判定：尾部天然巨大的场景（实时流/短独立查询）不硬凑 95%，改用
       响应缓存兜底 / 批处理合并 / off-peak 定价

场景模板见 SCENARIOS。
"""

from __future__ import annotations

from dataclasses import dataclass, field

LAG_WINDOW_DEFAULT = 192      # 实测 128-256 的中值（tok）


@dataclass
class Scene:
    """一个缓存场景的参数。所有长度按 token 计。"""
    name: str
    stable_prefix: int                 # Immutable Prefix（字节级稳定部分）
    history_per_turn: int              # 每轮追加进历史的量（含双方消息）
    dynamic_tail_per_turn: int         # 每轮结构性新增的 miss（新消息+触发内容+工具输出）
    turns: int                         # 评估时的累计轮数
    pollution_per_turn: int = 0        # 前缀污染（动态字段/重写导致的额外 miss），目标 0
    pollution_mode: str = "head"       # head=污染在 system 头部（时间戳/计数器）→ 全灭；
                                       # mid=污染在 system 中部 → 从污染点截断
    lag_window: int = LAG_WINDOW_DEFAULT
    multi_turn_shared: bool = True     # False=每轮独立请求（无历史累积）


@dataclass
class Verdict:
    hit_rate: float
    breakdown: dict = field(default_factory=dict)
    levers: list[str] = field(default_factory=list)
    reachable_95: bool = False


def predict(s: Scene) -> Verdict:
    """按三区模型预测稳态输入命中率，并给出到 95% 的杠杆建议。"""
    if not s.multi_turn_shared:
        # 独立请求：只有 stable_prefix 可命中（若 ≥64），尾巴与历史全 miss
        total = s.stable_prefix + s.dynamic_tail_per_turn
        if total == 0:
            return Verdict(0.0, {"error": "empty scene"})
        hit = min(s.stable_prefix, total)
        rate = hit / total
        breakdown = {"mode": "independent", "prefix": s.stable_prefix,
                     "tail": s.dynamic_tail_per_turn}
    elif s.pollution_per_turn > 0:
        # 前缀污染：断崖式杀伤（实测 poisoned 全 0），非线性叠加。
        # head=时间戳/计数器在最前 → 每轮前缀第 1 token 即分歧 → 命中率 ≈ 0
        # mid=污染在 system 中部 → 命中率 ≈ 污染点之前的前缀 / 总输入
        cum_history = s.history_per_turn * s.turns
        total_in = s.stable_prefix + cum_history + s.dynamic_tail_per_turn
        if s.pollution_mode == "head":
            hit = 0
        else:
            hit = max(s.stable_prefix // 2, 0)   # 保守估计：污染点取前缀中位
        rate = hit / total_in if total_in else 0.0
        breakdown = {"mode": "multi_turn_polluted", "pollution_mode": s.pollution_mode,
                     "hit_tok": hit, "total_in": total_in}
    else:
        # 多轮累积：第 t 轮总输入 = prefix + t*history + tail
        # 稳态（末轮）命中率 = 1 - (tail + lag + pollution累计折算) / 末轮总输入
        cum_history = s.history_per_turn * s.turns
        total_in = s.stable_prefix + cum_history + s.dynamic_tail_per_turn
        miss = s.dynamic_tail_per_turn + s.lag_window
        hit = max(total_in - miss, 0)
        rate = hit / total_in if total_in else 0.0
        breakdown = {"mode": "multi_turn", "prefix": s.stable_prefix,
                     "cum_history": cum_history, "tail": s.dynamic_tail_per_turn,
                     "lag": s.lag_window}
    verdict = Verdict(hit_rate=round(rate, 4), breakdown=breakdown)

    # 杠杆建议：要 95% 需要总输入 ≥ 20×miss
    target = 0.95
    if s.pollution_per_turn > 0 and verdict.hit_rate < target:
        verdict.levers.insert(0,
            "L3 污染归零是第一优先级：前缀污染是断崖式杀伤"
            f"（实测命中率从 ~99% 崩到 {verdict.hit_rate:.0%}），"
            "归零手段：时间戳挪 metadata、序列化加 sort_keys、历史只追加")
    if verdict.hit_rate < target and not s.pollution_per_turn:
        miss_est = verdict.breakdown.get("tail", 0) + verdict.breakdown.get("lag", 0)
        need_total = miss_est / (1 - target)
        cur_total = s.stable_prefix + s.history_per_turn * s.turns + s.dynamic_tail_per_turn
        need_prefix = need_total - cur_total
        if need_prefix > 0:
            verdict.levers.append(
                f"L1 前缀做大：共享前缀再增加 ~{int(need_prefix)} tok 的稳定内容"
                f"（few-shot/知识/规范），即可达 95%")
        if s.dynamic_tail_per_turn > need_total * 0.5:
            verdict.levers.append(
                f"L2 尾巴做小：动态尾巴 {s.dynamic_tail_per_turn} tok 过大"
                f"（检索裁剪/结构化抽取/压缩），压到 {int(need_total*0.5)} tok 以下")
        if not verdict.levers:
            verdict.levers.append(
                "L4 场景判定：动态占比结构性过高，95% 不可达。"
                "改用响应缓存兜底 / 批处理合并 / off-peak 定价")
    elif verdict.hit_rate >= target:
        verdict.reachable_95 = True
        verdict.levers.append("已达标。保持三区纪律（确定性序列化 + append-only + 动态后置）")
    return verdict


SCENARIOS: dict[str, Scene] = {
    # 情感陪伴：角色卡+人设大前缀，历史快速增长，尾巴极短
    "companion_long": Scene("情感陪伴·长关系", stable_prefix=12000,
                            history_per_turn=600, dynamic_tail_per_turn=80,
                            turns=300),
    "companion_new": Scene("情感陪伴·新关系（前 10 轮）", stable_prefix=12000,
                           history_per_turn=600, dynamic_tail_per_turn=80,
                           turns=10),
    # 编程 Agent：巨大 system+工具，历史快速增长（Reasonix 场景）
    "coding_agent": Scene("编程 Agent（DSH 类）", stable_prefix=15000,
                          history_per_turn=2500, dynamic_tail_per_turn=400,
                          turns=100),
    # RAG：固定知识库前缀 + 每轮检索尾巴较大
    "rag": Scene("RAG 文档问答", stable_prefix=8000,
                 history_per_turn=200, dynamic_tail_per_turn=2500,
                 turns=40),
    # 实验跑分（本项目）：共享说明 + 题目尾巴
    "bench": Scene("实验跑分", stable_prefix=4500,
                   history_per_turn=0, dynamic_tail_per_turn=300,
                   turns=50, multi_turn_shared=False),
    # 实时流：几乎全新输入
    "realtime": Scene("实时流/行情", stable_prefix=500,
                      history_per_turn=0, dynamic_tail_per_turn=3000,
                      turns=100, multi_turn_shared=False),
    # 有污染的 agent（反模式：时间戳进 system）
    "polluted_agent": Scene("Agent·前缀污染反模式", stable_prefix=8000,
                            history_per_turn=1500, dynamic_tail_per_turn=300,
                            turns=60, pollution_per_turn=50),
    # 多 Agent fan-out：编排者+N 子代理共享前缀（等效于前缀只付一次）
    "multi_agent_fanout": Scene("多 Agent fan-out（共享前缀）", stable_prefix=12000,
                                history_per_turn=800, dynamic_tail_per_turn=600,
                                turns=80),
    # 群聊 Join 模式（全卡合并，顺序确定）：大前缀 + 群聊历史
    "group_chat_join": Scene("群聊·Join 模式（多角色卡合并）", stable_prefix=9000,
                             history_per_turn=700, dynamic_tail_per_turn=150,
                             turns=200),
    # 群聊 Swap 模式（每轮换卡）：前缀每轮全变——反模式
    "group_chat_swap": Scene("群聊·Swap 模式（每轮换卡，反模式）", stable_prefix=1200,
                             history_per_turn=700, dynamic_tail_per_turn=900,
                             turns=200, pollution_per_turn=900, pollution_mode="head"),
    # 工具密集循环（ReAct/MCP）：大前缀 + 大工具结果尾巴
    "tool_heavy_loop": Scene("工具密集循环（ReAct/MCP）", stable_prefix=10000,
                             history_per_turn=1200, dynamic_tail_per_turn=1500,
                             turns=80),
}


def run_all() -> dict:
    out = {}
    for name, s in SCENARIOS.items():
        v = predict(s)
        out[name] = {
            "scene": s.name,
            "hit_rate": v.hit_rate,
            "reachable_95": v.reachable_95,
            "levers": v.levers,
            "breakdown": v.breakdown,
        }
    return out


if __name__ == "__main__":
    import json
    result = run_all()
    for name, r in result.items():
        mark = "[是]" if r["reachable_95"] else "[否]"
        print(f"[{mark}] {name:18s} hit={r['hit_rate']:.1%}  {r['scene']}")
        for lv in r["levers"]:
            print(f"      ↳ {lv}")
    with open("results/hitrate_budget.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("SAVED results/hitrate_budget.json")
