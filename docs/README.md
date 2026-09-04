# django-chainsaw-mcp documentation

Start at the row that matches what you are trying to do.

## Getting started

| | |
| --- | --- |
| **[Quickstart](quickstart.md)** | five minutes from clone to first finding |
| [Installing against a real project](usage.md) | the environment constraint that decides everything, Docker, troubleshooting |
| [Wiring it into an AI tool](clients.md) | Claude Code, Cursor, VS Code, Windsurf, Zed |

## Using it

| | |
| --- | --- |
| [Command line](cli.md) | every subcommand, exit codes, a CI workflow |
| [Tool reference](tools.md) | every MCP tool, its arguments and its output |
| [Suggestions and fixes](fixes.md) | findings as real code, and which are safe to apply |
| [Baselines](baseline.md) | adopting this on a codebase that already has findings |

## The checks, in depth

Each page explains what the check is for, how it works, and what it cannot see.

| | |
| --- | --- |
| [`deploy_safety`](deploy-safety.md) | is this destructive migration safe to ship **yet**? |
| [`find_unscoped_queries`](tenancy.md) | queries that read rows the caller may not own |
| [`what_happens_on`](signals.md) | what a save actually triggers, several hops away |
| [`missing_indexes`](indexes.md) | fields seeked on without an index |
| [`datetime_audit` and `serializer_exposure`](datetimes-and-serializers.md) | two defects that are correct today and wrong later |
| [`endpoint_cost`](endpoint-cost.md) | how many queries one request costs, before anybody sends one |
| [`api_contract`](api-contract.md) | what this branch changes about the API, and who it breaks |
| [`escaping_side_effects`](on-commit.md) | emails and tasks fired inside a transaction that can still roll back |
| [`bypassed_effects`](bypass.md) | bulk writes that skip everything the save() chain promised |
| [`race_conditions`](concurrency.md) | counters changed in Python and saved, and locks with no transaction |
| [`open_endpoints`](open-endpoints.md) | sensitive fields on endpoints anybody can call |
| [`unused_eager_loading`](overfetch.md) | joins and prefetches nothing in the response reads |
| [`money_precision`](money.md) | decimal amounts that stop being exact |
| [`blocking_in_async`](async-blocking.md) | synchronous calls that stop the whole event loop |
| [`fastapi_exposure`](fastapi.md) | endpoints that serialise more than they declare |
| [`sqlalchemy_nplusone`](sqlalchemy.md) | relationships loaded one row at a time |
| [`amplification`](amplification.md) | endpoints anyone can call that cost a great deal |

## Understanding it

| | |
| --- | --- |
| **[What it cannot do](limitations.md)** | the consolidated blind spots, and which were closed |
| [Architecture](architecture.md) | how it fits together, and why the bootstrap drives the design |
| [Authorship and licence](authorship.md) | AGPL, signed commits, what actually protects the work |
| [Contributing](../CONTRIBUTING.md) | setup, test style, how to add a tool |
| [Changelog](../CHANGELOG.md) | including every bug found and what it looked like |

---

## The one idea

Several Django MCP servers exist. They answer **what exists**: list the models,
dump the schema, run the ORM, read the settings.

Every check here answers a question with a consequence attached:

| Question | Check |
| --- | --- |
| What does this delete take with it? | `delete_impact` |
| Where are the N+1 queries? | `find_n_plus_one`, `serializer_nplusone` |
| Which template loop renders a partial that queries? | `scan_templates` |
| Which migration stops writes or breaks a rolling deploy? | `migration_risk` |
| Is this destructive migration safe to ship **yet**? | `deploy_safety` |
| Which queries read rows the caller does not own? | `find_unscoped_queries` |
| What does this save actually trigger? | `what_happens_on` |
| Which fields does the code seek on without an index? | `missing_indexes` |
| What will one request to this endpoint cost? | `endpoint_cost` |
| Which clients does this branch break? | `api_contract` |
| Which sends cannot be taken back if this rolls back? | `escaping_side_effects` |
| Which bulk writes skip the effects the model promised? | `bypassed_effects` |
| Which counters lose updates under load? | `race_conditions` |
| Which public endpoints return something sensitive? | `open_endpoints` |
| Which joins does this pay for and never read? | `unused_eager_loading` |
| Where does money stop being exact? | `money_precision` |
| Which call stops the event loop for every request? | `blocking_in_async` |
| Which endpoint returns every column it was handed? | `fastapi_exposure` |
| Which relationship loads one row at a time? | `sqlalchemy_nplusone` |
| Which endpoint can a stranger use to exhaust the database? | `amplification` |
| Which datetimes break when the clock moves? | `datetime_audit` |
| What does the API expose that nobody decided to? | `serializer_exposure` |
| **All of the above about one model, and the risks only visible combined** | `explain_model` |

## The rule every page follows

**A confident wrong answer is worse than an incomplete one.**

Static analysis does not fail by crashing. It fails by producing a plausible
sentence that sends someone in the wrong direction. So every check states what
it cannot see, in its own output and in its own documentation, and several of
them deliberately stay quiet rather than guess.

That is also why the [changelog](../CHANGELOG.md) keeps every bug found during
development. All of them ran without raising anything. Each produced perfectly
reasonable-looking output, and each was wrong.
