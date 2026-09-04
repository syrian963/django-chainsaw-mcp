# `deploy_safety`

## The problem a linter cannot solve

`django-migration-linter` classifies a migration operation in isolation.
`RemoveField` is backward incompatible, so it warns. Always.

That is correct and, repeated often enough, unread. The warning fires just as
loudly for a column nobody has touched in two years as for one half the codebase
selects. A tool that calls everything dangerous gets an ignore rule written for
it within a month.

The question that actually decides the deploy is not *is this operation
dangerous in principle*. It is **has the code caught up yet?**

## Why it matters during a rolling deploy

A rolling deploy replaces pods one at a time. For the length of that window, the
new schema is already live and the old code is still serving traffic.

```
   t0   old pods          old schema        fine
   t1   old pods          NEW schema        ← the dangerous window
   t2   mixed             NEW schema        ← still dangerous
   t3   new pods          NEW schema        fine
```

If a migration drops `Product.legacy_code` and any old code path still selects
it, every request through that path throws for the whole of t1 and t2. If
nothing references it any more, the same migration is boring and can ship today.

Same operation. Opposite verdict. The difference is not in the migration, it is
in the rest of the repository.

## What it does

For every unapplied migration that removes or renames something, the source
tree is searched for code that still refers to it.

```
BLOCKING shop.0002_remove_product_legacy_code  RemoveField 'legacy_code'
     shop/services.py:17  [string field name]  values("id", "sku", "legacy_code")
     shop/services.py:22  [keyword argument]   filter(legacy_code__startswith=code)
     shop/services.py:27  [attribute access]   f"{product.sku} / {product.legacy_code}"
     shop/services.py:32  [keyword argument]   Product(sku=sku, legacy_code="")

     safer: ship a release that stops using it, deploy that everywhere,
            then ship this migration.
```

and, just as importantly:

```
CLEAR    shop.0002_remove_product_legacy_code  RemoveField 'legacy_code'
         no remaining reference found outside migrations
```

**Being able to say "this one is fine" is the whole point.**

## How references are found

Python is parsed with `ast` and matched structurally, not searched as text:

| Code | Node | Reported as |
| --- | --- | --- |
| `product.legacy_code` | `Attribute` | attribute access |
| `values("legacy_code")` | `Constant`, exact match | string field name |
| `filter(legacy_code__gt=…)` | `keyword`, `name__` prefix | keyword argument |
| `Product(legacy_code="")` | `keyword` | keyword argument |
| `"legacy_code__id"` | `Constant`, lookup shape | orm lookup in string |

A docstring is a single `Constant` holding a sentence, and only an exact string
match counts, so prose mentioning the field cannot register. Comments are not in
the tree at all.

Templates have no comparable structure, so they use patterns with `{# … #}`
comments removed first.

Migration files are always excluded: they legitimately name the thing they
remove.

## Scoping

**Only apps inside the project are analysed.** `django.contrib` field names are
generic, its migrations are not a deploy decision the user makes, and scanning
for them produced the first false positives this tool ever had. Override with
`include_third_party=true`.

**Generic names are marked, not suppressed.** A symbol in the common set
(`name`, `id`, `type`, `status`, `code`, …) is still analysed but returns
`confidence: low` with a reason, because an unscoped match on `name` says very
little.

## Using it as a CI gate

```bash
django-chainsaw deploy-safety
```

Exits `1` when anything is blocking, `0` when everything is clear, `2` when the
project could not be loaded.

```yaml
- name: Migrations must not break the running code
  run: django-chainsaw deploy-safety
  env:
    DJANGO_CHAINSAW_PROJECT_PATH: .
    DJANGO_CHAINSAW_SETTINGS_MODULE: myproject.settings
```

The expand-and-contract deploy pattern becomes enforceable rather than
remembered: a branch that removes a column before the code stops using it fails
the pipeline, with the file and line numbers that have to change first.

## What it cannot see

- `getattr(obj, field_name)` and anything else resolved at runtime
- raw SQL built from strings
- references in another repository, a template rendered by a different service,
  a stored procedure, a BI query
- a field name reused for an unrelated purpose elsewhere in the same project,
  which is why generic names are flagged as low confidence

**`clear` means nothing was found here, not that nothing exists anywhere.** It
narrows the question; it does not remove the need to know your own system.
