# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""Boot the demo project once for the whole session.

`django.setup()` cannot be undone, so this is session-scoped by necessity
rather than by preference: a second call with a different project would raise,
which is the behaviour the server documents.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO = REPO_ROOT / "testprojects"


@pytest.fixture(scope="session")
def demo_root() -> Path:
    return DEMO


@pytest.fixture(scope="session")
def django_project(demo_root: Path):
    from django_chainsaw_mcp.django_env import (
        PROJECT_PATH_VAR,
        SETTINGS_MODULE_VAR,
        ensure_django,
    )

    os.environ.setdefault(PROJECT_PATH_VAR, str(demo_root))
    os.environ.setdefault(SETTINGS_MODULE_VAR, "demoshop.settings")

    # Analyses that scan a directory default to the configured project path, so
    # they find the demo project without every test passing a path.
    previous = os.getcwd()
    os.chdir(REPO_ROOT)
    try:
        yield ensure_django()
    finally:
        os.chdir(previous)
