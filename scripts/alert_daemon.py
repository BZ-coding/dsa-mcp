#!/home/zsd/codes/dsa-mcp/venv/bin/python
"""
alert_daemon.py — Phase 5a 盘中 alert 轮询 (2026-07-02)

技术栈:
- dsa_mcp_call.py stdio CLI 调用 (check_alert, analyze_trend)
- 本地 JSON dedup (~/.hermes/alert_state.json, flock 防并发)
- 飞书推送走 hermes send (gateway 转发)
- LLM 解读: 推送前调用 hermes chat 生成一句人话解读
- systemd 常驻, 5min 轮询

持仓列表:
- 从 ~/.hermes/portfolio/{lidan_stocks,zsd_hk_stocks,zsd_fund}.json 抽 symbol
- 基金场外品种跳过 (无盘中报价)

Iron rule:
- alert 只推状态**变化** (false→true, signal set 改变) — 不重复骚扰
- LLM 不参与 check_alert (确定性逻辑); 只在推送时生成解读
- 单 daemon 多 source 不抢锁: flock(LOCK_EX) + 状态文件原子替换
"""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

HERMES_HOME = Path("/home/zsd/.hermes")
STATE_FILE = HERMES_HOME / "alert_state.json"
LOCK_FILE = HERMES_HOME / ".alert_daemon.lock"
HISTORY_FILE = HERMES_HOME / "alert_history.jsonl"  # Phase A: append-only 推送历史
DSA_MCP_CALL = HERMES_HOME / "scripts" / "dsa_mcp_call.py"

# 2026-07-07: 改到 hermes agent 群 (oc_90857aaa9c3a4533fdc12c008ca14d00)
# 用户原话: "可以把这个alert发到这个群里来吗?" (当前 session = group: hermes agent 群)
# 历史: 02:15 有人以"cron 一致性" revert 回 home (oc_29c4141), 但用户是唯一事实源 → 改回
# skill alert-daemon-pattern §36 标准流程: probe + 注释 + log 验证
FEISHU_CHAT = "feishu:oc_90857aaa9c3a4533fdc12c008ca14d00"

# 持仓文件 → symbol list (持有跨市标的)
PORTFOLIO_FILES = [
    HERMES_HOME / "portfolio" / "lidan_stocks.json",     # A股 (002202 金风)
    HERMES_HOME / "portfolio" / "zsd_hk_stocks.json",    # 港股 (hk03690 美团)
]

POLL_INTERVAL_SEC = 300  # 5 min

# A股交易时段 (北京时)
TRADING_HOURS = [(dtime(9, 30), dtime(11, 30)), (dtime(13, 0), dtime(15, 0))]


# ──────────────────────────────────────────────────
# Portfolio extraction
# ──────────────────────────────────────────────────

def extract_symbols() -> list[str]:
    """从持仓 JSON 抽出 symbol 列表 (去重, 跳过基金场外)."""
    symbols = set()
    for f in PORTFOLIO_FILES:
        if not f.exists():
            continue
        try:
            d = json.loads(f.read_text())
            for p in d.get("positions", []) or []:
                sym = p.get("symbol") or p.get("code")
                if sym and not sym.startswith(("0", "1", "2", "3", "5", "6", "9")) and not sym.startswith("hk"):
                    continue  # 基金代码前缀跳过
                if sym:
                    symbols.add(sym)
        except Exception as e:
            print(f"[warn] read {f}: {e}", file=sys.stderr)
    return sorted(symbols)


def extract_symbol_name_map() -> dict[str, str]:
    """持仓 JSON → {symbol: 中文名}. 推送消息里把代码还原成中文名 (如 hk03690→美团-W).

    注意: 同名 hash 冲突时后读到的覆盖前者 (持仓 JSON 不应有重 symbol, 仅作 best-effort).
    """
    name_map: dict[str, str] = {}
    for f in PORTFOLIO_FILES:
        if not f.exists():
            continue
        try:
            d = json.loads(f.read_text())
            for p in d.get("positions", []) or []:
                sym = p.get("symbol") or p.get("code")
                name = (p.get("name") or "").strip()
                if sym and name:
                    name_map[sym] = name
        except Exception as e:
            print(f"[warn] read {f}: {e}", file=sys.stderr)
    return name_map


# ──────────────────────────────────────────────────
# dsa-mcp call (stdlib subprocess wrapper)
# ──────────────────────────────────────────────────

def call_dsa_mcp(tool: str, args: dict, timeout: int = 30) -> dict | None:
    """调 dsa-mcp stdio CLI, 返 text content 的 dict/list."""
    try:
        proc = subprocess.run(
            [sys.executable, str(DSA_MCP_CALL), tool, json.dumps(args, ensure_ascii=False)],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            print(f"[err] {tool} rc={proc.returncode}: {proc.stderr[:200]}", file=sys.stderr)
            return None
        return json.loads(proc.stdout)
    except subprocess.TimeoutExpired:
        print(f"[err] {tool} timeout", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[err] {tool} {e}", file=sys.stderr)
        return None


# ──────────────────────────────────────────────────
# State (with flock)
# ──────────────────────────────────────────────────

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    """原子写 (写 tmp + rename)."""
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))
    tmp.rename(STATE_FILE)


def append_history(ts: str, groups: list[tuple[str, list]]) -> None:
    """Phase A: append 一条推送历史 (JSONL append-only).

    args:
        ts: 推送时间 ISO8601
        groups: [(sym, to_push_signals)]
    """
    total_signals = sum(len(sigs) for _, sigs in groups)
    record = {
        "ts": ts,
        "sym_count": len(groups),
        "signal_count": total_signals,
        "groups": [
            {
                "sym": sym,
                "signals": [
                    {
                        "rule_id": s["rule_id"],
                        "severity": s.get("severity", "info"),
                        "name": s.get("name") or s["rule_id"],
                        "reason": s.get("reason", ""),
                        "sig_key": _signal_value(s),
                    }
                    for s in sigs
                ],
            }
            for sym, sigs in groups
        ],
    }
    try:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[warn] append_history: {e}", file=sys.stderr)


@contextlib.contextmanager
def acquire_lock():
    """flock 防并发 (防 cron 临时跑 + daemon 同时跑)."""
    LOCK_FILE.touch()
    f = open(LOCK_FILE, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield f
    finally:
        f.close()  # closes fd → releases flock


def trading_now() -> bool:
    """交易时段判断 (Phase 5c: 盘中也跑 + 盘后也跑; 周末停).

    Phase 5a 旧版: 仅 9:30-11:30 / 13:00-15:00 跑, 盘后 noise 0 (误判为真).
    Phase 5c 新版: 工作日全天跑 (盘中也跑 catch 5min tick, 盘后也跑抓收盘后的
    announcement 入库; cooldown 由 MAX_PUSHES_PER_DAY + MIN_PUSH_INTERVAL_SEC 防 spam).
    """
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    return True


# ──────────────────────────────────────────────────
# Push (hermes send)
# ──────────────────────────────────────────────────

def push_feishu(msg: str) -> bool:
    """走 hermes send 推到飞书."""
    try:
        proc = subprocess.run(
            ["hermes", "send", "-t", FEISHU_CHAT, msg],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            print(f"[err] hermes send rc={proc.returncode}: {proc.stderr[:200]}", file=sys.stderr)
            return False
        return True
    except subprocess.TimeoutExpired:
        print(f"[err] hermes send timeout (30s)", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[err] hermes send {e}", file=sys.stderr)
        return False


# ──────────────────────────────────────────────────
# LLM interpretation (hermes chat)
# ──────────────────────────────────────────────────

def llm_interpret(symbol: str, signals: list, trend: dict | None) -> str:
    """
    调 hermes chat 生成一句话人话解读.
    失败 fallback 到确定性字符串, 绝不阻塞推送.

    Phase 5c: 增加 ALERT_DAEMON_SKIP_LLM=1 环境变量跳过 LLM (测试/debug 用).
    """
    if os.environ.get("ALERT_DAEMON_SKIP_LLM") == "1":
        return "[LLM skipped]"
    sig_brief = ", ".join(f"[{s['rule_id']}]{s.get('reason','')}" for s in signals)
    trend_brief = ""
    if trend:
        trend_brief = f"趋势={trend.get('trend_status','')} 强度={trend.get('trend_strength','')}"

    prompt = (
        f"持仓 {symbol} 触发以下预警: {sig_brief}。"
        f"{trend_brief}。"
        f"用 1 句简短中文(≤30 字)给张胜东操盘建议, 用 markdown 强调标点符号。"
    )
    try:
        proc = subprocess.run(
            ["hermes", "chat", "--yolo", "-q", prompt],
            capture_output=True, text=True, timeout=90,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            # 提取 box 中最后一行 (跳过 banner / "Query:" 等)
            lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
            # 跳过包含 banner / Session 元信息的行
            skip_kw = (
                "Hermes", "──", "─", "Session:", "Resume", "Initializing",
                "Query:", "Duration:", "Messages:", "skill", "─",
            )
            for ln in reversed(lines):
                if any(kw in ln for kw in skip_kw):
                    continue
                # 还跳过 ://, --resume 这种命令行 / URL
                if ln.startswith(("Resume ", "─", "Session", "Duration", "Messages")) or "://" in ln:
                    continue
                return ln[:200]
    except subprocess.TimeoutExpired:
        print(f"[warn] llm interpret timeout (90s)", file=sys.stderr)
    except Exception as e:
        print(f"[warn] llm interpret: {e}", file=sys.stderr)
    # fallback
    return sig_brief[:120]


# ──────────────────────────────────────────────────
# Core loop
# ──────────────────────────────────────────────────

# Daily push cap (防 spamming; 异常市况如新股上市/重大资产重组 可临时调大)
MAX_PUSHES_PER_DAY = 5  # Phase F: 3→5, 用户决定 (观察一周可调回)

# Min interval between pushes for the same symbol (秒); 防短时间连续触发
# Phase H (2026-07-14): 30min 太短, 同 5 sig 在 9h 内被推 30+ 次. 改为 4h — 足以
# 让用户消化前一条 push, 又有 6 次/天的余量汇报进展 (4h cooldown + 4 push 上限).
MIN_PUSH_INTERVAL_SEC = 4 * 60 * 60  # 4 h


def _signal_value(sig: dict) -> str:
    """提取 signal 的可比 value (dedup key).

    设计原则 (Phase G 2026-07-07 修复):
    - 公告类信号 → 用 announcement_id (同一公告只推一次)
    - 价量类信号 → 用 rule_id 单独作 key (数值微变是"持续状态"不是"新事件")
      旧版 bug: reason 文本带数值 (如 "MA5(94.45) < MA20(96.49)") 作 key → RSI 71.8→72.0
      或 MA5(94.45)→(94.99) 微变都被当成"新信号"每 5min 重推, 一天能推 30+ 次同一 rule.
    修复: 价量类只看 rule_id, 不看数值. 数值变化 → 同 rule 视为"未变"skip.
      用户只在"信号消失再触发"或"严重度升级"时才收到新 push.
    """
    rid = sig.get("rule_id", "")
    # 公告类规则 (Phase 7+: 港股 HKEX 4 条新增) → 用 announcement_id 精确去重
    if rid in ("major_event", "insider_reduction", "earnings_warning",
               "regulatory_penalty", "lockup_expiry",
               "share_buyback", "dividend_distribution",
               "convertible_securities", "spin_off"):
        # 公告类: 同一 announcement_id 视为同一事件
        ann_id = sig.get("announcement_id") or sig.get("link") or sig.get("reason", "")[:60]
        # key 必须带 rule_id 否则 5 条公告全归到一个 dedup entry
        return f"ann:{rid}:{ann_id}"
    # 价量类: 数值微变是"持续状态"不是"新事件", 用 rule_id 单独 dedup.
    # 副作用: 用户看不到"RSI 71.8→72.0"的升级, 但避免了"每 5min 重推同 rule" 的 spam.
    return f"price:{rid}"


# 连续 N 次轮询均未见某 signal 才视为真正消失。防 8084 短暂空响应导致
# state 被清空、下一轮同一信号被当成“新事件”重复推送。
SIGNAL_MISSING_POLLS_TO_EXPIRE = 3


def _reconcile_signal_state(
    prev_state: dict,
    signals: list,
    missing_threshold: int = SIGNAL_MISSING_POLLS_TO_EXPIRE,
) -> tuple[list, dict]:
    """合并本轮 signals 与上轮状态，返回 (to_push, next_state).

    新信号立即进入 to_push；已知信号保留 pushed_at。暂时缺失的信号只增加
    missing_count，连续 missing_threshold 轮缺失才删除，避免数据源抖动造成
    reset → re-trigger → spam。
    """
    to_push = []
    next_state: dict = {}
    seen_keys: set[tuple[str, str]] = set()

    # Phase H (2026-07-14): cooldown gate — 已推送过的 signal 在 cooldown 窗口内
    # 视为"已知且未变化",不入 to_push. 30min cooldown 后允许再推一次 (汇报进展).
    # 之前 bug: 30min cap 之后下一轮 5min 内 signal 仍 re-emit → 走 _signal_value
    # 相同 key → 跳过 dedup, 但 cap 通过 → 再推一次 → 9 小时 spam 几十条.
    # 修复: dedup 阶段就拦, 即"prev_meta 存在 + pushed_at 在 cooldown 内" → skip.
    # 触发再推条件 (任一):
    #   1) prev_meta 不存在 (新 signal / 数据源短暂空响应后恢复)
    #   2) severity 升级 (low → medium / medium → high)
    #   3) pushed_at 距今 ≥ COOLDOWN (用户已知状态更新)
    from datetime import datetime as _dt, timedelta as _td
    now_dt = _dt.now()
    cooldown_td = _td(seconds=MIN_PUSH_INTERVAL_SEC)

    for sig in signals:
        rid = sig["rule_id"]
        sig_key = _signal_value(sig)
        seen_keys.add((rid, sig_key))
        prev_rule = prev_state.get(rid) if isinstance(prev_state.get(rid), dict) else None
        prev_meta = prev_rule.get(sig_key) if isinstance(prev_rule, dict) else None

        should_push = True
        if prev_meta and prev_meta.get("value") == sig_key:
            # 已知 signal: 检查 daily rule dedup + cooldown + severity 升级
            # Phase H.2 (2026-07-14): "每日同 rule 至多推一次" — 同 5 sig 在 9h 内被推
            # 30+ 次, 4h cooldown 也救不了 4h+ 后的 re-emit. 加 daily dedup:
            # 同 sig_key 在今日已被推过 (pushed_at 起始日期 == 今天) → 静默, 除非
            # severity 升级 (用户需要知道风险加剧).
            prev_ts = prev_meta.get("pushed_at", "")
            prev_sev = prev_meta.get("severity", "info")
            new_sev = sig.get("severity", "info")
            sev_rank = {"info": 0, "low": 1, "medium": 2, "high": 3}
            sev_escalated = sev_rank.get(new_sev, 0) > sev_rank.get(prev_sev, 0)

            if prev_ts and prev_ts.startswith(now_dt.strftime("%Y-%m-%d")) and not sev_escalated:
                # 今日已推过本 sig + 无升级 → 静默
                should_push = False
            elif prev_ts:
                # 跨日或冷却逻辑: 仍受 cooldown 约束
                try:
                    if _dt.fromisoformat(prev_ts) + cooldown_td > now_dt and not sev_escalated:
                        should_push = False  # cooldown 内, 静默
                except ValueError:
                    pass
                # Phase H.3 (2026-07-14): §I 短暂缺失恢复 → 仍视为"已推过"
                # §I 防 8084 短抖 (1-3 轮空响应) 引起的 reset → re-trigger spam.
                # §H 原 §25.8 修复只在 cooldown 内有效; 跨日 + 跨 cooldown 场景
                # 下, §I 短暂缺失过的 signal 应继承 daily dedup 待遇 (prev_ts 今日
                # 检查已不适用, 但 missing_count > 0 是"已知状态"信号, 不能当新事件).
                # 缺这层 → pytest test_alert_daemon_debounce::test_one_transient_missing
                # 跨日 + 过 cooldown 场景 fail; 实际线上表现为"跨日早盘短抖恢复 spam".
                if int(prev_meta.get("missing_count", 0)) > 0 and not sev_escalated:
                    should_push = False
            if sev_escalated:
                should_push = True  # severity 升级, 立即推 (覆盖所有 dedup)

        if should_push:
            to_push.append(sig)
            next_state.setdefault(rid, {})[sig_key] = {
                "value": sig_key,
                "severity": sig.get("severity", "info"),
                "pushed_at": prev_meta.get("pushed_at", "") if prev_meta else "",
            }
        else:
            # cooldown 内: 保留 meta (含 pushed_at / missing_count)
            meta = dict(prev_meta)
            meta.pop("missing_count", None)
            next_state.setdefault(rid, {})[sig_key] = meta

    for rid, sigs in prev_state.items():
        if not isinstance(sigs, dict):
            continue
        for sig_key, meta in sigs.items():
            if (rid, sig_key) in seen_keys or not isinstance(meta, dict):
                continue
            missing_count = int(meta.get("missing_count", 0)) + 1
            if missing_count >= missing_threshold:
                continue
            held = dict(meta)
            held["missing_count"] = missing_count
            next_state.setdefault(rid, {})[sig_key] = held

    return to_push, next_state


def _daily_push_count(state: dict, sym: str) -> int:
    """今日已推送次数 (按 pushed_at 字段).

    state schema (Phase 5c+): {sym: {rule_id: {sig_key: meta}}}
    必须遍历三层才能拿到 meta 里的 pushed_at.
    旧版 (两层遍历) 在 schema 升级后返 0, cap 完全失效.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    sym_state = state.get(sym, {})
    n = 0
    for rule_id, sigs in sym_state.items():
        if isinstance(sigs, dict):
            for sig_key, meta in sigs.items():
                if isinstance(meta, dict):
                    if meta.get("pushed_at", "").startswith(today):
                        n += 1
    return n


def _last_push_time(state: dict, sym: str) -> str:
    """sym 最近一次推送时间 (ISO).

    必须遍历三层嵌套 (Phase 5c+ schema: {sym:{rule_id:{sig_key:meta}}}),
    旧版两层遍历在 schema 升级后永远返 "", cooldown 失效 → 同 sym 5min 重推.
    """
    sym_state = state.get(sym, {})
    latest = ""
    for rule_id, sigs in sym_state.items():
        if isinstance(sigs, dict):
            for sig_key, meta in sigs.items():
                if isinstance(meta, dict):
                    ts = meta.get("pushed_at", "")
                    if ts > latest:
                        latest = ts
    return latest


def _format_market_tag(sym: str) -> str:
    """A 股 / 港股 标签."""
    if sym.startswith("hk"):
        return "港股"
    if sym.isdigit():
        return "A股"
    return ""


def _build_combined_msg(groups: list[tuple[str, list]]) -> str:
    """合成 1 条飞书消息 (覆盖本轮所有 sym).

    groups: [(sym, to_push_signals)] 列表, 已通过 dedup/cap/cooldown 过滤.
    Phase K (2026-07-16): sym 后面拼接持仓中文名, 避免裸代码. Fallback 链:
    持仓 JSON name → sym 原样. 每个 tick 都 fresh 读 (持仓变动即时反映).
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_signals = sum(len(sigs) for _, sigs in groups)
    name_map = extract_symbol_name_map()
    header = f"🔔 **alert_daemon · {len(groups)} 标的 {total_signals} 触发**\n"

    sections = []
    for sym, sigs in groups:
        tag = _format_market_tag(sym)
        cn_name = name_map.get(sym) or sym
        if cn_name == sym:
            label = f"**{sym}**"
        else:
            label = f"**{cn_name}** ({sym})"
        title = f"📊 {label}" + (f" ({tag})" if tag else "")
        lines = []
        for s in sigs:
            sev = s.get("severity", "?").upper()
            name = s.get("name") or s["rule_id"]
            reason = s.get("reason", "")
            lines.append(f"  • [{sev}] {name}: {reason}")
        sections.append(f"{title}\n" + "\n".join(lines))

    body = "\n\n".join(sections)
    # 推送次数 / 持仓 reminder
    footer = f"\n\n⏰ {now}"
    return f"{header}\n{body}{footer}"


def poll_once(verbose: bool = True) -> int:
    """一轮轮询, 返触发推送"组合数" (每 tick 最多 1 条飞书).

    Phase F' (2026-07-04) 合并推送: 同一 tick 多 sym 的信号合并成 1 条飞书.
    Dedup / Cap / Cooldown 设计 (Phase 5c, per-sym 独立):
    - state[sym][rule_id][signal_key] = {value, severity, pushed_at} 三层嵌套
    - 同一 rule 但 value 变化 → 重推 (同 tick 仍合并)
    - 同一 rule value 未变 → skip
    - rule 消失 → 清掉
    - 同一 sym 每日最多 MAX_PUSHES_PER_DAY 次推送 (合并后按 sym 各计 1 次)
    - 同一 sym 两次推送间隔 ≥ MIN_PUSH_INTERVAL_SEC 秒

    返回 0 或 1 (当轮是否合并推送了). 不再返回 sym 数.
    """
    with acquire_lock():
        state = load_state()
        symbols = extract_symbols()
        pushed = 0
        now_iso = datetime.now().isoformat(timespec="seconds")

        if verbose:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] polling {len(symbols)} symbols: {symbols}")

        # Phase F': 先收集所有 sym 通过 cap/cooldown 过滤的 to_push, 再合并推 1 条
        push_groups: list[tuple[str, list, dict]] = []  # [(sym, to_push, new_state_for_sym)]

        for sym in symbols:
            result = call_dsa_mcp("check_alert", {"symbol": sym})
            if not result:
                continue

            signals = result.get("signals", [])
            prev_state = state.get(sym, {})

            # 8084 / dsa-mcp 偶发返回空 signals 时不要立即清空 state。
            # 连续 3 轮缺失才过期，避免 reset → 下一轮同一信号重推。
            if not result.get("triggered") or not signals:
                _, held_state = _reconcile_signal_state(prev_state, [])
                if held_state:
                    state[sym] = held_state
                    if verbose:
                        max_missing = max(
                            int(meta.get("missing_count", 0))
                            for sigs in held_state.values()
                            for meta in sigs.values()
                            if isinstance(meta, dict)
                        )
                        print(
                            f"  [hold] {sym} no triggers "
                            f"({max_missing}/{SIGNAL_MISSING_POLLS_TO_EXPIRE}), keep state"
                        )
                elif sym in state:
                    state.pop(sym, None)
                    if verbose:
                        print(f"  [expire] {sym} no triggers for {SIGNAL_MISSING_POLLS_TO_EXPIRE} polls")
                continue

            # 合并新旧 signals：新 key 推送；短暂缺失 key 先 hold，连续 3 轮才删除。
            to_push, new_state_for_sym = _reconcile_signal_state(prev_state, signals)

            if not to_push:
                if verbose:
                    print(f"  [skip] {sym} all signals unchanged")
                state[sym] = new_state_for_sym
                continue

            # 推送限额检查 (per-sym 仍生效 — Phase F' 没改 cap 语义)
            daily = _daily_push_count(state, sym)
            last_iso = _last_push_time(state, sym)
            if daily >= MAX_PUSHES_PER_DAY:
                if verbose:
                    print(f"  [cap] {sym} daily cap hit ({daily}/{MAX_PUSHES_PER_DAY}), skip {len(to_push)} signals")
                state[sym] = new_state_for_sym
                continue
            if last_iso:
                from datetime import datetime as _dt
                elapsed = (_dt.fromisoformat(last_iso) - _dt.now()).total_seconds()
                if elapsed > -MIN_PUSH_INTERVAL_SEC:
                    if verbose:
                        print(f"  [cool] {sym} last push {int(-elapsed)}s ago, skip (cool {MIN_PUSH_INTERVAL_SEC}s)")
                    state[sym] = new_state_for_sym
                    continue

            # 通过 cap/cooldown: 加入合并队列
            push_groups.append((sym, to_push, new_state_for_sym))
            if verbose:
                print(f"  [queue] {sym} {len(to_push)} signals (waiting merge push)")

        # Phase F': 收集完毕 → 合成 1 条 → 单次飞书推送
        if push_groups:
            msg = _build_combined_msg([(sym, sigs) for sym, sigs, _ in push_groups])
            ok = push_feishu(msg)
            if ok:
                pushed_ts = datetime.now().isoformat(timespec="seconds")
                # Phase A: 先 append 推送历史 (无论如何)
                append_history(pushed_ts, [(sym, sigs) for sym, sigs, _ in push_groups])
                for sym, sigs, new_state_for_sym in push_groups:
                    for sig in sigs:
                        rid = sig["rule_id"]
                        sig_key = _signal_value(sig)
                        new_state_for_sym.setdefault(rid, {})[sig_key] = {
                            "value": sig_key,
                            "severity": sig.get("severity", "info"),
                            "pushed_at": pushed_ts,
                        }
                    state[sym] = new_state_for_sym
                pushed = 1  # 当轮 1 条合并飞书
                if verbose:
                    syms_str = ",".join(s for s, _, _ in push_groups)
                    print(f"  [push-merged] {len(push_groups)} syms [{syms_str}] ({sum(len(s) for _,s,_ in push_groups)} signals total)")

        save_state(state)
        return pushed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="单次轮询后退出 (调试用)")
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL_SEC, help="轮询间隔秒")
    ap.add_argument("--all-hours", action="store_true", help="忽略交易时段限制")
    args = ap.parse_args()

    print(f"[start] alert_daemon pid={os.getpid()} interval={args.interval}s")
    print(f"[start] state={STATE_FILE} lock={LOCK_FILE} push={FEISHU_CHAT}")

    while True:
        if args.all_hours or trading_now():
            try:
                n = poll_once(verbose=True)
                # Phase F': n 含义变了 (0=没推, 1=合并推了 1 条). 顺便报合并了几 sym.
                print(f"[tick {datetime.now().strftime('%H:%M:%S')}] merged_push={n}")
            except Exception as e:
                print(f"[err] tick: {e}", file=sys.stderr)
        else:
            print(f"[sleep {datetime.now().strftime('%H:%M:%S')}] outside trading hours")
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()