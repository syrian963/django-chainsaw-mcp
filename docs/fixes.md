# Suggestions and fixes

## The distinction that makes this useful

A report that ends in *add an ownership filter* has done the easy half. The
interesting question is which fixes a machine can write correctly and which it
cannot, and the answer differs per check.

Most tools blur that line, either refusing to suggest anything or offering a
`--fix` that will happily rewrite something it did not understand. Both are
worse than being explicit about it.

| Class | Meaning | Applied by `--write`? |
| --- | --- | --- |
| **mechanical** | one correct answer, derivable from the code alone | **yes** |
| **generated** | a machine writes the artefact, a human decides if it should exist | no, `--write-generated` creates the file to review |
| **advisory** | real code with names resolved, but the decision is about the domain | **never** |

## mechanical

No judgement, no way to be wrong.

```diff
- return Order.objects.filter(placed_at__gte=datetime.datetime.now())
+ return Order.objects.filter(placed_at__gte=timezone.now())
```

`timezone.now()` returns an aware datetime, which is what the ORM compares
against. There is one correct replacement. The import is added if the file does
not have it, exactly once.

## generated

The artefact is exactly right as text and possibly wrong as a decision.

```python
class Migration(migrations.Migration):
    # Concurrent index creation cannot run inside a transaction, and without
    # it the build locks the table against writes.
    atomic = False

    operations = [
        AddIndexConcurrently(
            model_name="product",
            index=models.Index(fields=["name"], name="product_name_idx"),
        ),
    ]
```

Correct migration, `AddIndexConcurrently`, `atomic = False`, the safe form. And
still the wrong thing to run if that table is written far more than it is read,
which the tool cannot know. So it is written to a file for review with the
dependency left as `REPLACE_WITH_LATEST`, which is deliberate: nobody applies a
migration they had to edit without reading it.

## advisory

Real code, in the right place, with the names resolved:

```diff
- return Order.objects.get(pk=pk)
+ return Order.objects.filter(customer=request.user).get(pk=pk)
```

**`request` is read from the enclosing function's signature, not assumed.** A
suggestion of `request.user` inside a function whose parameter is `req` produces
code that does not run, and one of those is enough for somebody to stop reading
the suggestions. When no request argument exists, the tool says so instead of
inventing one:

> No request argument was found in the enclosing function, so there is nothing
> to scope by from here. This may be a manager, a task, or a helper that should
> take the owner as a parameter.

And it names the limit of what it knows:

> `request.user` is the request argument of the enclosing function, but whether
> `customer` points at a user, a profile or an organisation is a question about
> your domain, not your syntax.

## Using it

```bash
django-chainsaw fix --tenant-root myapp.Organisation          # show, change nothing
django-chainsaw fix --tenant-root myapp.Organisation --write  # apply mechanical only
django-chainsaw fix --write-generated                         # write files to review
```

**Nothing is applied without asking.** A plain run prints diffs and exits.

## Safety

Four properties, each covered by `fix_check.sh` rather than promised:

1. **A run without `--write` changes nothing.** Verified by hashing the file
   before and after.
2. **`--write` applies the mechanical class only.** The advisory tenancy fix and
   the serializer rewrite are still in place afterwards.
3. **Generated files appear only with `--write-generated`.**
4. **Applying twice is a no-op**, not a double edit.

There is a fifth property that only shows up when something goes wrong: a fix is
**refused if the line no longer matches** what the analysis saw. Editing a file
between running the check and applying the fix is normal, and silently patching
the wrong line is the failure this prevents.

## What is deliberately not offered

- **Adding `db_index=True` to a model in place.** The migration is generated
  instead, because the index is the decision and the model line is just how it
  is spelled.
- **Inserting `select_related` into a view.** The finding is in the serializer
  or the template; the fix belongs on a queryset in a different file, and
  guessing which one is how a tool corrupts code.
- **Rewriting `fields = "__all__"` automatically**, when the generated list
  contains something sensitive. The list is correct and applying it would freeze
  today's over-exposure into an explicit, reviewed-looking decision. Worse than
  leaving it.

## From an assistant

The `suggest_fixes` MCP tool returns the same three groups as data, with the
classification attached to every entry. That matters more over MCP than on a
terminal: an assistant that applies an advisory fix because it looked like a
diff is worse than one that never suggested anything.
