# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning is [semantic](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
