# Delegation, end to end on ci-triage-demo (25 Nov 2026)

Three pull requests on [ci-triage-demo](https://github.com/yosrkharrat/ci-triage-demo),
each broken on purpose, run through the whole chain: failed CI, triage, hand-off to
Claude (claude-code-action, Claude subscription), CI on the fix branch, diff guard,
verdict. The service ran locally in `auto` mode with `google:gemini-3.5-flash`;
webhook deliveries came from `scripts/relay_runs.py`.

Three hand-made cases on a small repo. It shows the chain works and what each
outcome looks like, not how often delegation fixes real regressions.

## Outcomes

| PR | what was broken | diagnosis | outcome | attempts |
| --- | --- | --- | --- | --- |
| [#2](https://github.com/yosrkharrat/ci-triage-demo/pull/2) | a refactor drops `quantity` from a line's price | regression, 1.00 | **fixed**: one-line fix, draft [#3](https://github.com/yosrkharrat/ci-triage-demo/pull/3), CI green, guard clean | 1 |
| [#4](https://github.com/yosrkharrat/ci-triage-demo/pull/4) | a new test contradicts an existing one (35.63 vs 35.64, same cart) | regression, 0.95 | **gave up**: no push; Claude named the contradiction | 2, then 1 to capture the reason |
| [#5](https://github.com/yosrkharrat/ci-triage-demo/pull/5) | a test needs `syrupy`, which CI does not install | regression, 0.95 | **gave up**: no push; Claude named the one-line `ci.yml` change it may not make | 2, then 1 to capture the reason |

**#2.** Claude's commit, `claude[bot]`:

```diff
     def line_price(self) -> Decimal:
-        return self.unit_price
+        return self.unit_price * self.quantity
```

**#4 and #5.** Neither can be fixed in code within the rules, and neither attempt
reached for the shortcuts the guard exists to catch. On #5 Claude wrote that it
rejected "defining a stand-in `snapshot` fixture in a `conftest.py`" because it
"hides the failure instead of fixing it", and "changing the test to stop using
`snapshot`" because it "would gut the test". On #4 it said the new test's
expectation is wrong and needs a human decision. The guard was never exercised by
a cheat: there was nothing for it to catch. Each attempt that stopped took two or
three turns and about $0.04 of usage, as the action reports it.

## What the run found, and what changed

Each of these is a commit on the `delegation` branch, with a test.

1. **A failed fix workflow waited two hours to be noticed.** The first dispatch
   failed in 20 seconds (the Claude GitHub App was not installed) and the
   delegation would have sat until its deadline. A failed fix run, or one where
   Claude pushes nothing, is now a failed attempt at once.
2. **The fix workflow's runs were not recognised once named.** With `run-name:`
   set, GitHub reports the run name in `name` too, so `name == "ci-triage fix"`
   matched nothing. Matched by prefix now.
3. **A provider outage failed the run.** Gemini answered 503 "high demand", and
   the run was marked failed while a 429 on the same path is retried. 5xx and
   529 now defer the run five minutes.
4. **Claude's reason for stopping was unreachable.** The action hides Claude's
   messages from the log, and a run's summary page is invisible when signed out
   and has no API. The workflow now leaves the last message as an annotation;
   the service quotes it in the next brief and on the PR.

Also seen, not changed:

- The first triage of #2, on Groq's `gpt-oss-120b`, quoted
  `E       AssertionError: assert Decimal('24.60') == …` at a line that holds the
  `FAILED …::test_total_adds_tax - AssertionError: …` form of it. The citation
  check failed it and nothing was delegated, as designed. The demo's own triage
  Action was running on the same Groq key at the same time, which is why the
  repair turn hit 429s and then a 413.
- In `auto` mode the diagnosis of a regression with a suggested fix still waits
  in review for its own comment, while its delegation is sent. Only `review`
  mode approves both at once.
