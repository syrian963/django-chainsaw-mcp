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
    found = {}
    for name, value in vars(server).items():
        if name.startswith("_") or not inspect.isfunction(value):
            continue
        if value.__module__ != server.__name__:
            continue
        if name in {"main"}:
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
