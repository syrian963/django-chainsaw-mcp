# What this has been run against

Every performance and false-positive figure in this repository used to come
from a benchmark the tool generates itself and one codebase that cannot be
shared. Both are assertions rather than evidence, so the analysis was run
against public Django projects instead - code written by people who have never
heard of this tool, which is the only honest test of a false-positive rate.

Eighteen projects boot and run. The table is what they produced; the section
after it is what they broke.

## The runs

Of the twenty-one checks, two never apply to a Django project - one is for
FastAPI and one for SQLAlchemy - so nineteen is a full run. Sixteen means the
project has no Django REST Framework and the three DRF checks said so.
Eighteen is readthedocs, below.

| Project | Files | CPU | Checks run | Findings |
| --- | --- | --- | --- | --- |
| channels | 55 | 3.5 s | 19 | 0 |
| defectdojo | 2001 | 1337.7 s | 19 | 2109 |
| django-helpdesk | 152 | 14.1 s | 19 | 175 |
| django-machina | 294 | 14.1 s | 16 | 73 |
| django-oscar | 828 | 24 s | 19 | 260 |
| django-tenants | 207 | 3.7 s | 19 | 1 |
| djangoproject | 259 | 29.9 s | 16 | 148 |
| healthchecks | 653 | 28.4 s | 16 | 234 |
| mezzanine | 196 | 13.5 s | 16 | 113 |
| misago | 2025 | 161 s | 19 | 421 |
| openwisp | 393 | 46.1 s | 19 | 247 |
| paperless-ngx | 351 | 129.7 s | 19 | 741 |
| pretix | 1231 | 183.3 s | 19 | 1174 |
| readthedocs | 963 | 40.4 s | 18 | 197 |
| saleor | 4332 | 968 s | 19 | 3650 |
| wagtail | 1386 | 142 s | 19 | 427 |
| weblate | 1028 | 262.3 s | 19 | 932 |
| wger | 742 | 143.4 s | 19 | 691 |

Reproducing one of these takes three commands - clone, install, point the tool
at the settings module the project's own test suite uses. `docs/performance.md`
carries the exact invocation for three of them.

## What they found in this tool

Twelve defects, none of which the demo project or a single private codebase
had shown. They fall into two groups.

**The tree being walked is not the project being analysed.** Four projects
found four versions of this: a virtualenv inside the project directory made
every installed package look like a project app (Wagtail); a `tests/` package
was not covered by an exemption that knew only `tests.py` (Saleor, 87% of one
check's output); test modules and templates were read against settings they
never run under (django-oscar); and three tutorials with their own `manage.py`
had their URL names checked against the wrong resolver (django-tenants).

**A check that was wrong about correct code.** `@sync_to_async` and
`@database_sync_to_async` exist to take a synchronous body off the event loop,
and the walk followed the call graph straight through the decorator - so on
channels, the reference implementation of doing this properly, 25 of 25
findings were the idiom rather than the defect.

The rest: a ternary crashing a whole check, a default tenant root that no
project with a custom user model has, a serializer built at runtime reported
twice under a name pointing at no file, a stock Django form template called
missing, and `render` read as the shortcut when it was a widget method.

## What they found about reading the results

Three projects with no Django REST Framework returned `findings: 0` from three
DRF-only checks. That is not a clean zero, and it now says so: those checks
declare `drf` the way others declare FastAPI or SQLAlchemy, and a project
without it is told the check does not apply.

readthedocs booted under a settings module that imports cleanly and loads **no
models at all**. Eighteen of twenty-one checks read the model registry, so
every one of them reported nothing, correctly and uselessly, and the run as a
whole was worthless. `check` now says so before anything else:

    WARNING: This project has no models. Django booted, so the settings
    module imports, but nothing in INSTALLED_APPS defines a model.

## What a new check found before it was written

`defeated_prefetches` was built after the projects were already cloned, so it
is the one check whose yield on real code was known before it shipped. Nine of
them — Wagtail, Saleor, django-oscar, Misago, Weblate, NetBox, pretix,
DefectDojo, Read the Docs — contain 696 `prefetch_related` sites between them.
Seven of those prefetches are re-queried by the accessor that reads them, and
all seven were read by hand and confirmed:

| Project | Site |
| --- | --- |
| Wagtail | `wagtail/admin/views/pages/edit.py:207` |
| Saleor | `graphql/meta/permissions.py:120`, `:124`, `:163` |
| Saleor | `graphql/product/mutations/product_variant/product_variant_delete.py:112` |
| pretix | `src/pretix/base/services/invoices.py:277` |
| DefectDojo | `dojo/jira/helper.py:868` |

Saleor's variant one is the clearest: the prefetch is two lines above the
accessor, under a comment reading "Get cached variant with related fields".

The same projects also killed the two checks proposed before it. A
`Meta.ordering` spanning a relation appears 6 times in 562 orderings, which is
too rare to be worth a check. `.exclude(field=value)` on a nullable field
appears 588 times outside tests, which is plenty — but the premise was wrong:
Django emits `NOT (x = v AND x IS NOT NULL)` and keeps the NULL row, so all
588 would have been false positives. Both were dropped before a line was
written.

## Checks that have never fired on real code

Silence is not evidence of correctness, so both are stated with their
coverage rather than left as a zero:

- **`aggregates`** examined 174 aggregate calls and 265 `annotate()` calls
  across twelve projects and reported nothing. The defect it looks for -
  two multi-valued relations joined in one annotate, so the counts are the
  cartesian product - is genuinely rare, and the check does find it on the
  fixtures and on a private codebase.
- **`open`** checked 367 DRF views across eight projects and reported nothing.
  Every one of them sets `DEFAULT_PERMISSION_CLASSES`, which is what a mature
  project does.

Neither number proves the checks are right. They do mean the zeroes are
measurements rather than silence, which is the difference this page exists to
record.

## Projects that could not be analysed

Worth listing, because each is a real limit rather than a gap in effort:

- **django-rest-framework** and other libraries configure Django with
  `settings.configure()` in a conftest. There is no settings module to point
  at; the error now says that instead of naming a variable to set.
- **taiga-back** pins Python 3.10. This tool needs 3.12 and has to be
  installed into the target's own environment, so it cannot analyse a project
  pinned below that.
- **netbox, django-cms, kolibri, InvenTree, BookWyrm, Shuup** each need
  environment setup this harness did not do: a config file copied from an
  example, a package outside `install_requires`, git tags a shallow clone does
  not have. Nothing about the tool; everything about the fifteen minutes each
  would take.
