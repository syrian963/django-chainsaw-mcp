# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning is [semantic](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
