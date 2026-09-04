# Baselines

## The adoption problem every analyser has

Point `tenancy` at a five year old codebase and it returns two hundred
candidates. Nobody reads two hundred candidates.

What happens next is predictable. The gate goes in, the build turns red on the
first pull request, somebody adds `continue-on-error: true`, and from then on
the tool runs on every build and nobody looks at it. That is worse than not
having it, because it costs CI minutes and buys a false sense of coverage.

This is the same failure the `deploy_safety` documentation describes for
migration linters: **a tool that flags everything gets ignored.** Ratcheting is
how you avoid it.

## What a baseline does

Record what the project looks like today. From then on, fail only on findings
that were not there before.

- the existing two hundred stay in the report and stop blocking anybody
- anything new fails the build, with file and line numbers
- fixing an old one is reported, so the file can be regenerated and the number
  goes down

The count can only ever decrease, because the baseline is rewritten when
somebody fixes something. That is the ratchet.

## Using it

```bash
# once, to record where you are today
django-chainsaw tenancy --tenant-root shop.Customer --baseline --update-baseline

# from then on, in CI
django-chainsaw tenancy --tenant-root shop.Customer --baseline
```

`--baseline` with no value uses `.django-chainsaw-baseline.json`. Pass a path to
put it somewhere else.

**Commit the file.** It is a record of what was already wrong, and it belongs in
review: a pull request that regenerates it is either fixing something, which is
visible as removed entries, or hiding something, which is visible as added ones.

Supported by `tenancy`, `n+1` and `deploy-safety`. One file holds all three, and
recording one check leaves the others untouched.

## What it looks like

```
Against .django-chainsaw-baseline.json (recorded 2026-09-04T09:31:00+00:00):
1 new, 4 known, 1 fixed

Fixed since the baseline, regenerate it to lock this in:
  - shop/api.py  shop.Order unscoped via all

NEW findings, not in the baseline:
  medium  shop/leaky.py:5
          shop.Invoice unscoped via filter.first
```

Exit `1`, because of the one new finding. The four known ones were reported and
did not block.

## Fingerprints, and why the line number is not in them

A finding is matched on **file plus identity**, never on the line it sits at.
Identity is the part that describes the defect: for `tenancy` it is the model,
the queryset chain and the filter keys; for `n+1` the template expression; for
`deploy-safety` the operation and the symbol it removes.

Adding an import at the top of a file moves every finding below it down a line.
If the line number were part of the fingerprint, that commit would resurrect
twenty findings nobody touched, the build would go red for no reason, and the
baseline would be regenerated in irritation rather than reviewed. Once that
happens twice, nobody trusts the file.

The trade is real and worth naming: two findings of the same shape in the same
file are one fingerprint. Fixing one of them will not show as fixed until both
are gone. Under-reporting a fix is a much cheaper error than crying wolf on
every reformat.

## Adopting this on a real project

1. Run each check without a baseline and read the output once. Some of it will
   be wrong, because these are candidate-producing tools; some of it will not.
2. Fix whatever is quick.
3. Record the baseline for what is left.
4. Turn the gate on in CI.
5. Regenerate the file whenever findings are fixed, in the same pull request
   that fixes them.

Step 1 is not optional. A baseline recorded without reading it is a list of
accepted bugs, and calling it a baseline does not change that.
