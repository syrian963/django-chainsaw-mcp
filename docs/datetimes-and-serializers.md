# `datetime_audit` and `serializer_exposure`

Two checks for defects that are correct today and wrong later. Neither shows up
in review, because in both cases the diff that introduces the problem is not the
diff that contains it.

---

# `datetime_audit`

## The bug that waits for a clock change

With `USE_TZ = True`, Django stores UTC and hands back aware datetimes. Code
that builds its own with `datetime.now()` gets a naive one. Mixing them either
raises or, worse, compares against the wrong instant and keeps going.

It works for ten months. It breaks on the two nights a year the clock moves,
when a day has 23 or 25 hours, and by then nobody connects the wrong report to
the line that wrote it.

## In code

| Written | Why it is wrong | Instead |
| --- | --- | --- |
| `datetime.now()` | naive: local wall clock compared against UTC | `timezone.now()` |
| `datetime.utcnow()` | naive **holding** UTC: looks right, compares wrong | `timezone.now()` |
| `datetime.today()` | `now()` without a timezone | `timezone.localdate()` |
| `datetime.fromtimestamp(v)` | uses whatever zone the server is in | `fromtimestamp(v, tz=timezone.utc)` |
| `datetime(2026, 3, 29, 2, 30)` | a wall clock that may not exist, or may happen twice | `timezone.make_aware(...)` |

`timezone.now()` is recognised as correct, including under an alias, and
`make_aware(datetime(...))` is not flagged. Getting that second one wrong was a
real bug here: the first version of the literal check would have flagged the
correct code, which is the noise that gets a check switched off.

## On model fields

| Written | Why | Instead |
| --- | --- | --- |
| `default=datetime.now` | naive value on every row | `default=timezone.now` |
| `default=datetime(2026, 1, 1)` | evaluated once at import: every row gets the moment the process started | pass the callable, not a call |
| `auto_now=True, auto_now_add=True` | `auto_now` wins, `auto_now_add` is silently ignored | pick one |

The second is the one worth staring at. `default=timezone.now()` with
parentheses is a single value baked in at import time; `default=timezone.now`
without them is called per row. One character apart, and the wrong one looks
fine in review.

## USE_TZ off

If the project runs with `USE_TZ = False`, naive datetimes are the convention
and the code findings are informational. The output says so. The **model**
findings still stand: a default evaluated once at import is wrong either way.

## Serializers nothing imports

This used to be the largest hole in the check, and it pointed the wrong way:
the serializer leaking a password hash is very often the one nobody imported.

Django imports `models.py` and `admin.py` for every app plus whatever the
URLconf reaches. A `serializers.py` used only by a management command, or
behind a lazy import, or in an app whose URLs are not wired up in this
environment, is never imported — so `ModelSerializer.__subclasses__()` cannot
see it and the check reported the project as clean.

Every module whose source declares a serializer subclass is now imported before
the walk, and the result says what happened:

```json
"discovery": {
  "imported": ["shop.reporting_serializers"],
  "already_loaded": ["shop.api_serializers", "shop.viewsets"],
  "failed": []
}
```

A module that cannot be imported goes in `failed` rather than being swallowed.
It usually means the analysis is pointed at the wrong settings, and hiding that
would turn an environment problem into a silently incomplete answer.

## What it cannot see

Datetimes produced by a library, by a parser, or by arithmetic on a value that
was already naive. It reads the source and the field definitions, not the values
that flow through them.

---

# `serializer_exposure`

## The decision that keeps being re-made

```python
class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = "__all__"
```

That was correct when it was written. Then somebody adds
`password_reset_token` to the model, and the API starts returning it.

**There is no diff on the serializer.** Nothing in the review of that migration
mentions the API. The decision to expose the field was made months earlier by
someone who could not have known.

`exclude` has the same shape from the other end: it names what must not go out,
so anything added later goes out by default.

## What it reports

```
high    CustomerSerializer   shop.Customer, mode __all__, 4 fields
          ! password_reset_token  (credential)
          ! date_of_birth  (personal data)

medium  ProductSerializer    shop.Product, mode exclude=[], 5 fields
        Nothing currently looks sensitive, but the next migration decides
        that, not this file.

high    InvoiceSerializer    shop.Invoice, mode explicit, 3 fields
          ! internal_note  (internal only)
        the field list is explicit, which is right, but it names a field
        that looks sensitive

ok      OrderSerializer      (2 fields)
```

Three severities, three different arguments:

- **`__all__` or `exclude` with something sensitive in it now**: high.
- **`__all__` or `exclude` with nothing sensitive yet**: medium. The complaint
  is the pattern, not the current contents.
- **An explicit list that names something sensitive**: high. The list is right;
  a name on it deserves a second look.
- **An explicit list with nothing sensitive**: not reported at all.

## Name matching, not classification

This is the honest limit and it cuts both ways.

A field called `token` might be a public share link. A field called `notes`
might hold medical history and will not be flagged, because nothing in the name
says so. The categories are a prompt for a human, not a verdict.


## A serializer built at runtime is named by where you can find it

`ModelSerializer.__subclasses__()` returns classes made with `type()` as well
as classes somebody wrote. Misago narrows a serializer's field list that way,
in `misago/core/serializers.py`, and the result claims `__module__` as
wherever the factory happened to run — `rest_framework.serializers` — under a
name made of every field it keeps:

    AuthenticatedUserSerializerIdUsernameSlugEmailJoinedOnRank...Subset

Two rules, and the order matters:

- **A class the project binds somewhere is reported, whatever `__module__`
  says,** under the name it is bound as. Misago serves that class as
  `AuthenticatedUserSerializer` from `misago/users/serializers/auth.py`, and
  that string can be opened in an editor. Judging it by `__module__` instead
  suppressed a real finding about the fields it exposes.
- **A class the project binds nowhere is skipped.** A subset built and used
  inline has no file to send anybody to, and the finding against the class it
  was built from already says the same thing.

## What it cannot see

- Fields whose exposure depends on the request, since the check sees the class
  and not the call.
- Fields declared on the class rather than in `Meta`, and
  `SerializerMethodField`, which can expose anything at all.
- Nested serializers, where the exposure belongs to the inner one.
- Whether a view actually uses the serializer.

If DRF is not importable, the check says so and exits cleanly rather than
pretending the project has no serializers. On a project that does use DRF, that
message means the server is running in the wrong environment.
