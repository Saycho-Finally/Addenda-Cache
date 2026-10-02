"""300 轮大分母实测：把稳态命中率推上 99.5%+ 的最终证据。
只跑 A 臂（PrefixBank 三区纪律），B/C 已由 bench_reasonix_load.py 覆盖。
progress 逐题落盘。"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from cachecortex_adapter_live import make_live_adapter  # noqa: E402
from cachecortex.prefix_bank import PrefixBankSession  # noqa: E402

TURN_TEXT = ("任务 {i}：请修改模块 mod_{mod} 的第 {i} 号函数，"
             "处理边界条件 case_{i}，并补充测试 test_{i}。")
FILLER = ("这是一段用于占位的系统规范文本，内容本身不重要，长度才是变量。" * 400)


def turn_text(i: int) -> str:
    return TURN_TEXT.format(i=i, mod=i % 20)


def main() -> None:
    key = sys.argv[1]
    turns = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    prefix_tokens = int(sys.argv[3]) if len(sys.argv) > 3 else 6000
    adapter = make_live_adapter(key)
    system = FILLER
    while len(system) < prefix_tokens * 2:
        system += FILLER
    system = system[: prefix_tokens * 2]

    class RetryAdapter:
        """SSLError/瞬时网络错误重试（3 次，指数退避）。"""
        def __init__(self, inner):
            self.inner = inner
            self.name = inner.name

        def generate(self, req):
            last = None
            for attempt in range(3):
                try:
                    return self.inner.generate(req)
                except Exception as e:   # noqa: BLE001
                    last = e
                    time.sleep(2 ** attempt)
            raise last
    s = PrefixBankSession(RetryAdapter(adapter), [system], max_tokens=8)
    pf = f"results/progress_bench300_{time.strftime('%m%d_%H%M')}.jsonl"
    print(f"实测启动：prefix≈{prefix_tokens} tok × {turns} 轮 → progress: {pf}", flush=True)
    for i in range(turns):
        s.chat(turn_text(i))
        r = s.stats[-1]
        if (i + 1) % 25 == 0:
            print(f"  turn {r['turn']:3d} prompt={r['prompt']:6d} hit={r['hit']:6d} "
                  f"rate={r['rate']:.4f}", flush=True)
    rates = [x["rate"] for x in s.stats]
    tail = rates[-50:]
    out = {"turns": turns,
           "mean": round(sum(rates) / len(rates), 4),
           "steady_last50": round(sum(tail) / len(tail), 4),
           "last": rates[-1],
           "stats": s.stats}
    os.makedirs("results", exist_ok=True)
    path = f"results/bench300_{time.strftime('%m%d_%H%M')}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"RESULT mean={out['mean']} steady_last50={out['steady_last50']} last={out['last']}")
    print("SAVED", path)


if __name__ == "__main__":
    main()
