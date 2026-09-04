#!/usr/bin/env bash
# The baseline is only worth having if it does three things: accept what was
# already there, fail on something new, and notice when something is fixed.
cd "$(dirname "$0")" || exit 2
export PATH="$HOME/.local/bin:$PATH"
export DJANGO_CHAINSAW_PROJECT_PATH=testprojects
export DJANGO_CHAINSAW_SETTINGS_MODULE=demoshop.settings

BIN=./.venv/bin/django-chainsaw
BL=$(mktemp)
NEW=testprojects/shop/leaky.py
fails=0

cleanup() { rm -f "$BL" "$NEW"; }
trap cleanup EXIT

expect() {
  local want="$1" label="$2"; shift 2
  "$@" >/tmp/baseline_step.log 2>&1
  local got=$?
  if [ "$got" = "$want" ]; then
    printf '  ok    %-46s exit=%s\n' "$label" "$got"
  else
    printf '  FAIL  %-46s exit=%s (erwartet %s)\n' "$label" "$got" "$want"
    sed -n '$p' /tmp/baseline_step.log | sed 's/^/          /'
    fails=$((fails + 1))
  fi
}

echo "Baseline lifecycle"

# 1. No baseline yet: report, do not block.
expect 0 "no baseline recorded yet" \
  $BIN tenancy --tenant-root shop.Customer --baseline "$BL"

# 2. Record it.
expect 0 "record the baseline" \
  $BIN tenancy --tenant-root shop.Customer --baseline "$BL" --update-baseline

# Compare against what the tool itself reports rather than a fixed number, so
# adding fixtures to the demo project does not break this check.
reported=$($BIN --json tenancy --tenant-root shop.Customer 2>/dev/null \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['unscoped_count'])" 2>/dev/null)
recorded=$(python3 -c "import json; print(json.load(open('$BL'))['checks']['tenancy']['count'])" 2>/dev/null)
if [ -n "$recorded" ] && [ "$recorded" = "$reported" ]; then
  printf '  ok    %-46s %s findings\n' "baseline matches what the tool reports" "$recorded"
else
  printf '  FAIL  %-46s recorded=%s reported=%s\n' "baseline file contents" "$recorded" "$reported"
  fails=$((fails + 1))
fi

# 3. Same code, everything known: must not block.
expect 0 "known findings do not block" \
  $BIN tenancy --tenant-root shop.Customer --baseline "$BL"

# 4. A new leak appears.
cat > "$NEW" <<'PY'
from .models import Invoice


def leak(request, pk):
    return Invoice.objects.filter(pk=pk).first()
PY

expect 1 "a new finding fails the gate" \
  $BIN tenancy --tenant-root shop.Customer --baseline "$BL"

if grep -q "leaky.py" /tmp/baseline_step.log; then
  printf '  ok    %-46s\n' "the new finding is named in the output"
else
  printf '  FAIL  %-46s\n' "the new finding is named in the output"
  fails=$((fails + 1))
fi

# 5. Remove it again: back to green, and the report says nothing was fixed.
rm -f "$NEW"
expect 0 "removing it returns to green" \
  $BIN tenancy --tenant-root shop.Customer --baseline "$BL"

# 6. Fixing a baselined finding is reported as fixed.
cp testprojects/shop/api.py /tmp/api_backup.py
python3 - <<'PY'
import io
p = "testprojects/shop/api.py"
s = io.open(p, encoding="utf-8").read()
s = s.replace("    return Order.objects.all()",
              "    return Order.objects.filter(customer=request.user.customer)")
io.open(p, "w", encoding="utf-8").write(s)
PY
$BIN tenancy --tenant-root shop.Customer --baseline "$BL" >/tmp/baseline_step.log 2>&1
if grep -q "Fixed since the baseline" /tmp/baseline_step.log; then
  printf '  ok    %-46s\n' "a fixed finding is reported as fixed"
else
  printf '  FAIL  %-46s\n' "a fixed finding is reported as fixed"
  fails=$((fails + 1))
fi
cp /tmp/api_backup.py testprojects/shop/api.py
rm -f /tmp/api_backup.py

echo
if [ "$fails" -gt 0 ]; then
  echo "$fails Abweichung(en)"
  exit 1
fi
echo "Baseline-Lebenszyklus bestaetigt"
