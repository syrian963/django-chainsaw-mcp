# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""Emit findings as SARIF, so they land on the diff instead of in a log.

A finding printed to stdout in CI is a finding nobody reads. The build turns
red, somebody opens the log, scrolls, and closes it. SARIF is the format GitHub,
GitLab and every code-scanning UI understand, and uploading it puts each finding
as an annotation **on the line it is about**, inside the pull request.

That difference decides whether a tool changes behaviour or just costs CI
minutes.
"""

from __future__ import annotations

import json
from typing import Any

SARIF_VERSION = "2.1.0"
SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"

# SARIF has three levels and no "critical". Mapping it to error alongside high
# loses information, so the original severity is kept in properties.
_LEVEL = {
    "critical": "error",
    "high": "error",
    "medium": "warning",
    "low": "note",
}


def _split_location(location: str | None) -> tuple[str | None, int | None]:
    if not location:
        return None, None
    if ":" in location:
        path, _, tail = location.rpartition(":")
        if tail.isdigit():
            return path, int(tail)
    return location, None


def to_sarif(report: dict[str, Any], tool_version: str = "0.1.0") -> dict[str, Any]:
    """Convert a `check.run_all` report into a SARIF log."""
    rules: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []

    for finding in report.get("findings", []):
        rule_id = f"chainsaw/{finding['check']}"
        rules.setdefault(
            rule_id,
            {
                "id": rule_id,
                "name": finding["check"].replace("-", "_"),
                "shortDescription": {"text": f"django-chainsaw: {finding['check']}"},
                "help": {
                    "text": (
                        "Candidate finding from static analysis. See the tool "
                        "documentation for what this check can and cannot see."
                    )
                },
                "defaultConfiguration": {"level": _LEVEL.get(finding["severity"], "warning")},
            },
        )

        path, line = _split_location(finding.get("location"))
        result: dict[str, Any] = {
            "ruleId": rule_id,
            "level": _LEVEL.get(finding["severity"], "warning"),
            "message": {
                "text": finding["title"]
                + (f"\n\n{finding['detail']}" if finding.get("detail") else "")
                + (f"\n\nSuggested: {finding['fix']}" if finding.get("fix") else "")
            },
            "properties": {"severity": finding["severity"], "check": finding["check"]},
        }

        if path:
            region: dict[str, Any] = {}
            if line:
                region["startLine"] = line
            result["locations"] = [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": path},
                        **({"region": region} if region else {}),
                    }
                }
            ]

        results.append(result)

    return {
        "$schema": SCHEMA,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "django-chainsaw",
                        "informationUri": "https://github.com/syrian963/django-chainsaw-mcp",
                        "version": tool_version,
                        "rules": list(rules.values()),
                    }
                },
                "results": results,
                "invocations": [
                    {
                        "executionSuccessful": not report.get("checks_failed"),
                        "toolExecutionNotifications": [
                            {
                                "level": "error",
                                "message": {"text": f"check '{name}' failed to run: {state['error']}"},
                            }
                            for name, state in report.get("checks_run", {}).items()
                            if not state.get("ok")
                        ],
                    }
                ],
            }
        ],
    }


def dumps(report: dict[str, Any], tool_version: str = "0.1.0") -> str:
    return json.dumps(to_sarif(report, tool_version), indent=2)
