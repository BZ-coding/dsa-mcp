#!/usr/bin/env python3
"""
daily_recap.py — 盘后复盘 (Phase B, 2026-07-02)

功能:
- 读 ~/.hermes/alert_state.json (当日推送记录)
- 调 dsa-mcp analyze_trend 取每个 sym 趋势
- 输出 ~/.hermes/portfolio/alerts_log.md (md 表格)
- cron 16:00 工作日跑, 自动 append 日期段

Iron rule:
- 不调 LLM 生成解读 (确定性逻辑 + 模板, 避免 hallucination)
- 不修改 alert_state.json (只读)
- 输出 atomic write (tmp + rename), 防止写到一半被 cron 中断
"""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERMES_HOME = Path("/home/zsd/.hermes")
STATE_FILE = HERMES_HOME / "alert_state.json"
HISTORY_FILE = HERMES_HOME / "alert_history.jsonl"
LOCK_FILE = HERMES_HOME / ".daily_recap.lock"
DSA_MCP_CALL = HERMES_HOME / "scripts" / "dsa_mcp_call.py"
LOG_FILE = HERMES_HOME / "portfolio" / "alerts_log.md"
PORTFOLIO_FILES = [
    HERMES_HOME / "portfolio" / "lidan_stocks.json",
    HERMES_HOME / "portfolio" / "zsd_hk_stocks.json",
]


def extract_symbols() -> list[str]:
    """从持仓 JSON 抽出 symbol 列表 (去重, 跳过基金)."""
    symbols = set()
    for f in PORTFOLIO_FILES:
        if not f.exists():
            continue
        try:
            d = json.loads(f.read_text())
            for p in d.get("positions", []) or []:
                sym = p.get("symbol") or p.get("code")
                if sym and not sym.startswith(("0", "1", "2", "3", "5", "6", "9")) and not sym.startswith("hk"):
                    continue
                if sym:
                    symbols.add(sym)
        except Exception as e:
            print(f"[warn] read {f}: {e}", file=sys.stderr)
    return sorted(symbols)


def load_history_signals(history_file: Path, today: str) -> list[dict]:
    """从 append-only history 读取当天所有真实推送，不受当前 state 淘汰影响。"""
    out = []
    if not history_file.exists():
        return out
    for line in history_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = record.get("ts", "")
        if not ts.startswith(today):
            continue
        for group in record.get("groups", []) or []:
            sym = group.get("sym", "?")
            for sig in group.get("signals", []) or []:
                out.append({
                    "sym": sym,
                    "rule_id": sig.get("rule_id", "?"),
                    "severity": sig.get("severity", "info"),
                    "value": sig.get("sig_key") or sig.get("reason", ""),
                    "reason": sig.get("reason", ""),
                    "pushed_at": ts,
                })
    return out


def load_today_signals(state: dict, today: str) -> list[dict]:
    """从 state 抽今日推送的 signals.
    返 [{sym, rule_id, severity, value, pushed_at}]."""
    out = []
    for sym, rules in state.items():
        if not isinstance(rules, dict):
            continue
        for rid, keys in rules.items():
            if not isinstance(keys, dict):
                continue
            for sig_key, meta in keys.items():
                if not isinstance(meta, dict):
                    continue
                ts = meta.get("pushed_at", "")
                if ts.startswith(today):
                    out.append({
                        "sym": sym,
                        "rule_id": rid,
                        "severity": meta.get("severity", "info"),
                        "value": sig_key,  # 包含 announcement_id 或 reason
                        "pushed_at": ts,
                    })
    return out


def call_dsa_mcp(tool: str, args: dict, timeout: int = 30) -> dict | None:
    """调 dsa-mcp stdio CLI."""
    try:
        proc = subprocess.run(
            [sys.executable, str(DSA_MCP_CALL), tool, json.dumps(args, ensure_ascii=False)],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout)
    except Exception:
        return None


@contextlib.contextmanager
def acquire_lock():
    """flock 防并发 (cron + 手工同时跑)."""
    LOCK_FILE.touch()
    f = open(LOCK_FILE, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield f
    finally:
        f.close()


def format_trend(trend: dict | None) -> str:
    """格式化单个 sym 的趋势观察."""
    if not trend or "error" in trend:
        return "_(数据不可用)_"
    status = trend.get("trend_status", "?")
    strength = trend.get("trend_strength", "?")
    ma_align = trend.get("ma_alignment", "")
    vol_status = trend.get("volume_status", "")
    buy_signal = trend.get("buy_signal", "")
    score = trend.get("signal_score", "?")
    ma5 = trend.get("ma5", 0)
    ma20 = trend.get("ma20", 0)
    current = trend.get("current_price", 0)
    return (
        f"- **趋势**: {status} ({ma_align})\n"
        f"- **强度**: {strength}/100\n"
        f"- **关键**: MA5={ma5:.2f}, MA20={ma20:.2f}, 现价={current:.2f} ({vol_status})\n"
        f"- **建议**: {buy_signal} (score {score})"
    )


def format_signals_table(signals: list[dict]) -> str:
    """md 表格."""
    if not signals:
        return "_当日无 alert 推送_"
    lines = [
        "| 标的 | 规则 | 严重度 | 数值/时间 | 推送时间 |",
        "|------|------|--------|-----------|----------|",
    ]
    for s in signals:
        v = s["value"]
        if v.startswith("ann:"):
            # 公告类: ann:major_event:ann_id → 显示 announcement_id
            v_short = v.split(":")[-1] if ":" in v else v
            v_md = f"公告 #{v_short}"
        else:
            # 价量类优先显示原始 reason；旧 state 无 reason 时才回退到 key。
            reason = s.get("reason", "")
            if reason:
                v_md = reason[:50]
            else:
                v_short = v.split("|", 1)[-1] if "|" in v else v
                v_md = v_short[:50]
        lines.append(
            f"| {s['sym']} | {s['rule_id']} | {s['severity']} | {v_md} | {s['pushed_at']} |"
        )
    return "\n".join(lines)


def generate_recap(today: str | None = None, verbose: bool = True) -> str:
    """生成当日盘后复盘 md."""
    with acquire_lock():
        now = datetime.now()
        today = today or now.strftime("%Y-%m-%d")
        symbols = extract_symbols()
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        # history 是当日真实推送的权威源；state 只是当前 dedup 状态，信号消失会淘汰。
        signals = load_history_signals(HISTORY_FILE, today)
        if not signals:
            signals = load_today_signals(state, today)  # 兼容尚无 history 的旧部署

        # 调 trend for each sym
        trends = {}
        for sym in symbols:
            t = call_dsa_mcp("analyze_trend", {"symbol": sym, "days": 60})
            trends[sym] = t

        # 组装 md
        md_lines = [
            f"# {today} 盘后复盘",
            "",
            f"**生成时间**: {now.strftime('%H:%M:%S')} CST",
            f"**持仓标的**: {', '.join(symbols)}",
            "",
            "## 当日 Alert 触发",
            "",
            format_signals_table(signals),
            "",
            f"**总计**: {len(signals)} signals / {len(set(s['sym'] for s in signals))} sym",
            "",
            "## 趋势观察 (dsa-mcp analyze_trend)",
            "",
        ]
        SYM_NAMES = {
            "002202": "金风科技 (A股)",
            "hk03690": "美团-W (港股)",
            "hk09988": "阿里巴巴-W (港股)",
        }
        for sym in symbols:
            name = SYM_NAMES.get(sym, sym)
            md_lines.append(f"### {sym} {name}")
            md_lines.append("")
            md_lines.append(format_trend(trends.get(sym)))
            md_lines.append("")

        md_lines.append("## 下日看点")
        md_lines.append("")
        md_lines.append("_(自动生成; 需要更多上下文请手动补充)_")
        md_lines.append("")

        content = "\n".join(md_lines)

        # Atomic write
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = LOG_FILE.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.rename(LOG_FILE)

        if verbose:
            print(f"[recap] wrote {LOG_FILE} ({len(content)} bytes, {len(signals)} signals)")

        return content


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="指定日期 (默认今日, YYYY-MM-DD)")
    args = ap.parse_args()
    generate_recap(today=args.date, verbose=True)


if __name__ == "__main__":
    main()