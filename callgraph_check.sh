#!/usr/bin/env bash
# A call graph that silently fails to resolve one call form makes every check
# built on it quietly incomplete, which is worse than not having it at all.
# So every form the module claims to resolve gets a named case here.
cd "$(dirname "$0")" || exit 2
export PATH="$HOME/.local/bin:$PATH"
export DJANGO_CHAINSAW_PROJECT_PATH=testprojects
export DJANGO_CHAINSAW_SETTINGS_MODULE=demoshop.settings

find testprojects -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null

echo "Call graph"

./.venv/bin/python - <<'PY'
import ast, io, json, os, subprocess, sys
from pathlib import Path

from django_chainsaw_mcp import callgraph

fails = 0
def check(ok, label, detail=""):
    global fails
    print(("  ok    %-52s %s" if ok else "  FAIL  %-52s %s") % (label, detail))
    if not ok:
        fails += 1

graph = callgraph.build(Path("testprojects"))

# -- every resolution form the docstring promises --------------------------
forms = {
    "a name imported directly":
        ("shop.graph_forms.form_direct_name", "shop.fulfilment.finalise"),
    "a module imported and attributed":
        ("shop.graph_forms.form_module_attribute", "shop.fulfilment.finalise"),
    "self.method() in the same class":
        ("shop.graph_forms.Service.form_self_call", "shop.graph_forms.Service.own_hop"),
    "self.method() inherited from a base":
        ("shop.graph_forms.Service.form_inherited_call", "shop.graph_forms.Base.inherited_hop"),
}
for label, (caller, expected) in forms.items():
    fn = graph.functions.get(caller)
    got = sorted(fn.calls_in_atomic) if fn else []
    check(fn is not None and expected in fn.calls_in_atomic, label, ",".join(got))

# -- deferral is not reachability -----------------------------------------
correct = graph.functions.get("shop.checkout.checkout_correctly")
check(correct is not None and not correct.calls_in_atomic,
      "a call deferred to on_commit is not in the transaction",
      ",".join(sorted(correct.calls_in_atomic)) if correct else "missing")
check(correct is not None and correct.calls_deferred,
      "but it is still recorded as reachable code")

# -- transitive reach, and where it stops ---------------------------------
inside = callgraph.inside_transaction(graph)
check("shop.fulfilment.finalise" in inside, "one hop from the boundary is inside")
check("shop.fulfilment._tell_warehouse" in inside, "two hops is still inside")
check("shop.fulfilment.notify_only" not in inside,
      "a function reached only without a transaction is not")

path = inside.get("shop.fulfilment._tell_warehouse", [])
check(path and path[0] == "shop.checkout.checkout" and len(path) == 3,
      "the path names the caller that caused it", " -> ".join(path))

# -- a cycle must not hang or explode -------------------------------------
graph.functions["a"] = callgraph.Function("a", "m", "a", "f.py", 1, 2, calls={"b"},
                                          opens_atomic=True, calls_in_atomic={"b"})
graph.functions["b"] = callgraph.Function("b", "m", "b", "f.py", 3, 4, calls={"a"})
reached = graph.reachable(["a"], transaction_aware=True)
check("b" in reached, "a cycle terminates instead of looping")

# -- the checks that consume it -------------------------------------------
env = {**os.environ}
out = subprocess.run(["./.venv/bin/django-chainsaw", "--json", "on-commit"],
                     capture_output=True, text=True, env=env)
report = json.loads(out.stdout)
check(report["cross_module_count"] >= 2,
      "on-commit reports the cross-module cases",
      str(report["cross_module_count"]))

src = io.open("testprojects/shop/fulfilment.py", encoding="utf-8").read()
spans = {n.name: (n.lineno, n.end_lineno)
         for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef)}
start, end = spans["notify_only"]
noise = [f for f in report["findings"]
         if f["file"].endswith("fulfilment.py") and start <= f["line"] <= end]
check(not noise,
      "and stays silent on the identical call with no transaction",
      json.dumps(noise))

# A project that wraps Celery in its own notify() defeats a check that matches
# on `.delay`. Following the call closes that without anybody configuring it.
wrapped = [f for f in report["findings"]
           if "shop.fulfilment.notify" in (f.get("reached_through") or [])]
check(bool(wrapped),
      "a project's own dispatch wrapper is followed into",
      wrapped[0]["call"] if wrapped else "")

check(all("reached_through" in f or f.get("inside", "").startswith(("with ", "@"))
          for f in report["findings"]),
      "every finding says which transaction it is in")

sys.exit(1 if fails else 0)
PY
rc=$?

echo
if [ "$rc" -ne 0 ]; then
  echo "Abweichungen im Call-Graph"
  exit 1
fi
echo "Call-Graph bestaetigt"
