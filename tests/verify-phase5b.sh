#!/bin/bash
# Phase 5b ad-hoc verification (2026-07-02)
# Goal: prove alert-daemon + semantic announcement matching work end-to-end.
#
# 16-check bash verify. No `set -e` — failures must not abort.

PASS=0
FAIL=0

green() { printf '\033[32m%s\033[0m' "$1"; }
red()   { printf '\033[31m%s\033[0m' "$1"; }

check() {
    local desc="$1"; shift
    if "$@" >/dev/null 2>&1; then
        echo "  $(green PASS) $desc"
        PASS=$((PASS+1))
    else
        echo "  $(red FAIL) $desc"
        FAIL=$((FAIL+1))
    fi
}

echo "============================================================"
echo "Phase 5b ad-hoc verify — alert-daemon + semantic alert"
echo "$(date '+%F %T %Z')"
echo "============================================================"
echo

echo "[1] Daemon systemd status"
check "alert-daemon.service loaded" systemctl --user is-enabled alert-daemon.service
check "alert-daemon.service active"  systemctl --user is-active alert-daemon.service
echo

echo "[2] Script & deps"
check "alert_daemon.py exists & executable" test -x /home/zsd/.hermes/scripts/alert_daemon.py
check "dsa_mcp_call.py wrapper exists"      test -f /home/zsd/.hermes/scripts/dsa_mcp_call.py
check "dsa-mcp server stdio invokable" bash -c "python3 ~/.hermes/scripts/dsa_mcp_call.py list_strategies | grep -q bottom_volume"
check "8084 service running on :8084"       curl -sf -m 5 -o /dev/null http://localhost:8084/api/v1/stats
echo

echo "[3] dsa-mcp rules count (Phase 5b: 12 rules)"
out=$(python3 ~/.hermes/scripts/dsa_mcp_call.py list_alert_types 2>&1 | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")
echo "  rules count: $out"
[ "$out" -ge 12 ] \
    && { echo "  $(green PASS) >=12 alert rules"; PASS=$((PASS+1)); } \
    || { echo "  $(red FAIL) expected >=12 rules, got $out"; FAIL=$((FAIL+1)); }
echo

echo "[4] 8084 announcement endpoint"
out=$(curl -sf -m 5 'http://localhost:8084/api/v1/data?source=akshare&symbol=002202&data_type=announcement&fresh=false' | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('items',[])))")
echo "  002202 announcements: $out"
[ "$out" -ge 4 ] \
    && { echo "  $(green PASS) announcement endpoint returns 002202 items"; PASS=$((PASS+1)); } \
    || { echo "  $(red FAIL) announcement endpoint broken"; FAIL=$((FAIL+1)); }
echo

echo "[5] semantic unit test (checker.py any_of match)"
out=$(cd /home/zsd/codes/dsa-mcp && venv/bin/python -c "
import sys; sys.path.insert(0, 'src')
from dsa_mcp.alerts.checker import check_alert
ann = [{'title': '关于公司大股东减持股份的公告', 'announcement_time': '2026-07-02', 'link': 'http://x'}]
r = check_alert('000001', {}, None, announcements=ann)
ids = [s['rule_id'] for s in r['signals']]
print('OK' if 'insider_reduction' in ids else 'FAIL')
")
echo "  $out"
[ "$out" = "OK" ] \
    && { echo "  $(green PASS) insider_reduction keyword match works"; PASS=$((PASS+1)); } \
    || { echo "  $(red FAIL) insider_reduction not matched"; FAIL=$((FAIL+1)); }
echo

echo "[6] semantic unit test (regulatory_penalty keyword)"
out=$(cd /home/zsd/codes/dsa-mcp && venv/bin/python -c "
import sys; sys.path.insert(0, 'src')
from dsa_mcp.alerts.checker import check_alert
ann = [{'title': '公司被证监会立案调查', 'announcement_time': '2026-07-02', 'link': 'http://z'}]
r = check_alert('000001', {}, None, announcements=ann)
ids = [s['rule_id'] for s in r['signals']]
print('OK' if 'regulatory_penalty' in ids else 'FAIL')
")
echo "  $out"
[ "$out" = "OK" ] \
    && { echo "  $(green PASS) regulatory_penalty keyword match works"; PASS=$((PASS+1)); } \
    || { echo "  $(red FAIL) regulatory_penalty not matched"; FAIL=$((FAIL+1)); }
echo

echo "[7] pytest dsa-mcp suite (12 tests, 2 new semantic)"
out=$(cd /home/zsd/codes/dsa-mcp && venv/bin/python -m pytest -q 2>&1 | grep -E '^[0-9]+ passed' | head -1)
echo "  pytest: $out"
echo "$out" | grep -q "12 passed" \
    && { echo "  $(green PASS) pytest 12/12"; PASS=$((PASS+1)); } \
    || { echo "  $(red FAIL) pytest not 12 passed"; FAIL=$((FAIL+1)); }
echo

echo "[8] Dedup still works after semantic addition"
systemctl --user stop alert-daemon.service 2>/dev/null || true
sleep 1
rm -f /home/zsd/.hermes/alert_state.json /home/zsd/.hermes/.alert_daemon.lock
timeout 130 python3 -u /home/zsd/.hermes/scripts/alert_daemon.py --once --all-hours >/tmp/d1.log 2>&1 || true
timeout 30 python3 -u /home/zsd/.hermes/scripts/alert_daemon.py --once --all-hours >/tmp/d2.log 2>&1 || true
echo "=== run1 log ==="
cat /tmp/d1.log
echo "=== run2 log ==="
cat /tmp/d2.log
echo "=== state ==="
cat /home/zsd/.hermes/alert_state.json 2>&1
skip_count=$(grep -c '\[skip\]' /tmp/d2.log)
push_count=$(grep -c '\[push\]' /tmp/d2.log)
echo "  run2: skip=${skip_count} push=${push_count}"
if [ "$push_count" -eq 0 ] && [ "$skip_count" -ge 1 ]; then
    echo "  $(green PASS) dedup intact"
    PASS=$((PASS+1))
else
    echo "  $(red FAIL) dedup broken"
    FAIL=$((FAIL+1))
fi
systemctl --user start alert-daemon.service 2>&1 || true
echo

echo "[9] hermes send reachable"
check "hermes send CLI" command -v hermes
echo

echo "[10] rules.yaml semantic count"
out=$(python3 -c "
import yaml
with open('/home/zsd/codes/dsa-mcp/src/dsa_mcp/alerts/rules.yaml') as f:
    rules = yaml.safe_load(f)
ann_rules = [r for r in rules if any(c.get('field') == 'announcement_title' for c in r.get('conditions', []))]
print(f'{len(rules)} rules, {len(ann_rules)} semantic')
")
echo "  $out"
echo "$out" | grep -q "5 semantic" \
    && { echo "  $(green PASS) 5 semantic rules (含 major_event)"; PASS=$((PASS+1)); } \
    || { echo "  $(red FAIL) wrong semantic count"; FAIL=$((FAIL+1)); }
echo

echo "============================================================"
echo "RESULT: $(green "$PASS passed") / $([ $FAIL -gt 0 ] && red "$FAIL failed" || echo "$FAIL failed")"
echo "============================================================"
rm -f /tmp/d1.log /tmp/d2.log
exit $FAIL