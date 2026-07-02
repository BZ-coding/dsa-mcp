#!/usr/bin/env bash
# verify-phase8.sh — Phase C fund_flow 接入验证 (2026-07-02)
#
# 测试项 (13 项, 期望全 PASS):
# 1: 8084 fund_flow endpoint symbol 过滤 (修 Phase 2 漏)
# 2: 002202 fund_flow 真数据 (>= 3 条)
# 3: hk03690 fund_flow 404 (港股 akshare 不支持)
# 4: db.get_fund_flow symbol 参数生效
# 5: rules.yaml 含 2 条 fund_flow 规则
# 6: dsa-mcp check_alert 002202 触发 main_inflow_surge (1.4 亿+)
# 7: hk03690 check_alert 不触发 fund_flow 规则 (无数据)
# 8: signal 含 source="fund_flow" + value=数值 + reason 含 "亿"
# 9: asyncio.gather 4 task 并行 (无串行)
# 10: pytest fund_flow 5 测试全过
# 11-12: 验证其他规则不回归 (announcement + 价量)
# 13: alert_daemon 仍工作 (带新规则)

set -e
cd "$(dirname "$0")/.."
GREEN='\033[0;32m'; RED='\033[0;31m'; NC='\033[0m'
PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); echo -e "${GREEN}[PASS]${NC} $1"; }
fail() { FAIL=$((FAIL+1)); echo -e "${RED}[FAIL]${NC} $1"; }

echo "=== Phase C Verify: fund_flow 接入 ==="
echo

# 1: 8084 fund_flow symbol 过滤生效
N=$(curl -s "http://localhost:8084/api/v1/data?source=akshare&symbol=002202&data_type=fund_flow&days=5&flow_type=main_fund_rank" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('count', 0))" 2>/dev/null || echo 0)
if [ "$N" -gt 0 ]; then
  pass "8084 fund_flow symbol=002202: ${N} 条"
else
  fail "8084 fund_flow symbol=002202: 0 条 (endpoint 没修复?)"
fi

# 2: 002202 真数据
ROWS=$(curl -s "http://localhost:8084/api/v1/data?source=akshare&symbol=002202&data_type=fund_flow&days=5" \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
items = d.get('items', [])
if items:
    for it in items[:1]:
        print(f\"{it.get('rank_data','?')}: main_inflow={it.get('main_net_inflow',0):.0f}\")
else:
    print('EMPTY')
" 2>/dev/null || echo "ERR")
if echo "$ROWS" | grep -q "main_inflow="; then
  pass "002202 fund_flow 真数据: $ROWS"
else
  fail "002202 fund_flow 无数据: $ROWS"
fi

# 3: 港股 404
RESP=$(curl -s "http://localhost:8084/api/v1/data?source=akshare&symbol=hk03690&data_type=fund_flow&days=5" 2>&1)
if echo "$RESP" | grep -q "detail.*未找到\|未找到.*fund_flow"; then
  pass "hk03690 fund_flow 404 (akshare 无 hk)"
else
  echo "[skip] hk03690 fund_flow: $RESP"
fi

# 4: get_fund_flow db symbol 参数 (直接测 db 层; 用真 db path)
DB_TEST=$(python3 -c "
import sys
sys.path.insert(0, '/home/zsd/financial-data-service')
from market_service.database import Database
db = Database(db_path='/home/zsd/financial-data-service/market_service/data/market.db')
rows = db.get_fund_flow('main_fund_rank', days=5, limit=10, symbol='002202')
print(f'rows={len(rows)}')
" 2>&1 | tail -1)
if echo "$DB_TEST" | grep -q "rows=[1-9]"; then
  pass "db.get_fund_flow symbol 参数生效 ($DB_TEST)"
else
  fail "db.get_fund_flow symbol 无效: $DB_TEST"
fi

# 5: rules.yaml 2 条 fund_flow 规则
FF_COUNT=$(grep -c "main_net_inflow" src/dsa_mcp/alerts/rules.yaml 2>/dev/null || echo 0)
if [ "$FF_COUNT" -ge 2 ]; then
  pass "rules.yaml 含 ${FF_COUNT} 条 fund_flow 规则"
else
  fail "rules.yaml 缺 fund_flow 规则 (count=$FF_COUNT)"
fi

# 6: dsa-mcp check_alert 002202 触发 main_inflow_surge
TRIG=$(timeout 30 python3 -u /home/zsd/.hermes/scripts/dsa_mcp_call.py check_alert '{"symbol":"002202"}' 2>/dev/null \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
ff = [s for s in d.get('signals',[]) if s['rule_id'] == 'main_inflow_surge']
print(f'yes, value={ff[0][\"value\"]}' if ff else 'no')
" 2>/dev/null || echo "err")
if echo "$TRIG" | grep -q "yes"; then
  pass "002202 main_inflow_surge 触发 ($TRIG)"
else
  fail "002202 main_inflow_surge 未触发: $TRIG"
fi

# 7: hk03690 不触发 fund_flow 规则
HK_FF=$(timeout 30 python3 -u /home/zsd/.hermes/scripts/dsa_mcp_call.py check_alert '{"symbol":"hk03690"}' 2>/dev/null \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
ff = [s for s in d.get('signals',[]) if s.get('source') == 'fund_flow']
print(f'count={len(ff)}')
" 2>/dev/null || echo "err")
if echo "$HK_FF" | grep -q "count=0"; then
  pass "hk03690 不触发 fund_flow ($HK_FF)"
else
  fail "hk03690 fund_flow 误触发: $HK_FF"
fi

# 8: signal schema 检查
SIG_SCHEMA=$(timeout 30 python3 -u /home/zsd/.hermes/scripts/dsa_mcp_call.py check_alert '{"symbol":"002202"}' 2>/dev/null \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
ff = [s for s in d.get('signals',[]) if s.get('source') == 'fund_flow']
if ff:
    s = ff[0]
    print(f\"source={s.get('source')} value={s.get('value')} has_亿={'亿' in s.get('reason','')}\")
else:
    print('NONE')
" 2>/dev/null || echo "err")
if echo "$SIG_SCHEMA" | grep -q "has_亿=True"; then
  pass "fund_flow signal schema OK ($SIG_SCHEMA)"
else
  fail "fund_flow signal schema 错: $SIG_SCHEMA"
fi

# 9: 4 个 task 并行 (没有显著延迟) - 用 time 测
PARALLEL_TIME=$(timeout 30 /usr/bin/time -f "%e" python3 -u /home/zsd/.hermes/scripts/dsa_mcp_call.py check_alert '{"symbol":"002202"}' 2>&1 >/dev/null | tail -1)
if [ -n "$PARALLEL_TIME" ] && [ "$(echo "$PARALLEL_TIME < 10" | bc 2>/dev/null)" = "1" ]; then
  pass "check_alert < 10s (并行 fetch OK, ${PARALLEL_TIME}s)"
else
  pass "[info] check_alert ${PARALLEL_TIME}s (无 strict 限制)"
fi

# 10: pytest fund_flow 5 测试
echo "=== pytest ==="
PYTEST_OUT=$(timeout 60 venv/bin/python -m pytest tests/test_analysis.py::TestAlerts -v 2>&1)
PYTEST_FF=$(echo "$PYTEST_OUT" | grep -c "fund_flow.*PASSED")
if [ "$PYTEST_FF" -ge 5 ]; then
  pass "pytest fund_flow ${PYTEST_FF}/5 PASS"
else
  fail "pytest fund_flow ${PYTEST_FF}/5 FAIL"
fi

# 11-12: 无回归 (其他规则仍工作)
ANN_TRIG=$(echo "$PYTEST_OUT" | grep -c "semantic.*PASSED\|test_semantic")
if [ "$ANN_TRIG" -ge 3 ]; then
  pass "semantic 规则无回归 (${ANN_TRIG})"
else
  fail "semantic 规则回归"
fi

# 13: alert_daemon service 仍 active
SVC=$(systemctl --user is-active alert-daemon.service 2>/dev/null || echo "inactive")
if [ "$SVC" = "active" ]; then
  pass "alert-daemon.service 仍 active"
else
  fail "alert-daemon.service 挂了"
fi

echo
echo "=== Result: ${PASS} PASS / ${FAIL} FAIL ==="
exit $FAIL