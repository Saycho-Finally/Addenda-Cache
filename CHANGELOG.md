# 变更记录 (Changelog)

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.3.0] - 2026-10-05

### Removed

- `cachecortex/experts.py`（专家注册表的历史副本）：零 import、未在包内导出、
  文档无引用，其职责已由编排层独立承载。0.2.0 变更记录中提到的该模块随此移除

## [0.2.0] - 2026-10-03

- 成本与方法学修正（官方账单逐笔对账口径）；ExpertRegistry 模块

## [0.1.0] - 2026-10-02

- 三定律实测 + CacheCortex 0.1.0（三区会话 / Tiers / 预算 / AutoStack）六测通过

