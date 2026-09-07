# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The one output shape that is worth declaring to a client.

Thirty-six tools return thirty-six different dictionaries, and a schema for
each would be thirty-six things to keep true. `check` is the exception: its
envelope is already a contract, relied on by the CLI printer, the SARIF
export, the severity gate, the baselines and the HTML report, and the finding
inside it is built in exactly one place (`check._finding`). Writing that down
costs one model and buys two things:

- a client can see the keys before it calls, rather than calling once to find
  out what came back;
- the SDK validates the return against it, so a key renamed in `check.py`
  fails on the next run instead of silently disappearing from whichever
  consumer read it.

Both models allow extra keys on purpose. A check that starts carrying one more
piece of evidence should not be a protocol error, and the alternative -
forbidding extras - would make this file the thing that has to be edited first
before any check can say more than it does today.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["critical", "high", "medium", "low"]


class Finding(BaseModel):
    """One defect candidate, in the shape every check is merged into."""

    model_config = ConfigDict(extra="allow")

    check: str = Field(description="which analysis produced this")
    severity: Severity
    title: str = Field(description="one line, the defect and not the rule")
    location: str | None = Field(
        default=None,
        description=(
            "whatever the check found natural: a file and line, an app and "
            "migration, a serializer class. None when there is nothing to "
            "point at."
        ),
    )
    file: str | None = Field(
        default=None,
        description=(
            "project-relative path, set only when the location really is a "
            "file and a line. Given separately because a git diff cannot be "
            'compared against "app/x.py:12".'
        ),
    )
    line: int | None = None
    detail: str = Field(description="why this is a problem here, with the evidence")
    fix: str | None = Field(
        default=None, description="the change to make, when the check knows one"
    )


class CheckReport(BaseModel):
    """What `check` returns.

    Nothing is required. The error envelope (`ok: false` with a message) is a
    valid return from this tool - a settings module that will not import comes
    back that way rather than as a transport error - and a required field
    would turn that readable answer into a validation failure.
    """

    model_config = ConfigDict(extra="allow")

    ok: bool | None = Field(
        default=None,
        description="present and false only when the run could not happen at all",
    )
    error: str | None = None

    frameworks: dict[str, Any] | None = Field(
        default=None, description="what was detected in the project, and its version"
    )
    finding_count: int | None = None
    by_severity: dict[str, int] | None = None
    findings: list[Finding] | None = None
    checks_run: dict[str, Any] | None = Field(
        default=None, description="per check: whether it ran, and how many it produced"
    )
    checks_failed: list[str] | None = Field(
        default=None,
        description=(
            "checks that raised. These are not clean results - a project with "
            "a failed check has fewer findings than it has problems."
        ),
    )
    checks_not_applicable: dict[str, str] | None = Field(
        default=None, description="check name to the reason it does not apply here"
    )
    note: str | None = None
