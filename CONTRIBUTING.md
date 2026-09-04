# Contributing

## Setup

```bash
uv sync
```

Python 3.12 or newer. Django is a dev dependency: the server needs it importable
from the interpreter it runs in, but it is the *target project's* Django that
matters in real use, so it is not a runtime dependency of the package.

## Tests

```bash
uv run python smoke_test.py      # introspection functions, called directly
uv run python analysis_test.py   # the analysis tools, with assertions
uv run python client_test.py     # the server over the real MCP transport
bash exitcheck.sh                # CLI exit codes
```

`client_test.py` is the one that matters most. The others call Python functions,
which proves nothing about the protocol: it starts the server as a separate
process, speaks stdio to it, and checks what comes back over the wire.

Run all four before committing. They are fast.

## Adding a tool

1. **Put the logic in its own module** under `src/django_chainsaw_mcp/`. Front
   ends stay thin: `server.py` and `cli.py` should contain argument handling and
   printing, nothing else. That separation is why the CLI took an afternoon.

2. **Call `ensure_django()` first.** It is idempotent and thread-safe.

3. **Raise `ValueError` for caller mistakes.** `_guard` in `server.py` turns
   those into `{"ok": false, "error": ...}` so a typo does not kill the server.
   Do not catch broad exceptions inside the module.

4. **Say what the tool cannot see.** Every return value carries a `note`. This
   is not politeness: the failure mode of static analysis is a confident wrong
   answer, and the note is what stops someone acting on one.

5. **Write the failing test first.** Every bug in the README was found by a test
   that asserted a *specific* number or shape, not by one that checked the call
   did not raise.

## Test style

Assertions are specific on purpose:

```python
check(len(refs) == 4, f"expected exactly the 4 real references, got {len(refs)}")
check(3 not in lines, "line 3 is inside the module docstring and must not count")
```

`assert result` would have passed for every bug listed in the README. Each of
them produced output, and the output was wrong.

When fixing a bug, add the assertion that catches it before the fix, watch it
fail, then fix it.

## The demo project

`testprojects/` is a throwaway Django project, deliberately shaped:

- self-referencing FK (`Category.parent`) so cycle handling is exercised
- `PROTECT` on `OrderLine.product` so a blocked delete has a case
- a `OneToOne`, a `ManyToMany` and several reverse accessors
- a template written the way a slow page is written, including a nested loop
- a migration pair that removes `Product.legacy_code`, plus a `services.py` that
  still uses it in four different shapes

Add to it rather than mocking. Every shape in there exists because a bug hid in
its absence.

## Commit messages

Explain the decision, not the diff. `git log --stat` already shows what changed.

If a bug is fixed, say what the wrong behaviour looked like and why it was not
obvious. Several entries in the README came straight out of commit messages.

## Style

- No em dashes.
- Comments explain why, not what.
- German is fine in test output; documentation and code are English.
