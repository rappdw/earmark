# Decisions, 2026-11-20: out-of-window resolver fallback and branch flow

Made during step 07. Details are in `docs/steps/07-resolver.md` (§2.4) and the
"Continuous integration" section of `docs/ARCHITECTURE.md`.

| # | Decision | Choice | Deciding reason |
|---|----------|--------|-----------------|
| 1 | Fallback for episodes outside Apple's lookup window | Read Apple's public episode page: `serialized-server-data` first, the schema.org `schema:episode` JSON-LD as backup | Old episodes keep their GUID (so their episode key and cached transcript) and their public audio URL. A page change fails loudly and the weekly network test reports it. |
| 2 | When `impl` reaches `main` | Fast-forward `main` to each step's commit once its CI run is green; Dependabot opens PRs against `impl` | The scheduled network workflow only runs from `main`, so `main` must hold current code. Pointing Dependabot at `impl` avoids `uv.lock` conflicts with the build loop. |

## 1. Out-of-window fallback

**Problem.** The iTunes Lookup API returns at most 200 episodes per show, and often far
fewer: it seems to list only what is in the current feed. The Daily returns 44 of 2785
episodes (about ten months), and This American Life 16 of 518. `lookup?id=<episode id>`
always returns nothing. Sharing an old episode for research is normal.

**Options considered.**

- **A. Episode page, internal JSON first, JSON-LD backup (chosen).** This gives the GUID,
  exact release time, duration, show, and the public (`STDQ`) audio URL. The GUID was
  verified equal to the feed's.
- **B. JSON-LD only.** This uses only public markup, but has no GUID, no audio URL, and a
  date with no time. Old episodes that have dropped off capped feeds would fail.
- **C. No fallback.** This fails with `PermanentError`. It is the simplest, but loses the
  back catalogue.

**Guards.**

- The page is used only when the lookup misses.
- The page's show must equal the URL's show, because Apple redirects a wrong show id to
  the right show's page.
- An unknown episode, which Apple redirects to the show page, fails with a clear
  `PermanentError`.
- The weekly `network` test resolves one old episode.

**Would revisit if** Apple's page structure starts changing often, or the fallback is
found to cause harm rather than just break.

## 2. Branch flow

**Context.** GitHub runs `schedule` and `workflow_dispatch` workflows, and reads
Dependabot's config, only from the default branch (`main`). Before this decision `main`
held only the initial commit.

**Options considered.**

- **A. Fast-forward `main` to each green step (chosen).**
- **B. Merge at milestones through a reviewed PR.** Scheduled tests would check stale
  code between milestones.
- **C. Defer to step 18.** Network checks and Dependabot would be off for the whole
  build.

**How it works.** `/step` commits a verified step on `impl`, pushes, and waits for CI. On
green it runs `git push origin <sha>:main`. It never force-pushes; if the push is not a
fast-forward, it stops and asks. Dependabot's `target-branch: impl` is asserted by
`tests/test_ci.py`.

**Would revisit if** `main` needs to mean "approved by a person", for example if the
homelab deploys from it or others review the code. In that case use option B, or a
release tag or branch for deploys.
