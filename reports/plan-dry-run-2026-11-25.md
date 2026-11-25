# Planner dry run on the ci-triage-demo captures (25 Nov 2026)

`ci-triage plan` on the three captured demo runs, reading the source at each
run's commit through the contents and trees APIs. The diagnoses came from the
verdict cache (`google:gemini-3.5-flash`, the same verdicts the end-to-end run
used), so only planner tokens were spent.

| PR | expected | result |
| --- | --- | --- |
| [#2](https://github.com/yosrkharrat/ci-triage-demo/pull/2) `ci-triage-demo__45617743835` | one edit in `src/checkout/cart.py`, verified by `tests/test_cart.py::test_subtotal_adds_every_line` | **as expected**: 1 step, 4 test ids, 6 of 6 checks passed, no repair, 12,554 tokens over 5 requests |
| [#4](https://github.com/yosrkharrat/ci-triage-demo/pull/4) `ci-triage-demo__45620354346` | `feasible: false`: the new test contradicts an existing one | **not run yet**: Gemini free-tier daily quota (20 requests) spent |
| [#5](https://github.com/yosrkharrat/ci-triage-demo/pull/5) `ci-triage-demo__45621615858` | `feasible: false`: CI must install `syrupy`, a `.github/` change | **not run yet**: same quota |

## #2, as the planner returned it

How the plan was made: read the diff; read `src/checkout/cart.py`; listed
`tests`; read `tests/test_cart.py`.

> Goal: Fix Item.line_price in src/checkout/cart.py to return unit_price
> multiplied by quantity.
>
> 1. **edit `src/checkout/cart.py`**: In Item.line_price, return
>    `self.unit_price * self.quantity` instead of `self.unit_price`.
>
> Done when these pass: `test_subtotal_adds_every_line`, `test_total_adds_tax`,
> `test_total_applies_a_discount_before_tax`, `test_an_empty_cart_costs_nothing`
> (all in `tests/test_cart.py`).

This is the one-line change Claude made on the end-to-end run.

## Not a fit: Groq's free tier

Retried on `groq:openai/gpt-oss-120b` (`--planner-model`) while Gemini was out
of quota: refused at 9,802 tokens against the 8,000-token per-request cap. The
planner's tool budget (40k characters) is sized for Gemini, as the service
runs it; it is not meant to fit Groq's free tier.

## Still to do

- #4 and #5 on Gemini once its daily quota resets.
- The end-to-end runs on ci-triage-demo: a new two-file regression that should
  end **fixed** with the plan on the PR and in the brief, and a #5-style case
  that should **give up at the plan** with no Claude run.
