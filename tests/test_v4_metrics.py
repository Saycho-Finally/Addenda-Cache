"""v0.4 测试：按工作负载拆分的缓存口径与 TTFT 位移（C1 / C2）。

核心断言：**聚合口径会掩盖主负载的缓存侵蚀**——所以拆分口径必须独立可读，
且低命中负载要被显式标出，而不是被平均掉。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cachecortex.metrics import (LOW_HIT_THRESHOLD, CacheSample,  # noqa: E402
                                 diagnose, percentile, report, ttft_shift)

RESULTS = []


def check(name, cond, note=""):
    s = "PASS" if cond else "FAIL"
    RESULTS.append((name, s))
    print(f"  [{s}] {name}" + (f" ---- {note}" if note else ""))


STABLE = [CacheSample("stable_agent", 10000, 9000, 300) for _ in range(4)]
RESEARCH = [CacheSample("research_agent", 10000, 500, 1200) for _ in range(4)]


def test_percentile():
    print("分位数")
    xs = [10, 20, 30, 40]
    check("p50 线性插值 = 25", percentile(xs, 50) == 25.0, str(percentile(xs, 50)))
    check("p95 线性插值 = 38.5", percentile(xs, 95) == 38.5, str(percentile(xs, 95)))
    check("单元素", percentile([7], 95) == 7.0)
    check("空列表 = 0", percentile([], 50) == 0.0)


def test_split():
    print("按工作负载拆分")
    rep = report(STABLE + RESEARCH)
    check("两类负载各自成组",
          set(rep.by_workload) == {"stable_agent", "research_agent"},
          str(list(rep.by_workload)))
    check("稳定负载占比 0.9",
          rep.by_workload["stable_agent"]["cached_read_share"] == 0.9,
          str(rep.by_workload["stable_agent"]["cached_read_share"]))
    check("研究负载占比 0.05",
          rep.by_workload["research_agent"]["cached_read_share"] == 0.05)
    check("聚合口径被平均到 0.475（掩盖了两端差异）",
          rep.overall["cached_read_share"] == 0.475,
          str(rep.overall["cached_read_share"]))
    check("低命中负载被标出（默认阈值 0.4）",
          rep.low_hit_workloads == ["research_agent"],
          str(rep.low_hit_workloads))
    check("阈值可配：调低到 0.01 则不再告警",
          report(STABLE + RESEARCH, low_hit_threshold=0.01).low_hit_workloads == [])


def test_ttft_and_tokens():
    print("TTFT 与 token 口径")
    rep = report(STABLE)
    check("缓存读取 token 累计",
          rep.overall["cached_read_tokens"] == 36000)
    check("输入 token 累计", rep.overall["input_tokens"] == 40000)
    check("TTFT p50 有值", rep.overall["ttft_p50_ms"] == 300.0)
    check("TTFT 为 0 的样本不进分位数",
          report([CacheSample("w", 100, 50, 0)]).overall["ttft_p50_ms"] == 0.0)

    sh = ttft_shift([1000, 1200, 1400], [700, 800, 900])
    check("位移为正表示变快", sh["p50_shift_ms"] > 0, str(sh["p50_shift_ms"]))
    check("p50 位移 = 1200-800 = 400", sh["p50_shift_ms"] == 400.0,
          str(sh["p50_shift_ms"]))
    check("位移百分比被给出", abs(sh["p50_shift_pct"] - 1 / 3) < 1e-3,
          str(sh["p50_shift_pct"]))
    check("p95 位移也给出", "p95_shift_ms" in sh)
    check("空样本报错", "error" in ttft_shift([], [1]))


def test_diagnose():
    print("诊断输出")
    notes = diagnose(report(STABLE + RESEARCH))
    joined = " ".join(notes)
    check("指出低命中负载", "research_agent" in joined, joined[:60])
    check("提示检查前缀动态内容", "动态内容" in joined)
    check("指出聚合值掩盖差异", "掩盖" in joined, joined[:80])
    clean = diagnose(report(STABLE))
    check("无问题时明确说明未被掩盖",
          any("未掩盖" in n or "未掩盖问题" in n for n in clean), str(clean))

    r = report([])
    check("空样本集 n=0", r.overall["n"] == 0)
    check("to_dict 结构完整",
          set(r.to_dict()) == {"overall", "by_workload", "low_hit_workloads"})


if __name__ == "__main__":
    print("=" * 62)
    print("PPBExt-Cache v0.4：按工作负载拆分的口径 + TTFT 位移")
    print("=" * 62)
    test_percentile()
    test_split()
    test_ttft_and_tokens()
    test_diagnose()
    print("=" * 62)
    n_pass = sum(1 for r in RESULTS if r[1] == "PASS")
    print(f"总计：{n_pass}/{len(RESULTS)} PASS")
    print("CACHE_V4_OK" if n_pass == len(RESULTS) else "CACHE_V4_FAIL")
    sys.exit(0 if n_pass == len(RESULTS) else 1)
