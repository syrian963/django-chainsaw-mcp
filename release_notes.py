#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""The changelog section for one version, on stdout.

The release workflow uses this for the GitHub release body, so the Releases
page and CHANGELOG.md cannot drift apart: there is one text, written once,
and the release is a view of it.

It exits non-zero when the version has no section, which is the same thing the
workflow already checks before publishing anything - said twice on purpose,
because the first check runs before PyPI and this one runs after, and a
release with an empty body is the failure mode this exists to prevent.

    python release_notes.py 0.1.4
    python release_notes.py v0.1.4      # the leading v is optional
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_HEADING = re.compile(r"^## \[", re.M)


def section(text: str, version: str) -> str:
    """The changelog block for this version, heading included."""
    for chunk in _HEADING.split(text)[1:]:
        if chunk.split("]")[0] == version:
            return "## [" + chunk.rstrip() + "\n"
    raise LookupError(version)


def main(argv: list[str]) -> int:
    """Print the section, or say which version is missing one."""
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-2].strip(), file=sys.stderr)
        return 2
    version = argv[1].lstrip("v")
    changelog = Path(__file__).resolve().parent / "CHANGELOG.md"
    try:
        print(section(changelog.read_text(encoding="utf-8"), version), end="")
    except LookupError:
        print(f"CHANGELOG.md has no section for {version}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
