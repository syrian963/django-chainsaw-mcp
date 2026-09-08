# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Two defects that only a second unfamiliar project showed.

Wagtail found the vendored-app bug and neither of these. Saleor - 4332 files,
122 models, GraphQL instead of DRF, a custom user model - found both on its
first run, which is the argument for running against more than one stranger's
code rather than one.
"""

from __future__ import annotations

import ast

import pytest

from django_chainsaw_mcp.signals import _conditional_lines
from django_chainsaw_mcp.tenancy import DEFAULT_TENANT_ROOT, resolve_tenant_root


@pytest.fixture(autouse=True)
def _demo(django_project):
    """The demo project, loaded before anything reads a setting.

    Two of these read , and a module that only passes when
    some earlier test file happened to boot Django is the failure mode this
    repository already fixed once in .
    """


# --- a ternary is a conditional whose body is not a list --------------------


def test_a_ternary_does_not_take_the_whole_check_down():
    # `ast.If.body` is a list of statements; `ast.IfExp.body` is one
    # expression. Reading both the same way raised
    # `TypeError: 'Constant' object is not iterable` out of `bypass` on Saleor,
    # killing the check rather than skipping one receiver.
    tree = ast.parse("def receiver(sender, instance, **kwargs):\n    x = 1 if instance else 2\n")
    assert _conditional_lines(tree) == {2}


def test_a_ternary_nested_in_a_statement_is_still_read():
    tree = ast.parse(
        "def receiver(sender, instance, **kwargs):\n"
        "    instance.log(created='yes' if kwargs['created'] else 'no')\n"
    )
    assert 2 in _conditional_lines(tree)


def test_an_if_statement_still_reports_both_branches():
    tree = ast.parse(
        "def receiver(sender, instance, created, **kwargs):\n"
        "    if created:\n"
        "        instance.first()\n"
        "    else:\n"
        "        instance.second()\n"
    )
    assert _conditional_lines(tree) == {3, 5}


def test_a_body_with_nothing_conditional_reports_nothing():
    tree = ast.parse("def receiver(sender, instance, **kwargs):\n    instance.save()\n")
    assert _conditional_lines(tree) == set()


# --- the default tenant root a real project does not have -------------------


def test_the_default_resolves_to_the_projects_own_user_model(monkeypatch):
    # A project with a custom user has no `auth.User` at all, so the default
    # was not a weaker answer - it was no answer. Saleor's user is
    # `account.User`, and the check refused to run and listed 40 models it
    # might have meant.
    from django.apps import apps
    from django.conf import settings

    real = apps.get_model

    def without_auth_user(label, *args, **kwargs):
        if label == DEFAULT_TENANT_ROOT:
            raise LookupError("no such model")
        return real(label, *args, **kwargs)

    monkeypatch.setattr(apps, "get_model", without_auth_user)
    monkeypatch.setattr(settings, "AUTH_USER_MODEL", "shop.Customer", raising=False)

    label, note = resolve_tenant_root(DEFAULT_TENANT_ROOT)
    assert label == "shop.Customer"
    assert "AUTH_USER_MODEL" in note, "the substitution has to travel with the report"


def test_a_root_somebody_typed_is_never_quietly_replaced(monkeypatch):
    # A report about the wrong root is worse than an error. Only the default
    # is resolved; an explicit label is passed through and fails loudly later
    # if it does not exist.
    from django.conf import settings

    monkeypatch.setattr(settings, "AUTH_USER_MODEL", "shop.Customer", raising=False)
    assert resolve_tenant_root("shop.Order") == ("shop.Order", None)
    assert resolve_tenant_root("nope.Nope") == ("nope.Nope", None)


def test_a_project_that_kept_auth_user_is_untouched(django_project):
    # The demo project has a real `auth.User`, so nothing is resolved and no
    # note is produced. This is the case that must not gain a note.
    assert resolve_tenant_root(DEFAULT_TENANT_ROOT) == (DEFAULT_TENANT_ROOT, None)


def test_an_unusable_auth_user_model_setting_falls_back_to_the_error(monkeypatch):
    # `AUTH_USER_MODEL` naming a model that is not installed must not become a
    # second confusing substitution. The original label is returned and
    # `_ownership_paths` raises its own listing error.
    from django.apps import apps
    from django.conf import settings

    real = apps.get_model

    def nothing_resolves(label, *args, **kwargs):
        if label in (DEFAULT_TENANT_ROOT, "ghost.User"):
            raise LookupError("no such model")
        return real(label, *args, **kwargs)

    monkeypatch.setattr(apps, "get_model", nothing_resolves)
    monkeypatch.setattr(settings, "AUTH_USER_MODEL", "ghost.User", raising=False)
    assert resolve_tenant_root(DEFAULT_TENANT_ROOT) == (DEFAULT_TENANT_ROOT, None)


def test_the_reported_root_is_the_one_that_was_analysed(django_project):
    # The value in the report has to be the model the paths were built from,
    # not the argument that came in - otherwise a reader of a Saleor report
    # would see `auth.User` over an analysis of `account.User`.
    from django_chainsaw_mcp.tenancy import find_unscoped_queries

    report = find_unscoped_queries(tenant_root="shop.Customer")
    assert report["tenant_root"] == "shop.Customer"
    assert report["tenant_root_note"] is None


def test_the_check_itself_uses_the_resolver_and_not_just_the_helper(monkeypatch, django_project):
    """The wiring, not the resolver.

    Reverting the one line that calls `resolve_tenant_root` left every test
    above green, because they all called the helper directly. That is a test
    suite proving a function exists rather than proving it is used, so this
    one drives `find_unscoped_queries` with no argument on a project that has
    no `auth.User` and reads the root out of the report.
    """
    from django.apps import apps
    from django.conf import settings

    from django_chainsaw_mcp.tenancy import find_unscoped_queries

    real = apps.get_model

    def without_auth_user(label, *args, **kwargs):
        if label == DEFAULT_TENANT_ROOT:
            raise LookupError("no such model")
        return real(label, *args, **kwargs)

    monkeypatch.setattr(apps, "get_model", without_auth_user)
    monkeypatch.setattr(settings, "AUTH_USER_MODEL", "shop.Customer", raising=False)

    report = find_unscoped_queries()
    assert report["tenant_root"] == "shop.Customer", (
        "the report has to name the model the analysis actually used"
    )
    assert "AUTH_USER_MODEL" in (report["tenant_root_note"] or "")
    assert report["tenant_scoped_models"], "the check has to have actually run"


# --- the test layout the exemption list did not know about -----------------


def test_a_tests_package_is_exempt_and_not_only_a_tests_module():
    """`tests.py` was listed; `tests/test_foo.py` was not.

    The first is what `startproject` gives you and the second is what every
    project past a certain size uses, so the promise that tests are skipped
    held for small projects and quietly failed for large ones. On Saleor it
    was the difference between 1992 findings and 263 - 87% of the output came
    from code the check does not mean to read.
    """
    from pathlib import Path as P

    from django_chainsaw_mcp.tenancy import _exempt

    assert _exempt(P("saleor/order/tests.py"))
    assert _exempt(P("saleor/order/tests/test_orders.py"))
    assert _exempt(P("saleor/order/tests/mutations/test_draft.py"))
    assert _exempt(P("saleor/order/test_orders.py")), "a test module outside a tests package"
    assert _exempt(P("saleor/order/orders_test.py")), "the other naming convention"
    assert _exempt(P("saleor/order/conftest.py"))


def test_production_code_is_not_mistaken_for_a_test():
    from pathlib import Path as P

    from django_chainsaw_mcp.tenancy import _exempt

    assert not _exempt(P("saleor/order/models.py"))
    assert not _exempt(P("saleor/graphql/order/mutations.py"))
    # The word has to be the whole path segment or a real prefix, not a
    # substring: a `latest.py` or a `contest.py` is production code.
    assert not _exempt(P("saleor/order/latest.py"))
    assert not _exempt(P("saleor/order/contest.py"))
    assert not _exempt(P("saleor/protests/views.py"))


def test_the_exemption_can_still_be_turned_off(django_project):
    from django_chainsaw_mcp.tenancy import find_unscoped_queries

    narrow = find_unscoped_queries(tenant_root="shop.Customer")
    wide = find_unscoped_queries(tenant_root="shop.Customer", include_exempt=True)
    assert wide["files_scanned"] >= narrow["files_scanned"], (
        "include_exempt has to widen the scan, not narrow it"
    )


# --- three ways `dangling` was wrong about django-oscar ---------------------


def test_a_form_widget_template_shipped_inside_django_is_not_missing(django_project):
    """`TEMPLATES` is not the only engine that renders templates.

    Django's form widgets render through the engine `FORM_RENDERER` builds,
    which carries `django/forms/templates` on its own path and is invisible to
    `django.template.loader.get_template` unless the project also lists
    `django.forms` in INSTALLED_APPS. django-oscar includes
    `django/forms/widgets/input.html` from two of its own widget templates,
    and both were reported as dangling references to a file that ships inside
    Django.
    """
    from django_chainsaw_mcp.dangling import _template_exists

    assert _template_exists("django/forms/widgets/input.html", {})


def test_a_template_that_is_genuinely_absent_is_still_reported(django_project):
    # The fix widens where the check looks. It must not turn into "everything
    # exists", which would be the quiet failure.
    from django_chainsaw_mcp.dangling import _template_exists

    assert not _template_exists("shop/there_is_no_such_template.html", {})


def test_a_render_method_on_an_object_is_not_the_django_shortcut(django_project, tmp_path):
    """`wrapper.render("name", "value")` is a widget, not a view.

    `render` is matched at argument 1, which is the template for the shortcut
    `render(request, "x.html")` and the *form value* for
    `Widget.render(name, value)`. django-oscar's test called the second one
    and the check reported a missing template named "value".
    """
    from django_chainsaw_mcp.dangling import dangling_references

    module = tmp_path / "widgets.py"
    module.write_text(
        "def go(wrapper, request):\n"
        "    wrapper.render('name', 'value')\n"
        "    return render(request, 'shop/no_such_template.html')\n"
    )
    report = dangling_references(search_path=str(tmp_path), include_templates=False)
    names = {f["name"] for f in report["findings"] if f["kind"] == "template"}
    assert "value" not in names, "a widget's form value is not a template name"
    assert "shop/no_such_template.html" in names, (
        "the plain shortcut still has to be checked"
    )


def test_test_modules_are_skipped_and_the_report_says_how_many(django_project, tmp_path):
    """Not noise-reduction: tests run under a different settings module.

    django-oscar's tests reverse `catalogue:parent_detail`, registered by a
    test-only app under `tests/_site`. Under the sandbox settings the check
    was given, that name does not exist - so the finding was an answer to a
    question asked against the wrong URLconf.
    """
    from django_chainsaw_mcp.dangling import dangling_references

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_views.py").write_text(
        "from django.urls import reverse\n"
        "def test_it():\n"
        "    reverse('shop:no_such_name_at_all')\n"
    )
    (tmp_path / "views.py").write_text(
        "from django.urls import reverse\n"
        "def go():\n"
        "    return reverse('shop:also_no_such_name')\n"
    )

    default = dangling_references(search_path=str(tmp_path), include_templates=False)
    names = {f["name"] for f in default["findings"]}
    assert "shop:also_no_such_name" in names, "production code is still read"
    assert "shop:no_such_name_at_all" not in names, "the test module is not"
    assert default["test_files_skipped"] == 1
    assert "under their own settings" in default["note"], (
        "a reader has to be told the test modules were not read"
    )

    everything = dangling_references(
        search_path=str(tmp_path), include_templates=False, include_tests=True
    )
    assert {"shop:also_no_such_name", "shop:no_such_name_at_all"} <= {
        f["name"] for f in everything["findings"]
    }
    assert everything["test_files_skipped"] == 0


def test_an_unknown_root_is_still_an_error_and_names_what_exists(django_project):
    from django_chainsaw_mcp.tenancy import find_unscoped_queries

    with pytest.raises(ValueError) as raised:
        find_unscoped_queries(tenant_root="nope.Nope")
    assert "nope.Nope" in str(raised.value)
    assert "Known models" in str(raised.value)
