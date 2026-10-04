"""PPBExt-Cache 的 MCP server（stdio 传输，零依赖）。

暴露缓存外挂的**离线**能力为 MCP 工具（不触发真实 API 调用）：
  cache_hitrate_predict  —— 按场景参数预测缓存命中率（三定律模型）
  cache_stack_recommend  —— 按场景推荐杠杆组合（预算/栈）
  cache_tier_guide       —— provider 缓存能力三档（自动前缀/显式标记/无缓存）说明

协议：JSON-RPC 2.0 over stdio（initialize / tools/list / tools/call）。
运行：python mcp_server.py
"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, ".")

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
                       "Tier3 响应缓存），含各档的编排纪律",
        "inputSchema": {"type": "object", "properties": {}},
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
        return {"tiers": TIERS,
                "detection": "CacheTiers.probe：两次同前缀请求按 usage 字段判层"}
    raise ValueError(f"unknown tool: {name}")


def handle(req: dict) -> dict | None:
    method = req.get("method")
    rid = req.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2026-06-18",
            "serverInfo": {"name": "ppbext-cache", "version": "0.2.0"},
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
