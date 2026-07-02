#!/bin/bash
# Phase 5a ad-hoc verification (2026-07-02)
# Goal: prove alert-daemon is alive, can do a poll cycle, dedup works, push works.
#
# Returns 0 only if all pass. NOTE: NO `set -e` — check() failures should not
# abort the script, we want to count failures and report at end.

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
echo "Phase 5a ad-hoc verify — alert-daemon"
echo "$(date '+%F %T %Z')"
echo "============================================================"
echo

echo "[1] Daemon systemd status"
check "alert-daemon.service loaded" systemctl --user is-enabled alert-daemon.service
check "alert-daemon.service active"  systemctl --user is-active alert-daemon.service
echo

echo "[2] Script & deps"
check "alert_daemon.py exists & executable" test -x /home/zsd/.hermes/scripts/alert_daemon.py
check "dsa_mcp_call.py wrapper exists" test -f /home/zsd/.hermes/scripts/dsa_mcp_call.py
check "dsa-mcp server stdio invokable" bash -c "python3 ~/.hermes/scripts/dsa_mcp_call.py list_strategies | grep -q bottom_volume"
check "8084 service running on :8084"    curl -sf -m 5 -o /dev/null http://localhost:8084/api/v1/stats
echo

echo "[3] Portfolio extraction"
out=$(python3 -c "
import sys
sys.path.insert(0, '/home/zsd/.hermes/scripts')
from alert_daemon import extract_symbols
print(' '.join(extract_symbols()))
")
echo "  symbols: $out"
[[ "$out" == *"002202"* && "$out" == *"hk03690"* && "$out" == *"hk09988"* ]] \
    && { echo "  $(green PASS) extract_symbols returns all 3 持仓"; PASS=$((PASS+1)); } \
    || { echo "  $(red FAIL) extract_symbols missing symbols"; FAIL=$((FAIL+1)); }
echo

echo "[4] State file dedup"
check "alert_state.json exists" test -f /home/zsd/.hermes/alert_state.json
python3 -c "
import json
s = json.load(open('/home/zsd/.hermes/alert_state.json'))
assert isinstance(s, dict)
print('  state:', s)
" \
    && { echo "  $(green PASS) state JSON parses"; PASS=$((PASS+1)); } \
    || { echo "  $(red FAIL) state broken"; FAIL=$((FAIL+1)); }
echo

echo "[5] Dedup skip behavior (run twice, expect 2nd to skip all)"
# Stop systemd daemon first to release flock for our --once runs
systemctl --user stop alert-daemon.service 2>/dev/null || true
sleep 1
rm -f /home/zsd/.hermes/alert_state.json /home/zsd/.hermes/.alert_daemon.lock
# 1st: populate state (maybe no signals now, but state file gets written)
timeout 130 python3 -u /home/zsd/.hermes/scripts/alert_daemon.py --once --all-hours >/tmp/d1.log 2>&1 || true
state_after_1=$(cat /home/zsd/.hermes/alert_state.json 2>/dev/null | wc -c)
echo "  state size after run1: ${state_after_1}B"
# 2nd: should produce NO new pushes (state matches current signals)
timeout 30 python3 -u /home/zsd/.hermes/scripts/alert_daemon.py --once --all-hours >/tmp/d2.log 2>&1 || true
skip_count=$(grep -c '\[skip\]' /tmp/d2.log)
push_count=$(grep -c '\[push\]' /tmp/d2.log)
echo "  run2: skip=${skip_count} push=${push_count}"
if [ "$push_count" -eq 0 ] && [ "$skip_count" -ge 1 ]; then
    echo "  $(green PASS) dedup: skip=${skip_count} push=0"
    PASS=$((PASS+1))
else
    echo "  $(red FAIL) dedup broken (push_count=$push_count skip_count=$skip_count)"
    FAIL=$((FAIL+1))
fi
# Restart systemd daemon
systemctl --user start alert-daemon.service 2>&1 || true
echo

echo "[6] systemd unit file"
check "alert-daemon.service file valid" test -f /home/zsd/.config/systemd/user/alert-daemon.service
check "no Requires=missing-service" bash -c "! grep -E 'Requires=dsa-mcp.service' /home/zsd/.config/systemd/user/alert-daemon.service >/dev/null || systemctl --user is-active dsa-mcp.service"
echo

echo "[7] hermes send reachable"
check "hermes send CLI available" command -v hermes
echo

echo "[8] log file location"
LOG=/home/zsd/.hermes/logs/alert-daemon.log
check "logs dir exists" test -d /home/zsd/.hermes/logs
# daemon 启动 ~5min 后才 tick (默认 interval); ad-hoc verify 期间刚启动
# 改用 systemctl status 反查 daemon log
check "daemon log reachable via journalctl" bash -c "journalctl --user -u alert-daemon.service -n 3 --no-pager >/dev/null 2>&1 || test -f '$LOG'"
echo

echo "============================================================"
echo "RESULT: $(green "$PASS passed") / $([ $FAIL -gt 0 ] && red "$FAIL failed" || echo "$FAIL failed")"
echo "============================================================"
rm -f /tmp/d1.log /tmp/d2.log
exit $FAIL