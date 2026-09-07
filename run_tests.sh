#!/usr/bin/env bash
# Run every suite. Exits non-zero if any of them fails.
cd "$(dirname "$0")" || exit 2

# uv installs to ~/.local/bin, which a non-interactive shell does not pick up.
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null || { echo "uv not found on PATH"; exit 2; }

fails=0

suite() {
  local label="$1"; shift
  printf '%-34s ' "$label"
  if "$@" >/tmp/chainsaw_suite.log 2>&1; then
    printf 'OK\n'
  else
    printf 'FAIL\n'
    sed -n '$p' /tmp/chainsaw_suite.log | sed 's/^/      /'
    fails=$((fails + 1))
  fi
}

suite "portability_check.sh" bash portability_check.sh
suite "docs_check.sh"      bash docs_check.sh
suite "coverage_check.sh"      bash coverage_check.sh
suite "pytest"           uv run pytest
suite "smoke_test.py"    uv run python smoke_test.py
suite "analysis_test.py" uv run python analysis_test.py
suite "client_test.py"   uv run python client_test.py
suite "exitcheck.sh"     bash exitcheck.sh
suite "baseline_check.sh" bash baseline_check.sh
suite "since_check.sh"   bash since_check.sh
suite "fix_check.sh"     bash fix_check.sh
suite "cache_check.sh"   bash cache_check.sh
suite "config_check.sh"  bash config_check.sh
suite "contract_check.sh" bash contract_check.sh
suite "callgraph_check.sh" bash callgraph_check.sh
suite "oncommit_check.sh" bash oncommit_check.sh
# Slow: builds a throwaway venv and installs the package into it. Skip with
# SKIP_INSTALL_CHECK=1 while iterating.
if [ "${SKIP_INSTALL_CHECK:-0}" != "1" ]; then
  suite "install_check.sh" bash install_check.sh
fi

echo
if [ "$fails" -gt 0 ]; then
  echo "$fails Suite(s) fehlgeschlagen"
  exit 1
fi
echo "alle Suiten gruen"
