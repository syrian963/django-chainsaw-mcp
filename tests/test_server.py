# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Every MCP tool, called in this process.

`client_test.py` drives the server over the real stdio transport, which is the
only way to know the protocol wiring works. It runs the server as a
subprocess, so coverage cannot follow it: `server.py` measured 0% while being
exercised on every run.

The tools themselves are thin - they hand their arguments to an analysis and
wrap the failures - and that thinness is exactly where a typo survives. A
keyword that does not match the analysis's signature, or a default that the
analysis rejects, breaks that one tool for every client and nothing else
notices. So each one is called here with its defaults.

The equivalent test for the CLI found `choices` raising `KeyError` on every
run that had something to report, because a printer still referenced a key
that had been removed. This is the same shape of test for the other front end.
"""

from __future__ import annotations

import inspect

import pytest

from django_chainsaw_mcp import server

# Tools that need a real argument to do anything, with one that exists in the
# demo project.
ARGUMENTS: dict[str, dict[str, object]] = {
    "delete_impact": {"model_label": "shop.Customer"},
    "explain_model": {"model_label": "shop.Order"},
    "what_happens_on": {"model_label": "shop.Order"},
    "find_n_plus_one": {
        "template_path": "shop/order_list.html",
        "root_models": ["shop.Order"],
    },
    "find_unscoped_queries": {"tenant_root": "shop.Customer"},
    "suggest_fixes": {"tenant_root": "shop.Customer"},
    "check": {"tenant_root": "shop.Customer"},
}


def tool_functions() -> dict[str, object]:
    """The plain functions behind the registered tools.

    The decorator registers them with the server; the module keeps the
    function under its own name, which is what a unit test wants.
    """
    import asyncio

    # Prompts and the completion handler live in the same module and are not
    # tools. Asking the server which is which beats guessing from the name.
    registered = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    prompts = {prompt.name for prompt in asyncio.run(server.mcp.list_prompts())}

    found = {}
    for name, value in vars(server).items():
        if name.startswith("_") or not inspect.isfunction(value):
            continue
        if value.__module__ != server.__name__:
            continue
        if name in prompts or name not in registered | {"model_graph"}:
            continue
        found[name] = value
    return found


def test_the_module_exposes_every_registered_tool_as_a_function():
    # If the decorator ever stopped leaving the function behind, this file
    # would silently test nothing.
    assert len(tool_functions()) > 30


@pytest.mark.parametrize("name", sorted(tool_functions()))
def test_a_tool_returns_a_report_rather_than_raising(name):
    tool = tool_functions()[name]
    kwargs = ARGUMENTS.get(name, {})

    # Anything still required after that is a signature this test does not
    # know how to satisfy, and saying so is better than skipping quietly.
    signature = inspect.signature(tool)
    missing = [
        parameter.name
        for parameter in signature.parameters.values()
        if parameter.default is inspect.Parameter.empty
        and parameter.name not in kwargs
    ]
    assert not missing, f"{name} needs {missing}; add them to ARGUMENTS"

    report = tool(**kwargs)

    # A resource answers with a string by protocol, a tool with a mapping.
    # Both are valid; returning neither is not.
    if isinstance(report, str):
        import json

        json.loads(report)
        return
    assert isinstance(report, dict), f"{name} returned {type(report).__name__}"

    # `_guard` turns an expected failure into {"ok": False, "error": ...}.
    # That is a legitimate answer for a tool whose framework is absent, but
    # not for one whose framework this project has.
    if report.get("ok") is False:
        error = str(report.get("error", ""))
        assert "not a directory" not in error, f"{name}: {error}"
        assert "Missing environment" not in error, f"{name}: {error}"


def test_a_tool_that_cannot_run_says_so_instead_of_crashing():
    """The wrapper turns an expected failure into an answer, not a dead server.

    Tested on the wrapper directly. Patching `ensure_django` does not work
    once Django is up: the boot is cached, so nothing calls it again and the
    test would pass without exercising anything.
    """
    from django_chainsaw_mcp.django_env import DjangoBootError

    def refuse() -> dict:
        raise DjangoBootError("no settings module here")

    report = server._guard(refuse)
    assert report["ok"] is False
    assert "no settings module here" in report["error"]

    def blow_up() -> dict:
        raise ZeroDivisionError("not an expected failure")

    # Anything else has to propagate: swallowing a real bug would report a
    # clean result for a check that never ran.
    with pytest.raises(ZeroDivisionError):
        server._guard(blow_up)


def test_the_model_resource_is_registered():
    # A resource is addressable data and belongs in the resource list, not as
    # a tool. Losing the registration would be invisible otherwise.
    assert any("models" in str(uri) for uri in _resource_uris())


def _resource_uris() -> list[str]:
    import asyncio

    async def gather():
        return [r.uri for r in await server.mcp.list_resources()]

    return asyncio.run(gather())


# --- the protocol surface, not just the tools ------------------------------


def test_the_server_tells_a_client_how_to_use_it():
    # An assistant handed 36 tools with no ordering picks by name, and the
    # names do not say which question each answers.
    assert server.INSTRUCTIONS
    for expected in ("project_info", "empty result", "cannot see"):
        assert expected in server.INSTRUCTIONS, expected


def test_every_tool_declares_whether_it_reads_or_writes():
    # Without this a client asks permission for each of 36 read-only calls,
    # which is the difference between a server somebody uses while working and
    # one they use once.
    import asyncio

    tools = asyncio.run(server.mcp.list_tools())
    missing = [t.name for t in tools if t.annotations is None]
    assert not missing, f"no annotations on {missing}"

    writing = [t.name for t in tools if not t.annotations.read_only_hint]
    # Exactly one tool writes: the contract snapshot, and only with update=True.
    assert writing == ["api_contract_check"], writing

    for tool in tools:
        assert tool.annotations.destructive_hint is False, tool.name
        assert tool.annotations.open_world_hint is False, tool.name


def test_the_prompts_carry_the_order_the_tools_do_not():
    import asyncio

    prompts = asyncio.run(server.mcp.list_prompts())
    names = {p.name for p in prompts}
    assert {"before_deploy", "why_is_this_slow", "triage"} <= names, names
    for prompt in prompts:
        assert prompt.description, f"{prompt.name} has no description"


def test_a_prompt_interpolates_its_argument():
    import asyncio

    result = asyncio.run(
        server.mcp.get_prompt("what_breaks_if_i_delete", {"model": "shop.Customer"})
    )
    text = str(result.messages[0].content)
    assert "shop.Customer" in text
    # And it says the thing a tool cannot: what the answer does not mean.
    assert "rollback" in text.lower()


def test_every_prompt_names_at_least_one_real_tool():
    # A workflow that references a tool that does not exist sends an assistant
    # looking for it, which is worse than no workflow.
    import asyncio
    import re

    tool_names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    for prompt in asyncio.run(server.mcp.list_prompts()):
        arguments = {a.name: "x" for a in (prompt.arguments or []) if a.required}
        text = str(
            asyncio.run(server.mcp.get_prompt(prompt.name, arguments)).messages[0].content
        )
        mentioned = set(re.findall(r"`(\w+)`", text)) & tool_names
        assert mentioned, f"{prompt.name} names no tool that exists"


def test_a_model_argument_completes_from_the_real_project():
    # 424 models on a real project. Typing one from memory is how you get a
    # LookupError, and a wrong label looks the same as a model with nothing
    # attached to it.
    import asyncio
    from types import SimpleNamespace

    partial = SimpleNamespace(name="model", value="ord")
    result = asyncio.run(server.complete_argument(None, partial, None))
    assert result is not None
    assert "shop.Order" in result.values

    unrelated = SimpleNamespace(name="severity", value="h")
    assert asyncio.run(server.complete_argument(None, unrelated, None)) is None
