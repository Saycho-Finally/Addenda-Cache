"""PPBExt-Cache 的 MCP server（stdio 传输，零依赖）。

暴露PPBExt-Cache的**离线**能力为 MCP 工具（不触发真实 API 调用）：
  cache_hitrate_predict   —— 按场景参数预测缓存命中率（三定律模型）
  cache_stack_recommend   —— 按场景推荐杠杆组合（预算/栈）
  cache_tier_guide        —— provider 缓存能力三档（自动前缀/显式标记/无缓存）说明
  cache_workload_report   —— 按工作负载拆分的命中率口径 + TTFT 位移（调用方提供实测样本）

协议：JSON-RPC 2.0 over stdio（initialize / tools/list / tools/call）。
运行：python mcp_server.py
"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, ".")

from cachecortex.metrics import (LOW_HIT_THRESHOLD, CacheSample, diagnose,  # noqa: E402
                                 report, ttft_shift)

TOOLS = [
    {
        "name": "cache_hitrate_predict",
        "description": "按场景预测前缀缓存稳态命中率。参数：prefix_tokens（共享前缀长度）、"
                       "turns（会话轮数）、turn_tokens（每轮新增 token）、"
                       "prefix_stable（前缀是否保持不变，默认 true）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prefix_tokens": {"type": "integer"},
                "turns": {"type": "integer"},
                "turn_tokens": {"type": "integer", "default": 500},
                "prefix_stable": {"type": "boolean", "default": True},
            },
            "required": ["prefix_tokens", "turns"],
        },
    },
    {
        "name": "cache_stack_recommend",
        "description": "按场景（成本/延迟/合规偏好）推荐三区编排的杠杆组合",
        "inputSchema": {
            "type": "object",
            "properties": {
                "priority": {"type": "string",
                             "enum": ["cost", "latency", "compliance"]},
                "has_long_prefix": {"type": "boolean", "default": True},
            },
            "required": ["priority"],
        },
    },
    {
        "name": "cache_tier_guide",
        "description": "provider 缓存能力三档说明（Tier1 自动前缀 / Tier2 显式标记 / "
                       "Tier3 响应缓存），含各档的编排纪律，以及 Tier 与"
                       "缓存断点/回看窗/TTL 三者的映射表",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "cache_workload_report",
        "description": "按工作负载拆分的缓存口径（调用方提供实测样本，本工具只做计算）。"
                       "输出聚合与拆分两份命中率、缓存读取 token 占比、TTFT 的 p50/p95，"
                       "并标出低命中负载；可选传入 ttft_before/ttft_after 计算 TTFT 位移。"
                       "拆分的意义：聚合命中率会掩盖主负载的缓存侵蚀",
        "inputSchema": {
            "type": "object",
            "properties": {
                "samples": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "workload": {"type": "string"},
                            "input_tokens": {"type": "integer"},
                            "cached_read_tokens": {"type": "integer"},
                            "ttft_ms": {"type": "number"},
                        },
                        "required": ["workload", "input_tokens"],
                    },
                },
                "low_threshold": {"type": "number", "default": 0.4},
                "ttft_before": {"type": "array", "items": {"type": "number"}},
                "ttft_after": {"type": "array", "items": {"type": "number"}},
            },
            "required": ["samples"],
        },
    },
]

TIERS = {
    "tier1_auto_prefix": {
        "examples": ["DeepSeek", "OpenAI", "自建 vLLM/SGLang"],
        "discipline": "保持前缀稳定（append-only）；块级匹配（最小可缓存粒度）",
        "note": "落后窗约 128-256 token：最近生成内容下一轮不进缓存",
    },
    "tier2_explicit_marker": {
        "examples": ["Anthropic cache_control", "Gemini explicit cache"],
        "discipline": "显式标记缓存断点；注意 TTL 与最小可缓存长度",
        "note": "标记位置与内容变更都会失效",
    },
    "tier3_response_cache": {
        "examples": ["无原生缓存的 provider"],
        "discipline": "响应级缓存（语义去重）；temperature>0 的采样任务慎用（破坏独立性）",
        "note": "兜底档，命中语义不同于前缀缓存",
    },
}

# C3：Tier 与 provider 三要素（缓存断点 / 回看窗 / TTL）的映射。
# Tier2 一行的具体数值据 Anthropic 缓存文档口径；Tier1 为自动推进，无显式断点。
PROVIDER_MAPPING = {
    "tier1_auto_prefix": {
        "breakpoints": "无显式断点（provider 自动向前推进）",
        "lookback": "块级匹配，可缓存粒度由 provider 定",
        "ttl": "随会话活跃度，空闲即失效",
    },
    "tier2_explicit_marker": {
        "breakpoints": "单请求上限 4 个缓存断点",
        "lookback": "每断点最多 20 个内容块",
        "ttl": "分档 5 分钟 / 1 小时 / 24 小时；1 小时档须排在 5 分钟档之前",
    },
    "tier3_response_cache": {
        "breakpoints": "不适用（无前缀缓存）",
        "lookback": "不适用",
        "ttl": "由应用层自行管理",
    },
}


def _call_tool(name: str, args: dict) -> dict:
    if name == "cache_hitrate_predict":
        prefix = int(args["prefix_tokens"])
        turns = int(args["turns"])
        turn_tokens = int(args.get("turn_tokens", 500))
        stable = bool(args.get("prefix_stable", True))
        if not stable:
            return {"hitrate_estimate": 0.0,
                    "note": "前缀不稳定——命中率将大幅下降，先修复前缀纪律"}
        # 三定律的滞后窗近似：稳态命中 ≈ 1 - 滞后窗 / 总输入
        lag = 192  # 滞后窗均值（128-256 的中间值）
        total = prefix + turns * turn_tokens
        hit = max(0.0, 1 - lag / total)
        return {"hitrate_estimate": round(hit, 4),
                "lag_window_tokens": lag,
                "total_input_tokens": total,
                "note": "三定律滞后窗近似（稳态）；实测口径见仓库 reports/"}
    if name == "cache_stack_recommend":
        priority = args["priority"]
        has_long = bool(args.get("has_long_prefix", True))
        stacks = {
            "cost": ["三区 append-only", "命中率预算对齐", "off-peak 调度"] +
                    (["长前缀优先（命中率随分母上升）"] if has_long else []),
            "latency": ["预热（首轮冷启动消除）", "块级对齐", "并发聚簇（同前缀顺序发）"],
            "compliance": ["不可变前缀（审计可复现）", "命中率台账（逐笔可对账）",
                           "provider 能力分层留档"],
        }
        return {"priority": priority, "recommended_stack": stacks[priority]}
    if name == "cache_tier_guide":
        return {"tiers": TIERS, "provider_mapping": PROVIDER_MAPPING,
                "detection": "CacheTiers.probe：两次同前缀请求按 usage 字段判层"}
    if name == "cache_workload_report":
        samples = [CacheSample(workload=s["workload"],
                               input_tokens=int(s["input_tokens"]),
                               cached_read_tokens=int(s.get("cached_read_tokens", 0)),
                               ttft_ms=float(s.get("ttft_ms", 0.0)))
                   for s in args.get("samples", [])]
        rep = report(samples,
                     low_hit_threshold=float(args.get("low_threshold",
                                                      LOW_HIT_THRESHOLD)))
        out = rep.to_dict()
        out["diagnosis"] = diagnose(rep)
        if args.get("ttft_before") and args.get("ttft_after"):
            out["ttft_shift"] = ttft_shift(args["ttft_before"], args["ttft_after"])
        return out
    raise ValueError(f"unknown tool: {name}")


def handle(req: dict) -> dict | None:
    method = req.get("method")
    rid = req.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2026-06-18",
            "serverInfo": {"name": "ppbext-cache", "version": "0.4.0"},
            "capabilities": {"tools": {}}}}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = req.get("params", {})
        try:
            out = _call_tool(params.get("name", ""), params.get("arguments", {}))
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text",
                             "text": json.dumps(out, ensure_ascii=False)}]}}
        except Exception as e:                      # noqa: BLE001
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": f"error: {e}"}],
                "isError": True}}
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
