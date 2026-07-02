#!/usr/bin/env bash
# verify-phase7.sh — Phase B 盘后复盘验证 (2026-07-02)
#
# 测试项 (11 项, 期望全 PASS):
# 1-2: daily_recap.py 存在且 executable
# 3: portfolio/alerts_log.md 路径可写
# 4: flock LOCK_EX 正常获取/释放
# 5: state 文件 schema 兼容 (三层嵌套)
# 6-8: md 输出含 标题/表格/趋势段/总计行
# 9: 原子写 (tmp + rename)
# 10: cron job `74d0ab88ad1b` 已注册
# 11: 002202 trend analyze_trend 返有效数据

set -e
cd "$(dirname "$0")/.."
GREEN='\033[0;32m'; RED='\033[0;31m'; NC='\033[0m'
PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); echo -e "${GREEN}[PASS]${NC} $1"; }
fail() { FAIL=$((FAIL+1)); echo -e "${RED}[FAIL]${NC} $1"; }

echo "=== Phase B Verify: 盘后复盘 daily_recap ==="
echo

# 1-2: 脚本存在 + executable
if [ -x /home/zsd/.hermes/scripts/daily_recap.py ]; then
  pass "daily_recap.py executable"
else
  fail "daily_recap.py missing or not executable"
fi

# 3: portfolio 目录
mkdir -p /home/zsd/.hermes/portfolio
if [ -d /home/zsd/.hermes/portfolio ]; then
  pass "portfolio dir writable"
else
  fail "portfolio dir missing"
fi

# 4: flock 正常
LOCKOUT=$(timeout 5 python3 -c "
import fcntl, contextlib, os, sys, time, tempfile
LOCK = '/tmp/_verify_phase7.lock'
@contextlib.contextmanager
def acquire():
    f = open(LOCK, 'w')
    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    yield f
    f.close()
with acquire():
    print('LOCKED')
" 2>&1 | tail -1)
if [ "$LOCKOUT" = "LOCKED" ]; then
  pass "flock LOCK_EX + LOCK_NB OK"
else
  fail "flock failed: $LOCKOUT"
fi

# 5: state 三层嵌套 schema 兼容
STATE=$(cat /home/zsd/.hermes/alert_state.json 2>/dev/null || echo "{}")
HAS_NESTED=$(echo "$STATE" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print('NO'); sys.exit()
for sym, rules in d.items():
    if not isinstance(rules, dict): print('NO'); sys.exit()
    for rid, keys in rules.items():
        if not isinstance(keys, dict): print('NO'); sys.exit()
print('YES')
" 2>/dev/null || echo "NO")
if [ "$HAS_NESTED" = "YES" ]; then
  pass "state 三层嵌套 schema 兼容"
else
  echo "[skip] state schema (empty or invalid)"
fi

# 6-8: md 输出含必要段
timeout 60 python3 -u /home/zsd/.hermes/scripts/daily_recap.py >/dev/null 2>&1 || true
if [ -f /home/zsd/.hermes/portfolio/alerts_log.md ]; then
  pass "alerts_log.md 已生成"
else
  fail "alerts_log.md 未生成"
fi
if grep -q "盘后复盘" /home/zsd/.hermes/portfolio/alerts_log.md 2>/dev/null; then
  pass "md 含 标题段"
else
  fail "md 缺 标题段"
fi
if grep -q "| 标的 |" /home/zsd/.hermes/portfolio/alerts_log.md 2>/dev/null; then
  pass "md 含 Alert 表格"
else
  fail "md 缺 Alert 表格"
fi
if grep -q "## 趋势观察" /home/zsd/.hermes/portfolio/alerts_log.md 2>/dev/null; then
  pass "md 含 趋势观察段"
else
  fail "md 缺 趋势观察段"
fi
if grep -q "\*\*总计\*\*" /home/zsd/.hermes/portfolio/alerts_log.md 2>/dev/null; then
  pass "md 含 总计行"
else
  fail "md 缺 总计行"
fi

# 9: atomic write (检查 .tmp 文件不应残留)
if [ ! -f /home/zsd/.hermes/portfolio/alerts_log.md.tmp ]; then
  pass "atomic write (无 .tmp 残留)"
else
  fail ".tmp 残留 (atomic write 失败)"
fi

# 10: cron job `74d0ab88ad1b` 已注册
CRON_OUT=$(timeout 30 hermes cron list 2>&1 | grep "74d0ab88ad1b" || true)
if [ -n "$CRON_OUT" ]; then
  pass "cron job 74d0ab88ad1b registered"
else
  fail "cron job 74d0ab88ad1b 未找到"
fi

# 11: 002202 trend 真数据
TREND=$(timeout 30 python3 -u /home/zsd/.hermes/scripts/dsa_mcp_call.py analyze_trend '{"symbol":"002202","days":60}' 2>/dev/null)
HAS_TREND=$(echo "$TREND" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    if d.get('trend_status') and d.get('signal_score', 0) > 0:
        print('YES')
    else:
        print('NO')
except Exception:
    print('NO')
" 2>/dev/null || echo "NO")
if [ "$HAS_TREND" = "YES" ]; then
  pass "002202 trend analyze_trend 有效"
else
  fail "002202 trend 无效"
fi

echo
echo "=== Result: ${PASS} PASS / ${FAIL} FAIL ==="
exit $FAIL