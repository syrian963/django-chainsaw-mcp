# Performance

## Measured, not assumed

Everything was built against a demo project with eight models and eleven files.
That says nothing about a real codebase, so `benchmark.sh` generates one and
times the commands against it.

**2822 Python files, 1500 models, 60 apps:**

| Command | Time |
| --- | --- |
| `models --short` | 1.4 s |
| `deploy-safety` | 1.4 s |
| `tenancy` | 2.3 s |
| `datetimes` | 2.3 s |
| `indexes` | 3.3 s |
| `check` (everything) | **5.4 s** |
| `explain` one model | **5.1 s** |

```bash
bash benchmark.sh 60 25 45      # apps, models per app, view modules per app
```

Five seconds for a full analysis of a large project is fine in CI and fine at a
terminal.

## The number that was not fine

`explain` on a **single** model took 5.1 seconds, which is almost the entire
cost of analysing the whole project.

The reason is structural: `explain_model` runs the ownership, exposure, index
and datetime checks so it can correlate them, and every one of those walks the
whole tree. Asking about ten models cost fifty seconds of repeating the same
four analyses.

Over MCP that is the normal case, not an edge case. An assistant asked about one
model will be asked about the next one, and the tool would feel broken on
exactly the workflow it was built for.

## The cache

Project-wide analyses are memoised for the life of the process:

```
shop.Order           360 ms      <- cold
shop.Invoice           3 ms
shop.OrderLine         4 ms
shop.Product           3 ms
shop.Customer          2 ms

cache: {'hits': 16, 'misses': 4, 'invalidations': 0}
```

**About a hundred times faster from the second model onwards.**

Model-specific work is not cached, because it is cheap and because caching it
would need a second invalidation key for no gain.

### Invalidation

By **file modification time**, not content hash. Hashing 2822 files to decide
whether to skip five seconds of work is its own kind of slow, and mtime is what
build tools rely on for the same reason.

A changed tree drops the **whole** cache rather than one entry. These analyses
are project-wide, so one edited file can change any of them, and tracking which
would cost more than recomputing.

The fingerprint is a `stat` per watched file and no reads: a few milliseconds
against seconds of analysis, cheap enough to run before every lookup.

### Verified, not asserted

`cache_check.sh` runs against a copy of the demo project:

```
ok  a second model is much faster (369ms then 2ms)
ok  the cached run answers about the model asked for
ok  cached ownership is correct
ok  editing a file invalidates the cache
ok  the finding from the new file is present after invalidation
ok  the recomputed answer is not the stale one
```

The last three matter more than the first. **A cache that does not invalidate is
a bug that reports yesterday's findings with today's confidence**, which is
worse than being slow, and it is the failure that hides for months because
everything looks fast and nothing looks wrong.

## Where the time goes

Almost all of it is `ast.parse` on every Python file. There is no way around
reading the source, and the AST is what makes the analysis precise rather than
a text search, which is a trade already made deliberately in `deploy_safety`.

The CLI runs one command and exits, so the cache does nothing there. It exists
for the MCP server, which is a long-lived process answering many questions
about one project. That is where the repetition is.
