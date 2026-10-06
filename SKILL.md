---
name: ppb-cache
description: "LLM 前缀缓存的应用侧编排：三定律、provider 能力探测分层与场景预算。适用于「多轮会话成本高」「想提升前缀缓存命中率」「跨 provider 需要统一纪律」的场景。"
---

# ppb-cache

## 能力

- 三定律实测刻画（滞后窗 / 块级匹配 / 账号级跨会话复用）
- 三区会话编排（ImmutablePrefix + AppendOnlyLog + VolatileScratch）
- CacheTiers 能力探测（自动前缀 / 显式标记 / 无缓存三档的运行时判定）
- 11 类负载的命中率预算模型 + 场景化杠杆组合（AutoStack）

## 何时使用

- 多轮对话 / agent 循环的前缀复用（实测稳态命中率 99.81% @ 103K 分母）
- 需要按场景（成本/延迟/合规）选择缓存策略
- 跨 provider 迁移时需要能力探测

## 接口

```python
from cachecortex import (PrefixBankSession, DynamicInPrefixSession,
                         CacheTiers, CachePolicy, Scene, predict, plan_stack)
```

MCP 形态（4 个离线工具，不触发真实 API 调用）：
`cache_hitrate_predict` / `cache_stack_recommend` / `cache_tier_guide` /
`cache_workload_report`。

> 修正记录（2026-10-07）：本节此前写作 `ThreeRegionSession, CacheTiers, HitrateBudget`
> ——其中 `ThreeRegionSession` 与 `HitrateBudget` **在包内不存在**，照抄会 ImportError。
> 已按 `cachecortex/__init__.py` 的实际 `__all__` 更正。

## 边界

三定律为特定 provider 的实测刻画，跨架构（如 SSM）需重新探测。

---

*本技能为 PPBExt-Cache 仓库的 agent 可加载形态（SKILL.md 标准）。完整文档与数据见仓库 README。*
