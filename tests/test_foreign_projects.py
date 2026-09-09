# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""Two defects that only a second unfamiliar project showed.

Wagtail found the vendored-app bug and neither of these. Saleor - 4332 files,
122 models, GraphQL instead of DRF, a custom user model - found both on its
first run, which is the argument for running against more than one stranger's
code rather than one.
"""

from __future__ import annotations

import ast
from pathlib import Path

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


# --- a repository that holds more than one project -------------------------


def test_a_template_no_loader_can_reach_is_not_read(django_project, tmp_path):
    """A repository often carries templates the project cannot render.

    django-tenants ships three tutorials under `examples/`, each with its own
    settings and its own template directory that is on nobody's search path.
    Reading `{% url %}` out of those asked this project's resolver about names
    belonging to a different one - 30 of 37 findings.
    """
    from django_chainsaw_mcp.dangling import _is_reachable_template, _template_roots

    roots = _template_roots()
    assert roots, "the demo project has loader directories to compare against"

    inside = roots[0] / "whatever.html"
    assert _is_reachable_template(inside, roots)
    assert not _is_reachable_template(tmp_path / "examples" / "index.html", roots)


def test_with_no_readable_loader_directories_everything_is_read(tmp_path):
    # Scanning too much is the better failure: a project configuring templates
    # in a way this cannot read must not silently stop being checked.
    from django_chainsaw_mcp.dangling import _is_reachable_template

    assert _is_reachable_template(tmp_path / "anywhere.html", [])


def test_the_demo_projects_own_templates_are_still_read(django_project):
    # The filter narrows what is scanned, so the case that must not break is
    # the ordinary one.
    from django_chainsaw_mcp.dangling import dangling_references

    report = dangling_references()
    assert report["templates_scanned"] > 0, "the project's own templates stopped being read"
    assert report["templates_unreachable"] == 0
    assert report["nested_projects"] == []


def test_the_scan_itself_skips_unreachable_templates(django_project, tmp_path):
    """The wiring, again.

    Reverting the filter left every test above green, because they all called
    `_is_reachable_template` directly - the same shape of gap that let the
    tenant-root revert pass earlier in this file. This one drives the check
    over a directory no loader knows about and reads the counts back.
    """
    from django_chainsaw_mcp.dangling import dangling_references

    stray = tmp_path / "examples"
    stray.mkdir()
    (stray / "index.html").write_text(
        "{% extends 'base.html' %}\n{% url 'not_a_real_name_anywhere' %}\n"
    )

    report = dangling_references(search_path=str(tmp_path))
    assert report["templates_unreachable"] == 1
    assert report["templates_scanned"] == 0, "an unreachable template must not be read"
    assert not [f for f in report["findings"] if f["file"].endswith("index.html")]
    assert "outside every configured loader directory" in report["note"]


def test_a_directory_with_its_own_manage_py_is_a_different_project(django_project, tmp_path):
    """`manage.py` is the unambiguous marker of a nested project.

    Each django-tenants tutorial has one, its own `urlpatterns`, and an app
    called `customers` that is not the `customers` in INSTALLED_APPS.
    """
    from django_chainsaw_mcp.dangling import _nested_project_dirs

    root = tmp_path
    (root / "manage.py").write_text("")           # the analysed project's own
    sample = root / "examples" / "tutorial"
    sample.mkdir(parents=True)
    (sample / "manage.py").write_text("")
    vendored = root / ".venv" / "somepkg"
    vendored.mkdir(parents=True)
    (vendored / "manage.py").write_text("")

    nested = _nested_project_dirs(root)
    assert sample.resolve() in nested
    assert root.resolve() not in nested, "the root itself is the project, not a nested one"
    assert not any(".venv" in str(d) for d in nested), "skip dirs are not searched"


def test_files_in_a_nested_project_are_skipped_and_counted(django_project, tmp_path):
    from django_chainsaw_mcp.dangling import dangling_references

    (tmp_path / "views.py").write_text(
        "from django.urls import reverse\n"
        "def go():\n"
        "    return reverse('shop:missing_from_this_project')\n"
    )
    sample = tmp_path / "examples" / "tutorial"
    sample.mkdir(parents=True)
    (sample / "manage.py").write_text("")
    (sample / "views.py").write_text(
        "from django.urls import reverse\n"
        "def go():\n"
        "    return reverse('their_own_url_name')\n"
    )

    report = dangling_references(search_path=str(tmp_path), include_templates=False)
    names = {f["name"] for f in report["findings"]}
    assert "shop:missing_from_this_project" in names
    assert "their_own_url_name" not in names, (
        "a nested project's URL names belong to its own resolver"
    )
    # Two: the nested `views.py`, and the `manage.py` that marked it.
    assert report["files_in_nested_projects_skipped"] == 2
    assert "nested Django project" in report["note"]


# --- the async idiom the check was wrong about -----------------------------

_BODY = """\
def load_user(scope):
    session = scope["session"]
    return session.get("user_id")


async def consume(scope):
    {call}
"""

_THREADED = (
    "from channels.db import database_sync_to_async\n\n\n@database_sync_to_async\n"
    + _BODY.format(call="await load_user(scope)")
)

_UNWRAPPED = _BODY.format(call="load_user(scope)")


def _async_findings(tmp_path, source: str) -> list[dict]:
    from django_chainsaw_mcp.asyncio_blocking import blocking_in_async

    (tmp_path / "consumers.py").write_text(source)
    report = blocking_in_async(search_path=str(tmp_path))
    return list(report["direct"]) + list(report["reached_through_a_call"])


def test_a_body_handed_to_a_thread_by_a_decorator_does_not_block(django_project, tmp_path):
    """`@database_sync_to_async` is the fix, not the defect.

    The check followed the call graph straight through the decorator. On
    channels itself - the reference implementation of doing this correctly -
    all 25 findings were this: `channels/auth.py` puts the decorator on every
    function that touches the session, and each was reported as blocking the
    loop. A check that is wrong about the correct way to write the code is
    worse than one that stays quiet.
    """
    assert _async_findings(tmp_path, _THREADED) == []


def test_the_same_call_without_the_decorator_is_still_reported(django_project, tmp_path):
    # The fix narrows what is reported, so the case that must not disappear is
    # the one the check exists for: the identical body, called from async, with
    # nothing moving it off the loop.
    findings = _async_findings(tmp_path, _UNWRAPPED)
    assert findings, "an unwrapped synchronous call from async must still be reported"
    assert any("load_user" in str(f) for f in findings)


def test_reading_a_connections_settings_dict_is_not_a_query(django_project, tmp_path):
    """`settings_dict` is configuration, not a round trip.

    `_ORM_RECEIVERS` matches anywhere in the chain, so
    `db.connections["default"].settings_dict.get("NAME")` read as a database
    receiver - a dictionary lookup reported as a synchronous query. channels'
    own test suite does exactly that twice.
    """
    source = (
        "from django import db\n"
        "async def check_db():\n"
        '    return db.connections["default"].settings_dict.get("NAME")\n'
    )
    assert _async_findings(tmp_path, source) == []


def test_a_real_query_on_a_connection_is_still_reported(django_project, tmp_path):
    source = (
        "from django import db\n"
        "async def go():\n"
        '    return db.connections["default"].cursor().execute("select 1")\n'
    )
    assert _async_findings(tmp_path, source), "a cursor execute is a real round trip"


# --- a project that has no settings module at all --------------------------


def _boot_error(monkeypatch, root: Path) -> str:
    from django_chainsaw_mcp.django_env import (
        PROJECT_PATH_VAR,
        SETTINGS_MODULE_VAR,
        BootConfig,
        DjangoBootError,
    )

    monkeypatch.setenv(PROJECT_PATH_VAR, str(root))
    monkeypatch.delenv(SETTINGS_MODULE_VAR, raising=False)
    try:
        BootConfig.from_env()
    except DjangoBootError as exc:
        return str(exc)
    raise AssertionError("expected a boot error")


def test_a_library_that_configures_settings_in_code_is_named_as_such(monkeypatch, tmp_path):
    """"You did not set the variable" sends people after a file nobody wrote.

    django-rest-framework configures Django in `tests/conftest.py` with
    `settings.configure(...)`, which is how a library boots itself for its own
    test run. There is no settings module, so the generic message is a wild
    goose chase.
    """
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "conftest.py").write_text(
        "from django.conf import settings\n"
        "def pytest_configure():\n"
        "    settings.configure(INSTALLED_APPS=[])\n"
    )

    message = _boot_error(monkeypatch, tmp_path)
    assert "no settings module to point at" in message
    assert "tests/conftest.py" in message
    assert "settings.configure()" in message


def test_an_ordinary_project_gets_the_ordinary_message(monkeypatch, tmp_path):
    # The hint must not appear for a project that simply has the variable
    # unset, which is the common case and a different problem.
    (tmp_path / "myproject").mkdir()
    (tmp_path / "myproject" / "settings.py").write_text("DEBUG = True\n")

    message = _boot_error(monkeypatch, tmp_path)
    assert "Missing environment variable" in message
    assert "no settings module to point at" not in message


def test_the_hint_does_not_walk_the_whole_tree(monkeypatch, tmp_path):
    # This runs on a failure path. A `settings.configure()` buried somewhere
    # unconventional is not worth scanning a large project to find.
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "conftest.py").write_text("settings.configure()\n")

    assert "no settings module to point at" not in _boot_error(monkeypatch, tmp_path)


# --- serializers a factory built at runtime --------------------------------


def _subset_of(base, *fields):
    """Misago's own pattern, reduced: a narrowed serializer made with type().

    `misago/core/serializers.py` does exactly this, and the resulting class
    reports `__module__` as wherever the factory lives rather than where the
    project binds the result.
    """

    class Meta(base.Meta):
        pass

    Meta.fields = list(fields)
    name = base.__name__ + "".join(f.title() for f in fields) + "Subset"
    return type(name, (base,), {"Meta": Meta})


def test_a_class_the_project_binds_is_found_whatever_module_it_claims(django_project):
    """The predicate that replaced a wrong one.

    The first version asked whether the class was bound in the module its
    `__module__` names. Misago's factory calls `type()` from a mixin, so a
    served serializer claims `rest_framework.serializers` - and the check
    suppressed a real finding about the fields it exposes. Being bound
    somewhere in the project is the question; `__module__` is not.
    """
    import sys

    from django_chainsaw_mcp.discovery import binding_site

    module = sys.modules[__name__]
    generated = type("GeneratedThing", (), {})
    generated.__module__ = "rest_framework.serializers"

    assert binding_site(generated) is None, "bound nowhere yet"

    module.GeneratedThing = generated
    try:
        site = binding_site(generated)
        assert site is not None, "a class bound in a project module is findable"
        assert site.endswith(".GeneratedThing")
    finally:
        del module.GeneratedThing


def test_the_label_is_the_binding_site_when_they_disagree(django_project):
    import sys

    from django_chainsaw_mcp.discovery import serializer_label

    module = sys.modules[__name__]
    generated = type("UserSerializerIdUsernameEmailSubset", (), {})
    generated.__module__ = "rest_framework.serializers"
    module.ReadableName = generated
    try:
        label = serializer_label(generated)
        assert label.endswith(".ReadableName"), (
            f"a hundred-character generated name is not somewhere to go: {label}"
        )
    finally:
        del module.ReadableName


def test_an_ordinary_class_is_labelled_the_ordinary_way(django_project):
    from django_chainsaw_mcp.discovery import serializer_label

    assert serializer_label(_Ordinary).endswith("._Ordinary")


class _Ordinary:
    pass


def test_a_serializer_bound_nowhere_is_skipped_by_the_exposure_check(django_project):
    """Built and used inline, so there is no file to send anybody to.

    The finding against the class it was built from already says the same
    thing, and reporting both counts one defect twice.
    """
    from rest_framework import serializers as drf

    from django_chainsaw_mcp.discovery import binding_site
    from django_chainsaw_mcp.serializers import serializer_exposure

    class Declared(drf.ModelSerializer):
        class Meta:
            from django.contrib.auth.models import User

            model = User
            fields = ("id", "username", "password")

    # The subset has to keep the sensitive field, or the exposure check has
    # nothing to say about it and the test passes whether it is skipped or not.
    inline = _subset_of(Declared, "id", "password")
    assert binding_site(inline) is None, "the subset is bound nowhere"

    labels = {f["serializer"] for f in serializer_exposure()["findings"]}
    assert not any("Subset" in label for label in labels), (
        f"a runtime subset reached the report: {sorted(labels)}"
    )


# --- a zero that can be read -----------------------------------------------


def test_every_check_reports_what_it_looked_at(django_project):
    """`findings: 0` reads as clean and can mean the check saw nothing.

    `celery` reported nothing on Misago - 2025 files with Celery in them - and
    the merged report said `findings: 0`. The check itself knew the
    difference: 12 tasks, 41 dispatches, not one handed a model instance. That
    is a clean zero, and the aggregate had thrown the evidence away, while the
    server's own instructions promise a client that an empty result says which
    kind it is.
    """
    from django_chainsaw_mcp.check import run_all

    report = run_all(tenant_root="shop.Customer")
    silent = [
        name
        for name, state in report["checks_run"].items()
        if state["ok"] and not state.get("examined")
    ]
    assert not silent, (
        f"these checks ran without saying what they looked at, so a zero from "
        f"them cannot be read: {sorted(silent)}"
    )


def test_coverage_counts_are_not_finding_counts(django_project):
    # The four checks that count with `_count` are listed by hand, because the
    # same suffix also names results. Picking up `finding_count` would make
    # every zero look examined.
    from django_chainsaw_mcp.check import run_all

    report = run_all(tenant_root="shop.Customer")
    for name, state in report["checks_run"].items():
        examined = state.get("examined") or {}
        assert "finding_count" not in examined, name
        assert not any(key.startswith("high_severity") for key in examined), name


def test_the_celery_zero_on_a_project_with_tasks_is_a_clean_one(django_project, tmp_path):
    """The shape Misago has: tasks, dispatches, and identifiers passed.

    This is the case the aggregate could not distinguish from "no tasks here".
    """
    from django_chainsaw_mcp.celery_tasks import celery_arguments

    (tmp_path / "tasks.py").write_text(
        "from celery import shared_task\n\n"
        "@shared_task\n"
        "def send_confirmation(order_id):\n"
        "    pass\n"
    )
    (tmp_path / "views.py").write_text(
        "from .tasks import send_confirmation\n\n"
        "def place(order):\n"
        "    send_confirmation.delay(order.pk)\n"
    )

    report = celery_arguments(search_path=str(tmp_path))
    assert report["finding_count"] == 0
    assert report["tasks_found"] == 1
    assert report["dispatches_checked"] >= 1, (
        "the dispatch has to be counted, or the zero is indistinguishable "
        "from a project with no Celery in it"
    )


def test_a_project_with_no_tasks_at_all_says_so(django_project, tmp_path):
    from django_chainsaw_mcp.celery_tasks import celery_arguments

    (tmp_path / "views.py").write_text("def place(order):\n    return order\n")

    report = celery_arguments(search_path=str(tmp_path))
    assert report["finding_count"] == 0
    assert report["tasks_found"] == 0
    assert report["dispatches_checked"] == 0


# --- a project with no DRF in it -------------------------------------------


def test_the_drf_checks_declare_that_they_need_drf():
    """Three checks were marked as needing Django and need DRF.

    healthchecks has no DRF, so `n+1-serializer`, `serializers` and `open`
    each returned early with `rest_framework_installed: False` - and two of
    them did it without their coverage counters, so the merged report showed
    `findings: 0` with an empty `examined`: "this zero cannot be interpreted".
    It could. There is no DRF there, which is what FastAPI and SQLAlchemy
    checks already say for themselves.
    """
    from django_chainsaw_mcp.check import _REQUIRES

    assert _REQUIRES["n+1-serializer"] == "drf"
    assert _REQUIRES["serializers"] == "drf"
    assert _REQUIRES["open"] == "drf"


def test_a_project_without_drf_is_told_so_instead_of_reporting_zero():
    from django_chainsaw_mcp.check import _applicable

    class _Profile:
        is_django = True
        is_async_web = False

        def uses(self, name):
            return name != "drf"

    applies, reason = _applicable("serializers", _Profile())
    assert applies is False
    assert "REST Framework" in reason


def test_a_project_with_drf_still_runs_them(django_project):
    # The demo project has DRF, so these have to keep running there - a
    # requirement that excluded them everywhere would be a silent loss of
    # three checks.
    from django_chainsaw_mcp.check import run_all

    report = run_all(tenant_root="shop.Customer", only=["serializers", "open"])
    assert set(report["checks_run"]) == {"serializers", "open"}
    assert report["checks_not_applicable"] == {}


def test_the_drf_checks_still_wait_for_the_django_boot(django_project):
    """They read the app registry, so a failed boot has to disable them too.

    The boot guard tested `_REQUIRES[name] == "django"`, and a third value
    would have slipped past it - the checks would then run without Django and
    raise instead of reporting that they could not run.
    """
    import django_chainsaw_mcp.check as check_module

    source = Path(check_module.__file__).read_text()
    assert '_NEEDS_BOOT = {"django", "drf"}' in source
    assert 'if _REQUIRES.get(name) == "django":' not in source, (
        "the boot guard still compares against one framework by name"
    )


# --- a settings module that boots and loads nothing -------------------------


def test_a_project_with_no_models_is_named_as_such_not_as_a_bad_tenant_root(
    monkeypatch, django_project
):
    """The error ended in nothing and pointed at the wrong thing.

    readthedocs has a `settings/base.py` that imports cleanly and defines no
    models. `tenancy` raised "Unknown tenant root 'auth.User'. Known models: "
    - a sentence with an empty list at the end of it - and sent the reader
    after a tenant root that was never the problem.
    """
    from django.apps import apps

    from django_chainsaw_mcp.tenancy import find_unscoped_queries

    monkeypatch.setattr(apps, "get_models", lambda *a, **k: [])
    real = apps.get_model

    def nothing(label, *args, **kwargs):
        raise LookupError("no such model")

    monkeypatch.setattr(apps, "get_model", nothing)
    with pytest.raises(ValueError) as raised:
        find_unscoped_queries()
    message = str(raised.value)
    assert "no models at all" in message
    assert "INSTALLED_APPS" in message
    assert "Unknown tenant root" not in message
    del real


def test_the_run_warns_once_when_the_registry_is_empty(monkeypatch, django_project):
    """Every individual number honest, the run as a whole worthless.

    Eighteen of twenty-one checks read the model registry. On a settings
    module that loads nothing they all report zero, correctly and uselessly,
    and nothing in the merged report said the project had not really been
    loaded.
    """
    from django.apps import apps

    from django_chainsaw_mcp.check import _registry_warning

    monkeypatch.setattr(apps, "get_models", lambda *a, **k: [])
    warning = _registry_warning()
    assert warning is not None
    assert "no models" in warning


def test_a_project_of_only_contrib_apps_is_also_warned_about(monkeypatch, django_project):
    from django.apps import apps

    from django_chainsaw_mcp.check import _registry_warning

    class _Config:
        def __init__(self, label):
            self.label = label

    monkeypatch.setattr(apps, "get_models", lambda *a, **k: [object()])
    monkeypatch.setattr(
        apps, "get_app_configs",
        lambda: [_Config("auth"), _Config("contenttypes"), _Config("admin")],
    )
    warning = _registry_warning()
    assert warning is not None
    assert "contrib" in warning


def test_a_real_project_is_not_warned_about(django_project):
    # The demo has its own app, so the warning must stay silent - a banner on
    # every run would be read as decoration and then ignored on the run that
    # needed it.
    from django_chainsaw_mcp.check import _registry_warning

    assert _registry_warning() is None


def test_the_warning_reaches_the_merged_report(django_project):
    from django_chainsaw_mcp.check import run_all

    report = run_all(tenant_root="shop.Customer", only=["money"])
    assert "registry_warning" in report, (
        "a client reading the report has to see this without calling project_info"
    )
    assert report["registry_warning"] is None


# --- migrations reported as pending that shipped years ago ------------------


def _migration_entry(risk="rewrites_table"):
    return {
        "app": "shop",
        "name": "0002_something",
        "worst_risk": risk,
        "operations": [
            {"risk": risk, "detail": "This will rewrite the whole table.",
             "safer": "Do it in two steps."},
        ],
    }


def test_a_migration_finding_says_when_applied_could_not_be_told_from_pending():
    """The largest confident wrong answer this tool had.

    `migration_risk` is about migrations that have not run yet, and it learns
    which those are by reading `django_migrations`. When that read fails it
    falls back to an empty applied set, so the entire history is presented as
    about to run: 255 findings on DefectDojo, 830 on Saleor, and 146 of
    healthchecks' 234 - **62% of everything the tool said about that
    project**, every one of which shipped years ago.

    The flag was in the sub-report and the aggregate dropped it.
    """
    from django_chainsaw_mcp.check import _from_migrations

    findings = _from_migrations({
        "database_reachable": False,
        "migrations": [_migration_entry()],
    })
    assert len(findings) == 1
    assert "may have been applied long ago" in findings[0]["detail"]
    assert "could not be told apart" in findings[0]["detail"]


def test_a_readable_database_leaves_the_finding_alone():
    # The caveat is only true when it is true. On a project whose database
    # answers, these really are pending, and a hedge on every one of them
    # would teach people to skip the sentence.
    from django_chainsaw_mcp.check import _from_migrations

    findings = _from_migrations({
        "database_reachable": True,
        "migrations": [_migration_entry()],
    })
    assert "may have been applied" not in findings[0]["detail"]
    assert findings[0]["detail"].endswith("rewrite the whole table.")


def test_a_safe_migration_is_still_not_a_finding():
    from django_chainsaw_mcp.check import _from_migrations

    assert _from_migrations({
        "database_reachable": False,
        "migrations": [_migration_entry(risk="safe")],
    }) == []


def test_the_check_itself_says_it_in_the_note(django_project, monkeypatch):
    """Fail the read the way a missing database fails it.

    Patching `connections` does not work: the loader reaches it through
    `__getitem__`, and Python looks dunders up on the type rather than the
    instance. The loader is what actually raises, so that is what is broken
    here - and only when handed a connection, because the fallback path
    constructs it with None and has to keep working.
    """
    from django.db.migrations import loader as loader_module

    from django_chainsaw_mcp.migrations import migration_risk

    # The demo project's SQLite database is readable, so the note stays quiet.
    assert "django_migrations is unknown" not in migration_risk()["note"]

    real = loader_module.MigrationLoader

    def refuse(connection, *args, **kwargs):
        if connection is not None:
            raise RuntimeError("could not read django_migrations")
        return real(None, *args, **kwargs)

    monkeypatch.setattr(loader_module, "MigrationLoader", refuse)
    report = migration_risk()
    assert report["database_reachable"] is False
    assert "django_migrations is unknown" in report["note"]
    assert "as though it were about to run" in report["note"]
    # And the whole history is now in there, which is the point of saying so.
    assert report["migration_count"] > 0


# --- a fix whose two halves are the same text -------------------------------


def _tenancy_fix(source: str, tmp_path):
    """Build the advisory fixes for one file of source."""
    from django_chainsaw_mcp.fixes import build_fixes

    (tmp_path / "views.py").write_text(source)
    return build_fixes(  # returns a FixSet, not a list
        {
            "tenancy": {
                "findings": [
                    {
                        "model": "shop.Order",
                        "file": "views.py",
                        "line": 3,
                        "why": "unscoped",
                        "owner_path": "customer",
                        "chain": "objects",
                        "severity": "high",
                    }
                ]
            }
        },
        tmp_path,
    )


def test_a_queryset_with_no_rewritable_entry_point_offers_no_rewrite(tmp_path, django_project):
    """`re.sub` returns the subject unchanged when nothing matches.

    The pattern knows `.all()`, `.filter(`, `.get(` and `.exclude(`. A
    queryset that starts at a custom manager method matches none of them, so
    the "fix" came back identical to the original and was printed as a diff
    whose `-` and `+` lines are the same text.

    On healthchecks that was 13 of 37 advisory suggestions. Nothing was ever
    written - advisory fixes are not what `--write` touches - so the damage
    was to the report rather than to anybody's source, which does not make it
    a smaller lie.
    """
    fixes = _tenancy_fix(
        "def view(request):\n"
        "    from shop.models import Order\n"
        "    orders = Order.objects.for_user(request.user)\n",
        tmp_path,
    )
    tenancy = [f for f in fixes.fixes if f.check == "tenancy"]
    assert tenancy, "the finding itself must survive - the queryset is unscoped"
    for fix in tenancy:
        assert fix.new != fix.old, "a rewrite identical to the original is not a rewrite"
        assert fix.new is None
        assert "no mechanical place" in fix.caution


def test_a_standard_entry_point_still_gets_its_rewrite(tmp_path, django_project):
    # The narrowing must not cost the case the fixer exists for.
    fixes = _tenancy_fix(
        "def view(request):\n"
        "    from shop.models import Order\n"
        "    orders = Order.objects.filter(status='new')\n",
        tmp_path,
    )
    tenancy = [f for f in fixes.fixes if f.check == "tenancy"]
    assert tenancy
    rewritten = [f for f in tenancy if f.new]
    assert rewritten, "a .filter() queryset has somewhere to insert the scope"
    assert "customer=request.user" in rewritten[0].new
    assert rewritten[0].new != rewritten[0].old


def test_an_unknown_root_is_still_an_error_and_names_what_exists(django_project):
    from django_chainsaw_mcp.tenancy import find_unscoped_queries

    with pytest.raises(ValueError) as raised:
        find_unscoped_queries(tenant_root="nope.Nope")
    assert "nope.Nope" in str(raised.value)
    assert "Known models" in str(raised.value)
