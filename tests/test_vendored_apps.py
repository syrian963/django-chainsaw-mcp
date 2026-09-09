# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""Under the project root is not the same as part of the project.

A virtualenv at `.venv/` is the normal layout, so everything installed into it
also lives under the project path. `deploy_safety` asked only "is this app's
path under the root" and therefore counted Django's own contrib apps, DRF,
taggit and django-filters as the project's own.

Found by running the tool against Wagtail, where 11 of the 41 apps it called
the project's came out of `.venv/lib/python3.14/site-packages`. The visible
damage was a critical verdict on `contenttypes/0002_remove_content_type_name`:
a `RemoveField` for a field called `name`, matched against 25 unrelated places
that happen to use that word. The docstring on the function had said all along
that third-party field names are too generic to scan for.
"""

from __future__ import annotations

from pathlib import Path

from django_chainsaw_mcp.deploy_safety import _project_app_labels
from django_chainsaw_mcp.project import is_vendored


class _Config:
    """The two attributes `_project_app_labels` reads off an AppConfig."""

    def __init__(self, label: str, path: str) -> None:
        self.label = label
        self.path = path


def test_a_path_inside_a_virtualenv_is_not_part_of_the_project():
    assert is_vendored(Path(".venv/lib/python3.14/site-packages/taggit"))
    assert is_vendored(Path("venv/lib/site-packages/rest_framework"))
    assert is_vendored(Path("node_modules/whatever"))
    assert is_vendored(Path("build/lib/myapp"))


def test_the_projects_own_code_is_not_mistaken_for_a_dependency():
    assert not is_vendored(Path("shop/models.py"))
    assert not is_vendored(Path("src/myproject/apps/orders"))
    # The word has to be a path segment. A directory that merely contains it
    # is the project's own.
    assert not is_vendored(Path("venvironments/app"))
    assert not is_vendored(Path("my-build-tools/app"))


def test_an_installed_dependency_under_the_root_is_not_one_of_our_apps(monkeypatch, tmp_path):
    root = tmp_path
    (root / "shop").mkdir()
    vendored = root / ".venv" / "lib" / "python3.14" / "site-packages" / "taggit"
    vendored.mkdir(parents=True)

    configs = [
        _Config("shop", str(root / "shop")),
        _Config("taggit", str(vendored)),
    ]

    import django.apps

    monkeypatch.setattr(django.apps.apps, "get_app_configs", lambda: configs)

    labels = _project_app_labels(root)
    assert labels == {"shop"}, (
        "an app installed into a virtualenv inside the project directory is "
        "not a deploy decision the user makes"
    )


def test_the_root_itself_still_counts_as_the_project(monkeypatch, tmp_path):
    # A single-app project where the app *is* the root. The vendored test must
    # not exclude it by accident - `tmp_path` can sit under a directory whose
    # name is on the skip list, and the check is about the path below the root.
    import django.apps

    monkeypatch.setattr(
        django.apps.apps, "get_app_configs", lambda: [_Config("app", str(tmp_path))]
    )
    assert _project_app_labels(tmp_path) == {"app"}


def test_an_app_outside_the_project_is_still_excluded(monkeypatch, tmp_path):
    import django.apps

    monkeypatch.setattr(
        django.apps.apps, "get_app_configs", lambda: [_Config("elsewhere", "/usr/lib/other")]
    )
    assert _project_app_labels(tmp_path) == set()
