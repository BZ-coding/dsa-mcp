# dSA 源码审计报告

**审计对象**: `ZhuLinsen/daily_stock_analysis` @ `48b9e18a` (2026-07-01 22:31)
**审计者**: Hermes
**日期**: 2026-07-02
**目标**: 决定哪些模块能干净搬到 `dsa-mcp` 新仓

---

## 0. 审计方法

逐文件读源码 + grep 关键依赖 + 测试独立运行能力。
不看的：dSA 的 REST/Web/Desktop/orchestrator/llm/config/notification。

---

## 1. 工具层 (`src/agent/tools/`)

### 1.1 `analysis_tools.py` (20KB, ~520 行)

**工具清单** (4 个):
- `analyze_trend`: 调 `StockTrendAnalyzer.analyze(df, code)` 综合分析
- `calculate_ma`: 算任意周期 MA + bias + 多空排列
- `get_volume_analysis`: 量价分析 (5d/10d/20d 对比)
- `analyze_pattern`: K 线形态识别 (Doji/Hammer/Star/Morning Star/吞没/双底/突破/箱体)

**依赖**:
- `src.stock_analyzer.StockTrendAnalyzer` ✅ **可搬**（独立模块，849 行）
- `src.services.history_loader.load_history_df` ⚠️ **需改造**：❌ **铁律**: 不许依赖 dSA DB，必须改成接受 `pd.DataFrame` 参数注入（数据走我们 8084 MCP）
- `src.agent.tools.registry.ToolDefinition/ToolParameter` ✅ **可搬**（80 行零依赖）

**审计结论**: ✅ **必搬**，仅需把 `load_history_df(stock_code, days)` 改成函数签名 `analyze(df, code)`，**df 由 dsa-mcp 调用方（agent）从 8084 MCP 拿**。

**抽出工作量**: 中。`StockTrendAnalyzer` 是黄金（849 行自含算法），但要解耦 `history_loader`（**改完不再依赖 dSA DB**）。

---

### 1.2 `backtest_tools.py` (10KB, ~240 行)

**工具清单** (3 个):
- `get_skill_backtest_summary(skill_id)` - 按 skill 查回测统计
- `get_strategy_backtest_summary(eval_window_days)` - 整体回测统计
- `get_stock_backtest_summary(stock_code, ...)` - 单只股票回测记录

**依赖**:
- `src.services.backtest_service.BacktestService` - dSA 内部 SQLite 库

**审计结论**: ❌ **不搬**。
**理由**:
1. 这是**读 dSA 自己的回测数据库**，没有数据源就返回 `"info": "No backtest data available"`
2. ❌ **铁律**: 数据不许依赖 dSA DB，全部走我们 8084
3. 要复用必须自己实现回测引擎（成本大）
4. **真正的回测应该在我们仓做**（基于 8084 历史 SQLite），不属于"搬 dSA"范畴

**降级方案**: Phase 3 不实现 backtest MCP tool。Phase 4+ 我们自己写简易回测。

---

### 1.3 `data_tools.py` (24KB, ~700 行)

**工具清单** (7 个):
- `get_realtime_quote` - 调 `DataFetcherManager.get_realtime_quote`
- `get_daily_history` - 调 `load_history_df`
- `get_chip_distribution` - 调 `DataFetcherManager.get_chip_distribution`
- `get_analysis_context` - 读 dSA SQLite
- `get_stock_info` - 调 fundamental_context (估值/板块)
- `get_portfolio_snapshot` - 调 dSA PortfolioService
- `get_capital_flow` - 调 `DataFetcherManager.get_capital_flow_context`

**审计结论**: ❌ **全部不搬**。
**理由**:
- 100% 依赖 dSA 内部 fetcher 和 SQLite
- 我们有 8084 service + TDX MCP + akshare，已经覆盖
- 唯一有价值的是字段定义参考（`UnifiedRealtimeQuote` 28 字段，可借鉴）

---

### 1.4 `market_tools.py` (3KB, ~80 行)

**工具清单** (2 个):
- `get_market_indices(region)` - 大盘指数
- `get_sector_rankings(top_n)` - 板块涨跌榜

**审计结论**: ❌ **不搬**。
**理由**: 数据走 dSA fetcher，我们 8084 新增的板块/概念接口自己实现。

---

### 1.5 `search_tools.py` (6KB, ~180 行)

**工具清单** (2 个):
- `search_stock_news(stock_code, stock_name)`
- `search_comprehensive_intel(stock_code, stock_name)`

**依赖**: `src.search_service.SearchService` - 调外部 API（Anspire/Brave/Tavily）

**审计结论**: ⚠️ **暂不搬**。
**理由**:
1. 依赖外部 search API key（Anspire/Brave/Tavily），我们没配
2. 我们已有 `mcp_zhihu_zhihu_search` 和 `financial-news-aggregator` skill
3. **重复建设**

**降级方案**: dsa-mcp 不做 search tool。需要时调现有 zhihu MCP。

---

### 1.6 `registry.py` (8KB, ~270 行)

**内容**: `ToolParameter`/`ToolDefinition` dataclass + `ToolRegistry` 类 + `@tool` 装饰器

**依赖**: 零外部依赖，纯 Python dataclass + inspect

**审计结论**: ✅ **必搬**。
**理由**: 是 dSA 工具系统的核心抽象。搬到 dsa-mcp 当内部注册中心使用。

---

## 2. Agent Prompt 层 (`src/agent/agents/`)

7 个文件，但**真正能"搬 prompt"的是 6 个具体 agent**（base_agent 是抽象类）:

| 文件 | 大小 | 内容 | 可搬性 |
|---|---|---|---|
| `base_agent.py` | 12KB | 抽象基类 | ❌ 不搬（我们不用 dSA agent 框架） |
| `technical_agent.py` | 3KB | 4 段式 workflow + JSON 输出 | ✅ **抄 system_prompt** |
| `intel_agent.py` | 4KB | 新闻/资金流/风险检测 | ✅ **抄 system_prompt** |
| `risk_agent.py` | 5KB | 7 类风险检查清单 | ✅ **抄 system_prompt** |
| `portfolio_agent.py` | 6KB | 组合配置 + JSON 输出 | ✅ **抄 system_prompt** |
| `decision_agent.py` | 11KB | 决策汇总（chat mode + report mode） | ✅ **抄 system_prompt** |
| `__init__.py` | <1KB | 工厂 | ❌ 不搬 |

**审计结论**: ✅ **抄 6 个 system_prompt 字符串，存为 Markdown 文件**。

**工作量**: 小（直接 string extract + 写 .md）。

---

## 3. 核心算法 (`src/stock_analyzer.py`)

**`StockTrendAnalyzer` (849 行)** - 综合技术分析引擎

**接口**:
```python
analyzer = StockTrendAnalyzer()
result = analyzer.analyze(df: pd.DataFrame, code: str) -> TrendAnalysisResult
```

**内部函数**:
- `_calculate_mas(df)` - 算 MA5/10/20/60
- `_calculate_macd(df)` - MACD DIF/DEA/BAR
- `_calculate_rsi(df)` - RSI 6/12/24
- `_analyze_trend(df, result)` - 趋势状态判定
- `_calculate_bias(result)` - 乖离率
- `_analyze_volume(df, result)` - 量能状态
- `_analyze_support_resistance(df, result)` - 支撑/阻力
- `_analyze_macd(df, result)` - MACD 信号
- `_analyze_rsi(df, result)` - RSI 信号
- `_generate_signal(result)` - 综合买卖信号 + 评分

**依赖**: `pandas`, `numpy`, `enum`, `dataclass` + `src.config.bias_threshold` (一行 env)

**审计结论**: ✅ **必搬，黄金模块**。

**搬法**: 整文件复制 + 把 `from src.config import get_config` 改成本地简单常量。

---

## 4. 策略层 (`strategies/*.yaml`)

**15 个 YAML**, 每个结构相同:
```yaml
name: ma_golden_cross          # 英文 ID
display_name: 均线金叉          # 中文名
description: ...                # 一句话描述
category: trend                # trend|framework|pattern|reversal
core_rules: [1, 2, 3]          # 关联理念编号
required_tools: [...]          # 依赖哪些 tool
aliases: [...]                 # 同义词
default_priority: 20           # 优先级
market_regimes: [...]          # 适用市场状态
instructions: |                # ⭐ 完整中文策略说明（500-1500 字）
  **均线金叉（MA Golden Cross Strategy）**
  ...
```

**审计结论**: ✅ **全部复制到 dsa-mcp/src/dsa_mcp/strategies/**。
**理由**: 纯文本配置，零代码耦合，**直接给 agent 当 system prompt 片段注入**。

**15 个策略分类**:
- trend (5): bull_trend / ma_golden_cross / shrink_pullback / volume_breakout / dragon_head
- framework (8): box_oscillation / chan_theory / wave_theory / emotion_cycle / event_driven / expectation_repricing / growth_quality / hot_theme
- pattern (1): one_yang_three_yin
- reversal (1): bottom_volume

---

## 5. 预警系统

### 5.1 grep 结果
- `alert`: 784 行命中，**分散**在 `analyzer.py`/`risk_agent.py`/`notification_*.py`
- `warning`: 861 行命中，**和风险检查混在一起**
- `notification`: 891 行命中，**核心是 notification sender 系统**
- `threshold`: 184 行命中

### 5.2 真相
**dSA 没有独立的"预警模块"**。
"预警" 在 dSA 是 4 件事的混合:
1. 风险 agent (`risk_agent.py`) 检查个股风险
2. notification sender 系统 (`notification_sender/*.py`) 去重/发送
3. phase_decision_guardrail (`src/phase_decision_guardrail.py`) 阶段决策保护
4. notification_noise (`src/notification_noise.py`) 噪声控制

**全部和推送/上下文耦合，零独立"该不该预警"的判断逻辑**。

### 5.3 审计结论
⚠️ **不搬 dSA 的预警，自己设计**。

**理由**:
- dSA 没有可独立复用的"alert 判定逻辑"
- 它的 alert 是"风险检查 + 推送"合并体
- 我们的需求是"独立 alert tool，让 agent 调"，不是"发推送"

**用户明确（2026-07-02）**:
1. "数据全部依赖我们自己的金融数据采集系统，不许依赖 dsa db"
   - dsa-mcp 所有 tool 需要数据时，**必须通过 8084 MCP 拿**（或 8084 REST）
   - 不依赖 dSA 的 `history_loader` / SQLite / DataFetcherManager
2. "预警其实只是给出信号罢了，不用推送，我们搬过来的预警也只是 mcp 的一个功能"
   - 预警 = 纯信号生成（triggered / not_triggered + reason）
   - 不绑定推送 channel
   - 不绑定持久化
   - 不绑定调度（agent 自己决定什么时候调）

**用户再次修正（2026-07-02）**: "我们配过 API key，还有 mmx_search / zhihu search"

**修正**: 之前我标记"骨架+新闻 TODO"的 4 条规则实际**全部🟢立即可跑**，数据走 8084 `news_aggregator` collector（mmx_search/zhihu/rss/community/akshare 5 个数据源已配齐）。**无任何 alert 规则需要等 news MCP**。

**自设计 alert checker**（**数据全走 8084 MCP**）:
- 规则定义在 YAML（11 条种子规则，全部立即可跑）
- MCP tool: `check_alert(symbol, rule_id)` / `list_alert_types()`
- 内部: **先调 8084 MCP 拿数据**（quote + kline + news_aggregator）→ 跑规则 → 返回 `{triggered: bool, signals: [{rule_id, severity, value, reason}]}`
- agent 拿到信号后自己决定怎么用（写报告 / 触发后续 / 调用别的 tool）
- **不耦合推送**，**不耦合 dSA DB**

---

## 6. 数据层 (`data_provider/`)

**19 个 fetcher**, 我**没逐个读**，但从 `base.py` 接口已经清楚:
- 接口统一在 `BaseFetcher` 抽象类
- 我们有 8084 + akshare/sina/push2 + TDX MCP，**不再需要新 fetcher**
- 唯一可借鉴的是**字段标准定义**（`UnifiedRealtimeQuote` 28 字段 + `ChipDistribution`）

**审计结论**: ❌ **不搬任何 fetcher**。
**理由**: 我们数据底座是 8084，dSA fetcher 是它的"另一套实现"，没必要重复。

---

## 7. 通知层 (`notification_sender/`)

13 个 sender：飞书/企微/Telegram/Discord/Slack/Email/Pushover/Pushplus/Server酱³/Gotify/Ntfy/AstrBot/CustomWebhook

**审计结论**: ❌ **全部不搬**。
**理由**: 我们已有 Hermes scheduler + feishu chat_id 推送。

---

## 8. 总结: dsa-mcp 应该搬什么

### ✅ 搬 (核心价值)

| 模块 | 来源 | 大小 | 备注 |
|---|---|---|---|
| `registry.py` | `src/agent/tools/` | 8KB | 整文件搬，零依赖 |
| `StockTrendAnalyzer` | `src/stock_analyzer.py` | 25KB | 整文件搬，改 1 行 import |
| `analysis_tools.py` 部分 | `src/agent/tools/` | 12KB | 只搬 `calculate_ma` / `get_volume_analysis` / `analyze_pattern` 三个，**不搬 `analyze_trend`**（依赖 history_loader） |
| 6 个 agent prompt | `src/agent/agents/*.py` | ~5KB 文本 | 抄 `system_prompt` 字符串，存 `.md` |
| 15 个 YAML 策略 | `strategies/*.yaml` | ~37KB | 整目录复制 |
| `load_history_df` 改造版 | `src/services/history_loader.py` | 5KB | **不直接搬**，重写为接受 `pd.DataFrame` 参数 |

**总搬运量**: ~92KB 代码 + 5KB 文本 + 37KB YAML = **~134KB**

### ❌ 不搬

| 模块 | 理由 |
|---|---|
| `data_tools.py` (24KB) | 全依赖 dSA fetcher/SQLite |
| `market_tools.py` (3KB) | 同上 |
| `backtest_tools.py` (10KB) | 读 dSA 内部 backtest DB，无数据 |
| `search_tools.py` (6KB) | 依赖外部 search API |
| `base_agent.py` (12KB) | 抽象类，dSA agent 框架专属 |
| `analyze_trend` tool | 依赖 history_loader，需要注入改造 |
| `data_provider/` (19 个 fetcher) | 8084 已覆盖 |
| `notification_sender/` (13 个 sender) | Hermes + feishu 已覆盖 |
| `src/llm/` (~150KB) | 我们用 Hermes |
| `src/agent/orchestrator.py` (65KB) | Hermes = agent loop |
| `src/agent/agents/*.py` 类框架 | 用 prompt 模板，不用 class |
| `src/config.py` | 抄 bias_threshold 一个常量即可 |
| 预警系统 | dSA 没有独立模块，自己设计 |

---

## 9. 关键决策清单

| 决策 | 结论 | 影响 Phase 2/3 |
|---|---|---|
| `analyze_trend` 是否搬？ | ⚠️ **改造后搬**：把 `load_history_df(stock_code)` 改成接受 `df` 参数 | Phase 3 工作量 +1 天 |
| `analyze_pattern` 等纯函数是否搬？ | ✅ 直接搬 | Phase 3 工作量 -0.5 天 |
| backtest 是否搬？ | ❌ 不搬 | Phase 3 工作量 -1 天 |
| 预警 MCP 自己设计 | ✅ 自写 | Phase 3 工作量 +1 天 |
| 6 个 agent prompt 是否抄？ | ✅ 抄 | Phase 3 工作量 -0.5 天 |
| 15 YAML 是否搬？ | ✅ 整目录搬 | Phase 3 工作量 -0.5 天 |
| search tool 是否搬？ | ❌ 不搬 | Phase 3 工作量 -1 天 |

**Phase 3 净工作量**: 3-5 天 → 实际 **2-3 天**（砍掉 backtest + search 节省 2 天）

---

## 10. 风险提示

| 风险 | 影响 | 缓解 |
|---|---|---|
| `load_history_df` 改造引入 bug | analyze_trend 算错 | 写测试 + 对比 dSA 输出 |
| **违反数据铁律**: dsa-mcp 偷偷 import dSA fetcher | 数据从 dSA 走，破架构 | **Phase 3 第一天** grep `from data_provider` / `from src.services.history_loader`，必须为空 |
| 6 个 prompt 抄写漏字符 | agent 行为漂移 | 字符串完全 copy-paste，不重写 |
| 15 YAML 编码问题 | 中文乱码 | 复制时显式 utf-8 |
| **违反预警铁律**: alert tool 偷偷调 sender | 推送耦合 | Phase 3 grep `import.*sender` / `import.*notification`，必须为空 |
| dSA upstream 更新 | 我们代码漂移 | 写 `UPSTREAM.md` 记录差异点，定期手动 sync |
| `analysis_tools.py` 其他依赖 | import error | Phase 3 第一步就 `python -c "import"` 验证 |

**铁律验证清单（Phase 3 完成前必过）**:
- [ ] `grep -rn "from data_provider\|from src.services.history_loader\|from src.storage" src/dsa_mcp/` → **必须 0 行**
- [ ] `grep -rn "import.*sender\|import.*notification\|import.*feishu\|import.*webhook" src/dsa_mcp/` → **必须 0 行**
- [ ] `grep -rn "subprocess\|os.system" src/dsa_mcp/` → **必须 0 行**（不允许 dsa-mcp 主动触发任何外部副作用）

## 审计结束

Phase 1 完成。下一步：重写 PLAN 文档（Phase 2/3/4 根据审计结论调整）。