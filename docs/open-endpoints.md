# `open_endpoints`

## Two harmless facts

```
CustomerExportSerializer exposes password_reset_token.
PublicCustomerExport has permission_classes = [AllowAny].
```

The first is what `serializer_exposure` reports, and on its own it is a
judgement call — maybe that serializer only ever feeds an admin export. The
second is what a Semgrep rule reports, and on its own it is also a judgement
call — maybe that view serves a product catalogue.

Put them together and there is nothing left to judge. Anyone on the internet can
fetch every customer's password reset token.

Neither check can make that connection alone. The serializer does not know
which views use it; the view does not know what its serializer returns. The
connection is the finding, and it is the whole of what this check does.

## What it looks like

```
Default permission: AllowAny (DRF's built-in default, because the setting is not configured)
  -> every view without its own permission_classes is public.
13 view(s): 11 open, 1 protected, 1 deciding at runtime

  CRITICAL  shop.public_api.ImplicitlyOpenCustomers
            permission: AllowAny (DRF's built-in default, because the setting is not configured)
            serializer: shop.reporting_serializers.CustomerExportSerializer
            anyone can call this and the response includes password_reset_token, date_of_birth
            fix: set permission_classes on the view, or if it must be public, give it a
                 serializer with an explicit field list that omits password_reset_token, date_of_birth

  CRITICAL  shop.public_api.PublicCustomerExport
            permission: AllowAny (set on the view)
            ...

  MEDIUM    shop.public_api.PublicCatalogue
            permission: AllowAny (set on the view)
            anyone can call this and the serializer exposes every field on shop.Product
            (exclude=['id']), so the next migration decides what leaks
```

## The default is open

DRF's own default for `DEFAULT_PERMISSION_CLASSES` is `AllowAny`. A project
that never set `REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"]` has **every view
without an explicit `permission_classes`** open to the world, and not one of
those views says so — the code is the absence of a line.

That is why the default is reported before anything else, and why a finding
states **where the permission came from**: `set on the view` is a decision
somebody made; `DRF's built-in default` is the absence of one. Both are
critical when the serializer leaks; only one of them will be a surprise to the
team.

`ImplicitlyOpenCustomers` in the output above has no `permission_classes` line
at all. That is the shape most real leaks have.

## Severity

| | |
| --- | --- |
| **critical** | open, and the serializer exposes a field that looks sensitive |
| **medium** | open, and the serializer uses `__all__` or `exclude` — nothing sensitive today, but the next migration decides |

The medium tier can be dropped with `--sensitive-only` / `include_unbounded=false`.

## Listed, not judged

- **`get_permissions()` overrides.** The view chooses at runtime; it might be
  `IsAuthenticated` for `list` and `AllowAny` for `retrieve`. It is listed under
  `views_deciding_at_runtime` and produces no finding, because a finding would
  be a guess.
- **View modules that fail to import.** A view no URLconf reaches in this
  environment is imported the same way unreached serializer modules are. One
  that fails is listed with the error, because a module that cannot be seen is
  a module whose views cannot be checked — and hiding that would turn an
  environment problem into a clean report.

## In CI

```bash
django-chainsaw open --fail-on-findings
```

Exit 1 on any critical finding. Also in the aggregate `check` as `open`.

## What it cannot see

- **Routing.** A view class nobody wired into a URLconf is still checked, so an
  abstract base `TenantScopedViewSet` appears among the open views. It has no
  URL; it also has no `permission_classes`, and every subclass inherits that.
- **`get_serializer_class()`.** A view choosing its serializer at runtime is
  checked against `serializer_class` only, which may be the wrong one.
- **`authentication_classes`.** A view with `IsAuthenticated` and an empty
  authenticator list rejects everyone; that is a different bug and is not
  judged here.
- **Object-level permissions**, `has_object_permission` and friends. They gate
  which rows, not which fields, and a public list endpoint runs before they do.
- **What the sensitive-name list misses.** A field called `notes` holding
  medical history is invisible; a field called `token` that is a public share
  link is a false alarm. The list lives at the top of `serializers.py`.
