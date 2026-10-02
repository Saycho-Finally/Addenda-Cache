"""CacheTiers：缓存能力探测与分层降级策略（"适配任何运行环境和模型"的核心）。

设计目标：不限于特定 provider，任何运行环境与模型均可适配：

  Tier 1  AUTO_PREFIX      provider 自动前缀缓存（DeepSeek / OpenAI / 自建 vLLM/SGLang）
                           → 客户端只需前缀编排（PrefixBank），无需改请求结构
  Tier 2  EXPLICIT_MARKER  provider 显式标记（Anthropic cache_control / Gemini explicit）
                           → 自动在 system 末块注入 cache_control
  Tier 3  RESPONSE_FALLBACK provider 无任何缓存
                           → 客户端 exact-match 响应缓存兜底（同请求直接返回）

  探测：CacheTiers.probe() 发两次 ≥1024 tok 的同前缀请求，按 usage 字段判层。
  探测结果按 (provider, model) 缓存；探测成本 ~$0.01（off-peak flash）。

诚实边界：
  - 探测利用的是 usage 字段的有无与数值，字段口径随 provider 版本变动，探测结果
    附原始 usage 落盘供人工复核；
  - Tier 3 的响应缓存对 temperature>0 的采样任务会破坏独立性（同题返回同答案），
    默认禁止用于采样场景，仅用于确定性任务（temperature=0 / 分类 / 抽取）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum

from cachecortex.core import GenRequest, ModelAdapter


class CachePolicy(str, Enum):
    AUTO_PREFIX = "auto_prefix"
    EXPLICIT_MARKER = "explicit_marker"
    RESPONSE_FALLBACK = "response_fallback"


@dataclass
class ProbeResult:
    policy: CachePolicy
    evidence: dict = field(default_factory=dict)


def _cache_fields(usage: dict) -> dict:
    """跨 provider 的缓存字段归一化（Known 口径，2026-10）：
    DeepSeek  prompt_cache_hit_tokens / prompt_cache_miss_tokens
    OpenAI    prompt_tokens_details.cached_tokens
    Anthropic cache_read_input_tokens / cache_creation_input_tokens
    """
    out = {"hit": 0, "write": 0, "raw_keys": sorted(k for k in usage if "cache" in k.lower())}
    out["hit"] = int(
        usage.get("prompt_cache_hit_tokens")
        or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        or usage.get("cache_read_input_tokens")
        or 0)
    out["write"] = int(usage.get("cache_creation_input_tokens") or 0)
    return out


class CacheTiers:
    """探测 + 策略分发。一个 provider/model 组合只探测一次。"""

    def __init__(self, adapter: ModelAdapter, probe_tokens: int = 1200):
        self.adapter = adapter
        self.probe_tokens = probe_tokens
        self._probes: dict[str, ProbeResult] = {}
        self._resp_cache: dict[str, dict] = {}   # Tier 3 兜底

    # ------------------------------------------------ 探测

    def probe(self, force: bool = False) -> ProbeResult:
        key = self.adapter.name
        if not force and key in self._probes:
            return self._probes[key]
        # 共享前缀 ≥门槛（OpenAI 1024 / Anthropic 1024 / DeepSeek 实测 64 块）
        filler = ("缓存能力探测占位文本。" * 500)[: self.probe_tokens * 3]
        req = GenRequest(messages=[{"role": "system", "content": filler},
                                   {"role": "user", "content": "回答 OK"}],
                         max_tokens=8, thinking=False, temperature=0)
        g1 = self.adapter.generate(req)
        g2 = self.adapter.generate(req)
        u1, u2 = g1.raw or {}, g2.raw or {}
        c1, c2 = _cache_fields(u1), _cache_fields(u2)
        # 判层：第二次调用出现命中 → provider 有前缀缓存（自动生效）
        if c2["hit"] > 0 or c2["write"] > 0:
            policy = CachePolicy.AUTO_PREFIX
        elif any("cache" in k.lower() for k in c2["raw_keys"]):
            policy = CachePolicy.AUTO_PREFIX
        else:
            # 无任何缓存字段：可能 provider 无缓存，或 usage 未回传。
            # 保守降级到响应兜底，并附证据供人工复核。
            policy = CachePolicy.RESPONSE_FALLBACK
        res = ProbeResult(policy=policy, evidence={
            "call1": c1, "call2": c2,
            "note": "第二次同前缀调用出现 hit/write 字段即判 Tier 1；"
                    "无缓存字段判 Tier 3（可能只是 usage 未回传，附证据人工复核）",
        })
        self._probes[key] = res
        return res

    # ------------------------------------------------ 策略执行

    def chat(self, system_blocks: list[str], user_content: str,
             log: list[dict] | None = None, **gen_kw) -> "Generation":
        """按探测到的层级执行一次调用。
        system_blocks: 稳定前缀块（字节级冻结，调用方保证不修改）
        log: Append-Only 历史（多轮时传入，只追加）"""
        policy = self.probe().policy
        system = "\n\n".join(system_blocks)
        messages = [{"role": "system", "content": system}]
        if log:
            messages.extend(log)
        messages.append({"role": "user", "content": user_content})

        if policy is CachePolicy.EXPLICIT_MARKER:
            messages = self._inject_marker(messages)

        cache_key = None
        if policy is CachePolicy.RESPONSE_FALLBACK:
            cache_key = hashlib.sha256(
                json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            if cache_key in self._resp_cache and gen_kw.get("temperature", 0) == 0:
                hit = self._resp_cache[cache_key]
                hit.from_response_cache = True
                return hit

        g = self.adapter.generate(GenRequest(messages=messages, **gen_kw))
        if policy is CachePolicy.RESPONSE_FALLBACK and cache_key:
            self._resp_cache[cache_key] = g
        # 每次调用的缓存证据都挂在 raw 上（adapter 已采集 DeepSeek 字段）
        g.raw = {**(g.raw or {}), "cache_policy": policy.value}
        return g

    @staticmethod
    def _inject_marker(messages: list[dict]) -> list[dict]:
        """Anthropic 式显式标记：system 末块加 cache_control（ephemeral）。
        仅当 provider 走 Anthropic 消息格式时有效；OpenAI 格式下为无害透传字段，
        由具体 adapter 决定是否剥离。"""
        out = [dict(m) for m in messages]
        for m in out:
            if m.get("role") == "system":
                m["cache_control"] = {"type": "ephemeral"}
                break  # 只标记第一个 system 块
        return out
