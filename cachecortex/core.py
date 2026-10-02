"""CacheCortex：三区提示词缓存编排库（核心类型与协议）。

设计目标：任何 OpenAI 兼容端点（DeepSeek/OpenAI/vLLM/...）接入即得
三区会话管理 + provider 能力探测 + 命中率预算。核心零依赖。

三层划分（与 provider 的前缀缓存机制对齐）：
  Response cache      整请求去重（正交，另行实现）
  Prompt orchestration 本库的核心：三区会话 + 探测 + 预算
  Engine KV cache      vLLM/SGLang/LMCache（引擎内部，本库只出配置建议）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class GenRequest:
    """一次生成请求的最小描述（provider 无关）。"""
    messages: list[dict]
    max_tokens: int = 1024
    temperature: float = 0.0
    thinking: bool = True          # DeepSeek 等：是否启用思考模式
    effort: str | None = None      # reasoning_effort 旋钮（off/low/medium/high/max）
    extra: dict = field(default_factory=dict)


@dataclass
class Generation:
    """一次生成的结果与缓存证据。"""
    text: str
    reasoning: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int | None = None
    wall_clock: float = 0.0
    truncated: bool = False
    raw: dict = field(default_factory=dict)   # 原始 usage（含 provider 缓存字段）


class ModelAdapter(Protocol):
    """provider 适配协议：实现 generate 即可接入 CacheCortex。"""

    name: str

    def generate(self, req: GenRequest) -> Generation: ...


def cache_usage(usage: dict) -> dict:
    """跨 provider 的缓存字段归一化（hit/write，token 数）。
    已知口径：DeepSeek（prompt_cache_hit/miss_tokens）、
    OpenAI（prompt_tokens_details.cached_tokens）、
    Anthropic（cache_read_input_tokens / cache_creation_input_tokens）。"""
    hit = int(
        usage.get("prompt_cache_hit_tokens")
        or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        or usage.get("cache_read_input_tokens")
        or 0)
    write = int(usage.get("cache_creation_input_tokens") or 0)
    return {"hit": hit, "write": write}


__all__ = ["GenRequest", "Generation", "ModelAdapter", "cache_usage"]
