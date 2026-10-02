"""live 适配器：复用 exocortex 的 OpenAICompatAdapter（已验证 thinking 开关与
DeepSeek usage 缓存字段透传）。负责 cachecortex.GenRequest -> exocortex.GenRequest
的字段转换（exocortex 版多一个 seed 字段）。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from exocortex.adapter import OpenAICompatAdapter  # noqa: E402
from exocortex.adapter import GenRequest as _ExGenRequest  # noqa: E402


class LiveAdapter:
    """把 cachecortex 的 GenRequest 桥接到 exocortex 的 OpenAICompatAdapter。"""

    def __init__(self, inner):
        self.inner = inner
        self.name = inner.name

    def generate(self, req):
        ex_req = _ExGenRequest(
            messages=req.messages,
            max_tokens=req.max_tokens,
            temperature=req.temperature,
            thinking=req.thinking,
            seed=None,
        )
        return self.inner.generate(ex_req)


def make_live_adapter(key: str, model: str = "deepseek-flash",
                      base_url: str = "https://api.deepseek.com"):
    return LiveAdapter(OpenAICompatAdapter(base_url, key, model))
