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
    """Pull the structured result out of a CallToolResult.

    The attribute is camelCase on some MCP SDK builds and snake_case on others,
    so both are tried before falling back to parsing the text block. Hardcoding
    one of them worked here and broke as soon as the package was installed into
    a different environment.
    """
    for attribute in ("structured_content", "structuredContent"):
        value = getattr(result, attribute, None)
        if value:
            return value
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

        impact = _payload(await client.call_tool("delete_impact", {"model_label": "shop.Customer"}))
        cascaded = {row["from_model"] for row in impact.get("cascades", [])}
        print("DELETE_IMPACT cascades=", len(cascaded))
        if not {"shop.Order", "shop.OrderLine", "shop.Invoice"} <= cascaded:
            failures.append(f"cascade set incomplete over the wire: {sorted(cascaded)}")

        n1 = _payload(
            await client.call_tool(
                "find_n_plus_one",
                {
                    "template_path": str(ROOT / "testprojects/shop/templates/shop/order_list.html"),
                    "root_models": {"orders": "shop.Order", "single_order": "shop.Order"},
                },
            )
        )
        print("FIND_N_PLUS_ONE high=", n1.get("high_severity_count"))
        if "lines" not in n1.get("suggested_queryset", {}).get("prefetch_related", []):
            failures.append(f"reverse relation not suggested for prefetch: {n1.get('suggested_queryset')}")

        risk = _payload(await client.call_tool("migration_risk", {}))
        print("MIGRATION_RISK risky=", risk.get("risky_count"), "of", risk.get("migration_count"))
        if not risk.get("migration_count"):
            failures.append("migration_risk returned no migrations")

        safety = _payload(await client.call_tool("deploy_safety", {}))
        print("DEPLOY_SAFETY blocking=", safety.get("blocking_count"),
              "clear=", safety.get("clear_count"),
              "skipped=", safety.get("skipped_third_party_apps"))
        if safety.get("blocking_count") != 1:
            failures.append(f"expected one blocking migration, got {safety.get('blocking_count')}")
        if "contenttypes" not in safety.get("skipped_third_party_apps", []):
            failures.append("third-party migrations must be skipped over the wire too")

        tenancy = _payload(
            await client.call_tool("find_unscoped_queries", {"tenant_root": "shop.Customer"})
        )
        print("FIND_UNSCOPED_QUERIES candidates=", tenancy.get("unscoped_count"),
              "high=", tenancy.get("high_severity_count"))
        if tenancy.get("unscoped_count") != 4:
            failures.append(f"expected 4 unscoped querysets, got {tenancy.get('unscoped_count')}")
        if "shop.Product" in (tenancy.get("tenant_scoped_models") or {}):
            failures.append("Product must not be treated as tenant-scoped")

        resources = await client.list_resources()
        uris = [str(r.uri) for r in resources.resources]
        print("RESOURCES:", ", ".join(uris) or "(none)")
        if "django://models" not in uris:
            failures.append(f"model graph resource missing: {uris}")
        else:
            body = await client.read_resource("django://models")
            graph = json.loads(body.contents[0].text)
            print("RESOURCE django://models models=", graph.get("model_count"))
            if not graph.get("model_count"):
                failures.append("model graph resource returned nothing")

        # An unknown app label must come back as a readable error, not a crash.
        bad = _payload(await client.call_tool("list_models", {"app_label": "does_not_exist"}))
        if bad.get("ok") is not False or "Unknown app label" not in str(bad.get("error", "")):
            failures.append(f"bad app_label was not reported cleanly: {bad}")
        else:
            print("ERROR PATH ok: unknown app label reported cleanly")

        bad_model = _payload(await client.call_tool("delete_impact", {"model_label": "nope.Nope"}))
        if bad_model.get("ok") is not False or "Unknown model" not in str(bad_model.get("error", "")):
            failures.append(f"bad model label was not reported cleanly: {bad_model}")
        else:
            print("ERROR PATH ok: unknown model label reported cleanly")

    print()
    if failures:
        for line in failures:
            print("FAIL:", line)
        return 1
    print("ALLE CHECKS BESTANDEN")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
