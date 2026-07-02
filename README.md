# dsa-mcp

> **Port of [ZhuLinsen/daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) (52k★) analysis capabilities as an MCP server.**

## Status

**🚧 Planning** — Not yet implemented.

See `docs/PLAN-zsd-fin-modules.md` and `docs/dSA-AUDIT.md` for the full plan.

## What is this?

A standalone MCP server that exposes dSA's analysis algorithms (StockTrendAnalyzer, technical indicators, candlestick patterns, YAML strategies) as agent-callable tools. Designed to plug into the existing `financial-data-service` (port 8084) data layer.

## Architecture

```
agent (Hermes / VPBuddy / signal-arena / cron)
    │
    ├─→ [8086 MCP] financial-data-service   (data queries)
    │     └─ get_quote / get_kline / get_news / get_fund_flow / ...
    │
    └─→ [8087 MCP] dsa-mcp                  (analysis + alerts)
          ├─ analyze_trend / calculate_macd / analyze_pattern / ...
          ├─ list_strategies / get_strategy
          └─ check_alert / list_alert_types
```

## Iron Rules

1. **All data flows through 8084 MCP**. dsa-mcp MUST NOT depend on dSA's `data_provider`, `history_loader`, or SQLite. Data comes from `financial-data-service`.
2. **Alerts are pure signals**. dsa-mcp `check_alert` returns `{triggered, signals[]}` only. No push, no DB writes, no scheduling.

## Relationship with upstream

This project is a **port** of `ZhuLinsen/daily_stock_analysis` (MIT licensed). See `UPSTREAM.md` for sync notes.

- **License**: MIT (matches upstream)
- **Source code**: copied from dSA, with import paths rewritten to remove dSA DB dependencies
- **YAML strategies**: copied verbatim from `strategies/*.yaml`
- **Prompts**: copied verbatim from `src/agent/agents/*_agent.py` `system_prompt` strings

## Development

Will be added once Phase 2/3 begins. Currently just planning docs.

## License

MIT — see `LICENSE`.