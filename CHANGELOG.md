# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning is [semantic](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-09-07

The first release, not yet tagged. 21 checks in the aggregate run, 36 MCP
tools with five prompts, 34 CLI subcommands, 329 tests across 17 suites at
86% coverage, and a documentation page per check that states what that
check cannot see.

Everything below this heading is the history of getting there, newest
first. There is no separate Unreleased section: no tag exists yet, so
every change so far belongs to this release.

### Fixed

- **Three DRF-only checks were marked as needing Django,** so a project
  without REST Framework got `findings: 0` from them instead of "does not
  apply". Two of the three returned early without their coverage counters, so
  the merged report showed a zero with an empty `examined` - the one shape the
  aggregate had just been fixed to avoid. `n+1-serializer`, `serializers` and
  `open` now declare `drf` the way others declare FastAPI and SQLAlchemy, and
  the boot guard was widened with them, since they read the app registry too.
- **A settings module that loads nothing produced a confident empty report.**
  readthedocs has a `settings/base.py` that imports cleanly and defines no
  models; eighteen of twenty-one checks read the model registry, so every one
  of them reported nothing - correctly, and uselessly. `check` now returns
  `registry_warning` and prints it before anything else, for a project with no
  models and for one where only Django's own contrib apps are installed.
- **`tenancy` on that project raised a sentence that ended in nothing:**
  `Unknown tenant root 'auth.User'. Known models: `. The tenant root was never
  the problem. It now says the project has no models and where to look.

### Fixed

- **The aggregate threw away the evidence that makes a zero readable.** Every
  individual check reports what it looked at - `tasks_found`,
  `dispatches_checked`, `files_scanned` - and `run_all` reduced all of it to
  `findings: 0`, which reads as clean and can equally mean the check found
  nothing to look at. The server's own instructions promise a client that an
  empty result says which kind it is; that was true of the checks and false of
  the thing that merges them.

  `checks_run[name]["examined"]` now carries those counters, and
  `django-chainsaw check` prints them for every check that ran clean. A check
  that does not report its own coverage is named as such rather than listed as
  clean, because that zero cannot be read either way.

### Fixed

- **A serializer built at runtime was reported twice, under a name nobody can
  open.** `ModelSerializer.__subclasses__()` returns classes made with
  `type()` as well as classes somebody wrote. Misago narrows a field list that
  way, and the result claims `__module__` as wherever the factory ran -
  `rest_framework.serializers` - under a name made of every field it keeps:
  `AuthenticatedUserSerializerIdUsernameSlugEmailJoinedOn...Subset`. Now a
  class the project binds anywhere is reported under the name it is bound as,
  and a class bound nowhere is skipped: no file to send anybody to, and the
  finding against the class it was built from says the same thing. Misago goes
  from 2 exposure findings to 1, and from 6 serializer N+1 findings to 5, with
  no unreadable names left in either.

  The first attempt at this was **wrong in the direction that matters** and is
  worth recording: it asked whether the class was bound in the module its
  `__module__` names, which for Misago's factory is `rest_framework` - so it
  suppressed a genuine finding about the sensitive fields that serializer
  exposes. A check that removes a real finding is worse than one that adds a
  false one, and the second version asks the question that was actually meant.

### Fixed

- **"You did not set the variable" sent people after a file nobody wrote.**
  A library often has no settings module at all: django-rest-framework
  configures Django in `tests/conftest.py` with `settings.configure(...)`,
  which is the normal way to boot the framework for a library's own test run.
  When that pattern is present the error now says so, and says what to do
  instead - point at an application that uses the library. Only the handful of
  files where it is conventionally written are read, because this runs on a
  failure path and must not turn a missing variable into a scan of the tree.

### Fixed

- **`async` was wrong about the standard way to write async Django.**
  `@sync_to_async` and `@database_sync_to_async` exist to take a synchronous
  body off the event loop, and the check followed the call graph straight
  through the decorator into the body it wraps. On channels itself,
  `channels/auth.py` puts it on every function that touches the session:
  **25 findings out of 25 were the idiom rather than the defect.** The walk
  stops at a threaded function now - neither a finding nor a route to one -
  and both the bare and the called form are recognised, dotted or not. That
  project now reports nothing, which is the right answer for the reference
  implementation of doing this properly. The check still reports the identical
  body with the decorator removed; there is a test holding both side by side.
- **`db.connections["default"].settings_dict.get("NAME")` was reported as a
  synchronous query.** ORM receivers are matched anywhere in a chain, which is
  what lets `session.query(User).all()` be recognised through the call in the
  middle; it also made a dictionary of settings look like a connection.
  channels' own tests do it twice.

### Fixed

- **`dangling` read files belonging to other projects in the same
  repository.** A repository holds more than the project being analysed, and a
  name only means something against the settings that will load it. Two
  filters, both using machinery the check already trusts:
  - **Templates outside every loader directory are not read.** `DIRS` plus the
    app template directories, from every engine, is the list Django's own
    loaders walk; a file outside all of them is one this project cannot
    render. django-tenants ships three tutorials under `examples/` with their
    own template directories, and reading `{% url %}` out of them asked this
    resolver about names belonging to another. With no loader directory
    readable at all, everything is scanned - scanning too much is the better
    failure.
  - **A directory with its own `manage.py` is a different project.** Each of
    those tutorials has one, its own `urlpatterns` and its own app called
    `customers` that is not the `customers` in `INSTALLED_APPS`. Nested
    projects are named in the report and their files skipped; the directory
    holding the configured settings is never nested, however deep it sits.

  With the test-module skip, django-tenants goes from **37 findings to 1** -
  and that one is right: the library ships an admin override extending
  `admin/change_form.html`, which those settings genuinely cannot load because
  `django.contrib.admin` is not installed there.

### Fixed

- **A template that ships inside Django was reported as missing.** Form
  widgets render through the engine `FORM_RENDERER` builds, which carries
  `django/forms/templates` on its own search path and is unreachable from
  `django.template.loader.get_template` unless the project also lists
  `django.forms` in `INSTALLED_APPS`. django-oscar includes
  `django/forms/widgets/input.html` from two of its widget templates and both
  came back dangling. Both engines are asked now. A check that calls a stock
  Django template a broken reference is worse than one that says nothing.
- **`wrapper.render("name", "value")` was read as a missing template called
  "value".** Argument 1 of `render` is the template for the shortcut
  `render(request, "x.html")` and the form value for
  `Widget.render(name, value)`; only the plain function call is read now, and
  the same for `render_to_response`, whose `TemplateResponseMixin` form takes
  a context dict.
- **`dangling` scanned test modules, which run under different settings.**
  Not a judgement about how interesting test code is: django-oscar's tests
  reverse `catalogue:parent_detail`, registered by a test-only app under
  `tests/_site` and absent from the sandbox settings the check was pointed at.
  The name resolves perfectly when the tests run, so reporting it answered a
  question asked against the wrong URLconf. Skipped by default, counted in the
  report as `test_files_skipped` with the reason in the note, and
  `--include-tests` / `include_tests=True` reads them anyway. On django-oscar
  the three fixes together take the check from 5 findings to 0, and
  `--include-tests` still shows the 2 that are real under other settings.

### Fixed

- **A ternary in a signal receiver took down the whole check.** `ast.If.body`
  is a list of statements and `ast.IfExp.body` is a single expression, and
  both were read as a list, so `x = a if cond else b` raised
  `TypeError: 'Constant' object is not iterable` out of `bypass` - killing the
  check rather than skipping one receiver. Wagtail has no ternary in a
  receiver and never showed it; Saleor does. `bypass` goes from failed to 183
  findings there.
- **`tenancy` never ran on a project with a custom user model,** which is most
  serious Django projects. The default root was `auth.User`, and a project
  that replaced the user model has no `auth.User` at all - so the default was
  not a weaker answer, it was no answer: the check refused and listed forty
  models it might have meant. The default now resolves through
  `AUTH_USER_MODEL`, which is where Django keeps that answer. Only the
  default: a root somebody typed is never quietly replaced, because a report
  about the wrong root is worse than an error, and the substitution travels
  with the report as `tenant_root_note`. On Saleor the check went from failing
  to 35 scoped models.
- **The tenancy exemption knew `tests.py` and not `tests/`.** The first is
  what `startproject` gives you; the second is what every project past a
  certain size uses, so the documented promise that tests are skipped held for
  small projects and quietly failed for large ones. `tests/`, `testing/`,
  `test_*.py` and `*_test.py` are all exempt now. On Saleor: **1992 findings
  became 263** - 87% of the output had been test code the check never meant to
  read. This one changes results on real projects more than the other two.

### Fixed

- **A virtualenv inside the project directory made every installed package
  look like one of the project's own apps.** `deploy_safety` asked only
  whether an app's path was under the project root, and with the normal
  `.venv/` layout everything installed is. On Wagtail that made 11 of 41 apps
  wrong - Django contrib, DRF, taggit, django-filters - and produced a
  critical verdict on `contenttypes/0002_remove_content_type_name`: a
  `RemoveField` for a field called `name`, matched against 25 unrelated places
  that use that word. The function's own docstring had said since it was
  written that third-party field names are too generic to scan for; the guard
  just did not hold. `project.is_vendored` is now the one place that answers
  "under the root, but not ours", and the regression test was shown red before
  green. On Wagtail: 14 blocking findings became 13, and `contenttypes` moved
  to `skipped_third_party_apps` where it belongs.

### Fixed

- **The tests badge read 320 while the suite collected 329, and nothing
  checked it.** Every other number on that row is derived from the code by
  `docs_check.sh`; this one and the coverage figure were typed. Both are gated
  now - the tests badge against what pytest collects, the coverage badge
  inside `coverage_check.sh`, which is the only place that knows the measured
  value. Both proven red before green. The first version of the coverage gate
  was itself broken - `badge/coverage-86%25` holds two numbers and it read
  both - which passed the drift test for the wrong reason: a gate that is
  always red looks exactly like a gate that works.
- **The 0.1.0 notes described a release that did not match the repository.**
  They claimed 33 CLI subcommands, 199 tests and 16 suites; the real figures
  are 34, 329 and 17. No tag exists, so nothing had shipped under those
  numbers, and the seventeen entries sitting in an Unreleased section above
  them belonged to this release too - a reader of the published notes would
  have missed them. Folded in, numbers corrected.

### Fixed

- **`pytest tests/test_server.py` on its own reported 36 failures** that said
  "Missing environment variable" and meant "you ran me with the rest of the
  suite last time". The module read the demo project out of environment
  variables another test file happened to set first. It asks for the project
  now.

### Changed

- **Django 4.2 through 6.1 are tested, not assumed.** The classifiers claimed
  5.0 and 6.0; 4.2 LTS is what a lot of projects actually run, so CI now
  installs a target project on `django~=4.2.0` and runs the analysis against
  it. Verified by hand first: 188 findings on the demo project on 4.2.30, the
  same as on 5.2 and 6.1.
- Python 3.14 is in the classifiers and the CI matrix. It already worked; the
  metadata just did not say so.

- **`scopes()` walks the tree once instead of four times.** It walked for
  classes, again for functions, and once per function body for its locals. A
  profile put `ast.walk` at 3.6 million nodes yielded for a tree with 1.18
  million, which is where the time was. One walk now collects classes,
  functions and assignments, and each function takes a `bisect` slice of the
  assignments in its line range rather than filtering the whole list, which was
  quadratic in a file with many of both.
- **`benchmark.sh` takes the best of three runs and prints the worst next to
  it.** One wall-clock run is not a measurement: the same code read 172 s and
  326 s on a busy machine during this work, and a real regression was invisible
  in those numbers. A spread wider than about a third now says so on the face
  of the output, and `docs/performance.md` says to read the spread before the
  number.

- **A full `check` is 26.6 s instead of 35.6 s on the benchmark project.**
  Twenty checks each read and parsed the same files. One pass over 2144 files
  is 5.4 seconds and a full `ast.walk` over the result is 1.5, so the parsing
  was the expensive half; `read_source` and `parse_file` in `project` now cache
  by path, mtime and size, and `check` enables that for its run.
- **The cache is off by default, because the first version of it was a
  regression.** Caching unconditionally cost 315 MB for a single subcommand
  that reads each file once, and the benchmark said what that costs: `check`
  went to 78 s and every single-pass command got two to three times slower.
  It is opt-in now, and only `check` opts in.
- **The shared queryset resolver no longer walks each scope separately.** It
  mapped every node id to its enclosing scope, which meant a node inside a
  nested function was visited once per enclosing scope. On a real codebase
  that had taken `aggregates` from 22 s to 66 s and `choices` from 20 s to 61
  s. A line-span index gives the same answer for one walk per function body.
- The scope lookup is only performed for a chain that could be changed by it -
  one bottoming out at a bare name or at `self`. It was being paid for every
  call node in the project, and almost none of them is a queryset.
- `callgraph` uses the fingerprint helper in `project` instead of its own copy.

### Fixed

- **`check --since` was documented and did not exist.** `docs/quickstart.md`
  advertised `django-chainsaw check --since main --fail-on high` in a CI
  snippet, and `check` had no `--since`: anybody who copied that line got an
  argparse error. The flag exists now, because the feature is coherent and the
  documentation promised it.
- `docs_check.sh` parses **every** `django-chainsaw ...` line in the README and
  the docs pages, 48 of them, so a documented command that does not work fails
  the build. It verified the command *names* before, which is why this one got
  through. The first version of the extractor missed the failing line because
  it sits in a GitHub Actions step under `run:` rather than at the start of a
  line; that is fixed and the gate was demonstrated failing on the original
  bug before being kept.
- An aggregate finding now carries `file` and `line` separately when its
  location has them. `--since` compares paths against a git diff and cannot do
  that with `app/x.py:12`, and every consumer that wanted the file was parsing
  the string itself.

- **`django-chainsaw choices` raised `KeyError` on every run that found
  something.** When the untyped-comparison route was removed the printer kept
  a loop over the report key that went with it. No test entered that branch:
  the shell suites do not run `choices`, and the pytest tests call the
  analysis rather than the command. The in-process CLI test found it on its
  first run, which is the whole argument for having one.

- **Eighteen checks skipped for one missing environment variable printed the
  same 300-character reason eighteen times.** That is the first thing somebody
  sees who forgot `DJANGO_CHAINSAW_SETTINGS_MODULE`, and it buried the one
  sentence telling them what to do. Skipped checks are grouped by reason now,
  with the reason wrapped once.

- **A function with no queryset locals was indistinguishable from one with no
  scope at all**, so the module's locals applied inside it - which is how a
  `qs` in one function came to resolve to another function's model. Caught by
  the regression test for exactly that case.
- `docs/performance.md` quoted 338 seconds for a real project from a wall-clock
  measurement on a shared machine, where the same code ranged from 172 to 326
  seconds across runs. It now quotes CPU time and says plainly that the figure
  is an order of magnitude rather than a measurement.


### Release preparation

- `license`, `classifiers`, `keywords` and `[project.urls]` in
  `pyproject.toml`. Without them a published package has no license shown and
  no links anywhere.
- **ruff, and a lint gate in CI.** The first run found 122 findings, and three
  of them were real:
  - `endpoint_cost` referenced `views_seen` and `endpoints` in its early-return
    branch, where neither exists yet. That branch is the "no DRF views are
    importable" path, so the message written for the case where the tool cannot
    see anything raised `NameError` instead of explaining itself.
  - The sentence about views that declare no `serializer_class` was in that
    same dead branch and therefore missing from the report that needed it. On a
    real project 152 of the views declare none, so a near-empty result was
    reading as a clean one. It is in the main note now, with the count.
  - Two `discovered = load_serializer_modules(...)` results were assigned and
    never used, so a serializer module that failed to import was silently
    dropped from the analysis. Both are reported now.
- A sentence in `endpoint_cost`'s own output had been broken by an earlier edit
  and read as two half-clauses. Repaired.
- **A trust section at the top of the README.** The tool imports the target
  project, which means import-time side effects in that project run. That
  belongs above the fold, not in a note halfway down `usage.md`. It also says
  what the tool does not do: never runs a view or a task, reads the database
  only to ask which migrations are applied, writes files only when asked, and
  makes no network calls.
- `docs/performance.md` carries the cost on a real codebase, not only on the
  generated one: 338 s for a full `check` on 2120 files and 424 models, against
  35 s on the synthetic project of similar size. The generated project has
  shallow call graphs and the checks that walk one pay for that difference.

### Added

- **`docs/tested-against.md`: eighteen public Django projects, with what they
  found.** Every figure in this repository used to come from a generated
  benchmark and one codebase that cannot be shared. Eighteen projects now boot
  and run - defectdojo at 2001 files, Saleor at 4332, channels at 55 - and the
  page lists the runs, the twelve defects they exposed in this tool, the two
  checks that have never fired on real code with the coverage that makes those
  zeroes readable, and the projects that could not be analysed with the reason
  for each. `aggregates` examined 174 aggregate calls across twelve projects
  and found nothing; `open` checked 367 DRF views and found nothing. Neither
  proves a check is right, and both are now measurements instead of silence.

### Added

- **An answer to why `celery` had never reported anything.** Six projects, no
  findings, which looked like a check that did not work. It works: on Misago -
  2025 files with Celery in them - it reads 12 tasks and 41 dispatches and
  every one passes an identifier rather than a model instance. That is a clean
  zero and the correct answer. The investigation found no defect in the check
  and one in the aggregate, above.

### Added

- **A sixth public project, and the first real DRF application.** Misago -
  2025 files, 53 models, DRF 3.14, Celery - is where `serializers` and
  `n+1-serializer` fired on real code for the first time; six projects in,
  those two had never produced a finding outside the demo. All 21 checks
  green. `docs/performance.md` has six rows now and a correction to go with
  them: the per-file cost is **not** monotone in file count. Misago has half
  again as many files as Wagtail and is cheaper per file; Wagtail has five
  times Misago's models and twice Saleor's while costing a seventh of Saleor's
  time. Neither file count nor model count is the driver on its own, and the
  page now says a projection from file count alone will be wrong instead of
  implying a curve.

### Added

- **A fifth public project, on the one axis nothing had exercised.** Four
  projects had produced zero `async` findings between them, so the check was
  effectively untested against real code. channels is ASGI end to end - 55
  files, 7 models, all 21 checks green - and it found the two defects above
  immediately. `docs/performance.md` has five rows now, and a correction that
  came with the fifth: the two smallest projects are mostly Django boot, so
  their per-file cost measures the boot rather than the analysis, and channels
  looks dearer per file than a project four times its size. The per-file
  column means something from a few hundred files up.

### Added

- **A fourth public project, chosen for the axis the other three missed.**
  django-tenants is schema-based multi-tenancy: several URLconfs picked per
  request, several settings modules in one repository. 207 files, 3.7 s, all
  21 checks green. `docs/performance.md` now has four rows and the scaling is
  starker than two points suggested - 21x the files for 260x the time, from
  18 ms per file to 223 ms. Model count is visibly not the driver: Wagtail has
  twice Saleor's models and a seventh of the time. Still unmeasured causes,
  still not claimed.

### Added

- **A third public project, and a scaling table instead of a claim.**
  django-oscar - 828 files, 92 models, server-rendered where the other two are
  headless, so `n+1-template` fired for the first time on real code. All 21
  checks green, 260 findings, 24 s of CPU. `docs/performance.md` now carries
  all three runs side by side: 828 files at 29 ms each, 1386 at 102 ms, 4332
  at 223 ms. Five times the files is forty times the time, and model count is
  visibly not the driver - Wagtail has twice Saleor's models and a seventh of
  the time. The causes are not measured and so are not claimed; the table is.

### Added

- **A second public project in the reproducible-run docs.** One stranger's
  codebase is not a sample: Wagtail found the vendored-app bug and none of the
  three above. Saleor - 4332 files, 122 models, GraphQL rather than DRF, a
  custom user model - found all three on its first run.
  `docs/performance.md` carries the commands and the honest scaling note:
  1386 files cost 142 s of CPU and 4332 cost 968, so 3.1x the files is 6.2x
  the time and the cost is **not** linear in file count. Two plausible causes,
  neither measured, so neither claimed.

### Added

- **A repeatable run against a public project.** Every performance and
  false-positive claim here came either from a generated benchmark or from a
  codebase that cannot be shared, which makes both assertions rather than
  evidence. `docs/performance.md` now carries the commands to run the tool
  against Wagtail - 1386 files, 264 models, 142 s of user CPU, 0 checks
  failed, 427 findings - so the numbers can be reproduced by anyone. It paid
  for itself on the first run by exposing the vendored-app bug above.

### Added

- **`check` reports progress, and names the check it is on.** It runs 21
  analyses and is the only call here slow enough to matter - a minute or more
  on a large project - and a bare spinner for a minute is indistinguishable
  from a hung server. The analysis moved to a worker thread to make this
  possible: awaited on the event loop it held the loop for the whole run, so
  no notification could be written to the transport and the client could not
  cancel. Verified over stdio with a real progress token: 22 notifications,
  `deploy-safety (1 of 21)` through `merging`, ending on the total.
- **`check` declares its output shape, and the SDK enforces it.** One schema,
  not 36: its envelope is already a contract read by the CLI printer, the
  SARIF export, the severity gate, the baselines and the HTML report, and the
  finding inside it is built in one place. A key renamed in `check.py` now
  fails on the next run rather than quietly vanishing from whatever was
  reading it - proven by feeding the tool a finding with a severity outside
  the vocabulary and watching the call be rejected. Nothing is required, so
  the `{"ok": false, "error": ...}` answer a bad settings module produces is
  still a valid return and not a validation failure; extra keys are allowed,
  so a check that starts carrying more evidence is not a protocol error.

- **The MCP server now uses the protocol rather than a corner of it.** It had
  36 tools and one resource, and nothing else: no instructions, no
  annotations, no prompts, no completion. All four are the difference between
  a server that answers questions and one somebody can work with.
- **Every tool declares whether it reads or writes.** 35 carry `readOnlyHint`,
  `destructiveHint: false` and `idempotentHint: true`, so a client can stop
  asking permission for each of 36 calls. The exception is
  `api_contract_check` with `update=True`, which writes the snapshot and says
  so.
- **Server instructions.** An assistant handed 36 tools with no ordering picks
  by name, and the names do not say which question each answers. The
  instructions give the ordering, say to start with `project_info` when
  anything looks empty, and state the thing this server fails at if nobody
  says it: an empty result means "could not look" as often as "nothing to
  find", and the reports say which.
- **Five prompts**, because a tool answers one question and knowing which
  three to ask in which order is a workflow: `before_deploy`,
  `why_is_this_slow`, `what_breaks_if_i_delete`, `triage` and
  `review_this_branch`. Each names the tools to call and ends with what to
  report, including what the answer does not mean.
- **Model arguments complete** from the project's own registry. A real project
  has hundreds of models, typing one from memory is how you get a
  `LookupError`, and a wrong label looks exactly like a model with nothing
  attached to it.
- All of it is checked twice: in process, and over the real stdio transport in
  `client_test.py`, because declaring something in a module and it arriving on
  the wire are different questions. `docs_check.sh` also verifies the prompt
  count on the badge and that every prompt names a tool that exists - a
  workflow pointing at a tool that does not is worse than no workflow.
- `server.py` coverage 97%, total 86%, 320 tests.

- Tests for the `--json`, `--since` and `--baseline` paths of the CLI, which
  are what a pipeline actually runs. `cli.py` 63% to 77%, total coverage 86%,
  314 tests.

- **Tests for the two features that decide what CI is allowed to ignore.**
  `--since main` and `--baseline` both answer their question by *removing*
  findings, which makes them the only parts of this tool where a bug hides a
  real defect instead of inventing a fake one. Both were driven only through
  the CLI, so `gitdiff` measured 18% and `baseline` 38%. The cases a
  subprocess test cannot express are now covered directly: a finding path and
  a git path that do not share a prefix, a fingerprint that has to survive
  code moving down a file while still distinguishing two findings in it, an
  empty baseline file meaning "no baseline" rather than "corrupt", and a
  missing ref raising instead of reporting an empty diff.
- **Tests for the contract classification**, which is the product of that
  check rather than a detail: a new *required* field is an addition that
  breaks every existing writer, a field becoming nullable is risky rather than
  breaking, and a serializer that stopped resolving is unreadable rather than
  removed.
- Every CLI subcommand is now exercised in process. Coverage 83% to **86%**,
  with no module below 70%; `gitdiff` 18% to 86%, `baseline` 38% to 92%,
  `api_contract` 56% to 83%. 304 tests.

- **`SECURITY.md`**, and it leads with the thing that matters: this tool
  imports the project you point it at, so analysing an unfamiliar repository
  is the same act as running it. It also states what the tool does not do -
  never runs a view or a task, touches the database only to ask which
  migrations are applied, writes only when asked, makes no network calls -
  with each claim checked against the source rather than assumed, and names
  injection into the HTML report as a real vulnerability class rather than a
  cosmetic one.
- **Issue templates that ask the questions that actually resolve a report.**
  The bug form asks for the Django version, the Python version and whether
  `project-info` succeeds, because most "it found nothing" reports are a
  settings module that could not be imported. The new-check form asks the four
  questions every check here had to answer, including the one that decides it:
  what it must stay silent on.
- A pull request template that asks for the evidence rather than a promise: the
  test that fails without the fix, or the benchmark number with its spread.
- **A release workflow.** Publishing is driven by a tag, and the job refuses
  unless the tag matches the version in `pyproject.toml` and the changelog has
  a section for it. Trusted publishing, so there is no API token to leak.
- `CODE_OF_CONDUCT.md`, `.pre-commit-config.yaml` and `dependabot.yml`.
- **A screenshot of the HTML report** in the README, taken from the demo
  project in this repository.
- `docs_check.sh` also verifies that every local link in the README resolves.
  A broken image is the first thing a visitor sees and the easiest thing to
  break by moving a file. Demonstrated failing before it was kept.

- **`report`: every finding as one self-contained HTML file.** A thousand
  findings in a terminal is a scroll; the same thousand with a severity filter,
  a search box and a grouping toggle is a working session. Group by
  **endpoint** - which pages carry this, and through what call path - or by
  check, file or severity, with search and the severity toggles applying to
  whichever view is showing.
- Deliberately not a server, not networked and not a build step. The CSS, the
  script and the data are in the file, so it opens from a CI artifact or an
  email attachment with nothing installed. A test asserts there is no `<link>`,
  no `<img>`, no `src="http` and no `@import` in the output, because the tool
  promises nothing leaves the machine and a report that fetches a font would
  break that promise on somebody else's behalf.
- The caveats travel with the findings: checks that could not run are listed as
  unverified rather than omitted, checks that do not apply are grouped by
  reason, and the endpoint view carries how many findings no entry point
  reaches and how many of those are unresolved rather than unreached.

- **In-process tests for both front ends, and a coverage floor.** The script
  suites drive the CLI and the MCP server as subprocesses, which is the right
  way to test the exit codes CI will see and the wrong way to find out whether
  a printer reaches for a key that exists. Coverage could not follow those
  subprocesses either: `cli.py` measured 0% across 1479 statements while being
  exercised on every run. `tests/test_cli.py` calls 20 subcommands through
  `main(argv)` and `tests/test_server.py` calls all 36 MCP tools directly.
  Coverage went from 64% to 82%, `server.py` from 0% to 97%, and
  `coverage_check.sh` holds a floor of 78%.
- **Badges, with the numbers checked.** A badge goes stale in the same silence
  as prose, so `docs_check.sh` derives the checks, MCP tools, CLI commands and
  Python versions from the code and compares them against what the README
  shows. Demonstrated failing on a wrong number before it was kept.

- **`multiplied_aggregates`: counts and sums a join has multiplied.**
  `annotate(lines=Count("lines"), shipments=Count("shipments"))` joins two
  multi-valued relations, so an order with 3 lines and 2 shipments produces 6
  rows and both counts come back as 6. Nothing raises, and the only way to
  notice is to already know the answer - which is why it lives on dashboards.
  Django's own documentation warns about it and no linter checks it, because
  none of them resolve a field path against the model registry. `Count(
  distinct=True)` is treated as correct; `Sum` has no equivalent and needs a
  Subquery, so a query is still reported when every Count in it is distinct.
  A filter joining a second multi-valued relation is the same multiplication
  and is reported in its own bucket, because the fix is an `Exists()` rather
  than a `distinct`.
- **Only `Count` and `Sum` are reported.** A join repeats rows uniformly within
  each group, so `Min` and `Max` return the value they would anyway and `Avg`
  divides a multiplied total by a multiplied count. The first version included
  them and reported a correct `Min("items__begin")` on a real project as a
  defect.
- **A queryset held in a local variable is resolved**, as is a custom manager.
  On a real project only 27 of 247 `annotate()` calls started from a model
  directly; a check that understood only that spelling saw 11% of the code and
  reported "nothing found" about the rest. `annotate_calls_in_source` and
  `annotate_calls_seen` are in the report, because a clean result means nothing
  without the denominator.
- New CLI subcommand `aggregates`, MCP tool `multiplied_aggregates`, and
  `aggregates` in the aggregate check.

- **`dangling_references` also reads signal senders and Celery task names.**
  These are the quiet half of the same family. A string sender - `@receiver(
  post_save, sender="shop.Ordr")` - is resolved lazily through the app
  registry, so a misspelled label connects nothing: no exception, no system
  check message, no receiver, and the behaviour simply never happens. A beat
  entry naming a task that no longer exists is worse because it looks alive:
  beat keeps scheduling it on time, the worker rejects each message as
  unregistered, and the job stops happening on a schedule nobody watches.
  `send_task()` is the same failure from the sending end. Task names are
  derived from the source - Celery's default is `module.function`, an explicit
  `name=` overrides it - so a project whose app is only built inside the worker
  is still checked; the app's own registry is added when it is importable.

- **`dangling_references`: names the framework has to resolve, checked ahead of
  the request.** `redirect("order-detial")`, `render(request,
  "shop/order_detial.html")` and `{% url 'shop:order-detial' %}` are resolved
  while serving a request and checked by nothing before then: they import
  cleanly, satisfy a type checker, pass every test that does not walk that
  branch, and raise `NoReverseMatch` or `TemplateDoesNotExist` for the first
  person who does - on the error page, the rare redirect, the export somebody
  runs at month end. URL names come from every URLconf in the project, walked
  through each `include()` so namespaces are real; template names go through
  `get_template()`, so the project's own loaders decide. A template that exists
  and will not compile is not reported, because the question is whether it is
  there.
- **`by_name` groups the output.** One missing name used in thirty-five places
  is one problem, and thirty-five lines hide that.
- New CLI subcommand `dangling`, MCP tool `dangling_references`, and `dangling`
  in the aggregate check.

- **`choice_typos`: literals a field's `choices` will never match.**
  `Order.objects.filter(status="cancelled")` where the choices spell it with
  one L is valid Python, valid SQL, returns zero rows, raises nothing and is
  wrong forever - and an empty result reads exactly like "no orders are
  cancelled". The write side is worse: `choices` is enforced by `full_clean()`,
  which neither a queryset nor `create()` ever calls, so the value goes into
  the column and the application holds a state it does not believe exists.
  Nothing else finds this - django-stubs types the field as `str` rather than a
  Literal union of its choices, so mypy is satisfied, and the `DJ` rules never
  read the model registry.
- New CLI subcommand `choices`, MCP tool `choice_typos`, and `choices` in the
  aggregate check.

- **A nested serializer is attributed through its parent.** It is a field in a
  class body - inside no function at all - so neither the backward walk nor the
  method-body route could see it. The chain nested -> parent -> the view
  serving the parent is followed to any depth, and the reported path carries
  the nesting instead of stopping at the parent. On a large real project this
  took serializer findings from 79 unattributed to 47.

- **`request_impact` reads the URLconf.** A plain Django function view carries
  no decorator and belongs to no view class, so nothing in its source says it
  serves HTTP and every defect on its path was reported as reached by nothing.
  The URLconf also supplies the URL, so an entry point is now labelled
  `/reports/daily/` rather than with a dotted Python path. A URL that names a
  class does **not** promote every method on it: `_internal` on a routed
  ViewSet would stop the backward walk and hide the action that serves the
  request.
- **Serializers built inside a method body are attributed.** DRF's declarative
  `serializer_class` is one route and not the common one on a large codebase -
  a plain `APIView` builds the serializer in the body, where there is no
  attribute to read. The functions naming the class are found instead, with the
  name resolved through the module's own imports so that `Foo` in two modules
  stays two classes. On a large real project this took serializer findings from
  161 unattributed to 79, and the endpoints carrying a finding from 153 to 224.

- **Serializer findings are attributed through the view that declares them.**
  An N+1 in a serializer is a field on a class, with no enclosing function for
  the backward walk to start from, so every one of them landed in
  `unattributed` - by a wide margin the largest group there. DRF records which
  view declares which serializer, so the class holding the line is looked up
  there instead. A ViewSet that declares `serializer_class` and overrides
  nothing is named as the entry point itself, since there is no method to point
  at. A serializer no view declares stays unattributed, because it is.

- **`request_impact`: which findings does a request actually hit?** Every other
  check answers "where is this defect". On a large codebase that is a few
  hundred correct entries sorted by severity, and nobody knows where to start,
  because severity ranks the defect and not the risk - a medium on a hot login
  path outranks a critical in a helper nobody has called since 2021. This maps
  each finding to the function containing it and walks the call graph
  **backwards** to the HTTP routes, Celery tasks, signal receivers and
  management commands that reach it, each with the path taken. Backwards rather
  than forwards because the walk is then bounded by the findings instead of by
  the project, and the path falls out for free.
- **Framework hooks count as entry points.** Nothing in a project calls
  `get_queryset`; Django calls it on every request. Without that, a finding
  inside one is reached by no caller and reads as "probably fine". The list is
  closed rather than "every method on a view class", because making a private
  helper an entry point would stop the walk at the helper and hide the action
  that serves the request.
- `unattributed` findings are reported in their own bucket, named for what it
  is: not reached by any entry point this can see. Not "dead code", and not
  "safe" - a plain Django function view carries no decorator and belongs to no
  view class.
- New CLI subcommand `impact` and MCP tool `request_impact`.

- **`queries_in_loops` follows calls out of the loop.** A loop whose body
  contains no ORM call at all can still run one query per row, because the query
  is in a function the loop calls. In a codebase organised into services that is
  the normal shape, and the check could not see it. Plain calls made inside a
  loop are now resolved through the project call graph and reported when a query
  is reachable within `--max-depth` hops (three by default). The finding carries
  the path it took, ending at the query itself rather than at the function
  holding it. A call that reaches nothing stays silent.
- **Comprehensions count as loops.** `[enrich(o) for o in orders]` is the most
  idiomatic form of this bug and was the one shape the first version missed.
- `loops` gained `--no-follow-calls` and `--max-depth`.

### Changed

- **One place decides which model a queryset is about.** `choices`, `indexes`
  and `aggregates` each carried their own resolver that accepted only
  `Model.objects`; on a real codebase that spelling is 2655 of 7056
  queryset-shaped calls. The shared `querysets` module also understands a
  custom manager, a local variable (per function, so two functions using `qs`
  for two models stay two models, and a name assigned from two models is
  dropped rather than guessed), `self.model`, and `self.get_queryset()` when
  the class declares a model and has not overridden the method. That takes the
  same project to 2839 - a 7% gain, not a transformation, because most of the
  remainder (`self.instance.items`, `request.user.orders`) needs type
  inference and is not guessed at.
- The `self.get_queryset()` rule fires **zero** times on that codebase: 566
  classes declare a model and inherit the method, and almost nothing calls it
  there. Kept because it is correct, documented as earning nothing there.

- **"Nothing calls this" is now two different answers.** A method invoked as
  `target.recalculate()` cannot be resolved without type inference, and saying
  nothing calls it is a confident wrong answer about a line that visibly does.
  A finding whose holder is named by an unresolved call site now says so, with
  the number of call sites and how many functions share the name, and is
  counted in `unattributed_because_the_receiver_is_unknown`. On a large real
  project that is 201 of the 363 unattributed findings - a majority that was
  being reported as unreached.
- Resolving those by a unique method name was measured and rejected: it would
  have resolved 8% of unresolved calls and attributed 25 more findings, and the
  names that actually block attribution (`copy`, `update`) are shared by
  several classes, so the rule would not have helped where it mattered.

- **The call graph is built once per tree instead of once per check.** Seven
  analyses build one, and `check` runs all of them: on a project of 2100 files
  that was seven full parses of the same unchanged source. A stat-only
  fingerprint - file count, newest mtime, total size - decides whether the
  cached graph still matches, `refresh=True` forces a rebuild and
  `clear_cache()` empties it.

- **`check` runs what applies to the project in front of it.** The aggregate
  command required Django, so everything built for FastAPI was unreachable
  through the one entry point people actually use. Each check now declares what
  it needs - Django, FastAPI, SQLAlchemy, or nothing - the project is profiled
  first, and Django is only booted if a check that needs it was selected. On a
  FastAPI project that means three checks run and thirteen are reported as not
  applicable **with the reason**, rather than one boot error; on a Django
  project every check still runs and nothing is skipped.

### Fixed

- **The README stated the wrong number of checks, and gave a false reason for
  the name.** It said thirteen checks need the app registry when it had been
  eighteen for some time, and that the name stays because renaming the
  repository would break every link to it - GitHub permanently redirects a
  renamed repository, so that was simply untrue. The name stays because it is
  accurate: 18 of the 21 checks need Django.
- **`docs_check.sh` now reads those numbers out of the code and compares them.**
  Prose that states a number goes stale in silence, which is the worst way for
  it to go; the gate was demonstrated failing on a wrong number before it was
  kept.
- **`project.py` listed decimal precision and read-modify-save as needing
  nothing but Python.** Both walk `apps.get_models()`. The aspiration was
  reasonable and the sentence was false, which is the worse of the two.
- **`docs/performance.md` was measured against ten checks and there are
  twenty-one.** A full `check` on 2822 files is 35.6 s, not the 5.4 s the page
  claimed, and the page now carries the per-check breakdown. It also said the
  cache does nothing for the CLI, which stopped being true when the call graph
  gained one: that is worth about 7 s inside a single command.

- **The same nested field appeared as several identical findings.** It is
  reported once per root serializer that reaches it - deliberately, because the
  prefetch belongs on each root's queryset and those are different fixes - but
  the aggregate dropped `root_serializer`, so three separate findings read as
  one line printed three times.

- **An empty serializer-to-view map could not be told from one that failed to
  build.** One means "nothing to attribute" and the other means "could not
  look"; the exception was being swallowed. The reason is now reported.

- **A serializer finding pointed at a bare filename.** `serializers.py:16` is
  ambiguous the moment a project has two of them, which every project of any
  size does, and no editor or code-scanning UI can open it. Locations are now
  relative to the project root.

- **Decorators written with arguments were recorded as nothing.** `_dotted`
  returns "" for a `Call` node, so the call graph saw `@shared_task` but not
  `@shared_task(bind=True)` - and any consumer asking what a function *is* got
  a blank.
- **`queries_in_loops` findings from the new `through_a_call` bucket never
  reached the aggregate.** They were absent from `check`, from the gate, from
  SARIF and from baselines - the check found them and nothing downstream saw
  them.

- **The iterable of a loop was treated as being inside it.**
  `for o in Order.objects.all():` evaluates the queryset once, before the first
  iteration, so reporting it flagged the one line in the whole pattern that is
  fine.

- **An atomic file write dropped the executable bit.** Writing to a temp file
  and renaming is the right way to avoid destroying a file on a failed write,
  but `os.replace` does not carry the original mode over, so `exitcheck.sh`
  silently became non-executable and the suite reported "Permission denied"
  inside a larger run. The mode is preserved now, and `portability_check.sh`
  fails if any shell script loses it - a script that is not executable is a
  suite that quietly does not run.
- **An exit-code test assumed one check was the only source of critical
  findings**, for the third time: the skip list grew each time a new check
  produced one. It now selects a single check with no criticals and a single
  check that has one, which tests the same gate with no exclusivity
  assumption.

### Fixed

- **The async check re-parsed the whole file once per function.** Looking up
  one definition at a time meant 11300 parses of two thousand files on a real
  project: 60 seconds to check a single async function. Each file is parsed
  once now and its definitions indexed, which took that to 12 seconds, most of
  it the call graph doing real work. The check also returns immediately when a
  project defines no async functions at all, rather than building a graph to
  confirm an answer that was already known.
- **`celery_arguments` counted dispatches inside nested functions twice.**
  `_CallScan` descended into a nested `def` which was then scanned again as its
  own scope, so a real project reported 11 dispatches where the source has 10.
  It is 9 of 10 now, the tenth being a task this project does not define.
- **`amplification` reported an empty half as a clean result.** Zero findings
  is only good news when both halves had something to work with; on a project
  whose views declare no `serializer_class` the expensive half has no data at
  all. That now comes back under `sides_with_no_data` with an `answerable`
  flag, because "could not look" and "nothing to find" are different answers
  and this is the exact failure the check was written to catch in other
  people's tools.

### Added

- **`queries_in_loops`**: database work written inside a loop, separated into
  the three shapes that share one appearance and need three different fixes. A
  query that uses the loop variable runs once per row and needs a bulk fetch or
  a prefetch. A query that ignores it asks the same question N times for the
  same answer and simply belongs above the loop - nothing to trade off. A write
  is N round trips, and the bulk fix skips signals, which `bypassed_effects` is
  the check for. A nested loop raises the first case to critical.

  This closes a real gap: the template and serializer checks find the N+1 a
  framework causes, and on a large real project 152 of the DRF views declare no
  `serializer_class` at all, so every cost check was silent about all of them.

  `django-check` does static N+1 for relation access in a loop and is the
  closest existing tool; `nplusone` and the debug toolbar find it at runtime.
  The separation is what is added - "there is a query in this loop" and "this
  query belongs three lines higher" are different findings with different
  fixes.

  Silent on a loop over a literal list, on `dict.get()` and `list.count()`, and
  in tests. A chained lookup counts once: `Invoice.objects.filter(...).first()`
  is two qualifying calls and one query, and reporting both doubled every
  chained lookup in the first version.

- **`celery_arguments`**: model instances handed to Celery tasks, and
  dispatches whose argument count cannot match the task. The worker does not
  receive the instance - it receives whatever the serialiser made of it,
  rehydrated later on another machine, so the row may have changed in between,
  the whole object crosses the broker, and under the JSON serialiser, the
  default since Celery 4, it may not encode at all. Passing the primary key is
  the documented fix and nothing checks it: `flake8-pie`'s Celery lints cover
  task names, crontab arguments and expirations, none of which look at what is
  passed.

  Arity is reported too. Celery's `strict_typing` catches that at call time,
  which for a nightly job or an error branch means in production, months
  later. A `bind=True` task takes `self` from Celery and that is accounted for.

  A dispatch is only checked when the name resolves to a task this project
  defines, through the calling file's own imports. Matching on the bare name
  was the first version and this repository's own fixtures broke it: a plain
  object called `send_confirmation` in one module and a task of the same name
  in another produced eighteen findings that were not real.

- **`docs_check.sh`**: the documentation is checked the way the code is. It
  fails if a tool has no line in the README, a subcommand has no section in the
  CLI reference, an aggregate check name cannot be looked up anywhere, or a
  page under `docs/` is not linked from the index. It found four separate
  pieces of drift on its first run: nine tools missing from the reference, two
  subcommands with no section, one check name nobody could look up, and two
  orphaned pages.

### Changed

- **The README no longer claims to be a Django tool.** It described a scope the
  code had outgrown for several iterations, which is worse than saying nothing:
  somebody with a FastAPI project reads the first paragraph and leaves. The
  tools are now split into the ones that need the Django app registry and the
  ones that need only Python, `check` is shown profiling a FastAPI project, and
  the name is addressed rather than left to look like an oversight - it stays
  because renaming the repository would break every link to it.

- **`amplification`**: endpoints that are both reachable without credentials
  and expensive to answer. Two facts, each of which is somebody else's finding
  and neither of which is wrong alone - `GET /orders` has no authentication,
  which is right on a public catalogue, and `GET /orders` issues about 2852
  queries, which behind a login is a backlog item. Together they are one
  request, from anyone, that costs the database three thousand queries, and
  every one of those requests returns 200 so nothing in the access log looks
  like an attack. The other half is the unbounded list: public and unpaginated
  is not load, it is the whole table in one request.

  The tools that look for this are DAST scanners, which need the service
  running, reachable and holding enough rows for the cost to show. Both halves
  are in the source. Works on Django through DRF views and permission classes,
  and on FastAPI through routes and SQLAlchemy relationships.

  Severity separates the two: critical for 1000+ queries or unpaginated **and**
  expensive, high for the rest. An earlier version made every unpaginated
  public list critical and produced thirteen criticals on the demo project, one
  costing 2851 queries and eight costing one - a label everything wears says
  nothing. If one of the two underlying checks cannot run that is reported
  rather than returning an empty list, because an empty result and an
  unanswerable question look identical and only one is good news.

- **`sqlalchemy_nplusone`**: relationships SQLAlchemy loads one row at a time.
  Two shapes. The familiar one is a lazy relationship touched inside a loop,
  where the query and the access are usually far enough apart that neither line
  looks wrong. The quieter one has no loop at all: a FastAPI endpoint returning
  `db.query(Order).all()` under `response_model=list[OrderOut]` walks
  `Order.items` once per row during **serialisation**, after the function has
  returned, which is why nothing in the body mentions it.

  `nplusone` finds this at runtime by watching lazy loads happen, `lazy="raise"`
  turns it into an exception, and the documentation's own advice is query-count
  tests - all of which need the code path to run. Both halves are in the
  source: the query says what it eagerly loaded, the model says which
  attributes are relationships.

  A relationship declared `lazy="selectin"`, `"joined"`, `"subquery"` or
  `"immediate"` is never reported because it is already eager, and `lazy="raise"`
  is never reported because it is the recommended fix. Nothing is imported, so
  it runs on a checkout with no dependencies installed and no Django
  configured.

- **`fastapi_exposure`**: endpoints that serialise more than they declare.
  With no `response_model` and no return annotation, FastAPI serialises
  whatever the function returns - the whole ORM object, every column,
  including the ones added to the model next month. It is the FastAPI shape of
  `fields = "__all__"`, and worse in one way: a Django serializer at least
  lists what it exposes somewhere, while here the absence of one line is the
  entire bug, so there is nothing in the file to read or review.

  Every FastAPI guide says to set `response_model` and several say a CI rule
  should enforce it; no linter ships one. Severity follows reach:
  unauthenticated and unbounded is critical, the same behind a dependency is
  high because it still leaks to everyone who can log in. `Depends(get_db)`
  does not count as authentication and `Depends(get_current_user)` does.

  An endpoint returning a dict or a literal is silent - the author decided what
  goes in it - and so is one with a narrow `response_model` or a return
  annotation. Nothing is imported: a FastAPI app usually wants a database URL
  and a secret before it will import at all, and none of that is needed to read
  a decorator, so this runs with no Django configured and no application
  dependencies installed.

- **`blocking_in_async`, and the framework detection that made it possible.**
  FastAPI runs an `async def` endpoint on the event loop itself and a `def`
  endpoint in a threadpool, so a synchronous call inside `async def` does not
  slow one request - it stops every request in the process. Invisible at one
  request a second, an outage at two hundred, and the traceback points at
  whichever endpoint timed out rather than at the line.

  `ruff`'s ASYNC rules cover `open`, `time.sleep` and `subprocess` inside an
  async function. This adds the two that matter more: a synchronous database
  call, which is the common one, and a blocking call reached **through another
  project function**, where nothing at the call site looks blocking. The second
  comes back with the path that reaches it.

  A `def` endpoint is never reported - blocking in a threadpool is fine, and
  telling somebody to make a working sync endpoint async is the advice that
  causes the outage. Anything awaited is silent, which covers
  `asyncio.to_thread` and `run_in_executor` without a special case.

- **`project_profile` and a boot that no longer assumes Django.** Every check
  began with `ensure_django()`, so a FastAPI project got a settings error
  instead of an answer to a question that never needed Django. Frameworks are
  now detected from the project's own imports - a package sitting in the
  virtualenv that nothing imports is a fact about the environment, not the
  code - and checks that only read source skip the Django boot entirely.
  `django-chainsaw async` and `django-chainsaw profile` need only
  `DJANGO_CHAINSAW_PROJECT_PATH`.

- **`migration_risk` reports which migrations cannot be rolled back.**
  `RunPython` and `RunSQL` are the only operations Django cannot reverse on
  their own: without `reverse_code` / `reverse_sql` they raise
  `IrreversibleError`, so `migrate <app> <previous>` fails and the rollback a
  bad deploy needs is not available. This is tracked separately from row
  impact, because they are different questions - a harmless data migration
  with no reverse is still why a rollback fails at 3am, and a dangerous one
  that declares its reverse is not. `RunPython.noop` counts as declared: it
  says going backwards should do nothing, where `None` says nobody decided,
  and the two are indistinguishable afterwards.
- **`deploy_safety` inventories the raw SQL that qualifies its verdict.**
  Its `clear` result always meant "nothing was found here", with hand-written
  SQL named as the reason that is not a guarantee. The `cursor.execute()`,
  `.raw()`, `.extra()` and `RunSQL` sites are now listed, so the unbounded
  worry becomes a finite list - usually a short one - that a reviewer can
  actually check before dropping a column.

- **`money_precision`**: places where a decimal amount stops being exact.
  A `DecimalField` exists so that money is exact, and four things give that up
  somewhere the model definition cannot see: `Decimal(<inexact float>)`, which
  is wrong before anything is done with it; `float()` on a decimal column, a
  one-way door; `round()` instead of `quantize()`, which keeps the precision
  and quietly picks banker's rounding, so 0.125 becomes 0.12 where an invoice
  expects 0.13; and a `FloatField` holding money, where the column itself is
  the problem.

  The distinction that makes it usable is that `Decimal(0.5)` loses nothing and
  `Decimal(0.1)` does, so the check computes the round trip rather than matching
  the pattern. On a large real project that split 50 calls into 41 harmless and
  11 genuinely wrong; without it the check would have been 82% noise. The
  harmless ones are reported at low severity and hidden by default, because a
  literal that is exact today is one edit from not being.

  `float()` and `round()` are matched by attribute name against every
  DecimalField name in the project, since resolving what `invoice.amount` refers
  to would need type inference. Nothing in the linter ecosystem looks at any of
  this: the advice stops at the model definition and every one of these happens
  somewhere else.

- **`unused_eager_loading`**: `select_related` and `prefetch_related` the
  serializer never reads. Every tool in this space looks the other way - a
  relation touched but not prefetched, the N+1 - while this direction costs on
  every request: a JOIN per row, or a whole extra query and the objects it
  returns. It is invisible because it looks like an optimisation, and usually
  was one until the field it was added for got removed. `nplusone` finds it at
  runtime by watching which loaded objects go untouched, so it covers the paths
  the tests exercise; both halves are already in the source. A serializer with
  a method field, or a view overriding list/retrieve/to_representation, is low
  confidence and hidden by default, because "delete this select_related" is a
  destructive suggestion that reintroduces an N+1 when it is wrong.

- **`scan_templates` reads `render()` and `TemplateResponse` calls**, which is
  how most Django views are written. It previously understood only class-based
  views declaring `template_name` plus `model`/`queryset`; measured against a
  large real codebase that resolved context for 28 templates out of 2181, so
  the analysis had nothing to say about the other 2153. It now reads the
  template name and context keys from the call, resolves values through local
  assignments, and handles a context dict built before the call. On the same
  codebase: 81 templates analysed, 18 with findings, both roughly three times
  what it managed before. A computed template name is still not guessed at.
- **`{% include %}` is followed with the caller's context.** A partial rendered
  inside a loop is the N+1 nobody sees: the loop is in one file and the
  relation traversal is in another, and neither file is suspicious alone. Only
  a literal template name is followed, `{% include ... only %}` correctly gets
  an empty scope, `with x=y` rebinds what it names, recursion is capped and a
  self-including partial is not walked twice.

- **`endpoint_cost` reads the real page size and names unpaginated list
  endpoints.** The first version assumed `page_size` rows for every list view.
  A view with no `pagination_class` in a project with no
  `DEFAULT_PAGINATION_CLASS` returns the whole table, and the number 50 was a
  fiction dressed up as an input - it described every list view in the demo
  project. Paginated views now use their own page size; unpaginated ones are
  marked as floors and listed at the top level, because a list endpoint with
  no upper bound is a finding on its own.

- **`race_conditions` also reports `get_or_create`/`update_or_create` with no
  unique constraint behind the lookup.** Two requests miss the get together,
  both create, and the next call raises `MultipleObjectsReturned`, which the
  method does not catch. Django's docs say a database constraint is the only
  protection; the check reads the model for a unique field, `unique_together`
  or an unconditional `UniqueConstraint` covering the lookup, treats a superset
  of one as covered, ignores `defaults=`, and does not judge a lookup through a
  relation. Prior art: two open Django tickets and blog posts, no tool.

- **`open_endpoints`**: endpoints anyone can call, crossed with what their
  serializer exposes. `serializer_exposure` knows a serializer leaks a password
  reset token and a Semgrep rule knows a view has `AllowAny`; each is a
  judgement call alone and neither check can connect them, because the
  serializer does not know its views and the view does not know what its
  serializer returns. DRF's own default permission is `AllowAny`, so a project
  that never configured `DEFAULT_PERMISSION_CLASSES` has every unadorned view
  public; that is reported before anything else. View modules no URLconf
  reaches are imported first, the same way serializer modules are, and the
  ones that fail to import are listed rather than silently missing.
- `requests` added as a dev dependency: three demo fixtures import it, and a
  module that fails to import is a module whose views and serializers cannot
  be seen.

- **`race_conditions`**: a field read into Python, changed, and saved -
  `product.stock -= qty; product.save()` - so two concurrent requests
  overwrite each other and a sale is lost. Reported only when all three parts
  are on the same object in the same function; `F()` expressions and a
  `select_for_update()` inside `atomic()` are the fixes and are silent. Also
  `select_for_update()` with no transaction to hold the lock, which is not a
  race but a `TransactionManagementError` on the first request, judged with the
  call graph so a caller's transaction and `ATOMIC_REQUESTS` both count. No
  linter looks at either.

- **`bypassed_effects`**: bulk writes that skip everything the model's save()
  chain promised. `bulk_create`, `bulk_update` and `QuerySet.update` go
  straight to SQL, so no `save()` override runs and no receiver fires; Django
  says so in one sentence per method and nothing at the call site does. The
  finding is not the abstract fact but this call, on this model, skipping these
  named effects - transitively, so an Invoice receiver that never fires because
  the Invoice was never created is listed too. Models with nothing to skip are
  not reported, and `QuerySet.delete()` is left out because its signals do
  fire per object.

- **A blind-spot audit, and thirteen of them closed.** Every stated limitation
  across the docs was collected, sorted into what is genuinely undecidable from
  source and what was merely unimplemented, and the second list was worked
  through. `docs/limitations.md` now holds the consolidated version, including a
  table of what was closed, so the difference between "cannot" and "did not"
  stays visible.
- **`discovery`**: serializer modules are imported before any subclass walk.
  Django imports `models.py`, `admin.py` and whatever the URLconf reaches, so a
  serializer used only by a management command was invisible to four separate
  checks - and the serializer leaking a password hash is very often exactly that
  one. Modules that fail to import are reported rather than swallowed, because
  that usually means the analysis is pointed at the wrong settings.
- **Serializers now carry the views that serve them**, with nesting counted:
  `OrderLineSerializer` is named by no view and rendered on every order
  response, so calling it unserved would invite deleting a field a client reads.
- **`endpoint_cost` reads `SerializerMethodField` bodies** instead of marking
  the endpoint unbounded, and costs `only()` that omits a rendered column - an
  N+1 wearing a performance hat, since the code looks optimised. A method that
  touches a cache stops the count rather than inventing a number.
- **`what_happens_on` walks overridden `save()`/`delete()`** through the MRO.
  They are not signals, they run before every receiver, and they are often where
  the largest effect in the chain lives. It also counts receivers connected more
  than once, and marks writes that sit behind a condition.
- **`escaping_side_effects` enumerates `ATOMIC_REQUESTS` views.** Reporting only
  the setting left the largest class of this defect uncounted in exactly the
  projects that have it.

- **`callgraph`**: a project-wide call graph, built so the checks stop being
  limited to one file at a time. Every single-file check had the same blind spot
  and it was always the same sentence: the boundary and the call are in different
  modules. It resolves direct name imports, module attribute calls, `self.x()`
  within a class and `self.x()` inherited through a base, and counts what it
  could not resolve rather than dropping it, because a silent gap in a call graph
  turns every check built on it into a confident wrong answer.
- **`escaping_side_effects` now follows calls across modules.** A view that opens
  `atomic()` and calls a helper three modules away that sends mail is reported at
  the line that sends the mail, with the call path that put it inside a
  transaction — which is the actionable part, since a finding in `services.py` is
  meaningless without the caller that caused it.
- **`find_unscoped_queries` resolves scoping it could not previously see**: a
  `get_queryset` inherited from a base class or mixin, and a default manager that
  narrows on every access. Both used to produce false positives on code that was
  already correct. Neither is silently dropped: they are reported under
  `scoped_elsewhere` with the reason, because a suppression without a stated
  reason is just hiding a finding.

- **`escaping_side_effects`**: calls inside a transaction whose effect
  cannot be rolled back. Two defects share one shape. A task handed to a
  broker inside `atomic()` can be picked up before the commit, so the worker
  queries for a row that is not there — a race that passes every test,
  because tests run in a transaction that never commits against a worker
  that runs eagerly, and fails under load. An email or an outbound request
  cannot be undone at all, so a rollback leaves the outside world told about
  something that never happened. The ecosystem's answer is entirely runtime:
  `django-celery-oncommit`, `django-post-request-task`, Celery 5.4's
  `delay_on_commit()`. All of them fix the next call; none find the existing
  ones, and the `DJ` rules in flake8-django and ruff do not look at
  transaction boundaries. `ATOMIC_REQUESTS` is volunteered rather than
  waited for, because it wraps every view in a transaction with nothing
  visible at the call site. Calls already deferred to `on_commit`, and the
  same call outside any transaction, are asserted to produce nothing.

- **`endpoint_cost`**: how many database queries one request to each DRF
  endpoint will cost, derived from the view's queryset and its serializer
  rather than from traffic. Every existing tool answers this by running the
  application: the debug toolbar, silk, `assertNumQueries`. On the demo
  project the same serializer costs 2852 queries behind an unoptimised
  queryset and 2 behind an optimised one. The first version multiplied every
  nested level by the page size and reported 252,602, which is arithmetic
  rather than an estimate; how many children a parent has is a property of
  the data, so it is now a stated parameter and the output says the ratio is
  the reliable part.
- **`api_contract`**: what this branch changes about the shape the API
  promises, resolved from the serializer classes and compared against a
  committed snapshot. Changes are classified by who they hurt rather than
  listed, which is the part that matters: a new **required** field is an
  addition that breaks every existing writer, and a plain field-set diff
  puts it in the same bucket as a harmless optional one. `drf-api-checker`
  answers this by recording real responses during a test run, and OpenAPI
  diffing needs the schema generated; both need the application running.

- **Command line interface** (`django-chainsaw`) with exit codes, so the
  analysis can gate a pipeline instead of only answering questions. `0` clean,
  `1` findings, `2` load or argument error. Gates are opt-in: without
  `--max-high` or `--fail-on-risk` a command only reports.
- **`scan_templates`**: the N+1 analysis over a whole directory, resolving the
  template context from class-based views that declare `template_name` with
  `model` or `queryset`. `find_n_plus_one` needs a context map typed by hand,
  which is fine from an assistant and useless in CI.
- **`missing_indexes`**: fields the code filters or sorts on that carry no
  index, read from the source rather than from traffic. `django-indexes` answers
  the same question at runtime, which is accurate and only ever covers the paths
  traffic reached. Lookups a btree cannot serve are counted and ignored rather
  than flagged, relation traversals are skipped as the other table's problem,
  and everything Django already indexes is excluded. It cannot weigh anything,
  which is stated in its output: a filter on forty rows looks like one on forty
  million.
- **`portability_check.sh`**: fails if anything tracked in the repository
  contains a path from the machine it was written on, if an absolute path is
  assigned to a module constant, if a shell script does not `cd` to its own
  directory, or if a Python test does not anchor on `__file__`. Added after the
  question was raised, and it found a real defect on the first run.
- **`fix` and the `suggest_fixes` tool**: findings turned into code, grouped by
  how safe each one is to apply. **mechanical** has one correct answer from the
  code alone and is applied by `--write`; **generated** is an artefact a machine
  can write but a human must decide on, such as an index migration, written to
  a file for review; **advisory** is real code with the names resolved but a
  decision that belongs to the domain, and is never applied. The tenancy
  suggestion reads the request argument from the enclosing function's signature
  rather than assuming `request`, and says so when there is none. `--write`
  refuses any fix whose line changed since the analysis and is idempotent.
  `fix_check.sh` covers all four safety properties.
- **`check`**: one command that runs every analysis, merges the findings into
  one severity-sorted list and returns one exit code. Twelve subcommands is
  twelve things to learn before the tool does anything; nobody reads a manual to
  try something. `--only`, `--skip`, `--fail-on` and `--strict`, which exits 2
  when a check could not run rather than counting it as clean.
- **SARIF output** through `--sarif FILE`. A finding printed to a CI log is a
  finding nobody reads. SARIF puts each one as an annotation on the line it is
  about, inside the pull request, which is the difference between changing
  behaviour and costing CI minutes.
- **`serializer_nplusone`**: N+1 in DRF serializers. The template checker
  answers this for server-rendered pages; most Django written today renders
  JSON, and there the N+1 comes from a nested serializer field. Follows nested
  serializers so the suggested lookup is the full path, and groups the advice
  per serializer in a form that can be pasted into a view. This was the largest
  functional gap in the project.
- **`explain_model`**: every analysis for one model, plus **correlated risks**,
  which is the part no single check can produce. Owned, read without scoping and
  exposed over the API is a complete path from a URL to another customer's row,
  while each of those three alone is an ordinary warning.
- **A pytest suite** covering the analysis layer, 26 tests. The shell scripts
  stay for what pytest cannot reach in one process: CLI exit codes, a fresh
  virtualenv, a real git repository.
- **`docs/README.md`** as a documentation index and
  **`docs/quickstart.md`** for the five minute path.
- **`LICENSE` (AGPL-3.0), `NOTICE`, `CITATION.cff` and SPDX headers** on every
  source file. See [`docs/authorship.md`](docs/authorship.md) for the reasoning,
  including the honest cost of AGPL and why starting restrictive is the
  reversible choice.
- **`--since REF`** on `deploy-safety`, `n+1`, `tenancy` and `indexes`: report
  only findings in files the branch changed. Compared at the merge base rather
  than with a two-dot diff, so a branch that is behind main is not blamed for
  what main gained meanwhile. Uncommitted, staged and untracked files count. An
  unknown ref warns and falls back; unmatchable paths are kept and listed rather
  than dropped.
- **`datetime_audit`**: naive datetimes in code and ambiguous defaults on model
  fields. `datetime.now()`, `utcnow()`, `fromtimestamp()` without a zone, and
  literals built from parts. On models: a naive default, a default evaluated
  once at import so every row shares one value, and `auto_now` together with
  `auto_now_add`. `timezone.now()` and `make_aware(...)` are recognised as
  correct. With `USE_TZ = False` the code findings are informational and say so.
- **`serializer_exposure`**: what each DRF ModelSerializer exposes.
  `fields = "__all__"` is a decision made once and re-made silently by every
  migration after it, with no diff on the serializer for anyone to review.
  Reports `__all__` and `exclude` as patterns, and explicit lists that name a
  field whose name suggests it should not be public. Name matching, not
  classification, which is stated in the output.
- **`docs/clients.md`**: configuration for Claude Code, Claude Desktop, Cursor,
  Windsurf, VS Code, Zed and Docker, including the detail that costs an
  afternoon: the root key is `mcpServers` in most tools, `servers` in VS Code
  and `context_servers` in Zed.
- **Baselines** for `tenancy`, `n+1` and `deploy-safety`, through `--baseline`
  and `--update-baseline`. Every analyser here has the same adoption problem: on
  an old codebase it returns hundreds of candidates, the gate gets switched off
  in the first week, and then it runs forever with nobody looking. A baseline
  records what is already there so the gate fails on new findings only, reports
  fixed ones so the file can be regenerated, and therefore only ever counts
  down. Findings are fingerprinted on file plus identity and never on the line
  number, so adding an import does not resurrect findings nobody touched.
- **`what_happens_on`**: follows the signal chain a save or delete sets off,
  through receiver bodies and into the signals those writes fire in turn.
  Receivers come from the live registry; their bodies are read with the AST.
  Write targets resolve by class name and, more usefully, through the sending
  model's relations, so `instance.order.save()` continues the chain instead of
  ending it. Tools that list receivers exist; none follow the second hop, which
  is where a Celery task three models away turns up. It is also the half
  `delete_impact` documents itself as missing.
- **`find_unscoped_queries`**: reports querysets that read tenant-scoped rows
  without an ownership filter, the shape behind most IDOR reports. Generic
  static analysers struggle with this class because the defect is the absence of
  a filter and absence has no syntax; the existing answers are runtime SQL
  inspection or PostgreSQL row-level security. The model graph makes it
  checkable: it knows `Order` reaches the tenant root through `customer`, so
  filtering on `pk` alone is visibly not enough. Models with no path to the
  owner are never reported.
- **`deploy_safety`**: cross-references pending destructive migrations against
  the code that still uses them, and reports `blocking` with file and line
  numbers or `clear`. Python is parsed with `ast`; templates use patterns.
- **`delete_impact`**: follows `on_delete` transitively across the model graph.
- **`find_n_plus_one`**: relation traversals in a template, with the
  `select_related` / `prefetch_related` that would fix them.
- **`migration_risk`**: migration operations rated by production impact.
- **`django://models`** as an MCP resource rather than another tool.
- Documentation under `docs/`: usage, architecture, tool reference, CLI, and the
  reasoning behind `deploy_safety`.
- **`docs/usage.md`**: how to run this against a real project. The decisive
  constraint is that `django.setup()` imports your settings and therefore every
  entry in `INSTALLED_APPS`, so the interpreter needs your project's
  dependencies. Covers installing into the project venv, keeping it separate,
  Docker with `docker compose exec -T`, client configuration for Claude Code and
  JSON-configured clients, and a table mapping each error message to its cause.
- **`install_check.sh`**: builds a throwaway virtualenv, installs Django and this
  package into it, then drives both the CLI and the MCP server with that
  interpreter. The documented install path is verified, not assumed.

### Fixed

- **A `Prefetch(...)` object made the whole view invisible.** Any non-literal
  argument to `prefetch_related` marked the queryset as built at runtime, which
  skipped the view in `unused_eager_loading` and made `endpoint_cost` an upper
  bound - and `Prefetch` is the normal way to prefetch anything filtered. The
  path is a literal first argument and is now read normally. One carrying
  `to_attr` is kept apart instead: it loads the relation under another name, so
  a read of the relation path proves nothing and its absence proves nothing.

- **A ForeignKey column was reported as needing an index it already has.**
  `filter(date_id=...)` names the same indexed column as `date`, and Django
  indexes every FK by default, but only `field.name` was recorded as covered -
  never `field.attname`. Measured against a large real project that was four of
  the seven most-reported candidates, and the high-severity list went from 92
  to 77 once fixed. The same now applies to names in `Meta.indexes`,
  `Meta.constraints` and `unique_together`.

- **Template analysis crashed on the first custom tag library.** It parsed with
  a bare `Engine()`, which has no `libraries` and no `DIRS`, so `{% load %}`
  raised `TemplateSyntaxError` and `{% extends %}` could not find its parent.
  Every project of any size has a custom tag library, so this made the check
  useless on all of them while passing on a demo that has none. It now parses
  with the engine the project actually renders with, and a template that still
  cannot be read is listed under `templates_unreadable` with the reason instead
  of ending the run.
- **Three checks walked View subclasses without importing the view modules
  first**, the same defect `discovery` was written to fix for serializers. On a
  project whose URLconf does not reach every view at analysis time this made
  `endpoint_cost` report zero endpoints and `api_contract` call every
  serializer unserved - both silently, both looking like clean results.
  `load_view_modules` moved into `discovery` and is now used by all of them.
- **`reshapes_output` flagged every serializer in existence.** It walked the
  whole MRO for `get_fields`/`to_representation`, and DRF defines both on its
  own base classes, so the flag was always on and carried no information. It
  now counts only overrides written outside `rest_framework`. The test that
  should have caught this only asserted the key existed; it now pins both
  directions.
- **An empty result no longer reads as a clean one.** `endpoint_cost` reports
  how many views it looked at and how many declared no `serializer_class`, and
  `api_contract` reports `attribution_possible` and leaves `unserved` empty
  rather than listing every serializer, when no view in the project names one.
  A project whose views build their responses by hand gets "could not look",
  not "nothing to find".

- **The API contract needed a database, which defeated the point of it.**
  Reading `.choices` off a `PrimaryKeyRelatedField` iterates its queryset,
  so capturing the contract on a bare checkout died on `no such table:
  shop_category`. Related fields now report the model they point at, which
  is more stable anyway. `ManyRelatedField` needed covering separately: it
  is not a `RelatedField` subclass, it wraps one, and it has its own
  equally eager `.choices`. `contract_check.sh` now asserts the demo
  project has no database so this cannot come back.
- **A serializer that failed to instantiate was reported as removed.**
  Both cases look identical from outside, and only one is the API's fault.
  Saying "serializer removed" about a class that is still defined and
  merely names a model field that no longer exists is a confident wrong
  answer. It now gets its own verdict, and it fails the gate, because an
  unknown is not a pass.
- **`endpoint_cost` grouped nested findings by package name.** It split the
  relation path on the first dot and used the leading segment as the owning
  serializer, which is the module's package, so every nested crossing was
  silently dropped from the estimate.

Each of these ran without raising anything, which is the failure mode this kind
of tooling actually has.

- **Reverse relations lost their cardinality.** The check tested
  `many_to_many`, `many_to_one` and `one_to_one` but not `one_to_many`, so every
  reverse accessor came back as `Unknown`: exactly the half that matters,
  because reverse relations are where N+1 queries come from.
- **`delete_impact` reported nothing at all.** `on_delete` lives on
  `field.remote_field`, not on the field. `getattr(field, "on_delete", None)`
  returned `None` silently, every relation classified as `UNKNOWN`, and the tool
  cheerfully reported that deleting a customer had no consequences.
- **Nested loops were invisible.** Loop variables were bound to the string they
  iterated, so `{% for line in order.lines.all %}` left `line` unresolvable and
  everything inside went unreported. They now carry the resolved element model,
  which is also what makes `prefetch_related('lines')` appear.
- **`deploy_safety` matched docstrings and every `name` in the project.** The
  first version was a text search: it flagged `contenttypes.0002` removing a
  field called `name`, with hits across unrelated models, and counted a sentence
  in a module docstring as a live reference. Fixed by parsing Python with `ast`
  and by only analysing migrations from apps inside the project.
- **Narrowing the scan emptied the analysis instead of flipping the verdict.**
  `search_path` decided both where to look for references and which apps counted
  as project apps, so pointing it at a subdirectory excluded every app and
  returned nothing rather than reporting the migration as clear. App ownership
  now comes from the project path.
- **The test helper read one spelling of a field that has two.** `_payload` in
  `client_test.py` used `result.structuredContent`, which works on the SDK build
  pinned here and raises `AttributeError` on builds that call it
  `structured_content`. It only surfaced once the package was installed into a
  different environment, which is the entire reason `install_check.sh` exists.
- **The package imported `server` eagerly**, which made
  `python -m django_chainsaw_mcp.server` import the module twice and emit a
  `RuntimeWarning`. `main` is resolved lazily through `__getattr__`.

- **Duplicate findings from chained querysets.** `ast.walk` visits every `Call`
  in a chain, so `Order.objects.filter(...).first()` unwound twice and was
  reported twice. Findings are deduplicated per file, line and model, keeping
  the outermost chain.
- **CRLF line endings in the shell scripts.** Editing them from a Windows host
  through the WSL share wrote `\r\n`, and bash failed with a syntax error on a
  line that looked correct. A `.gitattributes` now pins `eol=lf`.

- **`find_unscoped_queries` flagged `create()`.** Adding signal fixtures
  surfaced it: `Invoice.objects.create(order=instance)` was reported as an
  authorisation problem. Inserting a row cannot leak anybody's data, and the bug
  class is unauthorised reads. Chains ending in a pure write are skipped;
  `get_or_create` and `update_or_create` still count, because they read first.

- **An empty baseline file was treated as corruption.** `mktemp` and `touch`
  both produce one, and `json.loads("")` raised, so the command exited 2 with a
  message about unreadable JSON when the honest answer was that no baseline had
  been recorded yet.

- **`smoke_test.py` and `analysis_test.py` only worked from the repository
  root.** Both set `DJANGO_CHAINSAW_PROJECT_PATH` to the bare string
  `"testprojects"`, which resolves against the caller's working directory. They
  now anchor on `Path(__file__)`. Found by `portability_check.sh` on its first
  run, which is the entire argument for having it.
- **Two tenancy assertions counted every finding in the project.** Adding
  `reports.py` added three genuine cross-customer reads and broke three
  assertions that had nothing to do with the change. A test that counts the
  whole project fails whenever the project grows, and that teaches people to
  edit the number instead of reading the failure. They assert on `api.py` now,
  which is the file written for that check.

- **The literal check missed `datetime.datetime(...)`** and matched only the
  bare `datetime(...)` form. Fixing it naively would then have flagged
  `timezone.make_aware(datetime.datetime(...))`, which is the correct code, so
  `make_aware` and `localize` now suppress the finding on their line.
- **A `client_test` assertion counted models.** Adding a fixture model broke it
  for reasons unrelated to the change. It names the models it expects now.

- **The correlated cascade check had its logic backwards.** A child that
  reaches the owner through this model has a path ending with this model's path,
  not starting with it, so every ordinary child was reported as belonging to a
  different owner.
- **An `exitcheck` expectation was wrong, not the code.** It asserted that
  `--fail-on critical` would pass, while the demo project deliberately ships a
  blocking migration, which is critical.

### Changed

- Ported from `FastMCP` to `MCPServer` for MCP Python SDK v2, where the class
  was renamed. Neither is related to FastAPI, despite the old name.
