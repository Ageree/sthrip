#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# Sthrip E2E Transfer Test Suite - Railway Production
# ═══════════════════════════════════════════════════════════════════════
set -uo pipefail

API_URL="https://sthrip-api-production.up.railway.app"
PASS=0; FAIL=0; SKIP=0

ok()   { echo "  PASS  $1"; PASS=$((PASS+1)); }
fail() { echo "  FAIL  $1 -- $2"; FAIL=$((FAIL+1)); }
skip() { echo "  SKIP  $1"; SKIP=$((SKIP+1)); }

echo "========================================================="
echo "  STHRIP E2E TRANSFER TEST SUITE"
echo "  API: $API_URL"
echo "========================================================="
echo ""

# ─── Step 0: Health ───
STATUS=$(curl -sf "$API_URL/health" | python3 -c "import json,sys;print(json.load(sys.stdin)['status'])")
[ "$STATUS" = "healthy" ] && ok "T00  Health check" || { fail "T00  Health" "$STATUS"; exit 1; }

# ─── Step 1: Register agents with unique names ───
TS=$(date +%s)
S_ADDR=$(python3 -c "print('4' + 'A' * 94)")
R_ADDR=$(python3 -c "print('4' + 'B' * 94)")

S_RESP=$(curl -s -X POST "$API_URL/v2/agents/register" -H "Content-Type: application/json" \
  -d "{\"agent_name\":\"snd-${TS}\",\"xmr_address\":\"$S_ADDR\"}")
S_KEY=$(echo "$S_RESP" | python3 -c "import json,sys;d=json.load(sys.stdin);assert 'api_key' in d,d;print(d['api_key'])")
S_NAME=$(echo "$S_RESP" | python3 -c "import json,sys;print(json.load(sys.stdin)['agent_name'])")

R_RESP=$(curl -s -X POST "$API_URL/v2/agents/register" -H "Content-Type: application/json" \
  -d "{\"agent_name\":\"rcv-${TS}\",\"xmr_address\":\"$R_ADDR\"}")
R_KEY=$(echo "$R_RESP" | python3 -c "import json,sys;d=json.load(sys.stdin);assert 'api_key' in d,d;print(d['api_key'])")
R_NAME=$(echo "$R_RESP" | python3 -c "import json,sys;print(json.load(sys.stdin)['agent_name'])")

ok "T01  Registered: sender=$S_NAME receiver=$R_NAME"

# ─── Step 2: Deposit ───
D=$(curl -s -X POST "$API_URL/v2/balance/deposit" \
  -H "Authorization: Bearer $S_KEY" -H "Content-Type: application/json" -d '{"amount":1.0}')
echo "$D" | python3 -c "import json,sys;assert json.load(sys.stdin).get('status')=='deposited'" 2>/dev/null && \
  ok "T02  Sender deposited 1.0 XMR" || fail "T02  Sender deposit" "$D"

D=$(curl -s -X POST "$API_URL/v2/balance/deposit" \
  -H "Authorization: Bearer $R_KEY" -H "Content-Type: application/json" -d '{"amount":0.1}')
echo "$D" | python3 -c "import json,sys;assert json.load(sys.stdin).get('status')=='deposited'" 2>/dev/null && \
  ok "T03  Receiver deposited 0.1 XMR" || fail "T03  Receiver deposit" "$D"

# ─── Step 3: Basic transfer ───
PAYLOAD=$(python3 -c "import json; print(json.dumps({'to_agent_name':'$R_NAME','amount':0.01,'memo':'test1'}))")
T=$(curl -s -X POST "$API_URL/v2/payments/hub-routing" \
  -H "Authorization: Bearer $S_KEY" -H "Content-Type: application/json" -d "$PAYLOAD")
T_STATUS=$(echo "$T" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('status','ERR'))" 2>/dev/null)
T1_FEE=$(echo "$T" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('fee','?'))" 2>/dev/null)
T1_PID=$(echo "$T" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('payment_id',''))" 2>/dev/null)
[ "$T_STATUS" = "confirmed" ] && ok "T04  Transfer 0.01 XMR, fee=$T1_FEE" || fail "T04  Transfer" "$T"

# ─── Step 4: Receiver balance ───
sleep 1
R_BAL=$(curl -s "$API_URL/v2/balance" -H "Authorization: Bearer $R_KEY" | python3 -c "import json,sys;print(json.load(sys.stdin)['available'])")
python3 -c "from decimal import Decimal; assert Decimal('$R_BAL') >= Decimal('0.11')" 2>/dev/null && \
  ok "T05  Receiver balance = $R_BAL" || fail "T05  Receiver balance" "got $R_BAL"

# ─── Step 5: Larger transfer ───
PAYLOAD=$(python3 -c "import json; print(json.dumps({'to_agent_name':'$R_NAME','amount':0.1}))")
T=$(curl -s -X POST "$API_URL/v2/payments/hub-routing" \
  -H "Authorization: Bearer $S_KEY" -H "Content-Type: application/json" -d "$PAYLOAD")
TS2=$(echo "$T" | python3 -c "import json,sys;print(json.load(sys.stdin).get('status','ERR'))")
[ "$TS2" = "confirmed" ] && ok "T06  Transfer 0.1 XMR" || fail "T06  Transfer 0.1" "$T"

# ─── Step 6: Micro transfer ───
PAYLOAD=$(python3 -c "import json; print(json.dumps({'to_agent_name':'$R_NAME','amount':0.0001}))")
T=$(curl -s -X POST "$API_URL/v2/payments/hub-routing" \
  -H "Authorization: Bearer $S_KEY" -H "Content-Type: application/json" -d "$PAYLOAD")
TS2=$(echo "$T" | python3 -c "import json,sys;print(json.load(sys.stdin).get('status','ERR'))")
[ "$TS2" = "confirmed" ] && ok "T07  Micro 0.0001 XMR" || fail "T07  Micro" "$T"

# ─── Step 7: Reverse transfer ───
PAYLOAD=$(python3 -c "import json; print(json.dumps({'to_agent_name':'$S_NAME','amount':0.005}))")
T=$(curl -s -X POST "$API_URL/v2/payments/hub-routing" \
  -H "Authorization: Bearer $R_KEY" -H "Content-Type: application/json" -d "$PAYLOAD")
TS2=$(echo "$T" | python3 -c "import json,sys;print(json.load(sys.stdin).get('status','ERR'))")
[ "$TS2" = "confirmed" ] && ok "T08  Reverse 0.005 XMR" || fail "T08  Reverse" "$T"

# ─── Step 8: Self-payment ───
PAYLOAD=$(python3 -c "import json; print(json.dumps({'to_agent_name':'$S_NAME','amount':0.001}))")
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$API_URL/v2/payments/hub-routing" \
  -H "Authorization: Bearer $S_KEY" -H "Content-Type: application/json" -d "$PAYLOAD")
[ "$CODE" = "400" ] && ok "T09  Self-payment rejected" || fail "T09  Self-payment" "HTTP $CODE"

# ─── Step 9: Non-existent recipient ───
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$API_URL/v2/payments/hub-routing" \
  -H "Authorization: Bearer $S_KEY" -H "Content-Type: application/json" -d '{"to_agent_name":"ghost-999","amount":0.001}')
[ "$CODE" = "404" ] && ok "T10  Non-existent recipient" || fail "T10  Non-existent" "HTTP $CODE"

# ─── Step 10: Insufficient balance ───
PAYLOAD=$(python3 -c "import json; print(json.dumps({'to_agent_name':'$R_NAME','amount':500}))")
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$API_URL/v2/payments/hub-routing" \
  -H "Authorization: Bearer $S_KEY" -H "Content-Type: application/json" -d "$PAYLOAD")
[ "$CODE" = "400" ] && ok "T11  Insufficient balance" || fail "T11  Insufficient" "HTTP $CODE"

# ─── Step 11: Zero amount ───
PAYLOAD=$(python3 -c "import json; print(json.dumps({'to_agent_name':'$R_NAME','amount':0}))")
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$API_URL/v2/payments/hub-routing" \
  -H "Authorization: Bearer $S_KEY" -H "Content-Type: application/json" -d "$PAYLOAD")
[ "$CODE" = "422" ] && ok "T12  Zero amount" || fail "T12  Zero" "HTTP $CODE"

# ─── Step 12: Over max ───
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$API_URL/v2/payments/hub-routing" \
  -H "Authorization: Bearer $S_KEY" -H "Content-Type: application/json" -d '{"to_agent_name":"x","amount":9999}')
[ "$CODE" = "422" ] && ok "T13  Over max amount" || fail "T13  Over max" "HTTP $CODE"

# ─── Step 13: Invalid name ───
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$API_URL/v2/payments/hub-routing" \
  -H "Authorization: Bearer $S_KEY" -H "Content-Type: application/json" -d '{"to_agent_name":"bad name!","amount":0.001}')
[ "$CODE" = "422" ] && ok "T14  Invalid name" || fail "T14  Invalid name" "HTTP $CODE"

# ─── Step 14: Idempotency ───
IDEM="idem-e2e-$(date +%s)"
PAYLOAD=$(python3 -c "import json; print(json.dumps({'to_agent_name':'$R_NAME','amount':0.001}))")
TA=$(curl -s -X POST "$API_URL/v2/payments/hub-routing" \
  -H "Authorization: Bearer $S_KEY" -H "Idempotency-Key: $IDEM" \
  -H "Content-Type: application/json" -d "$PAYLOAD")
PA=$(echo "$TA" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('payment_id',''))" 2>/dev/null)
sleep 2
TB=$(curl -s -X POST "$API_URL/v2/payments/hub-routing" \
  -H "Authorization: Bearer $S_KEY" -H "Idempotency-Key: $IDEM" \
  -H "Content-Type: application/json" -d "$PAYLOAD")
PB=$(echo "$TB" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('payment_id',''))" 2>/dev/null)
[ "$PA" = "$PB" ] && [ -n "$PA" ] && ok "T15  Idempotency OK" || fail "T15  Idempotency" "first='$PA' second='$PB'"

# ─── Step 15: Payment history ───
TOTAL=$(curl -s "$API_URL/v2/payments/history" -H "Authorization: Bearer $S_KEY" | python3 -c "import json,sys;print(json.load(sys.stdin).get('total',0))")
[ "$TOTAL" -ge 3 ] && ok "T16  Sender history: $TOTAL txs" || fail "T16  Sender history" "total=$TOTAL"

# ─── Step 16: Receiver incoming history ───
TOTAL=$(curl -s "$API_URL/v2/payments/history?direction=in" -H "Authorization: Bearer $R_KEY" | python3 -c "import json,sys;print(json.load(sys.stdin).get('total',0))")
[ "$TOTAL" -ge 1 ] && ok "T17  Receiver incoming: $TOTAL txs" || fail "T17  Receiver history" "total=$TOTAL"

# ─── Step 17: Payment lookup ───
if [ -n "$T1_PID" ]; then
    DETAIL=$(curl -s "$API_URL/v2/payments/$T1_PID" -H "Authorization: Bearer $S_KEY")
    DS=$(echo "$DETAIL" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('status',''))" 2>/dev/null)
    [ "$DS" = "confirmed" ] && ok "T18  Payment lookup: $DS" || fail "T18  Lookup" "status=$DS detail=$DETAIL"
else
    skip "T18  Payment lookup"
fi

# ─── Step 18: Rapid transfers ───
RAPID_OK=true
for i in 1 2 3 4 5; do
    PAYLOAD=$(python3 -c "import json; print(json.dumps({'to_agent_name':'$R_NAME','amount':0.001,'memo':'rapid-$i'}))")
    RT=$(curl -s -X POST "$API_URL/v2/payments/hub-routing" \
      -H "Authorization: Bearer $S_KEY" -H "Content-Type: application/json" -d "$PAYLOAD")
    RS=$(echo "$RT" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('status','ERR'))" 2>/dev/null)
    [ "$RS" != "confirmed" ] && { RAPID_OK=false; break; }
done
$RAPID_OK && ok "T19  Rapid 5x0.001" || fail "T19  Rapid" "failed at #$i"

# ─── Step 19: Invalid key ───
CODE=$(curl -s -o /dev/null -w "%{http_code}" "$API_URL/v2/balance" -H "Authorization: Bearer sk_bad")
[ "$CODE" = "401" ] && ok "T20  Invalid key" || fail "T20  Invalid key" "HTTP $CODE"

# ─── Step 20: Missing key ───
CODE=$(curl -s -o /dev/null -w "%{http_code}" "$API_URL/v2/balance")
[ "$CODE" = "401" ] || [ "$CODE" = "403" ] && ok "T21  Missing key" || fail "T21  Missing key" "HTTP $CODE"

# ─── Step 21: Discovery ───
DISC=$(curl -s "$API_URL/v2/agents?limit=500")
HAS=$(echo "$DISC" | python3 -c "import json,sys;ns=[i['agent_name'] for i in json.load(sys.stdin)['items']];print('y' if '$S_NAME' in ns and '$R_NAME' in ns else 'n')")
[ "$HAS" = "y" ] && ok "T22  Discovery" || fail "T22  Discovery" "has=$HAS"

# ─── Step 22: Profile ───
PNAME=$(curl -s "$API_URL/v2/agents/$R_NAME" | python3 -c "import json,sys;print(json.load(sys.stdin).get('agent_name',''))")
[ "$PNAME" = "$R_NAME" ] && ok "T23  Profile" || fail "T23  Profile" "name=$PNAME"

# ─── Step 23: Urgent transfer ───
PAYLOAD=$(python3 -c "import json; print(json.dumps({'to_agent_name':'$R_NAME','amount':0.001,'urgency':'urgent'}))")
T=$(curl -s -X POST "$API_URL/v2/payments/hub-routing" \
  -H "Authorization: Bearer $S_KEY" -H "Content-Type: application/json" -d "$PAYLOAD")
TS2=$(echo "$T" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('status','ERR'))")
FP=$(echo "$T" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('fee_percent','?'))")
[ "$TS2" = "confirmed" ] && ok "T24  Urgent fee=$FP" || fail "T24  Urgent" "$T"

# ─── Step 24: Additional deposit ───
BAL_B=$(curl -s "$API_URL/v2/balance" -H "Authorization: Bearer $S_KEY" | python3 -c "import json,sys;print(json.load(sys.stdin)['available'])")
curl -s -X POST "$API_URL/v2/balance/deposit" -H "Authorization: Bearer $S_KEY" -H "Content-Type: application/json" -d '{"amount":0.5}' >/dev/null
BAL_A=$(curl -s "$API_URL/v2/balance" -H "Authorization: Bearer $S_KEY" | python3 -c "import json,sys;print(json.load(sys.stdin)['available'])")
python3 -c "from decimal import Decimal; assert Decimal('$BAL_A') > Decimal('$BAL_B')" 2>/dev/null && \
  ok "T25  Extra deposit: $BAL_B -> $BAL_A" || fail "T25  Deposit" "before=$BAL_B after=$BAL_A"

# ─── Step 25: Reconciliation ───
sleep 1
SF=$(curl -s "$API_URL/v2/balance" -H "Authorization: Bearer $S_KEY" | python3 -c "import json,sys;print(json.load(sys.stdin)['available'])")
RF=$(curl -s "$API_URL/v2/balance" -H "Authorization: Bearer $R_KEY" | python3 -c "import json,sys;print(json.load(sys.stdin)['available'])")
RECON=$(python3 -c "
from decimal import Decimal
s, r = Decimal('$SF'), Decimal('$RF')
t = s + r
print('Sender:   {:.12f} XMR'.format(s))
print('Receiver: {:.12f} XMR'.format(r))
print('Total:    {:.12f} XMR'.format(t))
print('OK' if s > 0 and r > Decimal('0.1') else 'FAIL')
")
echo "$RECON" | grep -q "OK$" && ok "T26  Reconciliation" || { fail "T26  Reconcile" ""; echo "$RECON"; }

# ─── Summary ───
echo ""
echo "========================================================="
echo "  RESULTS:  PASS=$PASS  FAIL=$FAIL  SKIP=$SKIP"
echo "  TOTAL:    $((PASS+FAIL+SKIP))"
echo "========================================================="
[ "$FAIL" -eq 0 ] && echo "  ALL TESTS PASSED!" || echo "  $FAIL test(s) failed"
echo ""
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
