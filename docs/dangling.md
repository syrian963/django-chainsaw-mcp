# `dangling_references`

## The strings nothing checks

```python
return redirect("order-detial")
return render(request, "shop/order_detial.html", context)
```

```django
{% url 'shop:order-detial' order.pk %}
```

Each of these is a name the framework resolves *while serving a request*. Python
does not check it. The import succeeds, the type checker is satisfied, the test
suite passes — and the first person to take that branch gets `NoReverseMatch` or
`TemplateDoesNotExist`.

That is a different failure from an ordinary typo. It survives review, it
survives CI, and it lands on the path nobody was watching: the error page, the
rarely-taken redirect, the admin action, the PDF export somebody runs at month
end.

## Both sides use the project's own machinery

URL names come from the resolver, walked through every `include()`, so a
namespace is real: `shop:order-detail` is checked as `shop:order-detail`.

Template names go through `get_template()`, so whatever loaders the project
configured are the ones that decide. A template that exists and will not
compile is **not** reported — that is a different problem, and the question
here is whether the file is there.

## `ROOT_URLCONF` is not the only URLconf

A project serving two sites from one codebase picks the URLconf per request,
through `request.urlconf` set by middleware from the host. The names in the
other one reverse perfectly at runtime and are entirely absent from the
resolver this process booted with.

Checking against `ROOT_URLCONF` alone reported **629** working `reverse()`
calls as broken on the first real project this ran against. Every module in the
project that defines `urlpatterns` is now imported and walked, and a name is
only dangling when no URLconf registers it. That took the same project to 173,
and those turned out to be real.

## One cause, not thirty-five findings

`by_name` groups the output. On that project the 173 collapsed into a handful
of causes — most of them a single app whose `urlpatterns` were commented out
years ago while sixty `reverse("package__detail")` calls stayed behind:

```
  HIGH      url       'package__detail'  (35 use(s))
            apps/package/models.py:60
            apps/package/views.py:38
            ... and 33 more
```

Thirty-five lines would have hidden that. One line says it.

## What is deliberately silent

- **Anything not literal.** `render(request, f"shop/{which}.html")` cannot be
  resolved without running the code.
- **`redirect()` with a path or a URL.** It takes a name, a path or a model
  instance, so a literal containing a slash or a scheme is left alone.
- **A template that fails to compile.** It exists, which is the question asked.

## Usage

```bash
django-chainsaw dangling [--search-path DIR] [--skip-templates] [--fail-on-findings]
```

Also in the aggregate `check` as `dangling`.

## Limits

- **A name assembled from parts** — `reverse(f"{app}__detail")` — is invisible,
  and that spelling is common in generic code.
- **A URLconf that will not import** is skipped, which can make its names look
  missing. The count of URLconfs actually read is in the output, and it is the
  honest denominator.
- **`{% url %}` with a variable name** (`{% url view_name %}`) is not checked.
- **A name registered dynamically at runtime** is not in any `urlpatterns` this
  can read.
