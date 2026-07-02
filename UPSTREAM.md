# Upstream Sync Notes

This project is a **port** of [ZhuLinsen/daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) (MIT, 52k★ as of 2026-07-02).

## Source Upstream

- **Repo**: `https://github.com/ZhuLinsen/daily_stock_analysis.git`
- **Upstream commit at fork**: `48b9e18a` (2026-07-01 22:31 +0800, "fix: relax opencode static output instruction")
- **License**: MIT
- **What we port** (per `docs/dSA-AUDIT.md`):
  - `src/agent/tools/registry.py` (80 lines, dataclass + decorator)
  - `src/stock_analyzer.py` (849 lines, `StockTrendAnalyzer` class — pure algorithm)
  - `src/agent/tools/analysis_tools.py` — 3 pure functions (`calculate_ma`, `get_volume_analysis`, `analyze_pattern`)
  - 6 agent prompt strings from `src/agent/agents/*_agent.py`
  - 15 YAML strategy files from `strategies/`
  - **Self-designed** alert checker (dSA has no independent alert module)

## What we DO NOT port

- `data_tools.py` / `market_tools.py` / `backtest_tools.py` / `search_tools.py` — depend on dSA fetcher / SQLite / external APIs
- `src/llm/` — we use Hermes directly
- `src/agent/orchestrator.py` (65KB) — Hermes is the agent loop
- `data_provider/` (19 fetchers) — we have 8084 service
- `notification_sender/` (13 senders) — we use Hermes + feishu
- `src/agent/agents/*_agent.py` class framework — we only use their `system_prompt` strings

## Sync Strategy

**Manual, low-frequency**. We do not auto-sync with upstream because:
- We strip out dSA DB dependencies during porting
- We rewrite function signatures to accept `df: pd.DataFrame` parameters
- Upstream moves fast (847 commits, daily activity) and we don't need 95% of changes

**Manual sync checklist** (run quarterly):
1. `cd /home/zsd/codes/daily_stock_analysis && git pull --ff-only`
2. Compare our `src/dsa_mcp/analysis/` against upstream `src/agent/tools/analysis_tools.py`
3. Check if any new YAML in upstream `strategies/` worth porting
4. Check if any new agent in upstream `src/agent/agents/` worth porting prompt
5. Update this file with new upstream commit hash

## License Attribution

```
This project includes code derived from ZhuLinsen/daily_stock_analysis,
Copyright (c) 2026 ZhuLinsen, licensed under the MIT License.

Original source: https://github.com/ZhuLinsen/daily_stock_analysis
```