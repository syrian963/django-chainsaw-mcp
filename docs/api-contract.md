# `api_contract`

## The change that looks like tidying up

```diff
 class InvoiceSerializer(serializers.ModelSerializer):
     class Meta:
         model = Invoice
-        fields = ["id", "number", "internal_note"]
+        fields = ["id", "number"]
```

Nothing in that diff says a mobile client on a version nobody updated reads
`internal_note`. Nothing in the review says it either.

Every tool that catches this needs the application running.
`drf-api-checker` records real responses during a test run and compares later
ones against them, so it covers exactly what the tests happen to exercise.
Diffing OpenAPI schemas needs the schema generated, which needs the app booted
and the generator configured.

None of that is necessary. A `ModelSerializer` resolves its own fields at
import time. The names, the types, whether each is read only, required or
nullable, and what the nested ones expand to. That is the promise, and it can
be captured, committed and compared without sending a request.

## What it looks like

```bash
django-chainsaw contract --update      # record today's shape, then commit it
django-chainsaw contract               # what has this branch changed?
```

```
3 change(s) since the snapshot: 2 breaks clients, 1 safe

  BREAKS CLIENTS
    shop.api_serializers.InvoiceSerializer.internal_note
        removed - every client reading this field stops getting it
    shop.api_serializers.OrderSerializer.coupon
        added, required - an existing writer that does not send it now gets a 400
  safe
    shop.api_serializers.OrderSerializer.label
        added - a new optional field breaks nobody
```

Look at the middle one. `coupon` is an **addition**, and it is the change most
likely to page somebody. A diff of field names would put it in the same bucket
as `label`. The classification is the part that matters, not the list.

## How each change is classified

| | Change | Why |
| --- | --- | --- |
| **breaking** | a readable field is removed | every reader loses it |
| | a field becomes `write_only` | it is gone from responses, same thing |
| | a new **required** writable field | existing writers now get a 400 |
| | an existing field becomes required | same |
| | the field type changes | the client's parser may not survive it |
| | a choice is removed | a client still sending it gets a 400 |
| | `max_length` shrinks | values that were accepted now are not |
| **risky** | a field becomes nullable | it still parses, but the value may surprise |
| **unreadable** | the serializer no longer instantiates | see below |
| **additive** | a new optional field | nobody notices |
| | a required field becomes optional | relaxing never breaks |
| | a `write_only` field is removed | no reader depended on it |

The whole classification rests on one assumption, and it is the same one every
API version policy rests on: clients read what they were given and send what
they always sent.

## Removed and unreadable are not the same thing

The first version of this reported two serializers as **removed** when they had
not been touched. They had stopped *instantiating*, because a field named in
`Meta.fields` no longer existed on the model. From the outside the two look
identical: the serializer is missing from the capture either way.

Reporting that as a removal is a confident wrong answer, which is worse than no
answer. So an unresolvable serializer gets its own verdict:

```
  COULD NOT BE READ
    shop.api_serializers.InvoiceSerializer
        no longer resolves (ImproperlyConfigured) - the class is still defined
        but instantiating it raised, so its contract could not be compared
```

It fails the gate too. An unknown is not a pass.

## No database, on purpose

The point of resolving the contract from the class definitions is that it works
on a bare checkout: no migrations applied, no database, no server.

That is easy to lose. Reading `.choices` off a `PrimaryKeyRelatedField`
iterates its queryset, which opens a connection — and the first version did
exactly that and died on `no such table: shop_category`. Related fields now
report the model they point at instead, which is more stable anyway and is
already known. `contract_check.sh` asserts the demo project has no database, so
a regression fails there rather than in somebody's CI.

## In CI

```bash
django-chainsaw contract --fail-on-breaking
```

Exit 1 if anything breaks a client or could not be read, 0 otherwise. When a
breaking change is deliberate — you bumped the version, you told the consumers —
record the new shape and commit it:

```bash
django-chainsaw contract --update
```

The snapshot is a normal file in the repo, so the decision to break a client
shows up in a diff, with a name attached, in a review. That is most of the
value. The gate is the reminder; the commit is the record.

## Arguments

| Argument | Default | |
| --- | --- | --- |
| `--snapshot` | `.django-chainsaw-contract.json` | the committed contract |
| `--update` | off | overwrite it with the current shape |
| `--max-depth` | `3` | how far to expand nested serializers |
| `--fail-on-breaking` | off | exit 1 on breaking or unreadable |

## Which of these does anybody actually read?

Every serializer carries the views that name it, and the ones nothing reaches
are listed separately:

```json
"served_by": ["shop.viewsets.SlowOrderViewSet"],
"unserved":  ["shop.api_serializers.CustomerSerializer"],
"views_choosing_at_runtime": []
```

This does not suppress anything. A breaking change to an unserved serializer is
still breaking; it is just far less likely to reach a client, and that is worth
knowing before anyone spends a version bump on it.

Two things keep it honest. **Nesting counts as served**: `OrderLineSerializer`
is named by no view and rendered on every order response because
`OrderDetailSerializer` embeds it, so calling it unserved would invite deleting
a field a client is reading. And a view listed under `views_choosing_at_runtime`
overrides `get_serializer_class`, so it can return anything — while one of those
exists, "nothing serves this" is a probability, not a fact, and the output says
so.

## What it cannot see

- **Inside a `SerializerMethodField`.** It appears with its declared type and
  nothing about what it returns, because that is a runtime question. Changing
  what the method returns is a breaking change this cannot detect.
- **`to_representation` overrides**, and anything else that reshapes the output
  after the fields have had their say.
- **Fields added or dropped per request.** A serializer whose `get_fields`
  depends on `self.context` is captured in whatever shape it takes with no
  context, which is one of several real shapes.

These are all cases of not seeing a change. None of them produce a wrong
verdict about a change it does see.
