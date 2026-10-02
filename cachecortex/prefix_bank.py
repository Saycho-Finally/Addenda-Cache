"""PrefixBank：缓存感知会话层（三区结构的最小实现，对齐 Reasonix 四机制）。

目标：接入 DeepSeek，让多轮会话的每一次调用都吃到 90%+ 输入缓存命中。

三区结构（与 Reasonix / Claude Code / tRPC-Agent-Go 的实践收敛）：
  Immutable Prefix  system 块列表，字节级稳定，永不修改（缓存的地基）
  Append-Only Log   历史消息只追加不重写（多轮前缀持续命中）
  Dynamic Tail      每轮新输入，永远在最后（唯一 miss 的部分）

Reasonix 的四个机制与本实现的对应：
  ImmutablePrefix      → system_blocks 拼接后冻结 + prefix_fingerprint（SHA-256 校验）
  AppendOnlyLog        → log 只 append，提供 log_fingerprint 供回归测试
  VolatileScratch      → 思考/草稿不上传（adapter 侧 thinking=False 或不回传 reasoning）
  Auto-compact         → TODO（接近上下文上限时折叠，折叠后冷 miss 一次属预期）

反模式对照：DynamicInPrefixSession 在每轮 system 前部插入递增计数器，
演示"前缀污染"如何把命中率打穿——一正一反，教学与回归两用。

DeepSeek 缓存实测参数（2026-10-01，见 results/cache_probe_*.json）：
  块粒度 64 token；<64 tok 不缓存；分歧截断到块边界；稳态命中率 = 前缀/(前缀+尾巴)。
"""

from __future__ import annotations

import hashlib
import json
import time

from cachecortex.core import GenRequest, ModelAdapter


class PrefixBankSession:
    """缓存友好会话。system 块只写一次，历史只追加。"""

    def __init__(self, adapter: ModelAdapter, system_blocks: list[str],
                 max_tokens: int = 512):
        self.adapter = adapter
        self.max_tokens = max_tokens
        # Immutable Prefix：拼接一次，此后字节级冻结
        self._system = "\n\n".join(system_blocks)
        self.prefix_fingerprint = self._fingerprint(self._system)
        self.log: list[dict] = []          # Append-Only Log
        self.stats: list[dict] = []        # 逐次命中率记录
        self.read_tracker: set[str] = set()  # Layer 4：本轮已读资源集合（fold 后重置）
        self.warmups: list[dict] = []      # 预热记录
        self.folds: list[dict] = []        # 折叠记录

    @staticmethod
    def _fingerprint(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    def log_fingerprint(self) -> str:
        """AppendOnlyLog 的指纹。回归测试用：任意两次构建同状态会话，指纹必须一致。"""
        import hashlib
        blob = json.dumps(self.log, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def validate_stability(self, other: "PrefixBankSession") -> dict:
        """字节稳定性校验：两个同配置会话的 prefix 与 log 指纹必须一致。
        任何不一致都意味着序列化不确定性（缓存杀手）。"""
        return {
            "prefix_stable": self.prefix_fingerprint == other.prefix_fingerprint,
            "log_stable": self.log_fingerprint() == other.log_fingerprint(),
            "prefix_fp": self.prefix_fingerprint,
        }

    def _messages(self, user_content: str) -> list[dict]:
        msgs = [{"role": "system", "content": self._system}]
        msgs.extend(self.log)
        msgs.append({"role": "user", "content": user_content})
        return msgs

    def chat(self, user_content: str, temperature: float = 0.0) -> str:
        g = self.adapter.generate(GenRequest(
            messages=self._messages(user_content),
            max_tokens=self.max_tokens, thinking=False,
            temperature=temperature))
        self._record(g)
        # Append-Only：只追加，不重写历史。assistant 只回传最终答案（官方：非工具轮 reasoning 无需回传）
        self.log.append({"role": "user", "content": user_content})
        self.log.append({"role": "assistant", "content": g.text})
        return g.text

    def _record(self, g) -> None:
        raw = g.raw or {}
        hit = raw.get("prompt_cache_hit_tokens") or 0
        miss = raw.get("prompt_cache_miss_tokens") or g.prompt_tokens
        rate = hit / max(hit + miss, 1)
        self.stats.append({"turn": len(self.stats) + 1, "prompt": g.prompt_tokens,
                           "hit": hit, "miss": miss, "rate": round(rate, 4)})

    # ------------------------------------------------ ReadTracker（第四层）

    def track_read(self, key: str) -> None:
        """记录本轮已读资源（Reasonix ReadTracker 层）。
        用途：编辑门控（已读才允许改）、fold 后重建读集合。会话级，fold 后重置。"""
        self.read_tracker.add(key)

    def read_snapshot(self) -> frozenset:
        return frozenset(self.read_tracker)

    # ------------------------------------------------ Warmup（冷启动消灭）

    def warmup(self, temperature: float = 0.0) -> dict:
        """预热：发一个最小请求把 ImmutablePrefix 写进 provider 缓存。
        效果：之后的正式首条消息即命中前缀（消灭冷启动 miss）。
        成本：一次 max_tokens=1 的调用（输入全 miss，输出 1 tok）。"""
        from cachecortex.core import GenRequest as _GR
        g = self.adapter.generate(_GR(
            messages=[{"role": "system", "content": self._system},
                      {"role": "user", "content": "1"}],
            max_tokens=1, thinking=False, temperature=temperature))
        raw = g.raw or {}
        hit = raw.get("prompt_cache_hit_tokens") or 0
        miss = raw.get("prompt_cache_miss_tokens") or g.prompt_tokens
        self.warmups.append({"ts": time.time(), "hit": hit, "miss": miss,
                             "rate": round(hit / max(hit + miss, 1), 4)})
        return self.warmups[-1]

    # ------------------------------------------------ Auto-compact（缓存友好折叠）

    def fold(self, summarizer, keep_recent: int = 4) -> dict:
        """缓存友好折叠：旧轮摘要成一条 summary 消息，与保留的最近 keep_recent 轮
        组成新 AppendOnlyLog。关键约束：
          - summary 由 summarizer(summary_text) 生成一次，此后字节冻结（成为新前缀的一部分）
          - 折叠后的第一轮请求会冷 miss 新前缀（一次性的），之后恢复稳态命中
          - 被折叠的旧消息从 log 移除，但 ReadTracker 重置（Reasonix 语义）
        summarizer: callable(text) -> str，由宿主提供（可用任意 LLM/规则）。
        返回统计。"""
        if len(self.log) <= keep_recent * 2:
            return {"folded": False, "reason": "log too short"}
        keep = self.log[-keep_recent * 2:]
        old = self.log[:-keep_recent * 2]
        old_text = "\n".join(f"[{m['role']}] {m['content']}" for m in old)
        summary_text = summarizer(old_text)
        summary_msg = {"role": "user",
                       "content": f"[历史摘要]\n{summary_text}\n[摘要结束] 请基于摘要与后续对话继续。"}
        self.log = [summary_msg, *keep]
        self.read_tracker.clear()
        self.folds.append({"ts": time.time(), "folded_msgs": len(old),
                           "log_len_after": len(self.log)})
        return {"folded": True, "folded_msgs": len(old),
                "log_len_after": len(self.log)}

    def summary_fingerprint(self) -> str:
        """当前 log 的指纹（fold 后变化——新前缀的起点）。"""
        return self.log_fingerprint()


class DynamicInPrefixSession(PrefixBankSession):
    """反模式对照：每轮在 system 前部插入递增计数器（时间戳类污染的等价物）。
    预期：每轮前缀从计数器处即分歧 → 命中率趋近 0。
    nonce：随机会话前缀，避免跨会话缓存残留污染对照（DeepSeek 缓存账号级共享，
    同序列重跑会命中上一次的缓存，导致反模式验证失真——2026-10-01 实测教训）。"""

    def __init__(self, adapter: ModelAdapter, system_blocks: list[str],
                 max_tokens: int = 512, nonce: str = ""):
        super().__init__(adapter, system_blocks, max_tokens)
        self._nonce = nonce

    def chat(self, user_content: str, temperature: float = 0.0) -> str:
        poisoned = f"[{self._nonce}][turn {len(self.stats)+1}] {self._system}"
        g = self.adapter.generate(GenRequest(
            messages=[{"role": "system", "content": poisoned}] + self.log
            + [{"role": "user", "content": user_content}],
            max_tokens=self.max_tokens, thinking=False, temperature=temperature))
        raw = g.raw or {}
        hit = raw.get("prompt_cache_hit_tokens") or 0
        miss = raw.get("prompt_cache_miss_tokens") or g.prompt_tokens
        rate = hit / max(hit + miss, 1)
        self.stats.append({"turn": len(self.stats) + 1, "prompt": g.prompt_tokens,
                           "hit": hit, "miss": miss, "rate": round(rate, 4)})
        self.log.append({"role": "user", "content": user_content})
        self.log.append({"role": "assistant", "content": g.text})
        return g.text
