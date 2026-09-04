#!/usr/bin/env bash
# The value of this check is entirely in what it does NOT report. Finding a
# .delay() inside atomic() is easy; staying quiet about the same call wrapped
# in on_commit, and about the same call outside any transaction, is the part
# that decides whether anybody leaves it switched on.
cd "$(dirname "$0")" || exit 2
export PATH="$HOME/.local/bin:$PATH"
export DJANGO_CHAINSAW_PROJECT_PATH=testprojects
export DJANGO_CHAINSAW_SETTINGS_MODULE=demoshop.settings

BIN=./.venv/bin/django-chainsaw
SETTINGS=testprojects/demoshop/settings.py
SBACKUP=$(mktemp)
LOG=$(mktemp)
fails=0

cp "$SETTINGS" "$SBACKUP"
cleanup() {
  cp "$SBACKUP" "$SETTINGS"
  find testprojects -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null
  rm -f "$SBACKUP" "$LOG"
}
trap cleanup EXIT
find testprojects -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null

pass() { printf '  ok    %-52s %s\n' "$1" "${2:-}"; }
fail() { printf '  FAIL  %-52s %s\n' "$1" "${2:-}"; fails=$((fails + 1)); }

expect() {
  local want="$1" label="$2"; shift 2
  "$@" >"$LOG" 2>&1
  local got=$?
  if [ "$got" = "$want" ]; then
    pass "$label" "exit=$got"
  else
    fail "$label" "exit=$got (erwartet $want)"
    sed -n '1,4p' "$LOG" | sed 's/^/          /'
  fi
}

echo "Escaping side effects"

expect 1 "the demo project's defects fail the gate" \
  $BIN on-commit --fail-on-findings
expect 0 "without the gate it only reports" \
  $BIN on-commit

# Which functions the findings land in, resolved from the fixture at runtime.
# Asserting on line numbers directly would break every time the fixture grows,
# which has already happened three times in this project.
python3 - <<'PY'
import ast, io, json, os, subprocess, sys

src = "testprojects/shop/notifications.py"
tree = ast.parse(io.open(src, encoding="utf-8").read())
spans = {
    node.name: (node.lineno, node.end_lineno)
    for node in ast.walk(tree)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
}

def run(*extra):
    out = subprocess.run(
        ["./.venv/bin/django-chainsaw", "--json", "on-commit", *extra],
        capture_output=True, text=True, env={**os.environ},
    )
    return json.loads(out.stdout)

report = run()

def where(finding):
    for name, (start, end) in spans.items():
        if start <= finding["line"] <= end:
            return name
    return "?"

hit = {}
for f in report["findings"]:
    hit.setdefault(where(f), []).append(f["kind"])

fails = 0
def check(ok, label, detail=""):
    global fails
    if ok:
        print("  ok    %-52s %s" % (label, detail))
    else:
        print("  FAIL  %-52s %s" % (label, detail))
        fails += 1

# The four ways to lose, all in one function.
found = sorted(set(hit.get("place_order", [])))
check(found == ["cache_write", "email", "outbound_http", "task"],
      "all four escaping kinds found in one function", ",".join(found))

# Silence is the harder half.
check("place_order_correctly" not in hit,
      "the on_commit version reports nothing",
      ",".join(hit.get("place_order_correctly", [])))
check("send_receipt" not in hit,
      "code outside any transaction reports nothing",
      ",".join(hit.get("send_receipt", [])))

# A closed inner block does not end the outer transaction.
check("nested_still_counts" in hit,
      "a call after a closed inner block still counts")

# The `with` form, not just the decorator.
check("task" in hit.get("cancel_order", []),
      "the with-block form is found too")

# `.send()` is guessed from the name, so it stays out unless asked for.
low_default = [f for f in report["findings"] if f["confidence"] == "low"]
check(not low_default, "guessed calls are hidden by default",
      ",".join(f["call"] for f in low_default))

low_asked = [f for f in run("--include-low-confidence")["findings"]
             if f["confidence"] == "low"]
check(bool(low_asked), "and shown when asked for",
      ",".join(f["call"] for f in low_asked))

# The suggested fix has to be paste-ready, not a category name.
task = next(f for f in report["findings"] if f["kind"] == "task")
check(task["fix"].startswith("transaction.on_commit(lambda: ")
      and task["code"] in task["fix"],
      "the fix quotes the actual call")

sys.exit(1 if fails else 0)
PY
[ $? -eq 0 ] || fails=$((fails + 1))

# ATOMIC_REQUESTS is the case with nothing visible at the call site, so the
# tool has to volunteer it rather than wait to be asked.
python3 - <<'PY'
import io
p = "testprojects/demoshop/settings.py"
s = io.open(p, encoding="utf-8").read()
s = s.replace('"NAME": ":memory:",', '"NAME": ":memory:",\n        "ATOMIC_REQUESTS": True,')
io.open(p, "w", encoding="utf-8", newline="\n").write(s)
PY
find testprojects -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null

$BIN on-commit >"$LOG" 2>&1
if grep -q "ATOMIC_REQUESTS is on" "$LOG"; then
  pass "ATOMIC_REQUESTS is volunteered, not hidden"
else
  fail "ATOMIC_REQUESTS is volunteered, not hidden"
  sed -n '1,3p' "$LOG" | sed 's/^/          /'
fi
if grep -q "Every view runs inside a transaction" "$LOG"; then
  pass "and it says what that means"
else
  fail "and it says what that means"
fi

# The setting alone was the old behaviour and it left the largest class of this
# defect uncounted in exactly the projects that have it.
./.venv/bin/python - <<'PY'
import json, os, subprocess, sys
env = {**os.environ}
out = subprocess.run(["./.venv/bin/django-chainsaw", "--json", "on-commit"],
                     capture_output=True, text=True, env=env)
r = json.loads(out.stdout)
views = {f["view"].rsplit(".", 1)[-1] for f in r["atomic_request_views"]}
fails = 0
def check(ok, label, detail=""):
    global fails
    print(("  ok    %-52s %s" if ok else "  FAIL  %-52s %s") % (label, detail))
    if not ok:
        fails += 1

check("confirm" in views, "a function view wrapped by the setting is named")
check("create" in views, "and a viewset handler too")
check("helper" not in views, "a plain function with no request is not a view")
check("_internal" not in views, "and neither is a non-handler method")
sys.exit(1 if fails else 0)
PY
[ $? -eq 0 ] || fails=$((fails + 1))

cp "$SBACKUP" "$SETTINGS"
find testprojects -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null

$BIN on-commit >"$LOG" 2>&1
if grep -q "ATOMIC_REQUESTS is on" "$LOG"; then
  fail "and stays quiet when the setting is off"
else
  pass "and stays quiet when the setting is off"
fi

echo
if [ "$fails" -gt 0 ]; then
  echo "$fails Abweichung(en)"
  exit 1
fi
echo "Transaktions-Nebenwirkungen bestaetigt"
