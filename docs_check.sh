#!/usr/bin/env bash
# Documentation drifts silently. A tool added without a line in the README is
# a tool nobody finds, and a README that describes a scope the code outgrew is
# worse than one that says nothing - this repository shipped for several
# iterations claiming to be Django-only while a third of its checks were not.
#
# So the docs are checked the way the code is.
cd "$(dirname "$0")" || exit 2
export PATH="$HOME/.local/bin:$PATH"
export DJANGO_CHAINSAW_PROJECT_PATH=testprojects
export DJANGO_CHAINSAW_SETTINGS_MODULE=demoshop.settings

echo "Documentation"

./.venv/bin/python - <<'PY'
import asyncio
import io
import re
import sys
from pathlib import Path

fails = 0


def check(ok, label, detail=""):
    global fails
    print(("  ok    %-54s %s" if ok else "  FAIL  %-54s %s") % (label, detail))
    if not ok:
        fails += 1


async def main():
    global fails
    from django_chainsaw_mcp import server
    from django_chainsaw_mcp.check import ALL_CHECKS

    tools = sorted(t.name for t in await server.mcp.list_tools())

    readme = io.open("README.md", encoding="utf-8").read()
    tools_doc = io.open("docs/tools.md", encoding="utf-8").read()
    cli_doc = io.open("docs/cli.md", encoding="utf-8").read()
    index = io.open("docs/README.md", encoding="utf-8").read()

    missing_readme = [t for t in tools if f"`{t}`" not in readme]
    check(not missing_readme, "every MCP tool is named in the README",
          ", ".join(missing_readme))

    missing_reference = [t for t in tools if f"`{t}`" not in tools_doc]
    check(not missing_reference, "every MCP tool is in the tool reference",
          ", ".join(missing_reference))

    # Every CLI subcommand has a section in the CLI reference.
    from django_chainsaw_mcp.cli import build_parser

    parser = build_parser()
    subcommands = []
    for action in parser._subparsers._group_actions:      # noqa: SLF001
        subcommands.extend(action.choices)
    missing_cli = [c for c in sorted(set(subcommands)) if f"`{c}`" not in cli_doc]
    check(not missing_cli, "every CLI subcommand is in the CLI reference",
          ", ".join(missing_cli))

    # Every aggregate check name is one somebody can look up.
    missing_check = [c for c in ALL_CHECKS
                     if c not in cli_doc and c not in tools_doc and c not in index]
    check(not missing_check, "every aggregate check name appears in the docs",
          ", ".join(missing_check))

    # Every docs/*.md page is reachable from the index, or it is the index.
    pages = sorted(p.name for p in Path("docs").glob("*.md") if p.name != "README.md")
    unlinked = [p for p in pages if p not in index]
    check(not unlinked, "every documentation page is linked from the index",
          ", ".join(unlinked))

    # Every page the index links to exists.
    linked = set(re.findall(r"\]\(([a-z0-9-]+\.md)\)", index))
    broken = sorted(name for name in linked if not (Path("docs") / name).is_file())
    check(not broken, "every link in the index resolves", ", ".join(broken))

    sys.exit(1 if fails else 0)


asyncio.run(main())
PY
rc=$?

echo
if [ "$rc" -ne 0 ]; then
  echo "Dokumentation weicht vom Code ab"
  exit 1
fi
echo "Dokumentation deckt den Code"
