#!/usr/bin/env bash
# Coverage across every suite that runs in this process, with a floor.
#
# Counting only pytest reported cli.py and server.py at 0% while both were
# being exercised the whole time - by script suites that spawn a subprocess,
# which coverage cannot follow. The in-process tests for both front ends exist
# because of that gap, and the first run of them found `choices` raising
# KeyError on every run that had something to report.
#
# The floor sits below the current number on purpose. A gate pinned to today's
# value fails on the next honest refactor, and that teaches people to raise the
# gate rather than read it.
set -u
cd "$(dirname "$0")" || exit 2
export PATH="$HOME/.local/bin:$PATH"
export DJANGO_CHAINSAW_PROJECT_PATH=testprojects
export DJANGO_CHAINSAW_SETTINGS_MODULE=demoshop.settings

FLOOR=${FLOOR:-78}

uv run coverage erase >/dev/null 2>&1

uv run coverage run --source=django_chainsaw_mcp -m pytest tests/ -q >/dev/null 2>&1
if [ "$?" -ne 0 ]; then
  echo "  FAIL  pytest ist unter coverage fehlgeschlagen"
  exit 1
fi

# The script suites drive the same code from the outside. They are appended so
# that a module only these reach is not counted as untested.
for script in smoke_test.py analysis_test.py client_test.py; do
  uv run coverage run --append --source=django_chainsaw_mcp "$script" >/dev/null 2>&1
done

report=$(uv run coverage report)
total=$(printf '%s' "$report" | tail -1 | awk '{print $NF}' | tr -d '%')

echo "  Gesamtabdeckung: ${total}%  (Untergrenze ${FLOOR}%)"

# Anything under 70% is worth a name: it is either untested or unreachable,
# and both are worth knowing which.
weak=$(printf '%s' "$report" | grep -E '^src/' | awk '{ gsub(/%/, "", $NF); if ($NF + 0 < 70) print "    " $1 "  " $NF "%" }')
if [ -n "$weak" ]; then
  echo "  Module unter 70%:"
  printf '%s\n' "$weak"
fi

if [ "$total" -lt "$FLOOR" ]; then
  echo "  FAIL  Abdeckung unter der Untergrenze"
  exit 1
fi
echo "  ok    Abdeckung ueber der Untergrenze"
