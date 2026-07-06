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

# 2026-07-07: revert 改回 home channel (oc_29c414134c106a728ab5a91d56863cd4)
# 历史 daemon 一直推到 home (DM with ou_01c88c62c4f14c0f37dc5f73ee7c9ae2, 用户当前 DM session)
# 某时间点被改成 "oc_90857aaa9c3a4533fdc12c008ca14d00" 注释为 "用户从该群发起的请求"
# 但当前 session 是 DM 不是群, 而且 verify cron (725de2454fe3) deliver 也是 home,
# 全部 cron 都 deliver 到 oc_29c4141, alert-daemon 应该跟 cron 同目标避免 chat 错位
# skill alert-daemon-pattern §36 改 chat 标准流程: probe + 注释 + log 验证
FEISHU_CHAT = "feishu:oc_29c414134c106a728ab5a91d56863cd4"

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
MIN_PUSH_INTERVAL_SEC = 30 * 60  # 30 min


def _signal_value(sig: dict) -> str:
    """提取 signal 的可比 value (rule_id + 数值). 数值变化才重推同 rule.

    Phase 5c 增强:
    - announcement 类信号 → 用 announcement_id (同一公告只推一次)
      key = (rule_id, announcement_id) — 同一 rule 多个公告 各算一个 dedup entry
    - 价量类信号 → 用 reason[:60] (数值变化才算变)
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
    # 价量类: reason 文本作为变化信号 (RSI 70→80 → 文本不同)
    reason = sig.get("reason", "") or ""
    meta = sig.get("metadata") or {}
    return f"{rid}|{meta.get('value', '')}|{reason[:60]}"


def _daily_push_count(state: dict, sym: str) -> int:
    """今日已推送次数 (按 pushed_at 字段)."""
    today = datetime.now().strftime("%Y-%m-%d")
    sym_state = state.get(sym, {})
    n = 0
    for rule_id, meta in sym_state.items():
        if isinstance(meta, dict):
            if meta.get("pushed_at", "").startswith(today):
                n += 1
    return n


def _last_push_time(state: dict, sym: str) -> str:
    """sym 最近一次推送时间 (ISO)."""
    sym_state = state.get(sym, {})
    latest = ""
    for rule_id, meta in sym_state.items():
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
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_signals = sum(len(sigs) for _, sigs in groups)
    header = f"🔔 **alert_daemon · {len(groups)} 标的 {total_signals} 触发**\n"

    sections = []
    for sym, sigs in groups:
        tag = _format_market_tag(sym)
        title = f"📊 **{sym}**" + (f" ({tag})" if tag else "")
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

            if not result.get("triggered") or not signals:
                # 全部规则都消失 → 清掉 sym 状态
                if sym in state:
                    state.pop(sym, None)
                    if verbose:
                        print(f"  [reset] {sym} no triggers, cleared state")
                continue

            # 找出需要重推的 signals:
            # - 新增 signal key (prev_state[rid][signal_key] 不存在)
            # - signal value 变化
            to_push = []
            new_state_for_sym: dict = {}
            for sig in signals:
                rid = sig["rule_id"]
                sig_key = _signal_value(sig)
                prev_rule = prev_state.get(rid) if isinstance(prev_state.get(rid), dict) else None
                prev_meta = prev_rule.get(sig_key) if isinstance(prev_rule, dict) else None
                if prev_meta and prev_meta.get("value") == sig_key:
                    # value 未变 → 保留 prev meta (含 pushed_at)
                    new_state_for_sym.setdefault(rid, {})[sig_key] = prev_meta
                else:
                    # 新增 or value 变 → 列入推送队列
                    to_push.append(sig)
                    new_state_for_sym.setdefault(rid, {})[sig_key] = {
                        "value": sig_key,
                        "severity": sig.get("severity", "info"),
                        "pushed_at": "",  # 实际推送后才填
                    }

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