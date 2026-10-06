# 变更记录 (Changelog)

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [Unreleased]

### Fixed

- **MCP stdio 分帧不兼容规范客户端**（2026-10-07 复核确认）：服务原先只认
  "一行一条 JSON"（NDJSON），而 MCP 的 stdio 传输用 LSP 式 `Content-Length: N`
  头。规范客户端发来的头被 `json.loads` 抛错后**静默跳过**，客户端永远收不到响应
  （表现为握手挂住——这与协议版本无关，服务对任意版本都正确应答）。
  现已同时支持两种分帧，并按请求所用的分帧回复
- SKILL.md 的接口名不存在（2026-10-07 外部评审触发，已复核确认）：
  原写 `ThreeRegionSession` 与 `HitrateBudget` 两个名字（连同 `CacheTiers` 一起
  作为一条 import 语句），其中前两者在包内**不存在**，照抄会 ImportError。
  已按 `cachecortex/__init__.py` 的实际 `__all__` 更正，并补 MCP 四工具说明
- 新增跨仓库检查脚本 `doc_import_check.py`（工作区），用于自动发现
  "文档里的 import 与包内实际符号不一致"这类错误

### Added

- `reports/外部对照_三定律_2026-10-06.md`：三定律与公开实践的逐条对照（C5）。
  结论："账号级复用"作为概念已由 provider 产品化（DeepInfra 账号作用域显式保留等），
  本仓库贡献为 best-effort 行为的一手实测与编排纪律；README 三定律节同步加定位说明

## [0.4.0] - 2026-10-05

### Added

- **按工作负载拆分的缓存口径**（`cachecortex/metrics.py`）：聚合口径会掩盖主负载的
  缓存侵蚀，故默认同时输出聚合与拆分两份，并显式标出低命中负载（默认阈值 0.4）
- 两个业界主指标：`cached_read_share`（缓存读取 token / 输入 token）与
  TTFT 的 p50/p95 及 `ttft_shift`（启用缓存前后的位移，正数表示变快）
- `diagnose()` 把口径翻译成可执行判断（低命中负载 → 查前缀动态内容；负载间差异
  显著 → 指出聚合值掩盖了差异）
- MCP server 新增 `cache_workload_report`（第四个工具，per-workload 拆分入口）；
  `cache_tier_guide` 补 Tier 与缓存断点/回看窗/TTL 的映射表
- README 新增「效果口径」与「Tier 映射」两节
- `tests/test_v4_metrics.py`：25 项测试

## [0.3.0] - 2026-10-05

### Removed

- `cachecortex/experts.py`（专家注册表的历史副本）：零 import、未在包内导出、
  文档无引用，其职责已由编排层独立承载。0.2.0 变更记录中提到的该模块随此移除

## [0.2.0] - 2026-10-03

- 成本与方法学修正（官方账单逐笔对账口径）；ExpertRegistry 模块

## [0.1.0] - 2026-10-02

- 三定律实测 + CacheCortex 0.1.0（三区会话 / Tiers / 预算 / AutoStack）六测通过

