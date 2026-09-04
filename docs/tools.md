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

## Resource: `django://models`

The full model graph as JSON, identical to `list_models` with all apps and
fields. It is a resource rather than a tool because it is stable and
addressable: no arguments, same answer every time.
