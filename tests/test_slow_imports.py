# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""An import that waits, and the report that has to name it.

`n+1-serializer` reads 23 seconds of CPU against 563 seconds of wall clock on
DefectDojo, reproducibly, in runs where every other check has wall equal to
CPU. The check imports every module that declares a serializer, and an import
runs whatever that module runs at import time - a connection, an HTTP call, a
DNS lookup that has to time out first.

Interrupting an import is not an option: killing one halfway leaves a partial
entry in `sys.modules`, and the next import of it succeeds with a broken
module. So the tool measures instead, and these tests hold it to naming the
module rather than swallowing the wait.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path


def _project(tmp_path: Path, body: str) -> Path:
    """A package with one module that declares a serializer."""
    package = tmp_path / "slowapp"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "serializers.py").write_text(textwrap.dedent(body))
    return tmp_path


def _load(root: Path) -> dict:
    # Importing DRF for the first time costs about a second of real CPU, and
    # that would land on whichever module happened to import it first - which
    # is the difference this file is measuring. Pay it up front.
    import rest_framework.serializers  # noqa: F401

    from django_chainsaw_mcp import discovery

    discovery._loaded.clear()
    sys.path.insert(0, str(root))
    try:
        return discovery.load_serializer_modules(root)
    finally:
        sys.path.remove(str(root))
        for name in [n for n in sys.modules if n.startswith("slowapp")]:
            del sys.modules[name]


def test_an_import_that_waits_is_named_with_what_it_waited_on(tmp_path):
    root = _project(
        tmp_path,
        """
        import time
        from rest_framework import serializers

        # Standing in for a connection, an HTTP call, or a DNS lookup that
        # has to time out - anything that costs wall clock and no CPU.
        time.sleep(1.2)


        class Thing(serializers.Serializer):
            pass
        """,
    )
    report = _load(root)
    slow = report["slow_imports"]
    assert slow, "an import that took over a second was not reported"
    entry = slow[0]
    assert entry["module"] == "slowapp.serializers"
    assert entry["seconds"] >= 1.0
    assert entry["cpu_seconds"] < 0.5, (
        "the CPU figure is what separates a module doing work from one "
        "waiting, and nine minutes of waiting was the case that mattered"
    )


def test_a_quick_import_is_not_listed(tmp_path):
    # A line per module would bury the one that matters.
    root = _project(
        tmp_path,
        """
        from rest_framework import serializers


        class Thing(serializers.Serializer):
            pass
        """,
    )
    assert _load(root)["slow_imports"] == []


def test_a_module_that_raises_is_still_timed_and_still_reported_as_failed(tmp_path):
    # Waiting four minutes and then raising is the worst case of the two, and
    # the timing sat only on the success path in the first draft.
    root = _project(
        tmp_path,
        """
        import time

        time.sleep(1.1)
        raise RuntimeError("this module cannot import")
        """,
    )
    root_pkg = root / "slowapp" / "serializers.py"
    # The AST scan looks for a serializer declaration, so give it one that
    # never runs.
    root_pkg.write_text(
        "import time\n"
        "from rest_framework import serializers\n"
        "time.sleep(1.1)\n"
        "raise RuntimeError('this module cannot import')\n"
        "class Thing(serializers.Serializer):\n"
        "    pass\n"
    )
    report = _load(root)
    assert any(e["module"] == "slowapp.serializers" for e in report["failed"])
    assert any(e["module"] == "slowapp.serializers" for e in report["slow_imports"]), (
        "an import that waited and then failed is the most useful one to time"
    )


def test_the_check_passes_the_list_on(tmp_path, django_project):
    # Measuring it in discovery and dropping it before the report would leave
    # the reader of a slow run exactly where they started.
    from django_chainsaw_mcp.serializer_nplusone import serializer_nplusone

    report = serializer_nplusone()
    assert "slow_imports" in report
