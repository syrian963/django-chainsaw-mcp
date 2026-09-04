#!/usr/bin/env bash
# Project settings and inline suppression, on a copy so the fixtures stay clean.
cd "$(dirname "$0")" || exit 2
export PATH="$HOME/.local/bin:$PATH"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cp -r testprojects "$WORK/tp"

BIN=$PWD/.venv/bin/django-chainsaw
export DJANGO_CHAINSAW_PROJECT_PATH="$WORK/tp"
export DJANGO_CHAINSAW_SETTINGS_MODULE=demoshop.settings
fails=0

ok()  { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s\n' "$1"; shift; [ $# -gt 0 ] && printf '        %s\n' "$@"; fails=$((fails + 1)); }

echo "config and suppression"

baseline=$("$BIN" --json check --tenant-root shop.Customer 2>/dev/null \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['finding_count'])")

# --- tenant-root from pyproject.toml -----------------------------------------
cat > "$WORK/tp/pyproject.toml" <<'TOML'
[tool.django-chainsaw]
tenant-root = "shop.Customer"
TOML

out=$("$BIN" check 2>&1)
if echo "$out" | grep -q "Settings from"; then
  ok "the config file is found and reported"
else
  bad "the config file was not picked up" "$(echo "$out" | tail -2)"
fi

from_config=$("$BIN" --json check 2>/dev/null \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['finding_count'])")
if [ "$from_config" = "$baseline" ]; then
  ok "tenant-root from the file matches passing it as a flag ($baseline findings)"
else
  bad "the configured tenant root gave a different result" "flag=$baseline file=$from_config"
fi

# --- ignore patterns ----------------------------------------------------------
cat > "$WORK/tp/pyproject.toml" <<'TOML'
[tool.django-chainsaw]
tenant-root = "shop.Customer"
ignore = ["shop/reports.py"]
TOML

out=$("$BIN" check 2>&1)
if echo "$out" | grep -q "hidden by the ignore patterns"; then
  ok "ignore patterns are applied and reported, not silent"
else
  bad "ignored findings should be counted out loud" "$(echo "$out" | tail -3)"
fi
if ! echo "$out" | grep -q "shop/reports.py:38"; then
  ok "a finding in an ignored file is gone"
else
  bad "an ignored file still produced findings"
fi

# --- inline suppression, with and without a reason ---------------------------
rm -f "$WORK/tp/pyproject.toml"
python3 - "$WORK/tp/shop/api.py" <<'PY'
import io, sys
p = sys.argv[1]
lines = io.open(p, encoding="utf-8").read().splitlines()
# line 14 is `return Order.objects.get(pk=pk)`
lines[13] = lines[13] + "  # chainsaw: ignore[tenancy] - permission class checks the owner"
# line 19 is `return Order.objects.all()`
lines[18] = lines[18] + "  # chainsaw: ignore[tenancy]"
io.open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
PY

out=$("$BIN" check --tenant-root shop.Customer 2>&1)

if echo "$out" | grep -q "permission class checks the owner"; then
  ok "a suppression with a reason takes effect and the reason is shown"
else
  bad "the suppression with a reason did not apply" "$(echo "$out" | tail -4)"
fi

if echo "$out" | grep -q "did NOT take effect"; then
  ok "a suppression without a reason is refused, loudly"
else
  bad "a reasonless suppression must be refused, not silently honoured"
fi

if echo "$out" | grep -q "shop/api.py:19"; then
  ok "the refused suppression left its finding in place"
else
  bad "refusing a suppression must keep the finding"
fi

echo
if [ "$fails" -gt 0 ]; then
  echo "$fails Abweichung(en)"
  exit 1
fi
echo "Konfiguration und Unterdrueckung verhalten sich wie dokumentiert"
