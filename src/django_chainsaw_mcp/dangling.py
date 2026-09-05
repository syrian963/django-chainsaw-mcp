# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Strings that name something the framework has to find, checked ahead of time.

    return redirect("order-detial")
    return render(request, "shop/order_detial.html", context)
    {% url 'shop:order-detial' order.pk %}

Every one of these is a name the framework resolves while serving a request,
and a name nothing in Python checks. Rename a URL pattern or move a template
and these keep importing, keep passing every test that does not walk that exact
branch, and raise `NoReverseMatch` or `TemplateDoesNotExist` the first time a
real person opens the page.

That is a different failure from a typo in a variable: it survives review, it
survives the import, it survives CI, and it fails in production on a path
somebody rarely takes - which is exactly the path nobody was watching.

## Both directions are checked with the project's own machinery

The URL names come from the resolver, walked through every `include()` so that
namespaces are real: `shop:order-detail` is checked as `shop:order-detail`, not
as `order-detail`. The template names go through `get_template()`, which uses
the loaders the project configured rather than a guess at where templates live;
a template that exists but fails to compile is **not** reported, because the
question here is whether it is there.

## Why no tool does this

`django-lint` predates most of this and checks settings and field types.
`makemigrations --check` and Django's own system checks never look at a string
literal. A test suite finds it only where coverage happens to reach, and these
strings live in the branches coverage misses - the error page, the
rarely-taken redirect, the admin action.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from .django_env import ensure_django

# Calls whose first string argument is a URL name.
_URL_CALLS = {"reverse", "reverse_lazy", "resolve_url"}
# `redirect` takes a URL name, a path or a model instance, so only a literal
# that cannot be a path is treated as a name.
_REDIRECT_CALLS = {"redirect"}
# Calls whose template argument is a literal name.
_TEMPLATE_CALLS = {
    "render": 1,
    "render_to_string": 0,
    "get_template": 0,
    "select_template": None,   # takes a list
    "TemplateResponse": 1,
    "render_to_response": 0,
}

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", "dist", "build",
}

# `{% url 'name' %}` / `{% url "name" %}`, and the same for include/extends.
_TEMPLATE_URL = re.compile(r"{%\s*url\s+['\"]([^'\"]+)['\"]")
_TEMPLATE_FILE = re.compile(r"{%\s*(?:include|extends)\s+['\"]([^'\"]+)['\"]")


def _urlconf_modules(root: Path) -> list[str]:
    """Every module in the project that defines `urlpatterns`.

    `ROOT_URLCONF` is one URLconf, not necessarily the only one. A project
    that serves two sites from one codebase picks the URLconf per request -
    `request.urlconf`, set by middleware from the host - and the names in the
    other one reverse perfectly at runtime while being entirely absent from
    the resolver this process booted with.

    Checking against `ROOT_URLCONF` alone reported 629 working `reverse()`
    calls as broken on the first real project this ran against. A name is only
    dangling if no URLconf in the project registers it.
    """
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        for node in tree.body:
            targets = (
                node.targets if isinstance(node, ast.Assign)
                else [node.target] if isinstance(node, ast.AnnAssign)
                else []
            )
            if any(isinstance(x, ast.Name) and x.id == "urlpatterns" for x in targets):
                relative = path.relative_to(root).with_suffix("")
                found.append(".".join(relative.parts))
                break
    return found


def _walk_patterns(patterns: Any, prefix: str, depth: int, names: set[str]) -> None:
    if depth > 20:
        return
    for entry in patterns:
        nested = getattr(entry, "url_patterns", None)
        if nested is not None:
            namespace = getattr(entry, "namespace", None)
            _walk_patterns(
                nested, f"{prefix}{namespace}:" if namespace else prefix,
                depth + 1, names,
            )
            continue
        name = getattr(entry, "name", None)
        if name:
            names.add(f"{prefix}{name}")
            # A namespaced pattern is also reachable unqualified from inside
            # its own namespace, and reporting that spelling as missing would
            # be wrong.
            names.add(name)


def _registered_url_names(root: Path) -> tuple[set[str], int, str | None]:
    """Every name any URLconf in the project can reverse, namespaces included."""
    names: set[str] = set()
    problem: str | None = None
    conf_count = 0

    try:
        from django.urls import get_resolver

        _walk_patterns(get_resolver().url_patterns, "", 0, names)
        conf_count += 1
    except Exception as exc:  # noqa: BLE001 - no ROOT_URLCONF, or it raises
        problem = f"{type(exc).__name__}: {exc}"

    import importlib

    for dotted in _urlconf_modules(root):
        try:
            module = importlib.import_module(dotted)
            patterns = getattr(module, "urlpatterns", None)
            if patterns is None:
                continue
            _walk_patterns(patterns, "", 0, names)
            conf_count += 1
        except Exception:  # noqa: BLE001 - a urls module that will not import
            continue

    return names, conf_count, problem


def _template_exists(name: str, cache: dict[str, bool]) -> bool:
    """Whether the project's own loaders can find this template."""
    if name not in cache:
        from django.template import TemplateDoesNotExist
        from django.template.loader import get_template

        try:
            get_template(name)
            cache[name] = True
        except TemplateDoesNotExist:
            cache[name] = False
        except Exception:  # noqa: BLE001
            # It was found and would not compile, which is a different
            # problem and not this one's to report.
            cache[name] = True
    return cache[name]


def _looks_like_a_path(value: str) -> bool:
    return (
        "/" in value
        or value.startswith(("http://", "https://", "#", "."))
        or value == ""
    )


def _first_string(call: ast.Call, position: int | None) -> tuple[str, ...]:
    """The literal string argument(s) at `position`, or () if not literal."""
    if position is None:
        if not call.args:
            return ()
        candidate = call.args[0]
        if isinstance(candidate, (ast.List, ast.Tuple)):
            out = tuple(
                element.value for element in candidate.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            )
            return out
        return ()
    if len(call.args) <= position:
        return ()
    node = call.args[position]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return (node.value,)
    return ()


def _name_of(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def dangling_references(
    search_path: str | None = None,
    include_templates: bool = True,
) -> dict[str, Any]:
    """URL names and template names that nothing will resolve.

    Args:
        search_path: directory to scan. Defaults to the project path.
        include_templates: also read `{% url %}`, `{% include %}` and
            `{% extends %}` out of the templates themselves.
    """
    config = ensure_django()

    root = Path(search_path).expanduser().resolve() if search_path else config.project_path
    if not root.is_dir():
        raise ValueError(f"search_path is not a directory: {root}")

    url_names, urlconfs_read, url_error = _registered_url_names(root)
    template_cache: dict[str, bool] = {}

    findings: list[dict[str, Any]] = []
    checked = {"url": 0, "template": 0}
    files_scanned = 0

    def report(kind: str, where: str, line: int, code: str, value: str, why: str,
               fix: str) -> None:
        findings.append({
            "kind": kind,
            "file": where,
            "line": line,
            "code": code[:140],
            "name": value,
            "severity": "high",
            "why": why,
            "fix": fix,
        })

    for path in sorted(root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue

        files_scanned += 1
        lines = source.splitlines()
        relative = str(path.relative_to(root))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _name_of(node)
            code = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""

            if name in _URL_CALLS | _REDIRECT_CALLS:
                for value in _first_string(node, 0):
                    if name in _REDIRECT_CALLS and _looks_like_a_path(value):
                        continue
                    if not url_names:
                        continue
                    checked["url"] += 1
                    if value in url_names:
                        continue
                    report(
                        "url", relative, node.lineno, code, value,
                        f"no URL pattern is named {value!r}. "
                        + (
                            f"{name}() raises NoReverseMatch the first time this "
                            "line runs, which is whenever somebody takes this "
                            "branch"
                        ),
                        "correct the name, or add the pattern it expects",
                    )

            elif name in _TEMPLATE_CALLS:
                for value in _first_string(node, _TEMPLATE_CALLS[name]):
                    if not value.strip():
                        continue
                    checked["template"] += 1
                    if _template_exists(value, template_cache):
                        continue
                    report(
                        "template", relative, node.lineno, code, value,
                        f"no configured loader finds {value!r}. "
                        "TemplateDoesNotExist is raised while rendering, not "
                        "at import, so this passes every test that does not "
                        "render this view",
                        "correct the path, or move the template to where the "
                        "loaders look",
                    )

    templates_scanned = 0
    if include_templates:
        for path in sorted(root.rglob("*.html")):
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            templates_scanned += 1
            relative = str(path.relative_to(root))
            lines = source.splitlines()

            for number, text in enumerate(lines, start=1):
                for value in _TEMPLATE_URL.findall(text):
                    if not url_names:
                        continue
                    checked["url"] += 1
                    if value in url_names:
                        continue
                    report(
                        "url", relative, number, text.strip(), value,
                        f"no URL pattern is named {value!r}. A template raises "
                        "NoReverseMatch while rendering, which takes the whole "
                        "page down rather than one element of it",
                        "correct the name, or add the pattern it expects",
                    )
                for value in _TEMPLATE_FILE.findall(text):
                    checked["template"] += 1
                    if _template_exists(value, template_cache):
                        continue
                    report(
                        "template", relative, number, text.strip(), value,
                        f"no configured loader finds {value!r}",
                        "correct the path, or move the template",
                    )

    findings.sort(key=lambda f: (f["kind"], f["file"], f["line"]))

    # One missing name used in thirty-five places is one problem, and a list
    # of thirty-five lines hides that. On the first real project it ran
    # against, 173 findings collapsed into a handful of causes - one app whose
    # urlpatterns were commented out, and a few renamed patterns.
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for finding in findings:
        key = (finding["kind"], finding["name"])
        slot = grouped.setdefault(key, {
            "kind": finding["kind"],
            "name": finding["name"],
            "uses": 0,
            "files": [],
            "severity": finding["severity"],
            "why": finding["why"],
        })
        slot["uses"] += 1
        where = f"{finding['file']}:{finding['line']}"
        if len(slot["files"]) < 10:
            slot["files"].append(where)
    by_name = sorted(grouped.values(), key=lambda g: (-g["uses"], g["kind"], g["name"]))

    return {
        "findings": findings,
        "by_name": by_name,
        "distinct_names": len(by_name),
        "finding_count": len(findings),
        "url_findings": sum(1 for f in findings if f["kind"] == "url"),
        "template_findings": sum(1 for f in findings if f["kind"] == "template"),
        "url_names_registered": len(url_names),
        "urlconfs_read": urlconfs_read,
        "url_names_checked": checked["url"],
        "template_names_checked": checked["template"],
        "files_scanned": files_scanned,
        "templates_scanned": templates_scanned,
        "urlconf_error": url_error,
        "note": (
            "URL names and template names that nothing will resolve. Both are "
            "checked with the project's own machinery: the names come from the "
            "resolver walked through every include(), so a namespace is real "
            "and `shop:detail` is checked as `shop:detail`; the templates go "
            "through get_template(), so whatever loaders the project "
            "configured are the ones that decide. A template that exists and "
            "fails to compile is not reported, because the question here is "
            "whether it is there. Only literals are checked - a name built "
            "from a variable or an f-string cannot be resolved without running "
            "the code. `redirect()` also takes a path and a model instance, so "
            "a literal containing a slash or a scheme is left alone. Every "
            "URLconf in the project is read, not only ROOT_URLCONF: a project "
            "serving two sites picks the URLconf per request, and the names in "
            "the other one reverse perfectly at runtime."
            + (
                f" The URLconf could not be fully read ({url_error}), so URL "
                "names were not checked and their absence here means nothing."
                if url_error else ""
            )
        ),
    }
