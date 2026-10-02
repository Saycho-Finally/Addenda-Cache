"""CacheCortex 端到端单测（不调真实 API）。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cachecortex.core import GenRequest, Generation, cache_usage  # noqa: E402
from cachecortex.prefix_bank import (  # noqa: E402
    DynamicInPrefixSession, PrefixBankSession)
from cachecortex.cache_tiers import CachePolicy, CacheTiers  # noqa: E402
from cachecortex.hitrate_budget import SCENARIOS, Scene, predict  # noqa: E402
from cachecortex.optimize_stack import plan  # noqa: E402
from cachecortex._compat import majority_vote  # noqa: E402


class FakeAdapter:
    name = "fake"

    def __init__(self):
        self.n_calls = 0

    def generate(self, req: GenRequest) -> Generation:
        self.n_calls += 1
        return Generation(text="42", prompt_tokens=len(json.dumps(req.messages)) // 4,
                          completion_tokens=1, wall_clock=0.01, raw={})


import json  # noqa: E402


class FakeCachedAdapter(FakeAdapter):
    """模拟 Tier 1 provider：第二次起同 prompt 命中。"""

    def generate(self, req: GenRequest) -> Generation:
        g = super().generate(req)
        key = json.dumps(req.messages, ensure_ascii=False, sort_keys=True)
        seen = getattr(self, "_seen", None) or {}
        self._seen = seen
        n = seen.get(key, 0)
        seen[key] = n + 1
        if n > 0:
            g.raw = {"prompt_cache_hit_tokens": n * 1500,
                     "prompt_cache_miss_tokens": 120}
        return g


def test_prefix_bank_session():
    a = PrefixBankSession(FakeAdapter(), ["块一", "块二"])
    b = PrefixBankSession(FakeAdapter(), ["块一", "块二"])
    for i in range(5):
        a.chat(f"问题 {i}")
        b.chat(f"问题 {i}")
    v = a.validate_stability(b)
    assert v["prefix_stable"] and v["log_stable"]
    assert len(a.log) == 10
    print("OK prefix_bank: 指纹稳定 + append-only + 逐题记录")


def test_cache_usage_normalization():
    assert cache_usage({"prompt_cache_hit_tokens": 5})["hit"] == 5
    assert cache_usage({"prompt_tokens_details": {"cached_tokens": 7}})["hit"] == 7
    assert cache_usage({"cache_read_input_tokens": 9})["hit"] == 9
    print("OK cache_usage: 三 provider 口径归一化")


def test_budget_scenarios():
    v = predict(SCENARIOS["companion_long"])
    assert v.hit_rate >= 0.95, "陪伴长关系应 ≥95%"
    v2 = predict(SCENARIOS["realtime"])
    assert v2.hit_rate < 0.5, "实时流应结构性低"
    print("OK budget: 场景判定两端正确")


def test_plan_stack():
    p = plan(SCENARIOS["coding_agent"])
    assert p.expected_hit_rate >= 0.95
    assert any("PrefixBank" in lv for lv in p.levers)
    print("OK plan_stack: AutoStack 杠杆组合")


def test_majority_vote():
    top, dist = majority_vote(["a", "a", "b"])
    assert top == "a" and dist == {"a": 2, "b": 1}
    print("OK majority_vote: _compat 垫片")


def test_cache_tiers_probe():
    tiers = CacheTiers(FakeCachedAdapter())
    res = tiers.probe()
    assert res.policy.value == "auto_prefix"
    print("OK cache_tiers: Tier1 探测")


if __name__ == "__main__":
    test_prefix_bank_session()
    test_cache_usage_normalization()
    test_budget_scenarios()
    test_plan_stack()
    test_majority_vote()
    test_cache_tiers_probe()
    print("ALL_CACHECORTEX_TESTS_OK")
