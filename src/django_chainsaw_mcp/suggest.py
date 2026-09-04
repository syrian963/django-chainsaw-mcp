# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The fix engine as a serialisable report, for the MCP side.

The CLI prints diffs and can write files. An assistant needs the same
information as data, and it needs the safety classification most of all: an
assistant that applies an advisory fix because it looked like a diff is worse
than one that never suggested anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .datetimes import datetime_audit
from .django_env import ensure_django
from .fixes import ADVISORY, GENERATED, MECHANICAL, build_fixes
from .indexes import missing_indexes
from .serializers import serializer_exposure
from .tenancy import find_unscoped_queries

_EXPLAIN = {
    MECHANICAL: (
        "One correct answer, derivable from the code alone. No judgement, no way "
        "to be wrong. Safe to apply automatically."
    ),
    GENERATED: (
        "A machine can write the artefact; a human decides whether it should "
        "exist. Produced as a file to review, never applied."
    ),
    ADVISORY: (
        "Real code with the names resolved, but the decision belongs to somebody "
        "who knows the system. Shown, never applied."
    ),
}


def suggest_fixes(tenant_root: str = "auth.User") -> dict[str, Any]:
    """Findings as code, grouped by how safe each one is to apply."""
    config = ensure_django()
    root = Path(config.project_path)

    reports = {
        "datetimes": datetime_audit(),
        "serializers": serializer_exposure(),
        "indexes": missing_indexes(),
        "tenancy": find_unscoped_queries(tenant_root=tenant_root),
    }

    fixset = build_fixes(reports, root)

    def serialise(kind: str) -> list[dict[str, Any]]:
        return [
            {
                "check": fix.check,
                "title": fix.title,
                "file": fix.path,
                "line": fix.line,
                "before": fix.old,
                "after": fix.new,
                "extra_import": fix.extra_import,
                "creates_file": fix.new_file,
                "file_content": fix.new_file_content,
                "why": fix.why,
                "caution": fix.caution,
            }
            for fix in fixset.by_kind(kind)
        ]

    groups = {kind: serialise(kind) for kind in (MECHANICAL, GENERATED, ADVISORY)}

    return {
        "classes": _EXPLAIN,
        "counts": {kind: len(entries) for kind, entries in groups.items()},
        **groups,
        "note": (
            "Nothing here has been applied. Only the mechanical class is safe to "
            "apply without reading, and even there the fix is refused if the line "
            "has changed since the analysis. An advisory fix is a starting point "
            "for a person, not a patch: the code is real and the names are "
            "resolved, but whether it is the right thing to do depends on the "
            "domain, which no amount of parsing reveals."
        ),
    }
