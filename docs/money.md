# `money_precision`

## Where a decimal amount stops being exact

A `DecimalField` exists so that `0.1 + 0.2` is `0.3`. Every route out of
`Decimal` and back gives that up, and the loss is a fraction of a cent — so it
survives code review, passes the tests, and turns up months later as an invoice
total one cent off a sum nobody can reproduce.

There are four ways out, and they are **not equally bad**:

| | |
| --- | --- |
| `Decimal(0.1)` | wrong from birth: `0.1` has no exact binary form, so this is `0.1000000000000000055511151231257827021181583404541015625` |
| `float(invoice.amount)` | a one-way door — every operation after it is approximate |
| `round(amount, 2)` | exact, but banker's rounding: `0.125` becomes `0.12` where an invoice expects `0.13` |
| `FloatField(name="price")` | the column itself cannot hold money, so no care at the call sites fixes it |

## The distinction that makes it usable

`Decimal(0.5)` is **fine**. `0.5` is exactly representable, so nothing is lost.
`Decimal(0.1)` is not. A check that flags every `Decimal(<float>)` is wrong
about most of them, so this one computes the round trip:

```python
Decimal(value) == Decimal(repr(value))
```

Measured on a large real project: **50 `Decimal(<float>)` calls, of which 41
were exactly representable and harmless, and 11 were genuinely wrong.** Without
that split the check would be 50 findings, 82% of them noise, and switched off
by the end of the week.

The harmless ones are still reported, at **low** severity and hidden unless you
ask — `Decimal(0.0)` is fine today and one edit away from `Decimal(0.05)`.

## What it looks like

```
24 place(s) where a decimal amount stops being exact

  HIGH   apps/group/views/bookings.py:2455  [float_of_decimal]
         "purchase_price": float(entry.purchase_price),
         'purchase_price' is a DecimalField, and float() is a one-way door:
         every operation after this one is approximate
         fix: keep it a Decimal; if a float is genuinely needed, convert at the
              last possible moment and never convert back

  HIGH   shop/pricing.py:17  [decimal_from_inexact_float]
         return Decimal(0.19)
         Decimal(0.19) is actually 0.190000000000000002220446049250313080847263336181640625,
         because 0.19 has no exact binary form. It is wrong before anything is
         done with it
         fix: Decimal("0.19")
```

## In CI

```bash
django-chainsaw money --fail-on-findings
```

Exit 1 on any high-severity finding; low ones never fail the gate. Also in the
aggregate `check` as `money`, where low findings are excluded entirely.

## How `float()` and `round()` are matched

Resolving what `invoice.amount` refers to would need type inference. Instead,
every `DecimalField` **name** in the project is collected, and an attribute
access ending in one of those names counts as money.

So `float(order.quantity)` is left alone when no model has a decimal
`quantity`, and `float(x.price)` is reported even when `x` is not a model —
because `price` is a decimal column somewhere. The trade is deliberate: the
false positive costs a glance, the false negative costs a cent per booking.

The `FloatField` check is the exception. There is no `Decimal` to key off, so it
matches money-shaped names directly — `price`, `amount`, `total`, `betrag`,
`preis`, `brutto`, `netto` and the rest — in English and German.

## What it cannot see

- **Arithmetic that stays in floats the whole way.** If a value never touches a
  `Decimal`, nothing here connects it to money.
- **Which rounding rule is correct for you.** `round()` is flagged as *a*
  rounding decision made implicitly, not as a wrong one. Some totals genuinely
  want half-even.
- **`Decimal` built from a float held in a variable.** Only a literal, or a
  visible `float(...)` call, is decidable at the point of the call.
- **Currency mixing.** Adding EUR to USD is exact and still wrong; nothing here
  looks at units.
