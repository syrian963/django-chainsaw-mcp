#!/usr/bin/env bash
# Prove the documented install path actually works: install this package into a
# SEPARATE virtualenv that plays the role of a target project's environment,
# then run the analysis with that venv's interpreter.
#
# This is the scenario the docs describe, so it must not be guessed.
set -u
cd "$(dirname "$0")" || exit 2
export PATH="$HOME/.local/bin:$PATH"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "Ziel-venv unter $TMP/targetenv"
python3 -m venv "$TMP/targetenv" || exit 2
"$TMP/targetenv/bin/pip" install -q --upgrade pip

echo "Django installieren (die Abhaengigkeit des Zielprojekts)"
"$TMP/targetenv/bin/pip" install -q django || exit 2

echo "django-chainsaw-mcp in dasselbe venv installieren"
"$TMP/targetenv/bin/pip" install -q . || exit 2

echo
echo "--- CLI aus dem Ziel-venv ---"
export DJANGO_CHAINSAW_PROJECT_PATH="$PWD/testprojects"
export DJANGO_CHAINSAW_SETTINGS_MODULE=demoshop.settings

"$TMP/targetenv/bin/django-chainsaw" models --short | head -4
rc_models=$?

"$TMP/targetenv/bin/django-chainsaw" deploy-safety >/dev/null 2>&1
rc_safety=$?

echo
echo "--- MCP-Server aus dem Ziel-venv, ueber stdio ---"
export TARGET_PY="$TMP/targetenv/bin/python"
"$TMP/targetenv/bin/python" - <<'PY'
import asyncio, json, os
from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters

async def main():
    params = StdioServerParameters(
        command=os.environ["TARGET_PY"],
        args=["-m", "django_chainsaw_mcp.server"],
        env={k: os.environ[k] for k in
             ("DJANGO_CHAINSAW_PROJECT_PATH", "DJANGO_CHAINSAW_SETTINGS_MODULE", "PATH", "HOME")},
    )
    async with Client(params) as client:
        tools = await client.list_tools()
        print("tools:", len(tools.tools))
        result = await client.call_tool("project_info", {})
        payload = (
            getattr(result, "structured_content", None)
            or getattr(result, "structuredContent", None)
            or json.loads(result.content[0].text)
        )
        print("project_info ok:", payload.get("ok"), "django:", payload.get("django_version"))
        return 0 if payload.get("ok") else 1

raise SystemExit(asyncio.run(main()))
PY
rc_server=$?

echo
echo "models exit=$rc_models  deploy-safety exit=$rc_safety (1 erwartet)  server exit=$rc_server"
if [ "$rc_models" = 0 ] && [ "$rc_safety" = 1 ] && [ "$rc_server" = 0 ]; then
  echo "Installationsweg bestaetigt"
  exit 0
fi
echo "Installationsweg FEHLGESCHLAGEN"
exit 1
