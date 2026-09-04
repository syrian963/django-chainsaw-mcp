#!/usr/bin/env bash
# The contract check earns its place only if it separates the three cases a
# reviewer actually cares about: a change that breaks existing clients, a
# change that is safe, and a serializer it could not read at all. Getting the
# third one wrong is the worst outcome, because "removed" and "unreadable"
# look identical from the outside and only one of them is the API's fault.
cd "$(dirname "$0")" || exit 2
export PATH="$HOME/.local/bin:$PATH"
export DJANGO_CHAINSAW_PROJECT_PATH=testprojects
export DJANGO_CHAINSAW_SETTINGS_MODULE=demoshop.settings

BIN=./.venv/bin/django-chainsaw
SNAP=$(mktemp)
SRC=testprojects/shop/api_serializers.py
BACKUP=$(mktemp)
LOG=$(mktemp)
fails=0

cp "$SRC" "$BACKUP"
# An earlier suite may have mutated and restored these same files, and a
# .pyc left behind from that can outlive the source change it was built
# from. Start from a known state rather than inheriting one.
find testprojects -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null
cleanup() { cp "$BACKUP" "$SRC"; rm -f "$SNAP" "$BACKUP" "$LOG"; }
trap cleanup EXIT

# Every case mutates the same demo file, so a restore that silently does not
# happen would make the next case diagnose the previous case's edit. It is
# cheap to verify and it already caught one confusing failure.
restore() {
  cp "$BACKUP" "$SRC"
  if ! cmp -s "$BACKUP" "$SRC"; then
    printf '  FAIL  %-48s\n' "restoring the demo file"
    fails=$((fails + 1))
  fi
  # A correct file on disk is not enough. Python decides a cached .pyc is
  # still valid from the source's size and its mtime at one-second
  # granularity, and "internal_note" and "no_such_field" happen to be the
  # same length. Restoring inside the same second left the next process
  # importing the broken version out of __pycache__ while cmp reported the
  # source was identical, so the suite failed on the case after the one
  # that actually broke.
  find testprojects -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null
}

expect() {
  local want="$1" label="$2"; shift 2
  "$@" >"$LOG" 2>&1
  local got=$?
  if [ "$got" = "$want" ]; then
    printf '  ok    %-48s exit=%s\n' "$label" "$got"
  else
    printf '  FAIL  %-48s exit=%s (erwartet %s)\n' "$label" "$got" "$want"
    sed -n '1,6p' "$LOG" | sed 's/^/          /'
    fails=$((fails + 1))
  fi
}

says() {
  local label="$1" needle="$2"
  if grep -qi -- "$needle" "$LOG"; then
    printf '  ok    %-48s\n' "$label"
  else
    printf '  FAIL  %-48s (kein Treffer: %s)\n' "$label" "$needle"
    fails=$((fails + 1))
  fi
}

denies() {
  local label="$1" needle="$2"
  if grep -qi -- "$needle" "$LOG"; then
    printf '  FAIL  %-48s (unerwartet: %s)\n' "$label" "$needle"
    fails=$((fails + 1))
  else
    printf '  ok    %-48s\n' "$label"
  fi
}

# Declaring a field on a serializer without naming it in Meta.fields makes DRF
# assert, so both edits belong to the same fixture.
declare_field() {
  local decl="$1"
  python3 - "$decl" <<'PY'
import io, sys
decl = sys.argv[1]
name = decl.split(" =")[0].strip()
p = "testprojects/shop/api_serializers.py"
s = io.open(p, encoding="utf-8").read()
s = s.replace(
    "class OrderSerializer(serializers.ModelSerializer):",
    "class OrderSerializer(serializers.ModelSerializer):\n    " + decl + "\n",
    1,
)
s = s.replace('fields = ["id", "placed_at"]',
              'fields = ["id", "placed_at", "%s"]' % name, 1)
io.open(p, "w", encoding="utf-8").write(s)
PY
}

echo "API contract lifecycle"

# 1. Nothing recorded yet: say so and do not pretend to know anything.
expect 2 "no snapshot yet is an error, not a pass" \
  $BIN contract --snapshot "$SNAP"
says "and it says how to create one" "--update"

# 2. Record the shape clients currently rely on.
expect 0 "record the contract" \
  $BIN contract --snapshot "$SNAP" --update

# It has to work with no database. That is the whole premise: reading .choices
# off a related field opens a connection, and this project has no migrated
# database, so a regression there fails right here rather than in production.
if [ -f testprojects/db.sqlite3 ]; then
  printf '  FAIL  %-48s\n' "the demo project must have no database"
  fails=$((fails + 1))
else
  printf '  ok    %-48s\n' "resolved without a database"
fi

recorded=$(python3 -c "import json;print(json.load(open('$SNAP'))['serializer_count'])" 2>/dev/null)
if [ -n "$recorded" ] && [ "$recorded" -gt 0 ] 2>/dev/null; then
  printf '  ok    %-48s %s serializer(s)\n' "the snapshot has content" "$recorded"
else
  printf '  FAIL  %-48s recorded=%s\n' "the snapshot has content" "$recorded"
  fails=$((fails + 1))
fi

# 3. Unchanged code must be silent. A contract check that cries on every branch
#    gets switched off within a week.
expect 0 "unchanged code reports no change" \
  $BIN contract --snapshot "$SNAP" --fail-on-breaking
says "and says so in as many words" "unchanged"

# 4. Removing a field a client reads is the case this whole tool exists for.
sed -i 's/"id", "number", "internal_note"/"id", "number"/' "$SRC"
expect 1 "removing a read field fails the gate" \
  $BIN contract --snapshot "$SNAP" --fail-on-breaking
says   "the field is named"                  "internal_note"
says   "and it is called breaking"           "BREAKS CLIENTS"
restore

# 5. Adding an optional read-only field breaks nobody and must not block.
declare_field 'label = serializers.CharField(read_only=True)'
expect 0 "an added optional field does not block" \
  $BIN contract --snapshot "$SNAP" --fail-on-breaking
says   "it is still reported"                "label"
says   "classified as safe"                  "safe"
restore

# 6. A new required write field breaks every existing writer, even though it is
#    an addition. This is the distinction a plain field-set diff cannot make.
declare_field 'coupon = serializers.CharField(required=True)'
expect 1 "an added required field fails the gate" \
  $BIN contract --snapshot "$SNAP" --fail-on-breaking
says   "and the reason names the consequence" "400"
restore

# 7. A serializer that no longer instantiates is unknown, not removed. Calling
#    it "removed" would be a confident wrong answer.
sed -i 's/"id", "number", "internal_note"/"id", "number", "no_such_field"/' "$SRC"
expect 1 "an unresolvable serializer fails the gate" \
  $BIN contract --snapshot "$SNAP" --fail-on-breaking
says   "it is called unreadable"             "COULD NOT BE READ"
denies "and never called removed"            "serializer removed"
restore

# 8. Back to the recorded shape: green again.
expect 0 "restoring the code returns to green" \
  $BIN contract --snapshot "$SNAP" --fail-on-breaking

echo
if [ "$fails" -gt 0 ]; then
  echo "$fails Abweichung(en)"
  exit 1
fi
echo "API-Contract-Lebenszyklus bestaetigt"
