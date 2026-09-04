"""End-to-end check over the real MCP transport.

Everything before this only called the Python functions directly, which proves
nothing about the protocol. Here the server is started as its own process and
driven over stdio, exactly as a client such as Claude Code would do it.
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters

ROOT = Path(__file__).parent


def _params() -> StdioServerParameters:
    return StdioServerParameters(
        command="uv",
        args=["run", "python", "-m", "django_chainsaw_mcp.server"],
        cwd=str(ROOT),
        env={
            "DJANGO_CHAINSAW_PROJECT_PATH": str(ROOT / "testprojects"),
            "DJANGO_CHAINSAW_SETTINGS_MODULE": "demoshop.settings",
            "PATH": __import__("os").environ.get("PATH", ""),
            "HOME": __import__("os").environ.get("HOME", ""),
        },
    )


def _payload(result) -> dict:
    """Pull the structured result out of a CallToolResult."""
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError("tool returned no readable content")


async def main() -> int:
    failures: list[str] = []

    async with Client(_params()) as client:
        tools = await client.list_tools()
        names = sorted(t.name for t in tools.tools)
        print("TOOLS:", ", ".join(names))
        for expected in ("project_info", "list_models"):
            if expected not in names:
                failures.append(f"tool missing over the wire: {expected}")

        info = _payload(await client.call_tool("project_info", {}))
        print("PROJECT_INFO ok=", info.get("ok"), "django=", info.get("django_version"))
        if not info.get("ok"):
            failures.append(f"project_info failed: {info.get('error')}")

        models = _payload(await client.call_tool("list_models", {"app_label": "shop"}))
        count = models.get("model_count")
        print("LIST_MODELS models=", count)
        if count != 7:
            failures.append(f"expected 7 models in the demo app, got {count}")

        unknown = [
            f"{m['label']}.{f['name']}"
            for m in models.get("models", [])
            for f in m.get("fields", [])
            if f.get("relation", {}).get("kind") == "Unknown"
        ]
        if unknown:
            failures.append("relations without cardinality: " + ", ".join(unknown))

        # An unknown app label must come back as a readable error, not a crash.
        bad = _payload(await client.call_tool("list_models", {"app_label": "does_not_exist"}))
        if bad.get("ok") is not False or "Unknown app label" not in str(bad.get("error", "")):
            failures.append(f"bad app_label was not reported cleanly: {bad}")
        else:
            print("ERROR PATH ok: unknown app label reported cleanly")

    print()
    if failures:
        for line in failures:
            print("FAIL:", line)
        return 1
    print("ALLE CHECKS BESTANDEN")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
