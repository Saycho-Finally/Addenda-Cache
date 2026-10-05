"""CacheCortex：三区提示词缓存编排库。

快速上手：
    from cachecortex import PrefixBankSession, predict, cache_usage

三定律（一手实测，见 README）：
    滞后窗定律  命中率 = 1 − 滞后窗/总输入（滞后窗 ~128-256 tok 固定）
    块级匹配    64-token 块粒度，分歧截断到块边界，<64 tok 不缓存
    账号级复用  缓存跨进程/会话存活数小时
"""

from cachecortex.core import GenRequest, Generation, ModelAdapter, cache_usage
from cachecortex.prefix_bank import PrefixBankSession, DynamicInPrefixSession
from cachecortex.cache_tiers import CachePolicy, CacheTiers
from cachecortex.hitrate_budget import Scene, predict
from cachecortex.optimize_stack import plan as plan_stack

__version__ = "0.2.0"

__all__ = [
    "GenRequest", "Generation", "ModelAdapter", "cache_usage",
    "PrefixBankSession", "DynamicInPrefixSession",
    "CachePolicy", "CacheTiers",
    "Scene", "predict", "plan_stack",
    "__version__",
]
