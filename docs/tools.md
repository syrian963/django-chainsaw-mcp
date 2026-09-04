# Tool reference

Every tool returns JSON. Failures that are the caller's fault come back as
`{"ok": false, "error": "..."}` rather than as an exception, so a wrong path or
a typo does not take the server down.

---

## `project_info`

Boots the target project and reports what it is. **Run this first when
something is not working**: it is the smallest call that proves both halves of
the setup, the MCP transport and the Django boot.

No arguments.

```json
{
  "ok": true,
  "django_version": "6.1.1",
  "settings_module": "demoshop.settings",
  "project_path": "/path/to/testprojects",
  "debug": true,
  "installed_apps": ["contenttypes", "auth", "shop"],
  "database_engines": {"default": "django.db.backends.sqlite3"}
}
```

---

## `list_models`

Every concrete model with its fields, relation kind, direction and `on_delete`.

| Argument | Default | Meaning |
| --- | --- | --- |
| `app_label` | all apps | restrict to one app |
| `include_fields` | `true` | set false for a short overview |

Relations carry `direction`: `forward` for a declared field, `reverse` for an
accessor Django created. `kind` is one of `ManyToOne`, `OneToOne`, `ManyToMany`,
`OneToMany`.

```
shop.Order      lines     OneToMany   reverse  -> shop.OrderLine
                invoice   OneToOne    reverse  -> shop.Invoice
                customer  ManyToOne   forward  -> shop.Customer   CASCADE
```

---

## `delete_impact`

**What deleting one row takes with it.** Follows `on_delete` across the whole
graph, transitively, and survives self-referencing foreign keys.

| Argument | Default | Meaning |
| --- | --- | --- |
| `model_label` | required | `app_label.ModelName` |
| `max_depth` | `6` | how far to follow chained cascades |

```
shop.Customer: 3 model(s) lose rows, 0 can block the delete, 0 get fields cleared
  CASCADE  shop.Order       via customer  depth 1  [shop.Customer]
  CASCADE  shop.Invoice     via order     depth 2  [shop.Customer -> shop.Order]
  CASCADE  shop.OrderLine   via order     depth 2  [shop.Customer -> shop.Order]
```

Results are grouped into `cascades`, `blocked_by` (`PROTECT`, `RESTRICT`),
`fields_cleared` (`SET_NULL`, `SET_DEFAULT`) and `left_untouched`
(`DO_NOTHING`).

**Does not see:** `pre_delete` / `post_delete` signals and custom `delete()`
overrides, which can remove more than the graph implies. Row counts are not
read; this says *what*, never *how much*.

---

## `find_n_plus_one`

Relation traversals in one template, with the queryset change that would fix
them.

| Argument | Default | Meaning |
| --- | --- | --- |
| `template_path` | required | path to the template file |
| `root_models` | required | context variable to model label |

```json
{"orders": "shop.Order", "single_order": "shop.Order"}
```

A name bound in `root_models` is treated as the model when used directly and as
the element type when looped over. Loop variables inherit: inside
`{% for line in order.lines.all %}`, `line` resolves to `shop.OrderLine`, so
chains three hops deep are followed.

`severity` is `high` inside a loop and `low` outside it. Reverse and
many-to-many hops produce `prefetch_related`, forward hops `select_related`.

```
high  order.lines.all             prefetch_related('lines')
high  line.product.category.name  select_related('product__category')
high  order.customer.email        select_related('customer')
low   single_order.customer.email select_related('customer')
```

**These are candidates.** Whether a crossing costs a query depends on the
queryset in the view, which this does not read. Use `scan_templates` to avoid
writing the context by hand.

---

## `scan_templates`

`find_n_plus_one` over a whole directory, resolving the context from views
instead of asking for it.

| Argument | Default | Meaning |
| --- | --- | --- |
| `template_root` | project path | directory of templates |
| `project_root` | project path | where to look for views |
| `root_models` | none | context applied to every template |

Context is read from class-based views that declare `template_name` together
with `model` or `queryset`:

```python
class OrderListView(ListView):
    model = Order
    template_name = "shop/order_list.html"
    context_object_name = "orders"
```

That yields `{"orders": "shop.Order", "object": "shop.Order"}` for that
template. Function views and anything built at runtime cannot be resolved
statically; those templates are skipped and listed in
`templates_skipped_no_context`. Pass `root_models` to cover them.

---

## `migration_risk`

Migrations rated by what they do to a live database.

| Argument | Default | Meaning |
| --- | --- | --- |
| `include_applied` | `false` | also classify migrations that already ran |

| Risk | Meaning |
| --- | --- |
| `blocks_writes` | holds a lock that stops writes while it runs |
| `breaks_running_code` | the deployed code breaks during the rolling deploy window |
| `rewrites_table` | may rewrite the whole table under an exclusive lock |
| `safe` | no effect on existing rows |

Each operation carries a `detail` explaining the mechanism and, where one
exists, a `safer` alternative:

```
AddIndex        blocks_writes   Building an index without CONCURRENTLY holds a lock…
                                safer: use AddIndexConcurrently
RemoveField     breaks_running_code
                                safer: ship the code that stops using it first
```

**Does not know:** row counts, PostgreSQL version, or deploy strategy, all of
which change how bad a given operation actually is.

---

## `deploy_safety`

**Is a pending destructive migration safe to ship yet?** The one tool here with
no equivalent elsewhere. See [`deploy-safety.md`](deploy-safety.md) for the
reasoning; this is the interface.

| Argument | Default | Meaning |
| --- | --- | --- |
| `search_path` | project path | directory to scan for references |
| `max_hits_per_symbol` | `25` | stop after this many references |
| `include_third_party` | `false` | also analyse `django.contrib` migrations |

Every unapplied `RemoveField`, `RenameField`, `DeleteModel`, `RenameModel`,
`RemoveIndex` or `RemoveConstraint` comes back in `blocking` or `clear`.

---

## `find_unscoped_queries`

**Which queries read tenant-scoped rows without scoping the query?** The one
tool here that attacks a bug class generic scanners openly struggle with. See
[`tenancy.md`](tenancy.md) for the reasoning; this is the interface.

| Argument | Default | Meaning |
| --- | --- | --- |
| `tenant_root` | `auth.User` | the model that owns data |
| `search_path` | project path | directory to scan |
| `max_depth` | `4` | how many relation hops still count as owned |
| `include_exempt` | `false` | also scan admin, management commands, tests |

Returns `tenant_scoped_models` (each with its ownership path) and `findings`.
`severity` is `high` one hop from the owner, `medium` further away.

**Candidates, not vulnerabilities.** A filter in a base class, a mixin, a custom
manager or a `get_queryset()` override is invisible, and `Q()` objects are
treated as scoped because their contents cannot be read statically.

---

## `what_happens_on`

**What a save or delete actually triggers.** Follows the signal chain
transitively rather than listing registered receivers. See
[`signals.md`](signals.md) for the reasoning.

| Argument | Default | Meaning |
| --- | --- | --- |
| `model_label` | required | `app_label.ModelName` |
| `event` | `save` | `save` or `delete` |
| `max_depth` | `4` | how far to follow writes into further signals |

Returns the ordered `chain`, every `side_effects` entry found along it (Celery
tasks, cache writes, mail, outbound HTTP), `models_written`, and
`unreadable_receivers` for anything whose source could not be parsed.

Write targets resolve two ways: by class name for `Invoice.objects.create(...)`,
and through the sending model's relations for `instance.order.save()`. Without
the second the chain stops at the first hop.

**Does not see:** conditions (a write inside `if created:` is reported as if it
always happens), receivers connected after startup, overridden `save()` methods,
and dynamic dispatch.

---

## `missing_indexes`

Fields the code filters or sorts on that no index covers. Runtime tools answer
this by watching traffic, which only ever covers the paths traffic reached;
reading the source covers every path in the repository and needs no database.

| Argument | Default | Meaning |
| --- | --- | --- |
| `search_path` | project path | directory to scan |
| `min_occurrences` | `1` | only report a field asked for at least this often |

Not reported: primary keys, `unique=True`, `db_index=True`, foreign keys,
`Meta.indexes`, `Meta.constraints`, and the leading column of a composite index.
Lookups a btree cannot serve (`contains`, `icontains`, `iexact`, `regex`,
`endswith`) are counted in `ignored_lookups` rather than flagged. Relation
traversals belong to the other table and are skipped.

`high` at three or more occurrences, or at any use in `order_by`.

**It cannot weigh anything.** A filter on forty rows looks like one on forty
million, and every index costs write throughput. Shortlist, not task list. See
[`indexes.md`](indexes.md).

---

## `datetime_audit`

Naive datetimes in code, and model field defaults that are ambiguous or frozen
at import time. The defect is invisible for ten months and appears on the two
nights a year the clock moves.

| Argument | Default | Meaning |
| --- | --- | --- |
| `search_path` | project path | directory to scan |

Recognises `timezone.now()` as correct, including under an alias, and does not
flag `make_aware(datetime(...))`. With `USE_TZ = False` the code findings are
reported as informational; the model findings still stand.

---

## `serializer_exposure`

What each DRF `ModelSerializer` exposes. `fields = "__all__"` is a decision made
once and re-made silently by every migration after it.

| Argument | Default | Meaning |
| --- | --- | --- |
| `include_safe` | `false` | also list serializers with an explicit, clean field list |

Name matching, not classification: a field called `token` might be a public
share link, and a field called `notes` might hold medical history and will not
be flagged. Serializers in modules nothing imports at startup do not exist yet
and cannot be inspected.

Both are documented in
[`datetimes-and-serializers.md`](datetimes-and-serializers.md).

---

## `endpoint_cost`

How many database queries one request to each DRF endpoint will cost, before
anybody sends one. Every other tool that answers this runs the application and
tells you afterwards.

| Argument | Default | Meaning |
| --- | --- | --- |
| `page_size` | `50` | objects a list response returns |
| `nested_fan_out` | `5` | assumed children per parent, one level down |
| `list_only` | `false` | skip views that only return a single object |

On the demo project the same serializer costs 2852 queries behind an
unoptimised queryset and 2 behind an optimised one. The ratio is the reliable
part; the absolute number is only as good as `nested_fan_out`, which is a
property of your data that no amount of reading the code reveals.

Documented in [`endpoint-cost.md`](endpoint-cost.md).

---

## `api_contract`

The shape every serializer currently promises: field names, types, read only,
required, nullable, and what the nested ones expand to. Resolved from the class
definitions, so it needs no database and no running server.

| Argument | Default | Meaning |
| --- | --- | --- |
| `max_depth` | `3` | how far to expand nested serializers |

---

## `api_contract_check`

What this branch changes about that shape, and who it breaks.

| Argument | Default | Meaning |
| --- | --- | --- |
| `snapshot_path` | `.django-chainsaw-contract.json` | the committed contract |
| `update` | `false` | record the current shape instead of comparing |
| `max_depth` | `3` | how far to expand nested serializers |

Changes come back classified rather than listed, which is the point. A new
**required** field is an addition that breaks every existing writer; a plain
field-set diff puts it in the same bucket as a harmless optional one. A
serializer that no longer instantiates is reported as unreadable rather than
removed, because calling it removed would be a confident wrong answer.

Documented in [`api-contract.md`](api-contract.md).

---

## `escaping_side_effects`

Calls inside a transaction whose effect cannot be rolled back: a task the
broker already has, an email already sent, a webhook already delivered.

| Argument | Default | Meaning |
| --- | --- | --- |
| `search_path` | project root | directory to scan |
| `include_low_confidence` | `false` | also report calls guessed from the name, such as `.send()` |

Calls already wrapped in `transaction.on_commit`, and the same call outside any
transaction, produce nothing. `ATOMIC_REQUESTS` is reported without being asked
for, since it opens a transaction around every view with nothing visible at the
call site.

Documented in [`on-commit.md`](on-commit.md).

---

## `bypassed_effects`

Bulk writes on models whose `save()` chain they silently skip. `bulk_create`,
`bulk_update` and `QuerySet.update` go straight to SQL: no `save()` override, no
`pre_save`/`post_save`. Everything `what_happens_on` lists for the model does
not occur on those lines.

| Argument | Default | Meaning |
| --- | --- | --- |
| `search_path` | project root | directory to scan |
| `model` | all | restrict to one `app_label.ModelName` |

Only models whose chain does something are reported; a bulk write on a model
with no receivers and no override is just fast. `QuerySet.delete()` is not
listed because Django sends the delete signals per object.

Documented in [`bypass.md`](bypass.md).

---

## `race_conditions`

Read-modify-save races — a field read into Python, changed, and saved, so two
concurrent requests overwrite each other; `select_for_update()` calls with no
transaction to hold the lock, which raise on evaluation; and `get_or_create` /
`update_or_create` upserts whose lookup no unique constraint covers, which
create duplicates under load.

| Argument | Default | Meaning |
| --- | --- | --- |
| `search_path` | project root | directory to scan |
| `include_parameters` | `true` | also report instances passed in as parameters, at medium confidence |

`F()` expressions and a `select_for_update()` inside `atomic()` are the fixes
and are silent. Transactions are judged with the call graph: a decorator, a
caller's `atomic()` and `ATOMIC_REQUESTS` on a view all count.

Documented in [`concurrency.md`](concurrency.md).

---

## `open_endpoints`

Endpoints anyone can call, crossed with what their serializer exposes. Neither
half is a finding alone; the intersection is. DRF's default permission is
`AllowAny`, and whether the project changed that is reported first.

| Argument | Default | Meaning |
| --- | --- | --- |
| `include_unbounded` | `true` | also report open endpoints on `__all__`/`exclude` serializers with nothing sensitive today |

Views overriding `get_permissions()` are listed, not judged. View modules that
fail to import are reported, not swallowed.

Documented in [`open-endpoints.md`](open-endpoints.md).

---

## `unused_eager_loading`

`select_related` and `prefetch_related` the serializer never reads: the
opposite direction from an N+1, and it costs on every request.

| Argument | Default | Meaning |
| --- | --- | --- |
| `include_low_confidence` | `false` | also report views with a `SerializerMethodField` or an overridden `list`/`retrieve`/`to_representation` |

A queryset whose paths are built at runtime is skipped and counted, because
calling a path unused when the list is incomplete is the one way this check
could cause an N+1.

Documented in [`overfetch.md`](overfetch.md).

---

## `money_precision`

Places where a decimal amount stops being exact: `Decimal(<inexact float>)`,
`float()` on a decimal column, `round()` instead of `quantize()`, a
`FloatField` holding money, a float `default` on a `DecimalField`.

| Argument | Default | Meaning |
| --- | --- | --- |
| `search_path` | project root | directory to scan |

`Decimal(0.5)` is separated from `Decimal(0.1)`: the first is exactly
representable and loses nothing. On a real project that split 50 calls into 41
harmless and 11 wrong.

Documented in [`money.md`](money.md).

---

## `project_profile`

What the project is built on, counted from its own imports rather than from
what is installed. Needs no Django.

| Argument | Default | Meaning |
| --- | --- | --- |
| `search_path` | project root | directory to scan |

---

## `explain_model`

Everything about one model in one answer: fields, relations, what a delete
takes with it, the migrations that touch it, and the risks that are only
visible when those are put side by side.

| Argument | Default | Meaning |
| --- | --- | --- |
| `model_label` | required | `app_label.ModelName` |

---

## `serializer_nplusone`

DRF serializer fields that cross a relation the view did not prefetch,
following nested serializers to full paths like `lines__product__category`.

| Argument | Default | Meaning |
| --- | --- | --- |
| `include_safe` | `false` | also list serializers with nothing to fix |

---

## `blocking_in_async`

Synchronous calls that run on the event loop, directly or reached through a
project function. Needs no Django.

| Argument | Default | Meaning |
| --- | --- | --- |
| `search_path` | project root | directory to scan |
| `follow_calls` | `true` | also report blocking reached through a call |
| `max_depth` | `3` | how many calls deep to follow |

Documented in [`async-blocking.md`](async-blocking.md).

---

## `fastapi_exposure`

FastAPI endpoints with no `response_model` and no return annotation, and
declared models carrying a sensitive field. Needs no Django.

| Argument | Default | Meaning |
| --- | --- | --- |
| `search_path` | project root | directory to scan |

Documented in [`fastapi.md`](fastapi.md).

---

## `sqlalchemy_nplusone`

Relationships SQLAlchemy loads one row at a time: in a loop, and during
response serialisation where there is no loop to see. Needs no Django.

| Argument | Default | Meaning |
| --- | --- | --- |
| `search_path` | project root | directory to scan |

Documented in [`sqlalchemy.md`](sqlalchemy.md).

---

## `amplification`

Endpoints that are both reachable without credentials and expensive to answer.
Neither half is a finding alone.

| Argument | Default | Meaning |
| --- | --- | --- |
| `search_path` | project root | directory to scan |

Documented in [`amplification.md`](amplification.md).

---

## `check`

Every check that applies to this project, merged into one severity-sorted
list. The project is profiled first, so a FastAPI project gets the checks that
apply rather than a Django boot error, and what does not apply is reported
with the reason.

| Argument | Default | Meaning |
| --- | --- | --- |
| `tenant_root` | `auth.User` | the model that owns data, for the ownership check |
| `only` | all | run just these checks |
| `skip` | none | run everything except these |

---

## `suggest_fixes`

Findings turned into code, grouped by how safe each one is to apply:
mechanical, generated, advisory.

| Argument | Default | Meaning |
| --- | --- | --- |
| `tenant_root` | `auth.User` | the model that owns data |

Documented in [`fixes.md`](fixes.md).

---

## `celery_arguments`

Model instances handed to Celery tasks, and dispatches whose argument count
cannot match the task.

| Argument | Default | Meaning |
| --- | --- | --- |
| `search_path` | project root | directory to scan |

The worker receives whatever the serialiser made of the instance, rehydrated
later on another machine, so the row may have changed, the whole object
crosses the broker, and under the JSON serialiser it may not encode at all.

A dispatch is only checked when the name resolves to a task this project
defines, through the calling file's own imports.

Documented in [`celery.md`](celery.md).

---

## `queries_in_loops`

Database work written inside a loop, separated into the three shapes that need
three different fixes.

| Argument | Default | Meaning |
| --- | --- | --- |
| `search_path` | project root | directory to scan |
| `include_writes` | `true` | also report `save()`/`delete()` inside a loop |

A loop over a literal list is not treated as a loop over rows for reads, and
tests are skipped.

Documented in [`loop-queries.md`](loop-queries.md).

---

## Resource: `django://models`

The full model graph as JSON, identical to `list_models` with all apps and
fields. It is a resource rather than a tool because it is stable and
addressable: no arguments, same answer every time.
