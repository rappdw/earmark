---
description: Run the plan → implement → verify loop for steps of docs/PLAN.md, using the step-planner (Opus), step-implementer (Sonnet), and step-verifier (Sonnet) agents. After each verified step it commits, pushes, waits for CI, and moves on to the next step.
argument-hint: "[step number, e.g. 06; omit for the next todo step] [--one]"
---

Run the build loop starting at step $ARGUMENTS of `docs/PLAN.md`. If no step number is
given, start at the first `todo` step in the status table. You are the orchestrator: you
dispatch the three agents and decide what happens between them. You do not plan,
implement, or verify a step yourself, because the loop depends on those three roles
having separate contexts.

By default this is an outer loop: each step that passes is committed, pushed, and checked
by CI, and then the loop continues with the next step. With `--one`, run a single step
and stop after pushing it.

Before dispatching each step:

- Read the status table and the step's brief in `docs/PLAN.md`. If a step it depends on
  is not `done`, stop and say so.
- Make sure the working tree is clean and you are on the `impl` branch, creating it from
  `main` if it does not exist. If there are uncommitted changes you did not expect, stop
  and ask.

For each step:

1. **Plan.** Dispatch `step-planner` with the step number. When it returns, read
   `docs/steps/NN-<slug>.md`. If the planner reports that the step cannot be done as
   briefed, or made a decision that changes the product (a different library, a dropped
   criterion), stop the loop and put that to the user before implementing.
2. **Implement.** Dispatch `step-implementer` with the step number and the plan's path.
   If it stops because the plan is wrong, send its findings back to a `step-planner` to
   revise the plan, then implement again.
3. **Verify.** Dispatch `step-verifier` with the step number and the plan's path. Do not
   pass it the implementer's reply or your own view of the work.
4. **On `VERDICT: FAIL`,** send the blocking findings to the implementer, by continuing
   the same agent if it is still addressable, otherwise to a new one, then verify again
   with a fresh verifier. After two failed rounds, stop the loop and bring the findings
   to the user.
5. **On `VERDICT: PASS`,** set the step to `done` in the status table and commit
   everything as one commit titled `step NN: <title>`. Then push `impl` and wait for the
   CI run for that commit (`gh run watch <id> --exit-status`). If CI fails, stop the loop
   and bring the failure to the user; do not start the next step on a red build.
6. **Report and continue.** Give the user a short report for the step: the verdict, the
   commit, the CI result, the verifier's notes, and the unverified items. Then, unless
   `--one` was given, move on to the next step: the first `todo` row in the status
   table whose dependencies are all `done`. Stop when no such step remains.

Stop the loop early for anything that needs the user: a product decision from the
planner, two failed verify rounds, a red CI run, a step that cannot be verified without
something the sandbox lacks and the plan does not cover, or the user asking to stop.
When the loop ends, say why it ended and which steps are now unblocked.
