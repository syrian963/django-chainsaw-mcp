#!/usr/bin/env bash
# --write must apply the mechanical class and nothing else. That boundary is
# the entire safety argument, so it is checked rather than asserted, on a copy
# of the demo project so a bug here cannot damage the fixtures.
cd "$(dirname "$0")" || exit 2
export PATH="$HOME/.local/bin:$PATH"

BIN=$PWD/.venv/bin/django-chainsaw
WORK=$(mktemp -d)
fails=0
trap 'rm -rf "$WORK"' EXIT

ok()  { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s\n' "$1"; shift; [ $# -gt 0 ] && printf '        %s\n' "$@"; fails=$((fails + 1)); }

cp -r testprojects "$WORK/tp"
export DJANGO_CHAINSAW_PROJECT_PATH="$WORK/tp"
export DJANGO_CHAINSAW_SETTINGS_MODULE=demoshop.settings

echo "fix --write"

# Nothing may change without --write.
before=$(md5sum "$WORK/tp/shop/scheduling.py" | cut -d' ' -f1)
"$BIN" fix --tenant-root shop.Customer >/dev/null 2>&1
after=$(md5sum "$WORK/tp/shop/scheduling.py" | cut -d' ' -f1)
if [ "$before" = "$after" ]; then
  ok "a run without --write changes nothing"
else
  bad "a run without --write must not touch any file"
fi

# Apply.
"$BIN" fix --tenant-root shop.Customer --write >/dev/null 2>&1

if grep -q "timezone.now()" "$WORK/tp/shop/scheduling.py"; then
  ok "the mechanical datetime fix was applied"
else
  bad "timezone.now() should have replaced the naive call"
fi

if ! grep -qE "datetime\.datetime\.now\(\)|dt\.utcnow\(\)" "$WORK/tp/shop/scheduling.py"; then
  ok "the naive calls are gone"
else
  bad "a naive call survived" "$(grep -nE 'datetime\.datetime\.now\(\)|dt\.utcnow\(\)' "$WORK/tp/shop/scheduling.py")"
fi

if [ "$(grep -c 'from django.utils import timezone' "$WORK/tp/shop/scheduling.py")" = "1" ]; then
  ok "the timezone import is present exactly once"
else
  bad "the import should be added once, not zero or twice"
fi

# The advisory class must be untouched.
if grep -q "return Order.objects.get(pk=pk)" "$WORK/tp/shop/api.py"; then
  ok "the advisory tenancy fix was NOT applied"
else
  bad "an advisory fix was applied, which breaks the safety boundary"
fi

if grep -q 'fields = "__all__"' "$WORK/tp/shop/api_serializers.py"; then
  ok "the advisory serializer fix was NOT applied"
else
  bad "the serializer was rewritten, which needs a human"
fi

# The generated class must not appear unless asked for.
if [ ! -d "$WORK/tp/shop/migrations" ] || ! ls "$WORK/tp/shop/migrations" | grep -q chainsaw; then
  ok "no generated migration was written without --write-generated"
else
  bad "a generated file appeared without being asked for"
fi

"$BIN" fix --tenant-root shop.Customer --write-generated >/dev/null 2>&1
if ls "$WORK/tp/shop/migrations" | grep -q chainsaw; then
  ok "--write-generated writes the migration"
  if grep -q "AddIndexConcurrently" "$WORK"/tp/shop/migrations/*chainsaw*; then
    ok "the generated migration uses the concurrent operation"
  else
    bad "an index migration without CONCURRENTLY locks the table"
  fi
  if grep -q "atomic = False" "$WORK"/tp/shop/migrations/*chainsaw*; then
    ok "atomic = False, which CONCURRENTLY requires"
  else
    bad "AddIndexConcurrently cannot run inside a transaction"
  fi
else
  bad "--write-generated produced nothing"
fi

# A second --write must be a no-op, not a double edit.
snapshot=$(md5sum "$WORK/tp/shop/scheduling.py" | cut -d' ' -f1)
"$BIN" fix --tenant-root shop.Customer --write >/dev/null 2>&1
if [ "$snapshot" = "$(md5sum "$WORK/tp/shop/scheduling.py" | cut -d' ' -f1)" ]; then
  ok "applying twice is idempotent"
else
  bad "a second --write changed the file again"
fi

echo
if [ "$fails" -gt 0 ]; then
  echo "$fails Abweichung(en)"
  exit 1
fi
echo "die Sicherheitsgrenze von --write haelt"
