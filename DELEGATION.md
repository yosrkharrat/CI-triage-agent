# Plan: from failed run to verified fix

## Goal

When CI fails on a pull request, ci-triage diagnoses it. If the fix is a code
change, it sends Claude a verified brief, follows the CI runs that come after,
and only reports **fixed** when a mechanical check passes. Everything else gets
the routing it has today.

## What "fixed" means

A run of the **same workflow** (same `workflow_id`) on the **fix branch's latest
commit** completes, and all of these hold:

- every job that failed before now concludes `success` (skipped or missing
  doesn't count);
- no other job in that run fails, so a fix that breaks `lint` while fixing
  `test` is not fixed;
- the diff guard passes (Phase 5);
- the original PR head hasn't moved on in the meantime.

A green checkmark on its own is not proof. A run on an earlier commit of the fix
branch is ignored, and so is a run of a different workflow.

## Scope at first

`regression` only. `dependency` comes later: many of its fixes pin an action
under `.github/workflows/`, which the guard forbids, so they would always end in
`gave_up`. Adding it means allowing `.github/` edits that do nothing but pin an
action version (see Phase 5).

---

## Phase 0: Groundwork and a trigger spike

1. Commit the current dashboard work on its own. It's unrelated to delegation
   and the tree has many uncommitted changes. Then branch to `delegation`.
2. Spike on ci-triage-demo (about an hour, no code in this repo). It has to
   answer **two** questions. The second can change Phase 4.

**Question 1: what starts claude-code-action when the App sends it?**

- **A. `repository_dispatch`** with the brief in `client_payload`; the workflow
  passes it to claude-code-action's `prompt`. Nothing depends on comment parsing
  or bot filtering. Constraints to check:
  - the workflow file must be on the default branch, since that's where
    `repository_dispatch` runs;
  - it must check out `head_sha` explicitly and create the fix branch from it;
  - `client_payload` allows at most 10 top-level keys, so nest the brief;
  - the App needs `contents: write`.
- **B. An `@claude` comment posted by the App.** More visible on the PR, but
  claude-code-action may ignore bot comments unless `allowed_bots` is set.

**Question 2: after Claude pushes, does CI actually run on the fix branch, and
on which event?** It may not, for any of these reasons:

- the workflow only runs `on: pull_request`, or on pushes to `main`, so a push
  to a new branch starts nothing;
- a stacked PR (fix branch → the PR's head branch) won't match
  `pull_request: branches: [main]`;
- if claude-code-action pushes with the workflow's `GITHUB_TOKEN`, GitHub
  deliberately starts no new workflow run from that push. The push must use the
  App's token or the Claude GitHub App's.

Preferred design to test: ci-triage opens a **draft PR from the fix branch into
the PR's head branch**. That gives the follow-up runs a PR to attach to, and the
"fixed" outcome a place to live (the author merges it into their branch). If the
demo repo's `on:` filters block that, record which trigger does work (a push
trigger on `ci-triage/**`, or `workflow_dispatch` sent by ci-triage) and what a
user's workflow must contain for delegation to work.

**Output:**
- the chosen trigger and the App permissions it needs;
- the event that runs CI on the fix branch, and the workflow requirements it
  implies;
- `examples/ci-triage-fix.yml`.

## Phase 1: Data model (`store.py`)

A delegation spans several CI runs, so it gets its own table rather than more
columns on `runs`:

```
delegations
  id, repo, pr_number, installation
  origin_run      → runs.id (the triage that started it)
  workflow_id     (the workflow whose runs count; others on the branch are ignored)
  failed_jobs     (json: names of the jobs that failed in origin_run)
  head_branch, head_sha        (the PR's state when delegated)
  fix_branch      ci-triage/fix-<origin_run>
  fix_pr          (the draft PR number, if Phase 0 picks that design)
  status, attempt, max_attempts (default 2)
  brief           (exactly what was sent)
  last_run_id, guard_result (json), closed_reason
  created_at, updated_at, deadline

CREATE UNIQUE INDEX delegations_one_open ON delegations (repo, pr_number)
  WHERE status IN ('proposed', 'approved', 'dispatched', 'retry');
```

(SQLite doesn't accept `UNIQUE … WHERE` as a table constraint, so the
one-open-delegation-per-PR rule is a partial unique index.)

**Statuses:**
`proposed → approved → dispatched → fixed | retry → dispatched … | gave_up | rejected | stale | superseded`

Every transition is a compare-and-set, the same pattern as `claim_review`, so a
redelivered webhook and the worker can't both move the same delegation.

**Job kinds on `runs`.** Add a `kind` column (`triage | follow_up`, default
`triage`, added through `_ADDED_COLUMNS`). A failed run on a fix branch must not
also be queued as a normal triage. The normal path would look up PRs for
Claude's commit and could comment on the wrong one, and
`UNIQUE (repo, run_id, run_attempt)` leaves room for only one row per run. A
fix-branch run is queued once, as `follow_up`, and the follow-up job calls
triage itself when it needs a verdict.

**Tests:**
- legal and illegal transitions;
- the one-open-delegation-per-PR rule;
- a race test in the style of the existing 8-reviewer one;
- the `kind` column on an old database.

## Phase 2: Eligibility and the brief (pure functions, no I/O)

`eligible(result, run, pull, settings) → (bool, reason)`. All of these must
hold:

- the category is `regression` (and `dependency` later, see Scope);
- every citation verified (`evidence_ok`);
- confidence ≥ `CONFIDENCE_THRESHOLD`, reusing the constant rather than
  repeating 0.70;
- there's an open PR from the same repo: `head_repository == repository`, never
  a fork. `WorkflowRun` has no `head_repository` field today, so add it (the
  webhook payload already carries it). `pulls()` returns bare numbers, so
  eligibility needs the PR's head repo too;
- delegation is enabled;
- no delegation is already open on that PR.

Eligibility doesn't depend on `route()`. A regression with a `suggested_fix`
routes to `HUMAN_REVIEW` today, and that's the common case here.

`render_brief(run, verdict, ctx)`:

- the verified quotes with their job, file and line coordinates, **marked as
  data from the log, not instructions**. Log lines can carry text written by
  whoever controls the test output, and Claude reads them with write access.
  The same-repo rule limits that mostly to people who can already push, but say
  it anyway;
- the failing command, from `TriageContext.failing_command`;
- the files changed in the diff;
- the summary and reasoning;
- `suggested_fix`, marked as an unverified hint;
- the rules:
  - create `fix_branch` from `head_sha` and push there;
  - don't delete, skip, xfail or rename tests;
  - don't edit `.github/` or test configuration;
  - keep the change minimal.

Both are pure functions, tested like `decide` and `render_comment`. This phase
spends no quota.

## Phase 3: Dispatch (`service.py`, `github.py`)

**Settings:** `CI_TRIAGE_DELEGATE = off | review | auto`, defaulting to `off`.
`review` is the first real mode.

**In `process()`, after `decide()`:**
- If eligible, create a delegation that is `proposed` in review mode or
  `approved` in auto mode.
- `process()` only looks up PRs on the `AUTO_POST` path today, so eligibility
  does its own lookup when that one was skipped.
- The diagnosis comment still follows its normal routing.

**One approval, not two.** An eligible verdict with a `suggested_fix` sits in
`awaiting_review` for its comment while its delegation sits in `proposed`.
Approving the delegation also approves the diagnosis: it moves the run out of
`awaiting_review` through `claim_review` and posts the comment. Rejecting the
delegation leaves the diagnosis in review.

**`Pipeline.dispatch(delegation)`** makes the GitHub call chosen in Phase 0 (a
new `GitHubClient.repository_dispatch` or `create_comment`). Then it records
`dispatched` and posts a short note on the PR: "Handed to Claude, branch
ci-triage/fix-…, attempt 1 of 2."

**Endpoints:** `POST /delegations/{id}/approve` and `/reject`, behind the
existing review token.

**Tests:** `FakePipeline` gets a `dispatch()` method that records its calls.
Cover:
- approve → dispatch, and the diagnosis is approved with it;
- reject → nothing sent;
- off mode → no delegation row.

## Phase 4: Follow-through (the "checks until it's fixed" part)

**Webhook change.** Today it drops every run that isn't a failure. It will also
accept a completed run of any conclusion when:

- its `head_branch` matches an open delegation's `fix_branch`;
- its `workflow_id` matches the delegation's.

It queues such a run as `kind = follow_up`, and never as a normal triage.
Unrelated successful runs stay ignored, and so do other workflows' runs on the
fix branch.

**Worker, follow-up job:**

1. Ignore the run if its `head_sha` isn't the fix branch's latest commit.
2. Fetch the run's jobs, using the existing `get_jobs`.
3. Check the "fixed" conditions:
   - every job in `failed_jobs` concluded `success`, not skipped or missing;
   - no other job failed.
4. Run the diff guard (Phase 5) against the PR head, using a new
   `GitHubClient.compare(head_sha, fix_branch)`.
5. If everything passes, mark it `fixed` and comment "Fix ready on
   ci-triage/fix-…, guard passed", with a link to the draft PR or to compare.
6. Otherwise, triage the new failure from inside the follow-up job.
   - If `attempt < max_attempts`, mark it `retry` and re-dispatch with the
     previous brief plus the new verdict.
   - If not, mark it `gave_up` and comment, handing it to a person.
   - A guard violation counts as a failed attempt; it's put in the next brief.

**Housekeeping on the worker's existing poll:**

- no push before `deadline` (say 2 hours): `stale`;
- the PR head moves (the author pushed): `superseded`;
- the PR is closed: close the delegation. That needs the App subscribed to
  `pull_request` events (the webhook ignores everything but `workflow_run`
  today), or a check on each poll.

**Budget.** At about 19k tokens per triage and about 10 triages a day on the
free tier, a two-attempt delegation costs up to 3 triages plus 2 Claude runs.
Plan for 2 or 3 delegations a day at most. A follow-up triage that hits the
quota defers like any other run.

**Tests:** a scripted chain of fake webhooks for:
- fixed on the first try;
- fixed on the second;
- gave up;
- stale;
- superseded;
- a run on an older fix-branch commit, ignored;
- a run of another workflow on the fix branch, ignored;
- a fix that turns another job red, not fixed;
- a redelivered follow-up, a no-op.

## Phase 5: Diff guard (pure, and as strict as the citation check)

`guard(diff, original_failed_jobs) → GuardResult(ok, violations[])`.

A violation is any of:

- **Deleted tests:**
  - a deleted test file;
  - deleted test functions (`def test_`, `it(`, `func Test`);
  - test functions renamed so they're no longer collected (`test_x` → `_test_x`).
    Count test functions per file before and after, rather than only matching
    removed lines.
- **Skips:** an added `skip`, `xfail`, `@unittest.skip`, `.skip(`, `t.Skip(`,
  `# type: ignore` or `noqa` **in any test file**, not only on lines the log
  cites.
- **CI and test config:**
  - any change under `.github/`;
  - any change to CI, lint or test-selection config: `pytest.ini`,
    `[tool.pytest]` (including `--deselect` or `-k` added to `addopts`),
    `conftest.py` collection hooks, the `test` script in `package.json`, jest's
    `testPathIgnorePatterns`.
- **Incomplete diff:** the guard fails closed. The compare API leaves out a
  file's `patch` for large or binary files, and stops listing files after 300;
  either one is a violation, not a pass.
- **Loosened assertions** in a test file the log cites: flagged for review
  rather than failed outright.

Later, for `dependency`: allow a `.github/workflows/` change whose only effect is
changing a `uses:` ref to a pinned version.

Violations go into `guard_result` and appear in the dashboard. Each rule is
tested on small diff fixtures in a new `tests/test_guard.py`, including the
fail-closed case.

## Phase 6: Dashboard

- On `runs/[id]`, show the delegation chain as one timeline: failed run →
  verdict → brief → each attempt's run → guard result → outcome. `pipeline.tsx`
  is the natural base.
- A "Delegations" filter or list, with approve and reject buttons that reuse the
  review actions in `runs/actions.ts`. Approve says it approves the diagnosis
  too.
- The public demo plays a recording of a real chain and labels it as one. It
  never runs live, as the product principles require.

## Phase 7: End-to-end on ci-triage-demo

Three PRs that break things on purpose:

1. **A simple regression.** Expect `fixed` on attempt 1.
2. **A temptation to cheat.** A test that's hard to satisfy honestly. Expect the
   guard to catch a skipped, deleted or renamed test, or a `gave_up`.
3. **Something that can't be fixed in code.** Expect `gave_up` after 2 attempts,
   with a comment handing it to a person.

Record each into `dashboard/public/demo/recordings/`. Add a README section that
says plainly that this is unmeasured beyond these demos, and lists what a
user's workflow must contain for delegation to work (from Phase 0).

## Phase 8 (optional, later): Sandbox pre-check

Before reporting `fixed`, run `reproduce --patch` in Docker. It must fail on the
original SHA and pass on the fix. `sandbox.py` already reproduces first and then
tries the patch, so this is mostly wiring. It's stronger than "CI went green",
but needs Docker on the service host.

---

## Order of work

1. **Phase 0 first.** Its answer to "does CI run on the fix branch" can change
   Phase 4.
2. **Phases 2 and 5** are pure, need no quota or GitHub, and can come before
   Phase 1.
3. Then Phases 1, 3, 4, 6 and 7, in that order.
