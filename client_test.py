"""End-to-end check over the real MCP transport.

Everything before this only called the Python functions directly, which proves
nothing about the protocol. Here the server is started as its own process and
driven over stdio, exactly as a client such as Claude Code would do it.
"""

import asyncio
import json
import sys
from itertools import pairwise
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
        labels = {m["label"] for m in models.get("models", [])}
        print("LIST_MODELS models=", models.get("model_count"))
        # Named, not counted: a count assertion breaks whenever the demo
        # project grows, and the reflex is to edit the number.
        expected = {"shop.Order", "shop.OrderLine", "shop.Invoice", "shop.Customer",
                    "shop.Product", "shop.Category", "shop.Tag"}
        missing = expected - labels
        if missing:
            failures.append(f"models missing over the wire: {sorted(missing)}")

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
        api_lines = sorted(
            f["line"] for f in tenancy.get("findings", []) if f["file"].endswith("api.py")
        )
        if api_lines != [14, 19, 24, 29]:
            failures.append(f"api.py should contribute four findings, got lines {api_lines}")
        if "shop.Product" in (tenancy.get("tenant_scoped_models") or {}):
            failures.append("Product must not be treated as tenant-scoped")

        chain = _payload(
            await client.call_tool("what_happens_on", {"model_label": "shop.OrderLine"})
        )
        print("WHAT_HAPPENS_ON receivers=", chain.get("receiver_count"),
              "written=", chain.get("models_written"))
        if chain.get("receiver_count") != 3:
            failures.append(f"expected a three-receiver chain, got {chain.get('receiver_count')}")
        if "delay" not in {e["call"] for e in chain.get("side_effects", [])}:
            failures.append("the Celery task three hops away did not surface over the wire")

        idx = _payload(await client.call_tool("missing_indexes", {}))
        print("MISSING_INDEXES candidates=", idx.get("candidate_count"),
              "high=", idx.get("high_severity_count"))
        flagged = {(f["model"], f["field"]) for f in idx.get("findings", [])}
        if ("shop.Product", "name") not in flagged:
            failures.append("Product.name should be reported as missing an index")
        if ("shop.Product", "sku") in flagged:
            failures.append("sku is unique and indexed, it must not be reported")

        # Declaring things in the module and them arriving over the wire are
        # two different questions. The in-process tests answer the first.
        annotated = [t for t in tools.tools if getattr(t, "annotations", None)]
        writing = [
            t.name for t in annotated
            if not getattr(t.annotations, "readOnlyHint",
                           getattr(t.annotations, "read_only_hint", None))
        ]
        print(f"ANNOTATIONS: {len(annotated)} of {len(tools.tools)} tool(s), "
              f"writing={writing}")
        if len(annotated) != len(tools.tools):
            failures.append("some tools arrive without annotations, so a client "
                            "cannot tell a read from a write")
        if writing != ["api_contract_check"]:
            failures.append(f"unexpected non-read-only tools over the wire: {writing}")

        prompts = await client.list_prompts()
        prompt_names = sorted(p.name for p in prompts.prompts)
        print("PROMPTS:", ", ".join(prompt_names) or "(none)")
        for expected in ("before_deploy", "triage"):
            if expected not in prompt_names:
                failures.append(f"prompt missing over the wire: {expected}")

        if "what_breaks_if_i_delete" in prompt_names:
            filled = await client.get_prompt(
                "what_breaks_if_i_delete", {"model": "shop.Customer"}
            )
            body = "".join(
                getattr(m.content, "text", "") or "" for m in filled.messages
            )
            print(f"GET_PROMPT what_breaks_if_i_delete: {len(body)} chars")
            if "shop.Customer" not in body:
                failures.append("a prompt argument did not reach the rendered text")

        # Completion is a separate request type, and a handler that works in
        # process can still be unregistered on the server.
        try:
            from mcp.types import PromptReference

            completed = await client.complete(
                ref=PromptReference(type="ref/prompt", name="what_breaks_if_i_delete"),
                argument={"name": "model", "value": "ord"},
            )
            values = completed.completion.values
            print("COMPLETE model='ord':", values)
            if "shop.Order" not in values:
                failures.append(f"model completion did not suggest shop.Order: {values}")
        except Exception as exc:
            failures.append(f"completion over the wire failed: {type(exc).__name__}: {exc}")

        # A progress notification that is declared and never sent looks
        # exactly like a fast tool. Only a real client subscribing to the
        # token can tell the difference, so `check` is driven here with one
        # and the notifications are counted.
        seen: list[tuple[float, float | None, str | None]] = []

        async def note(progress, total, message):
            seen.append((progress, total, message))

        report = _payload(
            await client.call_tool(
                "check",
                {"tenant_root": "shop.Customer"},
                progress_callback=note,
            )
        )
        print(f"CHECK findings={report.get('finding_count')} "
              f"progress_notifications={len(seen)}")
        if not seen:
            failures.append("check reported no progress over the wire")
        else:
            named = [m for _, _, m in seen if m]
            print("  first:", seen[0], "last:", seen[-1])
            if len(named) != len(seen):
                failures.append("a progress notification arrived without a message")
            # The point of the message is naming the check that is running.
            # A counter alone would be the thing this replaced.
            if not any("indexes" in (m or "") for m in named):
                failures.append(f"progress never named a real check: {named[:5]}")
            monotonic = all(a[0] <= b[0] for a, b in pairwise(seen))
            if not monotonic:
                failures.append("progress went backwards")
            if seen[-1][0] != seen[-1][1]:
                failures.append(f"the last progress was not the total: {seen[-1]}")
        if report.get("finding_count") is None:
            failures.append("check returned no findings key over the wire")

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
