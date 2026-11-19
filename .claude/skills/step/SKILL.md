---
description: Run one plan → implement → verify loop for a step of docs/PLAN.md, using the step-planner (Opus), step-implementer (Sonnet), and step-verifier (Sonnet) agents.
argument-hint: <step number, e.g. 03>
---

Run the build loop for step $ARGUMENTS of `docs/PLAN.md`. You are the orchestrator: you
dispatch the three agents and decide what happens between them. You do not plan,
implement, or verify the step yourself, because the loop depends on those three roles
having separate contexts.

Before dispatching:

- Read the status table and the step's brief in `docs/PLAN.md`. If a step it depends on
  is not `done`, stop and say so.
- Make sure the working tree is clean and you are on the `impl` branch, creating it from
  `main` if it does not exist. If there are uncommitted changes you did not expect, stop
  and ask.

Then:

1. **Plan.** Dispatch `step-planner` with the step number. When it returns, read
   `docs/steps/NN-<slug>.md`. If the planner reports that the step cannot be done as
   briefed, or made a decision that changes the product (a different library, a dropped
   criterion), stop and put that to the user before implementing.
2. **Implement.** Dispatch `step-implementer` with the step number and the plan's path.
   If it stops because the plan is wrong, send its findings back to a `step-planner` to
   revise the plan, then implement again.
3. **Verify.** Dispatch `step-verifier` with the step number and the plan's path. Do not
   pass it the implementer's reply or your own view of the work.
4. **On `VERDICT: FAIL`,** send the blocking findings to the implementer, by continuing
   the same agent if it is still addressable, otherwise to a new one, then verify again
   with a fresh verifier. After two failed rounds, stop and bring the findings to the
   user.
5. **On `VERDICT: PASS`,** set the step to `done` in the status table and commit
   everything as one commit titled `step NN: <title>`. Do not push.

Finish by telling the user the verdict, the commit, the verifier's notes and unverified
items, and which steps are now unblocked. Run one step per invocation unless asked for more.
