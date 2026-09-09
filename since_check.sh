#!/usr/bin/env bash
# --since is only worth having if it reports what a branch added and stays
# quiet about what was already there. Both halves are checked against a real
# throwaway repository, because the merge-base logic is the whole point and a
# mock would not exercise it.
cd "$(dirname "$0")" || exit 2
export PATH="$HOME/.local/bin:$PATH"

REPO=$(mktemp -d)
BIN=$(cd . && pwd)/.venv/bin/django-chainsaw
fails=0

cleanup() { rm -rf "$REPO"; }
trap cleanup EXIT

note() { printf '  ok    %s\n' "$1"; }
bad()  { printf '  FAIL  %s\n' "$1"; shift; printf '        %s\n' "$@"; fails=$((fails + 1)); }

echo "--since lifecycle"

# A repository with the demo project inside it.
cp -r testprojects "$REPO/testprojects"
cd "$REPO" || exit 2
# `-b main` explicitly: git's default first-branch name is still `master`,
# and this script asks for `--since main` below. Without it the script passes
# only on machines whose `init.defaultBranch` is already `main` - which is why
# it went green here for four days and red the first time CI ran it.
git init -q -b main .
git config user.email t@example.com
git config user.name Test
git add -A
git commit -q -m "base"

export DJANGO_CHAINSAW_PROJECT_PATH="$REPO/testprojects"
export DJANGO_CHAINSAW_SETTINGS_MODULE=demoshop.settings

baseline_total=$("$BIN" --json tenancy --tenant-root shop.Customer 2>/dev/null \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['unscoped_count'])")

# 1. Nothing changed since the commit: nothing to report.
out=$("$BIN" tenancy --tenant-root shop.Customer --since HEAD 2>&1)
if echo "$out" | grep -q "0 candidate(s) in changed files"; then
  note "a clean tree reports nothing, total was $baseline_total"
else
  bad "a clean tree should report nothing" "$(echo "$out" | tail -2)"
fi

# 2. Exit code stays 0 when the gate is on but nothing changed.
"$BIN" tenancy --tenant-root shop.Customer --since HEAD --fail-on-findings >/dev/null 2>&1
if [ $? -eq 0 ]; then
  note "the gate passes when the branch changed nothing"
else
  bad "the gate must pass when the branch changed nothing"
fi

# 3. A branch introduces one leak.
git checkout -q -b feature
cat > testprojects/shop/new_view.py <<'PY'
from .models import Order


def leak(request, pk):
    return Order.objects.get(pk=pk)
PY

out=$("$BIN" tenancy --tenant-root shop.Customer --since main 2>&1 \
      || "$BIN" tenancy --tenant-root shop.Customer --since master 2>&1)
if echo "$out" | grep -q "new_view.py"; then
  note "the new leak is reported"
else
  bad "the new leak should be reported" "$(echo "$out" | tail -3)"
fi
if echo "$out" | grep -q "1 candidate(s) in changed files"; then
  note "only the new leak is reported, not the pre-existing ones"
else
  bad "only the branch's own finding should be reported" "$(echo "$out" | tail -2)"
fi

# 4. With the gate on, that fails the build.
"$BIN" tenancy --tenant-root shop.Customer --since main --fail-on-findings >/dev/null 2>&1
rc=$?
if [ $rc -eq 1 ]; then
  note "the gate fails on a finding the branch added"
else
  bad "the gate should fail on a new finding" "exit=$rc"
fi

# 5. An unknown ref must warn and fall back, never crash or silently pass.
out=$("$BIN" tenancy --tenant-root shop.Customer --since no-such-ref 2>&1)
if echo "$out" | grep -q "ignored, git said"; then
  note "an unknown ref warns and falls back to the full report"
else
  bad "an unknown ref should warn, not crash" "$(echo "$out" | tail -2)"
fi

echo
if [ "$fails" -gt 0 ]; then
  echo "$fails Abweichung(en)"
  exit 1
fi
echo "--since verhaelt sich wie dokumentiert"
