#!/usr/bin/env bash
# Verify the CLI exit codes. 0 = clean, 1 = findings, 2 = error.
cd "$(dirname "$0")" || exit 2
export DJANGO_CHAINSAW_PROJECT_PATH=testprojects
export DJANGO_CHAINSAW_SETTINGS_MODULE=demoshop.settings

BIN=./.venv/bin/django-chainsaw
fails=0

expect() {
  local want="$1"; shift
  local label="$1"; shift
  "$@" >/dev/null 2>&1
  local got=$?
  if [ "$got" = "$want" ]; then
    printf '  ok    %-42s exit=%s\n' "$label" "$got"
  else
    printf '  FAIL  %-42s exit=%s (erwartet %s)\n' "$label" "$got" "$want"
    fails=$((fails + 1))
  fi
}

echo "CLI exit codes"
expect 1 "deploy-safety, blocking migration"      $BIN deploy-safety
expect 0 "deploy-safety, no references in scope"  $BIN deploy-safety --search-path testprojects/demoshop
expect 1 "n+1 --max-high 2 (5 found)"             $BIN n+1 --templates testprojects --max-high 2
expect 0 "n+1 --max-high 99"                      $BIN n+1 --templates testprojects --max-high 99
expect 0 "n+1 without threshold"                  $BIN n+1 --templates testprojects
expect 1 "migrations --fail-on-risk"              $BIN migrations --fail-on-risk
expect 0 "migrations without gate"                $BIN migrations
expect 2 "delete-impact with unknown model"       $BIN delete-impact nope.Nope
expect 0 "delete-impact shop.Customer"            $BIN delete-impact shop.Customer
expect 0 "models --short"                         $BIN models --short
expect 1 "tenancy --fail-on-findings"             $BIN tenancy --tenant-root shop.Customer --fail-on-findings
expect 0 "tenancy without gate"                   $BIN tenancy --tenant-root shop.Customer
expect 0 "signals shop.OrderLine"                 $BIN signals shop.OrderLine
expect 2 "signals with unknown model"             $BIN signals nope.Nope

echo
if [ "$fails" -gt 0 ]; then
  echo "$fails Abweichung(en)"
  exit 1
fi
echo "alle Exit-Codes wie erwartet"
