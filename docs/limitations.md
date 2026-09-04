# What this cannot do, and why

Every check here states its own blind spots in its own output. This page is the
consolidated version, kept for one reason: **a confident wrong answer is worse
than an incomplete one**, and the fastest way to produce confident wrong answers
is to lose track of what the tool is actually guessing at.

The list is short, because most of what used to be on it has been closed. What
is left is on it because it is genuinely undecidable from source, not because
nobody got to it yet.

## Closed, and how

These were limitations and are not any more. They are listed so the difference
between "cannot" and "did not" stays visible.

| Was | Now |
| --- | --- |
| a transaction opened in another module | followed through the call graph, with the path shown |
| a project's own task-dispatch wrapper | followed into, no configuration needed |
| `ATOMIC_REQUESTS` views not enumerated | enumerated in their own tier |
| a scoping filter in a base class or mixin | resolved through the class hierarchy |
| a default manager that narrows | read from the manager's `get_queryset` |
| a filter further down the same function | read from the whole function body |
| a permission check after the fetch | labelled on the finding |
| serializers nothing imports | imported before the walk |
| whether a view serves a serializer | cross-referenced, nesting included |
| an overridden `save()` in the signal chain | walked through the MRO |
| a receiver connected twice | counted |
| queries inside a `SerializerMethodField` | counted from the method body |
| `only()` dropping a rendered column | costed as the N+1 it is |
| a write behind `if created:` | marked conditional |

## Undecidable from source

### What a function returns at runtime

`get_full_name(self, obj)` can return a string today and a dict tomorrow. That
is a breaking API change and no amount of reading the class definition finds it.
The method's **query cost** is counted, because that is a property of its body.
Its **return shape** is not, because that is a property of its execution.

Same for `to_representation`: its presence is reported, its effect is not.

### How many children a parent has

The single number that dominates a query estimate is the fan-out, and it is a
property of your data. Fifty orders with three lines each is 150 rows; with
thirty each it is 1500. So it is a parameter with a stated default, and the
output says the **ratio between two endpoints is the reliable part** while the
absolute number is only as good as the assumption.

### Whether a cache hits

A `cached_property`, a Redis layer, a `get_or_set`. The query count depends on
the hit rate, which depends on traffic. Where a cache is detected inside a
method field, the endpoint says so and stops counting rather than inventing a
number.

### Whether `.all()` on a related manager is free

It costs nothing if the manager was prefetched and one query if it was not, and
which of those happened depends on a queryset built somewhere else, possibly at
runtime. Method-field costs are therefore an **upper bound**, stated as one.

### SQL built from strings

`cursor.execute(f"SELECT ... {column}")` is opaque. So is a stored procedure, a
BI query, a report in another service. `deploy_safety` saying **clear** means
nothing was found *here*; it narrows the question, it does not remove the need
to know your own system.

### Attributes resolved at runtime

`getattr(obj, field_name)` where `field_name` is computed. The field is used and
nothing in the source says which one.

### `Q()` objects and positional filter arguments

Their contents cannot be read statically, so a query chain that uses one is
treated as **scoped**. That is a deliberate bias towards silence: on an
authorisation check, one missed candidate costs less than a check that cries
wolf on every complex query and gets switched off.

### Receivers connected after startup

The signal registry is read once, after `django.setup()`. A receiver connected
later — inside a request, a test fixture, a conditional import — is not in it.

### Another repository

A second service reading the same database, a shared client library, a mobile
app compiled last year. Nothing in this repository knows they exist. This is the
real limit on `api_contract`: it tells you what **changed**, not who was reading.

### Whether a skipped effect was wanted

`bypassed_effects` says a `bulk_create` skipped the audit receiver. An import
that deliberately skips the audit trail looks identical to one that forgot. The
finding states what did not happen; whether that is a bug is a decision, and
the honest fix for the deliberate case is a comment at the call site.

### A lock held some other way

`race_conditions` sees `select_for_update()` and `F()`. An advisory lock, a
distributed lock, or a queue with concurrency 1 around the read-modify-save is
just as safe and looks exactly like the race.

### Routing

`open_endpoints` checks every view class, because whether a class is wired into
a URLconf is not knowable from the class. An abstract base view with no URL
therefore appears among the open views. It also has no `permission_classes`,
and every subclass inherits that, so it is not wasted information.

## Two things that are not limitations

**Unresolved calls in the call graph.** Most of them are the standard library,
third-party packages and the ORM. None are project functions and none belong in
the graph. The count is reported so a resolution rate falling off a cliff is
visible, not because the number should be zero.

**A suppressed finding.** Anything ruled out is reported with the grounds, under
`scoped_elsewhere` or an equivalent. A suppression without a stated reason is
just hiding a finding, and the grounds can be wrong — a `get_queryset` filtering
on the *wrong* tenant satisfies every test here and is still a leak. The list is
there to be read.
