"""Phase 2 对标基准：Reasonix 式负载（终端编程式会话）下的命中率实测。

两种模式：
  --sim   模拟模式（默认，零成本）：SimulatedDeepSeek 按实测机制模拟 provider 缓存
          （64 tok 块粒度 / 滞后窗 / 账号级跨会话），验证 PrefixBank 的设计命中率
  --live  真实模式（需 key）：打真实 DeepSeek API，同负载实测

负载设计（模拟 Reasonix 场景：终端编程 Agent）：
  ImmutablePrefix：system ~6,000 tok（任务说明 + 工具规范 + few-shot）
  每轮：user ~200 tok（指令）+ assistant ~300 tok（回复）
  轮数：120 轮（长会话）
  理论命中率（滞后窗定律）：1 − ~250/(6000+120×500) ≈ 99.6%

对照臂：
  A. PrefixBank 三区纪律（本次实现）
  B. 前缀污染（每轮 system 插计数器——模拟不良框架）
  C. 无预热 vs 有预热（轮 1 的冷启动差）
"""

import argparse
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cachecortex.core import GenRequest, Generation  # noqa: E402
from cachecortex.prefix_bank import DynamicInPrefixSession, PrefixBankSession  # noqa: E402

FILLER = ("这是一段用于占位的系统规范文本，内容本身不重要，长度才是变量。" * 400)


def make_system(target_tokens: int) -> str:
    return FILLER[: target_tokens * 2]  # 中文 ~0.5 字/token 的保守估计


def turn_text(i: int) -> str:
    return (f"任务 {i}：请修改模块 mod_{i % 20} 的第 {i} 号函数，"
            f"处理边界条件 case_{i}，并补充测试 test_{i}。")


class SimulatedDeepSeek:
    """按 CACHE-1/2/3 实测机制模拟 provider 缓存：
    64 tok 块粒度前缀匹配；<64 tok 不缓存；跨请求账号级共享；滞后窗忽略
    （滞后窗是"上轮内容本轮才缓存"的效应，模拟器用精确块匹配即可）。"""

    def __init__(self):
        self.blocks: dict[str, bool] = {}   # 块指纹 -> 是否已缓存
        self.name = "sim:deepseek"

    def generate(self, req: GenRequest) -> Generation:
        text = json.dumps(req.messages, ensure_ascii=False)
        # 逐 64-tok 块模拟前缀匹配（简化：字符块近似）
        hit_chars = 0
        block = 256  # ~64 tok × 4 字符
        total_chars = len(text)
        pos = 0
        while pos + block <= total_chars:
            fp = text[pos:pos + block]
            if fp in self.blocks:
                hit_chars += block
                pos += block
            else:
                break
        # 写入未缓存块（本轮的输入全部进入缓存）
        pos2 = 0
        while pos2 + block <= total_chars:
            self.blocks.setdefault(text[pos2:pos2 + block], True)
            pos2 += block
        prompt_tok = total_chars // 4
        hit_tok = hit_chars // 4
        return Generation(
            text="done", prompt_tokens=prompt_tok,
            completion_tokens=1, wall_clock=0.01,
            raw={"prompt_cache_hit_tokens": hit_tok,
                 "prompt_cache_miss_tokens": max(prompt_tok - hit_tok, 0)})


def run_arm(cls, adapter, system: str, turns: int, tag: str,
            nonce: str = "", warmup: bool = False) -> dict:
    kw = {} if cls is PrefixBankSession else {"nonce": nonce}
    s = cls(adapter, [system], max_tokens=8, **kw)
    if warmup and hasattr(s, "warmup"):
        s.warmup()
    for i in range(turns):
        s.chat(turn_text(i))
    rates = [x["rate"] for x in s.stats]
    tail = rates[-max(len(rates) // 5, 1):]
    steady = sum(tail) / len(tail)
    return {"tag": tag, "turns": turns, "rates": rates,
            "mean": round(sum(rates) / len(rates), 4),
            "steady_last20pct": round(steady, 4),
            "warmup_used": warmup}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["sim", "live"], default="sim")
    ap.add_argument("--turns", type=int, default=120)
    ap.add_argument("--prefix-tokens", type=int, default=6000)
    ap.add_argument("--key", default=None)
    args = ap.parse_args()

    if args.mode == "live":
        from cachecortex_adapter_live import make_live_adapter  # type: ignore
        adapter = make_live_adapter(args.key)
        print("live 模式：真实 DeepSeek API")
    else:
        adapter = SimulatedDeepSeek()
        print("sim 模式：SimulatedDeepSeek（实测机制建模）")

    system = make_system(args.prefix_tokens)
    turns = args.turns
    results = {}

    results["A_prefixbank"] = run_arm(PrefixBankSession, adapter, system, turns, "A")
    results["B_poisoned"] = run_arm(DynamicInPrefixSession, adapter, system, turns, "B",
                                    nonce=f"n{random.randint(10**8, 10**9)}")

    # C 预热 A/B：新会话 + warmup，看轮 1 命中率
    s = PrefixBankSession(adapter, [system], max_tokens=8)
    w = s.warmup()
    s.chat(turn_text(0))
    results["C_warmup_turn1"] = {"warmup_hit": w, "turn1": s.stats[0]}

    # 汇总
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "rates"}
                      for k, v in results.items()}, ensure_ascii=False, indent=2))
    os.makedirs("results", exist_ok=True)
    out = f"results/bench_reasonix_load_{args.mode}_{time.strftime('%m%d_%H%M')}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"mode": args.mode, "prefix_tokens_target": args.prefix_tokens,
                   "turns": turns, **results}, f, ensure_ascii=False, indent=2)
    print("SAVED", out)


if __name__ == "__main__":
    main()
