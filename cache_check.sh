#!/usr/bin/env bash
# A cache that does not invalidate is a bug that reports yesterday's findings
# with today's confidence, which is worse than being slow.
cd "$(dirname "$0")" || exit 2
export PATH="$HOME/.local/bin:$PATH"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cp -r testprojects "$WORK/tp"

export DJANGO_CHAINSAW_PROJECT_PATH="$WORK/tp"
export DJANGO_CHAINSAW_SETTINGS_MODULE=demoshop.settings

echo "cache behaviour"
uv run python - <<'PY'
import os
import time
from pathlib import Path

from django_chainsaw_mcp import cache
from django_chainsaw_mcp.explain import explain_model

root = Path(os.environ["DJANGO_CHAINSAW_PROJECT_PATH"])
fails = []


def check(condition, message):
    if condition:
        print(f"  ok    {message}")
    else:
        print(f"  FAIL  {message}")
        fails.append(message)


def timed(label):
    start = time.perf_counter()
    report = explain_model(label, tenant_root="shop.Customer")
    return report, (time.perf_counter() - start) * 1000


first, cold = timed("shop.Order")
second, warm = timed("shop.Invoice")

check(warm < cold / 3, f"a second model is much faster ({cold:.0f}ms then {warm:.0f}ms)")
check(cache.stats()["hits"] > 0, "the cache recorded hits")

# The answer must still be right, not just fast.
check(second["model"] == "shop.Invoice", "the cached run answers about the model asked for")
check(
    second["ownership"].get("path") == "order__customer",
    f"cached ownership is correct, got {second['ownership'].get('path')}",
)

# Change a file, and the cache must not serve the old answer.
before = len(second["correlated_risks"])
new_leak = root / "shop" / "extra_leak.py"
new_leak.write_text(
    "from .models import Invoice\n\n\n"
    "def leak(request, pk):\n"
    "    return Invoice.objects.get(pk=pk)\n",
    encoding="utf-8",
)
# mtime resolution on some filesystems is coarse; make the change unambiguous.
time.sleep(1.1)
new_leak.touch()

invalidations_before = cache.stats()["invalidations"]
third, _ = timed("shop.Invoice")
after_stats = cache.stats()

check(
    after_stats["invalidations"] > invalidations_before,
    f"editing a file invalidates the cache (invalidations {invalidations_before} -> "
    f"{after_stats['invalidations']})",
)

unscoped = [
    r for r in third["correlated_risks"]
    if r["id"] == "unscoped_read_of_exposed_owned_model"
]
check(bool(unscoped), "the finding from the new file is present after invalidation")
check(
    len(third["correlated_risks"]) >= before,
    "the recomputed answer is not the stale one",
)

raise SystemExit(1 if fails else 0)
PY
rc=$?

echo
if [ "$rc" -ne 0 ]; then
  echo "Cache-Verhalten fehlerhaft"
  exit 1
fi
echo "Cache beschleunigt und invalidiert korrekt"
