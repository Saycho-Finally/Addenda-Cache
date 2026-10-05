# PPBExt-Cache

**一句话**：DeepSeek 的前缀缓存命中，服务端只提供能力，**命中率是客户端的设计决策**——本项目把"客户端可观测、可预测、可编排"的部分形式化为一个 Python 库（CacheCortex）和三条经验定律，在真实 API 上实测稳态输入命中率 **98.3%~99.8%**（随负载分母变化，与预测公式一致），并给出覆盖 11 类负载的命中率预算表。

> 作者：Saycho-Finally（独立研究者） ｜ AI 使用声明见 [AI_DISCLOSURE.md](AI_DISCLOSURE.md) ｜ License: MIT ｜ 零依赖 ｜ Python ≥3.10 ｜ 技术报告：[reports/三定律技术报告.md](reports/三定律技术报告.md)

---

## 这个库能做什么（三件事，均有实测数字）

1. **三区会话状态机**——immutable prefix（字节级冻结）/ append-only log（只追加）/ dynamic tail（每轮新增），SHA-256 指纹回归测试，逐次 hit/miss 记账。实测 103K 分母的编程式负载稳态命中率 **99.81%**。
2. **provider 能力探测与分层降级**——两次同前缀请求自动判定 provider 缓存能力（DeepSeek / OpenAI / Anthropic / 无缓存），按层选策略：前缀编排 / 显式标记注入 / 响应缓存兜底。
3. **命中率预算器**——输入场景参数（前缀/尾巴/轮数），输出命中预测与到 95% 的杠杆建议。内置 11 类负载模板（情感陪伴 / 编程 Agent / RAG / 群聊 / 多 Agent / 实时流…），全部有预测值。

## 三定律（一手实测，DeepSeek API）

1. **滞后窗定律**：append-only 会话有固定滞后窗（~128-256 tok），`命中率 ≈ 1 − 滞后窗/总输入`。4 个规模点（1.7K→103K）实测与公式精确吻合。
2. **块级匹配**：前缀按 64-token 块匹配，分歧只截断到块边界；**<64 tok 的 prompt 不进缓存**——短 prompt 重复采样没有缓存红利。
3. **账号级跨会话复用**：缓存跨进程/会话存活数小时——对照实验须加 nonce 隔离，生产中重启应用不丢命中率。

## 快速上手

```bash
pip install -e .
```

```python
from cachecortex import PrefixBankSession, CacheTiers, predict, Scene

# 三区会话：system 冻结，历史只追加
session = PrefixBankSession(adapter, system_blocks=[
    "你的任务说明……",      # 越长越好（滞后窗固定，分母决定命中率）
    "输出规范……", "few-shot 示例……",
])
for q in questions:
    session.chat(q)
print(f"命中率: {sum(s['rate'] for s in session.stats)/len(session.stats):.1%}")

# 场景预算：构建前先算能到多少
v = predict(Scene(name="我的场景", stable_prefix=6000,
                  history_per_turn=500, dynamic_tail_per_turn=200,
                  turns=100))
print(v.hit_rate, v.levers)
```

## 结果速览

| 实验 | 结果 |
|---|---|
| 共享前缀批处理（1536 tok × 50 题，真实 API） | 稳态 **88.99%**（公式精确吻合） |
| 三区会话（2.5K 前缀 × 30 轮，真实 API） | 稳态 **92.4%**（峰值 94.6%） |
| 编程式负载（90K 前缀 × 100 轮，真实 API） | 稳态 **99.81%**（滞后窗定律第四次精确验证） |
| 前缀污染对照（nonce 隔离） | **0%**（反模式断崖式确认） |
| 预热对照 | 轮 1 命中从 0% → **96.7%**（冷启动消灭） |

## 效果口径：必须按工作负载拆分

**聚合命中率会掩盖主负载的缓存侵蚀。** 同一个系统里，"稳定 agent" 与"研究型 agent"
的命中率可能相差数倍，平均值会把两端都糊掉。本库因此**默认同时输出聚合与拆分两份**，
并把低命中负载显式标出（默认阈值 0.4——工程经验值，稳定负载低于它通常意味着
前缀里混进了动态内容：时间戳、用户 ID、增长的工作记忆）。

`cachecortex/metrics.py` 给出四个口径：

| 口径 | 含义 | 用途 |
|---|---|---|
| `cached_read_share` | 缓存读取 token / 输入 token | 判断缓存边界有没有盖住正文 |
| `ttft_p50` / `ttft_p95` | 首 token 延迟分位数 | 命中应把 p50 与 p95 一起压低 |
| `ttft_shift` | 启用缓存前后 p50/p95 的位移（正数 = 变快） | 量化"快了多少" |
| `by_workload` | 上述口径的按负载拆分视图 | 定位是哪个负载在吃掉缓存 |

`diagnose()` 把口径翻译成可执行的判断，只在有信号时给结论。示例：两类负载分别为
0.90 与 0.05 时，聚合值 0.475 看起来"还行"，但诊断会直接点出
`research_agent` 需要检查前缀。

### Tier 与 provider 三要素的映射（断点 / 回看窗 / TTL）

| Tier | 缓存断点 | 回看窗 | TTL |
|---|---|---|---|
| Tier1 自动前缀 | 无显式断点（provider 自动向前推进） | 块级匹配，可缓存粒度由 provider 定 | 随会话活跃度，空闲即失效 |
| Tier2 显式标记 | 单请求上限 4 个缓存断点 | 每断点最多 20 个内容块 | 分档 5 分钟 / 1 小时 / 24 小时；1 小时档须排在 5 分钟档之前 |
| Tier3 响应缓存 | 不适用（无前缀缓存） | 不适用 | 由应用层自行管理 |

Tier2 一行的具体数值据 Anthropic 缓存文档口径；Tier1 行为为自动推进，无显式断点。
该表同时通过 MCP 工具 `cache_tier_guide` 以程序可读形式暴露。

## 这个仓库不是什么

先说边界：

- **不是新的缓存机制**。前缀缓存属于服务端（DeepSeek/Anthropic/vLLM 等），本仓库是应用侧编排层——它不读写 KV tensor，也不能让 provider 支持它本不支持的行为。
- **不是跨 provider 的实测结论**。三定律的块粒度（64 tok）与时效在 DeepSeek API 上实测；其他 provider 的参数来自文献与探测设计，未逐一实测。
- **不是生产规模验证**。单会话最长 300 轮、最大前缀 90K tok；Reasonix 等报告的"亿级 token/日"规模未测。
- **不是对抗性方案**。它假设你控制自己的 prompt 组装；若上游框架在你看不到的地方改写 prompt，本库无法拦截。
- **不是"命中率高 = 省大钱"的承诺**。收益 = 输入占比 × 命中率 × 价差倍数，三者相乘。输出占比高的任务（推理类）即使 99% 命中也省不了几个钱——预算器会先告诉你这一点。

反过来说，它**是**：一套每个数字都能在几分钟内独立复现的实验笔记，和一个小到可以读完的编排库。

## 仓库结构

```
cachecortex/      库源码（core / prefix_bank / cache_tiers / hitrate_budget / optimize_stack / metrics）
tests/            端到端单测与口径测试（31 项，不调真实 API）
benchmarks/       对标基准（sim 模拟 / live 真实双模式）+ 实测原始 JSON
results/          关键实验数据快照
reports/          三定律技术报告
AI_DISCLOSURE.md  AI 使用声明
```

## 引用的先行者

- Reasonix（esengine/DeepSeek-Reasonix）：三区结构与生产级命中率报告——纪律来源与对标对象
- TokenPilot（arXiv:2606.17016）：双粒度上下文管理
- CacheBlend（EuroSys 2025）/ EPIC（ICML 2025）/ CacheGen（SIGCOMM 2024）：serving 层非前缀 KV 复用
- NVIDIA Dynamo 文档：块价值分层与工具暂停驱逐
- OpenAI Prompt Caching 201：append-only 循环与 prompt_cache_key
- SillyTavern 社区：世界书前缀区实践、ST-Message-Chunker、Cache-Refresh


---

## 贡献与引用

- 贡献指南见 [CONTRIBUTING.md](CONTRIBUTING.md)；行为准则见 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- 安全问题请走 [SECURITY.md](SECURITY.md) 的私密渠道（勿开公开 Issue）
- 版本变更见 [CHANGELOG.md](CHANGELOG.md)；学术引用格式见 [CITATION.cff](CITATION.cff)
- 许可：MIT
