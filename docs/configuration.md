# Configuration and suppression

## Settings live in pyproject.toml

`--tenant-root myapp.Organisation` is not a preference. It is a fact about the
project that never changes. Requiring it on every run means it ends up in a
shell alias on one laptop, missing from CI, and wrong in the README.

```toml
[tool.django-chainsaw]
tenant-root = "myapp.Organisation"
fail-on = "high"
ignore = ["*/migrations/*", "legacy/**"]
skip = ["migrations"]
baseline = ".django-chainsaw-baseline.json"
```

`pyproject.toml`, because a Python project already has one and nobody needs to
learn a new file. The nearest one with a `[tool.django-chainsaw]` section wins,
searching upwards from the project path.

| Key | Default | |
| --- | --- | --- |
| `tenant-root` | `auth.User` | the model that owns data |
| `fail-on` | `high` | severity that makes `check` exit 1 |
| `ignore` | none | glob patterns for paths where findings are expected |
| `skip` | none | checks not to run |
| `baseline` | none | path to the baseline file |

**A command line flag always wins.** *Why is it using the wrong tenant root* is
a bad afternoon, and the answer has to be visible in the command that was typed.

Ignored findings are **counted out loud**:

```
3 finding(s) hidden by the ignore patterns in /srv/app/pyproject.toml
```

A number that silently drops is how a config file becomes a place where
problems go to be forgotten.

## Inline suppression, with a mandatory reason

```python
return Order.objects.get(pk=pk)  # chainsaw: ignore[tenancy] - permission class checks the owner
```

On the finding's own line or the line directly above it. Naming checks in
brackets limits it to those; leaving the brackets off suppresses everything on
that line.

```
1 finding(s) suppressed in the source:
  shop/api.py:14  permission class checks the owner
```

### The reason is not optional

```python
return Order.objects.all()  # chainsaw: ignore[tenancy]
```

```
Suppressions that did NOT take effect:
  shop/api.py:19  a suppression needs a reason:
                  # chainsaw: ignore[check] - why this one is fine
```

**The finding stays.** A suppression with no reason is refused, and refused
loudly rather than quietly doing nothing, so the difference between "rejected"
and "the matcher is broken" is visible.

This is deliberate and it is the one place the tool is opinionated about
process rather than code. A `# noqa` with no explanation is a decision somebody
made in a hurry, and in a year it is indistinguishable from a bug nobody
noticed. The reason is the only part still worth reading by then, and requiring
it costs eight words.


## A library is not a project

This needs an importable settings module, and a library often has none.
django-rest-framework configures Django in `tests/conftest.py` with
`settings.configure(...)`, which is the normal way to boot the framework for a
library's own test run — so there is nothing to put in
`DJANGO_CHAINSAW_SETTINGS_MODULE`, and the error says that rather than telling
you to set a variable that has no value for this repository.

The checks that need no Django still run: framework detection, and the
`async` walk. The other twenty report that they could not, with the reason.

To analyse a library's effect on real code, point this at an application that
uses it.

## Which mechanism for which situation

| | |
| --- | --- |
| **one line that is genuinely fine** | inline suppression with a reason |
| **a whole directory that will never be clean** | `ignore` in the config |
| **hundreds of findings you intend to fix eventually** | a [baseline](baseline.md) |
| **only what this branch introduced** | [`--since main`](cli.md) |

They stack. A realistic setup on an old project is `ignore` for vendored code,
a baseline for the backlog, and `--since` on pull requests, with inline
suppressions for the handful of cases a person looked at and accepted.
