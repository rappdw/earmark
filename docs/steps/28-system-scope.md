# Step 28: System scope: the three parts and the two seams

The brief is in `docs/PLAN.md`, "Step 28", with the operator's decisions of 2026-11-24 in
"System scope: one repo for the whole system" just above it, and pka-skills' answers in
`docs/decisions/2026-11-24-pka-receiving-skill.md`.

This is a docs step. It changes Markdown documents and the doc-driven tests. It changes no
source code, no deploy file, no workflow, no `.env.example`, and creates none of the
folders that steps 29–31 build (`.claude-plugin/`, `plugins/`, `contract/`, `shortcut/`).
The step can be done as briefed. No product decision is needed; one wording is an
interpretation the operator should confirm (section 8, D1).

Rules for the implementer that are not about the docs themselves:

- **Do not run any `git stash` command.** The operator keeps local work in a stash.
- **Placeholders only**, in every file this step touches: addresses at `example.org`,
  `example.net` or `example.com` (`earmark@example.org`, `you@example.org`,
  `pka-personal@example.net`, `pka-work@example.net`, `stranger@example.com`); the IPs
  `192.168.1.10`, `192.168.1.0/24`, `127.0.0.1`, `0.0.0.0` and `1.1.1.1`; the hosts `spark`,
  `earmark.example.com`, `smtp.example.org`; and `<…>` for every secret. Never write a real
  LAN address, host name, mail address or token. The repository name `rappdw/earmark` is
  already public in `docs/PLAN.md` and may be used.
- **Claim nothing about unbuilt parts.** The Shortcut file (step 31), `contract/` (step 29),
  the plugin and its skill (steps 29, 30) are described as planned, with their step. The
  Shortcut has never run on a phone, the GPU image has never been built, and the system has
  never run end to end. Say so wherever a part is described.

## 1. Outcome

When the step is done:

- **`docs/ARCHITECTURE.md`** opens with `## System: three parts and two seams`. It has the
  three parts (what each is, where it runs, where it lives, and what is built and run), the
  two seams (their contracts and their tests), how mail reaches a PKA through AMAP (named
  as outside this repo), the definition of done, and the repo layout. Folders that later
  steps create are named with the step that creates each. The rest of the document is
  introduced as the description of part 2, the service. The mail contract's provenance is
  recorded: drafted by pka-skills, owned here, moving to `contract/` in step 29.
- **`CLAUDE.md`** and **`README.md`** say the repo is the whole system. "earmark itself files
  nothing" becomes "The service files nothing; the PKA files, through the receiving skill."
- **`docs/runbook-spark.md`** has phases 0–12. Phase 6 no longer asks a PKA to allowlist the
  sender: the operator adds earmark's sending address to the AMAP router's allowlist for each
  PKA's address, and checks AMAP's attachment limit against `EARMARK_MAX_ATTACHMENT_MB`.
  Phase 11 is a placeholder for installing the receiving skill, which step 30 fills in.
  Phase 12, the last, is the one-episode check of the whole system, and ends in a
  **Check:**.
- **`docs/deploy.md`** sections 1, 2, 8.2–8.5 and 16 say the same about AMAP, the allowlist
  and the attachment limit.
- **`tests/test_docs.py`**, **`tests/doc_helpers.py`** and **`tests/test_doc_helpers.py`** pin
  all of this, and fail when a doc names a planned path before it exists without the step
  that builds it.

## 2. Findings

### 2.1 The docs as they are (read on 2026-11-24, commit `9bba6fb`)

- **Where the old allowlisting text lives.** `grep -rn allowlist` over the docs finds the
  wrong model (asking a PKA, or a PKA's "runtime") in:
  - `docs/runbook-spark.md` line 69 (phase 0: "a channel to ask each PKA to allowlist a
    sender") and lines 244–245 (phase 6, step 1);
  - `docs/deploy.md` line 466 (8.2: "the PKAs allowlist this sender"), line 470 (8.3: "Each
    PKA's runtime reads attachments only from…"), line 505 (8.4: "the PKAs' mail lane"),
    lines 517–520 (8.5), line 694 (section 16's row "mail arrives, but the PKA does not file
    it");
  - `docs/runbook-spark.md` line 239–240 (phase 5's check: "the PKAs will ignore this
    sender").

  `docs/PLAN.md` line 1148 (step 27's brief) has the old wording too. It is a historical
  brief and stays.
- **"earmark itself files nothing"** is in `CLAUDE.md` line 6 only. README line 8 says the
  PKAs "do the analysis and filing".
- **A drift between the two Re-scope copies.** `docs/PLAN.md` "Re-scope" marks the bullet
  "No PKA-side skill is needed from earmark" with "*Superseded on 2026-11-24:* the receiving
  skill is part of this repo (step 30). See "System scope" below." The copy in
  `docs/ARCHITECTURE.md` "Re-scope decisions" (line 1812) lacks it. That section says it is
  copied from PLAN and that a revision changes both. This step adds the same note to
  ARCHITECTURE.
- **No doc mentions a planned path today.** `grep` for `contract/`, `earmark-pka`,
  `earmark-receive`, `earmark.shortcut`, `marketplace`, `shortcut/` and `plugins/` over
  README, CLAUDE.md, ARCHITECTURE, deploy, runbook, shortcut and asr finds nothing. None of
  `.claude-plugin/`, `plugins/`, `contract/` or `shortcut/` exists.
- **Top-level tracked entries** (`git ls-files | cut -d/ -f1 | sort -u`): `.claude`,
  `.dockerignore`, `.env.example`, `.github`, `.gitignore`, `.python-version`, `CLAUDE.md`,
  `Dockerfile`, `Dockerfile.gpu`, `Makefile`, `README.md`, `compose.yaml`, `deploy`, `docs`,
  `pyproject.toml`, `src`, `tests`, `uv.lock`. The brief's layout names every top-level
  directory except `.github/` (section 8, D4).
- **Doc tests at HEAD.** `uv run pytest tests/test_docs.py tests/test_doc_helpers.py -q`:
  92 passed. `_phases()` asserts exactly `Phase 0` … `Phase 10` then `Send back`.
- **Anchors that other docs use.** Nothing links to a runbook phase anchor or to an
  ARCHITECTURE heading anchor (`grep` for `runbook-spark.md#` and `ARCHITECTURE.md#`). The
  runbook links `deploy.md#85-the-pka-side`, so section 8.5 keeps its heading
  `### 8.5 The PKA side`. deploy line 23 says "runbook phases 5 and 6", which stays true.

### 2.2 Traps in the existing doc tests (dead ends)

- **`\bclaude\b` is in `REMOVED`** (`tests/test_docs.py` line 515), checked over README,
  deploy, shortcut, runbook and asr. It matches `.claude-plugin/…` and `.claude/`, because
  `.` and `-` and `/` are not word characters, and it matches "Claude Code". So **none of
  those five docs may write `.claude-plugin`, `.claude/`, "Claude" or "Claude Code"**. Write
  "this repository's plugin marketplace", "the `earmark-pka` plugin". The layout with
  `.claude-plugin/` goes only in `docs/ARCHITECTURE.md` and `CLAUDE.md`, which that scan does
  not read. Do not loosen the pattern in this step; step 30's planner decides how the
  runbook's install phase names the tool.
- **Other `REMOVED` words to avoid** when describing the PKA: `vault`, `Obsidian`, `entity`
  or `entities`, `digest`, `embedding`, `/search`. Say "files the note in the PKA's
  workspace". The word "analysis" is allowed.
- **Relative links must resolve** (`test_doc_relative_links_resolve`). Never *link* to a
  planned path (`[…](shortcut/…)`, `[…](../plugins/…)`); write it in backticks only.
- **Anchors must resolve** (`test_doc_anchors_resolve`). The new links
  `ARCHITECTURE.md#system-three-parts-and-two-seams` and `ARCHITECTURE.md#definition-of-done`
  resolve only if the headings are exactly `## System: three parts and two seams` and
  `### Definition of done`, and each is unique in ARCHITECTURE.
- **`docs/ARCHITECTURE.md` cannot join the placeholder scan as a whole.** It holds the
  sandbox's DNS answer (Known risk 5), the tailnet range, and two four-part version numbers
  (cuBLAS and cuDNN), all of which the IPv4 pattern matches. So only its System section is
  scanned (6.4). (This plan is scanned too, so it does not quote them.)

### 2.3 Claude Code plugin layout (checked 2026-11-24)

The layout names Claude Code plugin paths, so they were checked against the current docs,
code.claude.com "Create a marketplace" (`/docs/en/plugin-marketplaces`) and "Add components
to a plugin" (`/docs/en/plugins/components.md`):

- A marketplace is a directory or repository with `.claude-plugin/marketplace.json`. It
  requires `name`, `owner` and `plugins`. Each entry needs `name` and `source`.
- A relative `source` is written from the marketplace root, the directory that contains
  `.claude-plugin/`, for example `"./plugins/my-first-plugin"`. `..` is invalid.
- A plugin's manifest is `<plugin>/.claude-plugin/plugin.json`, where only `name` is
  required. Skills live at the plugin root as `skills/<name>/SKILL.md`, and a skill "can
  also carry supporting files in its directory".
- Marketplace installs get "a copy in the plugin cache". Hosted marketplaces are added with
  `claude plugin marketplace add <owner>/<repo>`; inside a session, `/plugin marketplace
  add …` does the same. The install id is `<plugin>@<marketplace>`.

So the brief's layout matches the documented structure. Manifest fields, update behaviour
and install scope are step 30's to check; this step only names the paths.

### 2.4 The attachment cap against AMAP's limit (measured in the sandbox)

`EARMARK_MAX_ATTACHMENT_MB` (an integer, `gt=0`, default 10) caps the two attachments' raw
bytes together, in MiB (`pipeline/factory.py`: `settings.max_attachment_mb * 1024 * 1024`;
`mail/message.py` `compose_mail`). The message on the wire is larger: attachments are
base64 with 76-character lines. Built with the stdlib `email` package and
`email.policy.SMTP`, as `compose_mail` does, two attachments totalling exactly 10 MiB give a
message of 14,349,678 bytes, 1.368 times the attachments (13.7 MiB). A 300,000-byte payload
gives 1.371 times.

So "AMAP's limit must be at least `EARMARK_MAX_ATTACHMENT_MB`" holds as written only if
AMAP limits attachments by their decoded size. If AMAP limits the whole message, its limit
must be at least about 1.37 times the cap (about 14 MiB at the default). The runbook check
says both (section 8, D3). AMAP's actual limit, and how it counts, are unknown: that is the
point of the check.

### 2.5 Not verifiable here

AMAP's delivery, its allowlist and DMARC handling, its attachment limit, the doorbell, the
PKA reading the attachments, and the whole system. The runbook turns each into an operator
check. The docs say so.

## 3. Files

| File | Change |
|---|---|
| `docs/ARCHITECTURE.md` | New opening section "System"; intro, "Decisions" rows, the mail contract's heading and preamble, "Re-scope decisions", "Testing", "Known risks", "Settled questions" updated (4.1). |
| `CLAUDE.md` | Opening paragraphs, "Status", one rule reworded, one rule added (4.2). |
| `README.md` | Intro, a new `## The system` section, "What the service keeps" (4.3). |
| `docs/runbook-spark.md` | Intro, phases 0, 5 and 6 edited; new phases 11 and 12; "Send back" (4.4). |
| `docs/deploy.md` | Sections 1, 2, 8.1–8.5 and 16 edited (4.5). |
| `tests/doc_helpers.py` | New helpers (4.6). |
| `tests/test_doc_helpers.py` | Tests for them (4.7). |
| `tests/test_docs.py` | Updated and new tests (section 6). |

Nothing else changes. That includes `src/`, `Dockerfile*`, `compose.yaml`, `deploy/`,
`.github/`, `.env.example`, `pyproject.toml`, `docs/PLAN.md`, `docs/shortcut.md`,
`docs/asr.md` and `docs/decisions/`. `docs/PLAN.md`'s status table is the orchestrator's.

## 4. Interfaces: the documents and the test helpers

The headings, markers, table first cells and sentences named here are what the tests read.
Wording elsewhere is the implementer's, in the house style: short declarative sentences, no
marketing, "not built" and "never run" said plainly.

### 4.1 `docs/ARCHITECTURE.md`

**The top of the file**, in this order:

1. `# Architecture`.
2. One paragraph: this repo holds a small system in three parts, the iOS Shortcut, the
   service and the receiving skill, joined at two seams. "System" says how they fit. The
   rest of this document describes part 2, the service.
3. **`## System: three parts and two seams`**, the first `## ` heading in the file. Its
   subsections, in this order:

   - A short intro: one repo (the operator's decision of 2026-11-24, because the project is
     too small to split; one plan and one runbook cover the system, and a change across a
     seam lands in one commit). AMAP is outside this repo.
   - **`### The three parts`**: a table with the header
     `| Part | What it is | Runs on | Lives in | Built and run |` and exactly these three
     rows, whose first cells are exactly `1. The Shortcut`, `2. The service` and
     `3. The receiving skill`:

     | Part | What it is | Runs on | Lives in | Built and run |
     |---|---|---|---|---|
     | 1. The Shortcut | The iOS Shortcut that sends audio or a link to the service | the operator's iPhone | `docs/shortcut.md`, which says how to build it by hand; `shortcut/earmark.shortcut` from step 31 | Never run on a phone. The file is not built (step 31). |
     | 2. The service | API, worker, GPU transcription, mail delivery | the DGX Spark | `src/earmark/`, with its deployment files at the top level | Built in steps 01–27 and tested in CI on fakes and stubs. Never run on the Spark: the GPU image has never been built, and no real mail has been sent. |
     | 3. The receiving skill | Handles an earmark email that AMAP has delivered: checks it, files the note, acts on the instructions within a narrow scope | each PKA instance the operator chooses | `plugins/earmark-pka/`, listed in this repo's plugin marketplace (steps 29, 30) | Not built (steps 29, 30). |

   - **`### The two seams`**: a table with the header
     `| Seam | Between | Contract | Tested by |` and exactly two rows, first cells `Request`
     and `Mail`:

     | Seam | Between | Contract | Tested by |
     |---|---|---|---|
     | Request | Shortcut → service | `POST /transcriptions`: the request model in code, and "HTTP API" below | the service's API tests (`tests/test_api.py`), and the doc tests that post `docs/shortcut.md`'s JSON example and upload fields to the app; no Shortcut has sent one yet |
     | Mail | service → AMAP → receiving skill | the mail contract, `contract: 1` ("The mail contract" below; `contract/mail-v1.md` from step 29) | today the service's note and message tests only (`tests/test_note.py`, `tests/test_message.py`); from steps 29 and 30, sample emails in `contract/samples/v1/` that both sides test against |

     Then two bullets from the brief: the request seam has no folder, because its contract
     is the request model in code plus this document; the mail seam's folder `contract/`
     covers that seam only (step 29).
   - **`### How mail reaches a PKA`**. AMAP is outside this repo, and earmark only relies on
     it. A numbered list, from `docs/PLAN.md` "How mail reaches a PKA":
     1. The service sends ordinary SMTP email to the PKA instance's email address. earmark
        is not an AMAP fleet member, just an outside sender.
     2. AMAP takes the email delivered to that address and provides it to the PKA through
        AMMP (the Agent Mediated Messaging Protocol). The AMAP router applies the sender
        allowlist and DMARC. Attachments are readable (disposition `clean`) only for an
        allowlisted sender that passes DMARC.
     3. Inside the PKA's sandbox, AMAP rings a content-free doorbell in the live session:
        "you have mail; use the inbox read tool". With no session running, the mail waits
        for the next one.
     4. The PKA reads the message and its attachments with the `inbox` tools. The receiving
        skill takes over from there (step 30).

     Then these bullets:
     - **Intake is AMAP's job.** No earmark code reads, polls or selects mail. The service
       sends plain email, embeds no agent, and is deterministic apart from ASR output.
     - **The sender allowlist is AMAP router config**, set by the operator for each PKA's
       address. It is not a PKA's responsibility, and not something to ask a PKA for.
       earmark cannot see it: SMTP's reply is all it learns.
     - **AMAP's attachment limit is not known yet.** `EARMARK_MAX_ATTACHMENT_MB` (10) must
       not exceed it. If AMAP counts the whole message, base64 makes the message about 1.37
       times the attachments (2.4). Runbook phase 6 checks it.
     - **Trust.** earmark is not an enrolled peer, so the `inbox` tools' strict rule applies
       to its mail. The receiving skill acts on the instructions only within the narrow
       scope the operator granted as an explicit exception (`docs/PLAN.md` "Trust"), and the
       transcript note is always data. That rests on the sending address being used for
       nothing but earmark.
   - **`### Definition of done`**. It contains this sentence verbatim (line breaks free):
     "One episode shared on the phone is transcribed on the Spark, emailed, read by a PKA
     through the receiving skill, and filed, with the instructions acted on." Then: "The
     runbook's last phase (phase 12) is this check." Then, verbatim: "A step being `done`
     means its part's code and tests pass; it does not mean the system works." Then a
     status line: the system has not met it yet; parts 1 and 3 are incomplete (steps 30,
     31), and the system has never run end to end.
   - **`### Repo layout`**: the marker `<!-- repo-layout -->` on its own line, then a fence
     with info string `text`, exactly this (column alignment free, but at least two spaces
     between a path and its description):

     ```text
     .claude-plugin/marketplace.json   the marketplace "earmark", listing ./plugins/earmark-pka (step 30)
     plugins/earmark-pka/              part 3: the plugin (step 30)
       .claude-plugin/plugin.json
       skills/earmark-receive/SKILL.md
       skills/earmark-receive/scripts/   the contract checker, its only copy (step 29)
     contract/                         the mail seam only (step 29)
       README.md                         what the seam is, its versions, where the checker lives
       mail-v1.md                        the contract 1 text
       samples/v1/                       sample emails
     shortcut/earmark.shortcut         part 1: the signed export, the folder's only file (step 31)
     src/earmark/                      part 2: the service
     deploy/, Dockerfile, Dockerfile.gpu, compose.yaml   part 2: deployment
     tests/                            every part's tests, one gate
     docs/                             architecture, plan, runbook, deploy and Shortcut docs
     .github/                          CI: the gate, the live tests, the container build
     .claude/                          the build loop's agents and /step (development only)
     ```

     Format rules, which the parser in 4.6 relies on: a child line is indented two spaces
     more than its parent, and a parent is a single directory ending in `/`; a line's path
     field ends at the first run of two or more spaces; a path field may list several paths
     separated by `, `; a step is written `(step NN)` in the description.

     After the fence, two bullets: "Planned paths carry the step that creates them; a path
     without one exists now." and "The service keeps its conventional layout (`src/earmark/`
     with its deployment files at the top level). It is not moved into a part folder."
4. **`## The service`**: the current intro (lines 3–12, "earmark is a self-hosted
   transcription and delivery service…" to "(step 26)"), reworded to start "The service
   (part 2) is…", then "How to read this document", updated:
   - "System" describes the whole repo, including parts that later steps build, each marked
     with its step;
   - the sections after "The service", up to "Settled questions", describe the service's
     code as it is;
   - "Settled questions" records what the planners of steps 24–28 decided;
   - "History: removed in step 22" stays.

   Keep the paragraph "When a step changes a contract…".

**Elsewhere in the file:**

- **"Decisions" table.** The `Scope` row: "The service transcribes, and delivers the
  transcript by email (step 25). It files nothing: the PKA files, through the receiving
  skill (part 3, steps 29–30)." Its "How to change it" cell adds "System". The `Delivery`
  row: "the pka-skills mail contract (`contract: 1`)" becomes "the mail contract
  (`contract: 1`, drafted by pka-skills, owned by this repo)". Mention that AMAP, outside
  this repo, carries it to the PKA.
- **"Module layout".** Add one opening sentence: these are the service's modules; the whole
  repo's layout is "Repo layout" under "System".
- **The mail contract heading** `#### The mail contract (pka-skills, \`contract: 1\`, 2026-11-23)`
  becomes `#### The mail contract (\`contract: 1\`)`. Its preamble becomes:
  - **Provenance.** Drafted by pka-skills on 2026-11-23. Owned by this repo since 2026-11-24
    (the operator's decision; pka-skills confirmed that `contract: 1` stands as drafted).
    From step 29 its source of truth is `contract/mail-v1.md`, and this copy and the one in
    `docs/PLAN.md` "Re-scope" are replaced by links. Until then these two copies are it, and
    they change together.
  - Status: **DRAFT**. It becomes final once the operator approves the PKA's receiving
    procedure, which is now the receiving skill (step 30). A revision comes with a contract
    version bump, because mail sent under one version can still be waiting in an inbox when
    the next one ships. Step 25 built to this text.

  The contract text itself (from "- **Both files**" to the end of "PKA de-duplication")
  does not change by one character.
- **"Re-scope decisions" → "Operator decisions (2026-11-23)".** After the bullet "**No
  PKA-side skill is needed from earmark.** PKA is gaining inbound email.", add the same
  line `docs/PLAN.md` has, adapted only in its pointer: "*Superseded on 2026-11-24:* the
  receiving skill is part of this repo (step 30). See "System" above." Keep its "Built in
  step …" convention out of this bullet.
- **"Testing" → "What `tests/test_docs.py` checks".** Change the heading note to "(rewritten
  in step 27, extended in step 28; exact lists in `docs/steps/27-docs-runbook-shortcut.md`
  and `docs/steps/28-system-scope.md` section 6)". Add bullets:
  - this file opens with "System": the three parts with what is not built or never run, the
    two seams, how mail reaches a PKA through AMAP, and the definition of done;
  - the repo layout's paths exist or carry a `todo` step from the `docs/PLAN.md` status
    table, and it names every top-level directory;
  - while a planned path does not exist, no doc names it in a paragraph without the step
    that builds it;
  - the sender allowlist is AMAP router config in every doc, never something to ask a PKA;
  - `CLAUDE.md` and the README say the service files nothing and the PKA files.

  Change "Every runbook phase ends in a check, and one check is a real delivery to the
  operator's own address" to add "; phase 11 is the receiving skill's install (step 30) and
  the last phase, 12, is the one-episode check of the whole system". Change the
  placeholders bullet to name `CLAUDE.md`, this file's System section and the step plans 27
  and 28 as scanned too.
- **"Known risks".** Add item 8:
  "**AMAP, outside this repo.** earmark relies on AMAP to deliver its mail to each PKA: the
  router's sender allowlist and DMARC, `clean` attachments, the doorbell. None of it can be
  exercised here, and earmark learns only the SMTP reply. AMAP's attachment limit is not
  known; `EARMARK_MAX_ATTACHMENT_MB` must not exceed it, and a whole-message limit needs
  about 1.37 times the cap (`docs/steps/28-system-scope.md` section 2.4). Runbook phases 6
  and 12 check it." Also extend item 4's last sentences: the receiving skill and the
  Shortcut file do not exist yet (steps 30, 31).
- **"Settled questions".** The intro says "steps 24–28". Change the step-25 "**Still open:**"
  bullet to "the contract is final only once the operator approves the receiving procedure,
  now the receiving skill (step 30)". Add "**Settled by step 28**" (see
  `docs/steps/28-system-scope.md` section 8):
  - the runbook keeps phases 0–10 and appends phase 11 (installing the receiving skill,
    step 30) and phase 12 (the one-episode check), so no existing phase number changes;
  - the repo layout is a `text` fence after `<!-- repo-layout -->`, checked against the
    repo and the status table, and it also names `.github/`;
  - AMAP's attachment limit is checked both ways: decoded attachments against the cap, or
    the whole message against about 1.37 times the cap;
  - `deploy.md` keeps section 8.5's heading, so its anchor stays.

### 4.2 `CLAUDE.md`

- **The opening**, replacing lines 3–6 ("Self-hosted transcription and delivery service…
  earmark itself files nothing."):
  - "One repo for a small system: a podcast episode or a piece of audio, shared from the
    operator's iPhone, ends up as a note filed by the operator's PKA instances, each with its
    own context."
  - The three parts: the iOS Shortcut (part 1; `docs/shortcut.md`, a file to import from step
    31), the service on the homelab's DGX Spark (part 2, `src/earmark/`), and the receiving
    skill each chosen PKA instance installs from this repo's plugin marketplace (part 3,
    `plugins/earmark-pka/`, steps 29–30, not built yet).
  - The two seams: the request (`POST /transcriptions`) and the mail contract
    (`contract: 1`). AMAP, outside this repo, carries the mail from the service to each
    PKA.
  - `docs/ARCHITECTURE.md` "System" has the parts, the seams, the definition of done and the
    repo layout.
  - The sentence, verbatim: "The service files nothing; the PKA files, through the receiving
    skill."
- **"Today (after step 25)"** becomes "The service today (after step 27)". Its text stays.
- **"Status"** gains a bullet: "A step being `done` means its part's code and tests pass. It
  does not mean the system works: that is the runbook's last phase, the one-episode check."
- **"Shape of the code"** becomes "Shape of the service". Add one line after the diagram:
  "The whole repo's layout is "Repo layout" in `docs/ARCHITECTURE.md`."
- **Rules.**
  - "**Mail follows the contract exactly** (step 25): pka-skills `contract: 1`, a draft,
    copied in `docs/ARCHITECTURE.md`." becomes "…: `contract: 1`, drafted by pka-skills and
    owned by this repo, still a draft. Until step 29 moves it to `contract/`, it is copied in
    `docs/PLAN.md` and `docs/ARCHITECTURE.md`." The rest of the bullet stays.
  - A new rule after it: "**Intake is AMAP's job** (2026-11-24). AMAP is outside this repo.
    The service sends plain SMTP to a PKA instance's address and embeds no agent; no earmark
    code reads, polls or selects mail. The sender allowlist and DMARC are AMAP router
    config, which the operator sets for each PKA address; never write it up as something to
    ask a PKA for."

### 4.3 `README.md`

- **Intro** (lines 3–8). First: "earmark turns a podcast episode or a piece of audio, shared
  from an iPhone, into a note filed by the operator's personal knowledge assistants (PKAs).
  This repository holds the whole system, in three parts (below)." Then the current text,
  starting "Most of this README is about part 2, the service: a self-hosted transcription
  and delivery service." Keep the rest of it. Drop "The operator's personal knowledge
  assistants (PKAs) do the analysis and filing" (the new first sentence covers it).
- **`## The system`**, a new section before `## How it works`:
  - a bullet list, one bullet per part, separated by blank lines so each is its own
    paragraph:
    - **1. The Shortcut**, on the iPhone: sends a link or a file with the destination and
      the instructions. Built by hand from [docs/shortcut.md](docs/shortcut.md). It has
      never run on a phone, and a file to import comes in step 31.
    - **2. The service**, on the DGX Spark, in `src/earmark/`: transcribes and emails the
      transcript. The rest of this README. The GPU image has never been built or run.
    - **3. The receiving skill**, in each chosen PKA instance: checks an earmark email,
      files the note, and acts on the instructions within a narrow scope. It will be the
      `earmark-pka` plugin in `plugins/earmark-pka/`, from this repository's plugin
      marketplace. Not built yet (steps 29, 30).
  - Then a paragraph: the two seams, the request (`POST /transcriptions`) and the mail
    contract (`contract: 1`); AMAP, outside this repository, delivers the mail to each PKA;
    the sentence verbatim "The service files nothing; the PKA files, through the receiving
    skill."; and a link to
    [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#system-three-parts-and-two-seams).
  - Nothing in this section may say `.claude`, "Claude", `vault` or `Obsidian` (2.2).
- **"What earmark keeps"** becomes "**What the service keeps.**". Its text stays.
- **Guides.** The ARCHITECTURE line becomes "the system, the service's design, contracts,
  and security rules".

### 4.4 `docs/runbook-spark.md`

The format of step 27 stays: `## Phase N: Title` for N = 0…12, in order, then
`## Send back`; each phase has exactly one `**Check:**`, not as its first paragraph, with no
`###` heading after it.

- **Intro.** The ordered list gains "7. The receiving skill on each PKA (step 30, not built
  yet), then one episode through the whole system." "What has never run" gains: "The
  receiving skill and the Shortcut file are not built yet (steps 30 and 31), and the whole
  system has never run: phase 12 is its first run."
- **Phase 0.** Replace "The two PKA addresses, and a channel to ask each PKA to allowlist a
  sender." with "The two PKA addresses, and access to the AMAP router's configuration for
  them: you add a sender to their allowlist, and read their attachment limit." Add "A
  session on each PKA instance, to see what arrives." The check is unchanged.
- **Phase 5's check.** "If DMARC fails, fix it before phase 6: the PKAs will ignore this
  sender." becomes "…: AMAP's router does not make the attachments of mail that fails
  DMARC readable." The rest stays; `test_runbook_real_delivery_check` must still pass.
- **Phase 6** becomes `## Phase 6: The PKA recipients and AMAP`:
  1. For each PKA address, add `earmark@example.org` to the AMAP router's sender allowlist
     for that address. This is AMAP router configuration, which you set. It is not
     something to ask a PKA for, and earmark cannot see it: SMTP's reply is all it learns
     ([deploy.md section 8.5](deploy.md#85-the-pka-side)).
  2. Find AMAP's attachment limit for each PKA address, and whether it counts the
     attachments or the whole message. Compare it with `EARMARK_MAX_ATTACHMENT_MB` (default
     10 MiB, the two attachments together): a limit on the attachments must be at least the
     cap; a limit on the whole message must be at least about 1.37 times the cap (about
     14 MiB at the default), because base64 adds about a third. If it is lower, set
     `EARMARK_MAX_ATTACHMENT_MB` to a whole number of MiB that fits.
  3. Set `EARMARK_ALLOWED_RECIPIENTS=pka-personal@example.net,pka-work@example.net` (keep
     `you@example.org` for test mail), and recreate the containers with `docker compose up -d`.
  4. Send one transcription to each PKA, with instructions such as "File this; no
     analysis".

  Keep the `all` paragraph and the DRAFT sentence, reworded: "The mail contract is DRAFT
  until you approve the receiving procedure, which is the receiving skill (step 30)."

  **Check:**
  - doctor shows `recipients ok 2 allowed: p…@example.net, p…@example.net` (or 3);
  - a request to `stranger@example.com` gets 422 `to is not an allowed recipient
    (EARMARK_ALLOWED_RECIPIENTS)`;
  - you have written down AMAP's attachment limit for each address and how it counts, and
    it is at least `EARMARK_MAX_ATTACHMENT_MB` (or about 1.37 times it, for a
    whole-message limit);
  - in each PKA's session, the message arrives (the doorbell rings, or it is there at the
    next session), and the `inbox` tools show both attachments with disposition `clean`.

  Filing is not checked here: it needs the receiving skill (phases 11 and 12).
- **Phases 7–10** are unchanged.
- **`## Phase 11: Install the receiving skill on each PKA (step 30)`**, a placeholder. Body:
  "Not written yet. The receiving skill, `earmark-receive` in the `earmark-pka` plugin from
  this repository's plugin marketplace, is not built: step 30 builds it and writes this
  phase. It installs at project scope, on the PKA instances you choose, never at user
  scope." The body paragraph itself must say "step 30" (6.3, the planned-names test reads
  paragraphs, and the heading is a paragraph of its own). Do not show install commands.

  **Check:** "None yet. Until step 30 fills in this phase there is nothing to check, and
  phase 12 cannot pass."
- **`## Phase 12: The whole system: one episode, end to end`**, the last phase. Intro: this
  is the system's definition of done
  ([ARCHITECTURE.md](ARCHITECTURE.md#definition-of-done)). Every earlier phase checks one
  part; this checks them together. It cannot pass before phase 11 exists (step 30). Steps:
  1. On the phone, in Apple Podcasts, share one episode to the Earmark Shortcut and choose a
     PKA whose instance has the receiving skill.
  2. As instructions, give something inside the receiving skill's narrow scope, for example
     "File this under podcasts/economics and tag it economics." (a destination folder and a
     tag).
  3. Wait for the `Sent:` push, then let that PKA's session handle the mail (the doorbell,
     or the next session).

  **Check:**
  - the Shortcut shows a `job_id`, and the push says `Sent: <title>` with
    `To: p…@example.net`;
  - the worker log shows the episode transcribed on `cuda`, or the cached transcript reused
    if it was shared before;
  - the PKA reads the message with the `inbox` tools and handles it through the receiving
    skill, not by hand;
  - the note is filed in the PKA's workspace, with `source: "earmark"` and the same `guid`
    as the attachment, and the PKA lists the changes it made and commits nothing;
  - the instructions were acted on: the note is in the folder you named and carries the
    tag;
  - step 30 adds the receiving skill's own evidence to this check. Until then, write down
    what the PKA showed.
- **`## Send back`** gains:
  5. AMAP's attachment limit for each PKA address, and how it counts (phase 6);
  6. the phase 12 outcome: pass, or the first thing that failed.

### 4.5 `docs/deploy.md`

Section numbers and headings stay (7, 8.5 and 17 are referred to from elsewhere).

- **Section 1, "What is tested".** The email bullet becomes: "Email delivery is tested only
  against an in-process SMTP stub. TLS against your server, DMARC at the recipient, and
  AMAP delivering the mail to a PKA are operator checks (runbook phases 5 and 6). The
  receiving skill is not built yet (step 30), and the whole system has never run (runbook
  phase 12)." Keep the four phrases `test_deploy_doc_states_test_status` reads.
- **Section 2, the diagram.** The mail line becomes
  `worker ──(SMTP submission, TLS)──► smtp.example.org ──► each PKA's address ──(AMAP)──► the PKA`.
  Add one bullet: AMAP, outside this repo, applies the sender allowlist and DMARC, and
  hands the mail to the PKA (section 8.5).
- **8.1.** The last paragraph: the mail contract is "The mail contract" in "Delivering" in
  ARCHITECTURE, drafted by pka-skills and owned by this repo. It is **DRAFT** until you
  approve the receiving procedure, which is now the receiving skill (step 30).
- **8.2.** "Use a dedicated mailbox, not your personal one: the PKAs allowlist this sender."
  becomes "Use a dedicated mailbox, for nothing but earmark: AMAP's router allowlists this
  sender for each PKA address, and the receiving skill's trust in the instructions rests on
  no one else sending from it."
- **8.3.** The first sentence becomes "AMAP's router makes attachments readable only for an
  allowlisted sender whose mail passes DMARC." The rest stays.
- **8.4.** The `EARMARK_MAX_ATTACHMENT_MB` bullet becomes: "defaults to 10 MiB, for the two
  attachments together. A three-hour transcript is about 300 KB. Keep it at or below AMAP's
  attachment limit for the PKA addresses, which is not known yet (runbook phase 6 finds it).
  If AMAP limits the whole message, base64 makes the message about 1.37 times the
  attachments."
- **8.5 The PKA side** (heading unchanged). Replace the bullets with:
  - How mail reaches a PKA, in two or three sentences: earmark sends plain email to the
    PKA's address; AMAP, outside this repo, applies the allowlist and DMARC and provides it
    to the PKA through AMMP; the PKA reads it with its `inbox` tools after a doorbell in its
    session. Link [ARCHITECTURE.md "System"](ARCHITECTURE.md#system-three-parts-and-two-seams).
  - **The sender allowlist is AMAP router configuration.** Add `earmark@example.org` to the
    AMAP router's allowlist for each PKA's address. You set it; it is not something to ask
    a PKA for. earmark cannot see whether it was done: SMTP's 250 reply is all it learns.
  - **AMAP's attachment limit** must be at least `EARMARK_MAX_ATTACHMENT_MB` (8.4).
  - **The test** is the PKA's `inbox` tools showing both attachments with disposition
    `clean` (runbook phase 6). Filing needs the receiving skill (step 30, not built yet).
- **Section 16.** Replace the row "mail arrives, but the PKA does not file it" with two rows:

  | Symptom | Likely cause |
  |---|---|
  | the PKA gets the mail, but its attachments are not readable (not `clean`) | `earmark@example.org` is not on the AMAP router's allowlist for that address, or DMARC fails (the 8.3 check). |
  | the PKA reads the mail but does not file it | The receiving skill is not installed there (runbook phase 11; step 30, not built yet). |

### 4.6 `tests/doc_helpers.py` additions

```python
@dataclass(frozen=True)
class LayoutEntry:
    path: str  # full path from the repo root, as written ("plugins/earmark-pka/.claude-plugin/plugin.json")
    description: str  # the text after the path field, stripped ("" when none)
    step: str | None  # "29": the entry's own "(step NN)", else the nearest ancestor's, else None


def layout_entries(body: str) -> list[LayoutEntry]:
    """Entries of a repo-layout fence body (4.1 "Repo layout"). A line's path field ends at
    the first run of two or more spaces; it may hold several paths separated by ', ', each
    an entry with the same description and step. A line indented more than the line above
    is a child of the nearest preceding line with less indentation, which must be a single
    path ending in '/'; the child's path is joined to it. Blank lines are skipped.
    AssertionError for a child of a multi-path line or of a path not ending in '/', and for
    indentation that is not a multiple of two."""


def plan_status(plan_text: str) -> dict[str, str]:
    """Step number -> status from the table in docs/PLAN.md's '## Status' section, both
    stripped, for example {'01': 'done', '01a': 'done', '28': 'todo'}. AssertionError if
    the table has no rows."""


def step_numbers(text: str) -> set[str]:
    """Every step number that text mentions as 'step NN' or 'steps NN, MM and KK', with
    ranges 'NN–MM' or 'NN-MM' expanded, each zero-padded to two digits ('9' -> '09'); a
    suffixed number such as '01a' is kept as written. Case-insensitive."""


def paragraphs(text: str) -> list[str]:
    """Blocks of text outside fences separated by blank lines, each stripped, empty ones
    dropped. A table, or a list with no blank lines between its items, is one block."""
```

A sketch of the step pattern, for the implementer to refine:
`\bsteps?\s+(\d+[a-z]?(?:(?:\s*,\s*|\s+and\s+|\s*[–-]\s*)\d+[a-z]?)*)`. Expand a range only
when both ends are plain integers.

### 4.7 `tests/test_doc_helpers.py`

| Test | Proves |
|---|---|
| `test_layout_entries_children_and_steps` | an inline copy of the 4.1 fence (not read from ARCHITECTURE, so the helper test does not move with the doc) gives, among others, `LayoutEntry("plugins/earmark-pka/.claude-plugin/plugin.json", "", "30")`, `LayoutEntry("plugins/earmark-pka/skills/earmark-receive/scripts/", "the contract checker, its only copy (step 29)", "29")`, `LayoutEntry("contract/samples/v1/", "sample emails", "29")`, `LayoutEntry("src/earmark/", "part 2: the service", None)` |
| `test_layout_entries_multi_path_line` | `"deploy/, Dockerfile, Dockerfile.gpu, compose.yaml   part 2: deployment"` gives four entries with that description and `step is None` |
| `test_layout_entries_rejects_bad_nesting` | a child under the multi-path line raises; an odd indent raises |
| `test_plan_status` | on a two-row sample table under `## Status` returns both rows; on the real `docs/PLAN.md` it has `"27": "done"` and keys `"28"`–`"31"` |
| `test_step_numbers` | `"steps 29, 30 and 31; step 28; steps 22–24; Step 9; step 01a"` gives `{"09", "01a", "22", "23", "24", "28", "29", "30", "31"}`; `"no steps here"` gives `set()` |
| `test_paragraphs` | splits on blank lines, keeps a table as one block, ignores text inside fences |

## 5. Behaviour and edge cases

- **"Not something to ask a PKA for" is the intended wording**, from the brief. The
  allowlist scan (6.4) bans `ask … PKA to` and `PKA(s) allowlist`, which the old text used,
  not "ask a PKA for". Write the new text with "for", or reword; do not write "ask the PKA
  to".
- **The layout test relaxes by itself as steps land.** A planned path may exist early (step
  29's verifier sees `contract/` before the status table says `done`); that passes, because
  an existing path needs nothing. A path that is missing must carry a step whose status is
  `todo`. So when step 29 is marked `done`, its paths must exist, and a step that is dropped
  or renumbered shows up here.
- **The planned-names test reads paragraphs, not sentences.** A heading is its own
  paragraph, so phase 11's heading "(step 30)" does not cover its body: the body says
  "step 30" too. A Markdown table or a list without blank lines is one paragraph, so one
  step mention covers it; the README's part bullets are separated by blank lines on
  purpose, so each carries its own step.
- **`contract: 1` is not the path `contract/`.** The planned-name token for that directory
  keeps its slash, so prose about the contract never trips the test.
- **ARCHITECTURE is scanned for planned names as a whole, outside fences**, but for
  placeholders and for the allowlist wording only in its System section (2.2): older
  sections keep history ("mail lane" in "Inputs from the other agents") and non-placeholder
  numbers.
- **No install commands, no claims.** Phase 11 shows no `/plugin …` command and phase 12
  does not describe the skill's internals beyond the decided outcomes (filed, `source:
  "earmark"`, the `guid`, instructions acted on, nothing committed). Step 30 adds the rest.
- **The contract text is frozen.** Only the mail contract's heading and preamble change. A
  change to a key, file name, format or rule would be a contract revision with a version
  bump (CLAUDE.md), which this step does not make.
- **The runbook's placeholder phase still ends in `**Check:**`**, because
  `test_runbook_phases_end_in_checks` requires one in every phase. Its check says plainly
  that there is nothing to check yet.
- **Anchors.** Keep `### 8.5 The PKA side` (the runbook links `#85-the-pka-side`). Renaming
  runbook phase 6 changes its anchor, which nothing links to (2.1).

## 6. Tests

All in `tests/test_docs.py` unless named otherwise. The fixtures are the documents
themselves and `docs/PLAN.md`'s status table. No network, no new fixture files.

Shared setup to add:

- `PLANS = ["docs/steps/27-docs-runbook-shortcut.md", "docs/steps/28-system-scope.md"]`,
  replacing `PLAN`.
- `ARCH = "docs/ARCHITECTURE.md"`; `_system()` returns
  `section(read_doc(ARCH), "## System: ")`.
- `_flat(text)` returns `" ".join(text.split())`, for sentences that wrap.
- `_layout()` returns `layout_entries(first_fence(after_marker(_system(), "repo-layout"),
  "text").body)`.
- `_assert_placeholders(text, label)`: the body of today's `test_docs_use_placeholders_only`,
  moved into a function so it can run on a section.

### 6.1 Brief criterion: "The doc tests pin the system section: the three parts, the two seams, and the definition of done."

| Test | Proves |
|---|---|
| `test_architecture_opens_with_system_section` | the first line starting `## ` in ARCHITECTURE (outside fences) is `## System: three parts and two seams`, and `## The service` follows it before `## Decisions` |
| `test_architecture_system_parts` | the rows of `table_rows(section(_system(), "### The three parts"))` have first cells exactly `["1. The Shortcut", "2. The service", "3. The receiving skill"]`; "Runs on" cells contain `iPhone`, `DGX Spark`, `PKA`; "Lives in" cells contain `shortcut/earmark.shortcut`, `src/earmark/`, `plugins/earmark-pka/` |
| `test_architecture_system_seams` | the `### The two seams` table's first cells are exactly `["Request", "Mail"]`; Request's contract cell contains `POST /transcriptions`; Mail's contract cell contains `contract: 1`; Mail's "Tested by" cell contains `contract/samples/v1/` and `step_numbers` of it includes `"29"` |
| `test_architecture_mail_reaches_pka_through_amap` | `section(_system(), "### How mail reaches a PKA")` contains `AMAP`, `outside this repo`, `AMMP`, `allowlist`, `DMARC`, `router`, `doorbell`, `` `clean` ``, `inbox`, `EARMARK_MAX_ATTACHMENT_MB`, and "No earmark code reads, polls or selects mail" |
| `test_architecture_definition_of_done` | `_flat(section(_system(), "### Definition of done"))` contains the two verbatim sentences of 4.1 and `phase 12` |
| `test_architecture_repo_layout_matches_repo` | for every `_layout()` entry: the path exists under `ROOT`, or its `step` is not `None` and `plan_status` gives it `todo`. Also: every entry whose `step` is `None` exists, and the brief's paths are all present (`.claude-plugin/marketplace.json`, `plugins/earmark-pka/skills/earmark-receive/SKILL.md`, `plugins/earmark-pka/skills/earmark-receive/scripts/`, `contract/mail-v1.md`, `contract/samples/v1/`, `shortcut/earmark.shortcut`, `src/earmark/`, `compose.yaml`) |
| `test_architecture_repo_layout_covers_top_level` | every directory directly under `ROOT`, except `.git`, `.venv`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `__pycache__`, `dist`, `build`, `data`, `htmlcov` and names ending `.egg-info` (the `.gitignore` list), is the first segment of some layout path |
| `test_architecture_mail_contract_provenance` | `section(text, "#### The mail contract")` matches exactly one heading, which is `` #### The mail contract (`contract: 1`) ``; its flattened text contains `drafted by pka-skills` (case-insensitive), `2026-11-23`, `2026-11-24`, `owned by this repo`, `contract/mail-v1.md`, `**DRAFT**`, and the step set of its preamble includes `"29"` and `"30"` |

### 6.2 Brief criterion: "The runbook's last phase is the one-episode check, and it ends in a **Check:**."

| Test | Proves |
|---|---|
| `test_runbook_phases_end_in_checks` (updated) | `_phases()` now expects `Phase 0` … `Phase 12` then `Send back`; 13 phases; each has exactly one `**Check:**`, not first, with no `###` after it |
| `test_runbook_last_phase_is_the_system_check` (new) | the last phase's title starts `Phase 12: ` and contains `one episode`; it links `ARCHITECTURE.md#definition-of-done`; the text after its `**Check:**` contains `job_id`, `Sent:`, `cuda`, `inbox`, `receiving skill`, `source: "earmark"`, `guid`, `commits nothing` and `instructions` |
| `test_runbook_skill_install_placeholder` (new) | phase 11's title contains `receiving skill` and `step 30`; its body contains `not built`, `project scope` and a paragraph with `step 30`; its check contains `phase 12 cannot pass` |
| `test_runbook_real_delivery_check`, `test_runbook_covers_the_brief` (kept) | still pass; `test_runbook_covers_the_brief` also needs `AMAP` |

### 6.3 Brief criterion: "No doc claims a part works that has not been run. The Shortcut file, the receiving skill and the GPU image are described as unbuilt or unverified."

| Test | Proves |
|---|---|
| `test_architecture_parts_say_what_is_unbuilt` (new) | in the parts table's last column: part 1 contains `Never run on a phone` and `step 31`; part 2 contains `never been built` and `GPU image`; part 3 contains `Not built` and its `step_numbers` include `"30"`. The `### Definition of done` text says the system `has never run end to end` |
| `test_readme_system_section_says_what_is_unbuilt` (new) | `section(readme, "## The system")` contains `never run on a phone`, `step 31`, `never been built`, `Not built yet`, and its `step_numbers` include `"29"` and `"30"` |
| `test_deploy_doc_states_test_status` (extended) | also: the first section contains `receiving skill`, `not built` and `phase 12` |
| `test_planned_paths_are_marked_planned[doc]` (new, over `DOCS`, `CLAUDE.md` and `ARCH`) | for each planned name, while it does not exist, every paragraph (`paragraphs(text)`) that contains it also contains its step in `step_numbers(paragraph)`. Planned names: each `_layout()` entry that does not exist, as its path with a trailing `/` removed unless the path has a single segment (so `contract/` stays `contract/`), with its step; plus `earmark-pka` → `30`, `earmark-receive` → `30`, `earmark.shortcut` → `31`, `marketplace.json` → `30`, each only while its layout path is missing. The layout fence itself is outside `paragraphs`, so it is not scanned here |

### 6.4 Criteria this plan adds

| Criterion | Test |
|---|---|
| The sender allowlist is AMAP router config, never a PKA's job (the brief's "Build") | `test_sender_allowlist_is_amap_router_config[doc]` over `DOCS`, `CLAUDE.md` and `_system()`: no case-insensitive match of `ask (each\|the\|a) PKAs? to\b`, `\bPKAs? (to )?allowlists?\b`, `allowlisted at (each\|the) PKA`, `PKA's runtime`, or (for `DOCS` only, since ARCHITECTURE's history keeps it) `mail lane` |
| The runbook and deploy guide set the allowlist at AMAP and check AMAP's limit | `test_runbook_phase_6_uses_amap`: phase 6 contains `AMAP`, `router`, `allowlist`, `earmark@example.org`, `EARMARK_MAX_ATTACHMENT_MB` and `1.37`; its check contains `attachment limit`, `EARMARK_MAX_ATTACHMENT_MB` and `` `clean` ``. `test_deploy_doc_pka_side_is_amap`: `section(deploy, "### 8.5 ")` contains `AMAP`, `router`, `allowlist`, `outside this repo`, `` `clean` `` and `EARMARK_MAX_ATTACHMENT_MB`; `section(deploy, "### 8.4 ")` contains `AMAP` and `1.37` |
| `test_deploy_doc_mail_section` (kept) | still finds `allowlist` and `DRAFT` in section 8 |
| CLAUDE.md and the README say the repo is the system | `test_readme_and_claude_md_name_the_system`: both contain "The service files nothing; the PKA files, through the receiving skill." (flattened); neither contains `earmark itself files nothing` or `earmark files nothing`; both contain `receiving skill`, `AMAP`, `contract: 1`, `POST /transcriptions`, `plugins/earmark-pka/`; `CLAUDE.md` contains "Intake is AMAP's job" and "does not mean the system works" |
| The two Re-scope copies agree on the superseded bullet | `test_rescope_pka_skill_bullet_superseded_in_both`: `docs/PLAN.md` and ARCHITECTURE each contain `*Superseded on 2026-11-24:* the receiving skill is part of this repo (step 30).` |
| Placeholders only in every file this step touches | `test_docs_use_placeholders_only[doc]` over `[*DOCS, *PLANS, "CLAUDE.md"]` via `_assert_placeholders`; `test_architecture_system_section_uses_placeholders` runs it on `_system()` |
| No removed behaviour in the edited docs | `test_docs_describe_no_removed_behaviour[doc]` (kept, unchanged patterns) |
| No relative link or anchor is broken | `test_doc_relative_links_resolve`, `test_doc_anchors_resolve` (kept): they now cover the README's and the runbook's links into ARCHITECTURE |
| The doc helpers behave as documented | `tests/test_doc_helpers.py`, section 4.7 |
| No part folder is created, no file outside the list changes | the verifier's commands (section 7) |

**Mutations the verifier can try** in a scratch copy (section 7): delete the
definition-of-done sentence; rename `3. The receiving skill`; drop "(step 31)" from the
layout's `shortcut/earmark.shortcut` line; write "Ask each PKA to allowlist" back into phase 6;
add `plugins/earmark-pka/` to a README paragraph without a step; delete phase 12's
`**Check:**`. Each must fail a test named above.

## 7. Verification

From the repository:

```bash
make check
GITHUB_ACTIONS=true make check
uv run pytest tests/test_docs.py tests/test_doc_helpers.py -q
git status --porcelain -- src deploy .github Dockerfile Dockerfile.gpu compose.yaml \
  .env.example pyproject.toml uv.lock Makefile docs/PLAN.md docs/shortcut.md docs/asr.md \
  docs/decisions                                                      # expect no output
ls -d .claude-plugin plugins contract shortcut 2>/dev/null           # expect no output
git stash list | wc -l                                                # same count as before
grep -rniE 'ask (each|the|a) PKAs? to|PKAs? allowlist|mail lane|files nothing' \
  README.md CLAUDE.md docs/deploy.md docs/runbook-spark.md
#   expect only the two "The service files nothing; the PKA files…" lines
grep -rnoE '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b' README.md CLAUDE.md docs/deploy.md \
  docs/runbook-spark.md docs/steps/28-system-scope.md \
  | grep -vE ':(127\.0\.0\.1|0\.0\.0\.0|1\.1\.1\.1|192\.168\.1\.(10|0))$'  # expect no output
git diff docs/ARCHITECTURE.md | grep -E '^[-+]' | grep -E 'Both files|Attachment [12]|PKA de-dup'
#   expect nothing: the contract text itself is unchanged
```

For the last check, also read the diff of "The mail contract" in ARCHITECTURE by eye: only the
heading and the preamble before "- **Both files**" may change.

**Mutation checks (optional, recommended).** Copy the tree to the scratchpad without the
venv (`rsync -a --exclude .venv --exclude .git ./ <scratch>/earmark/`), apply one mutation
from 6.4 at a time there, and run `uv run --project <scratch>/earmark pytest
<scratch>/earmark/tests/test_docs.py -q`. Each must fail. Nothing in the working tree changes.

**Static review.**

- Read the System section against 4.1, and the layout against 2.3.
- Read runbook phases 6, 11 and 12 for copy-pastable commands and for any claim that the
  receiving skill, the Shortcut file or the system works.
- Say plainly in the report that nothing in this step can be run: AMAP, the PKA, the
  Shortcut and the Spark are all operator checks.

There is no live smoke: no code changes. The README quickstart is untouched; running it
verbatim (step 27's procedure) is optional.

## 8. Deviations

- **D1: The DRAFT condition is read as "the receiving skill".** ARCHITECTURE, deploy 8.1 and
  runbook phase 6 said the contract is DRAFT "until you approve the PKA's receiving
  procedure". pka-skills will not build one (decision record), and step 30 builds the
  receiving skill instead. This plan words the condition as "the receiving procedure, which
  is now the receiving skill (step 30)". That reading is the planner's; the operator may want
  a different trigger for calling `contract: 1` final.
- **D2: The runbook appends phases 11 and 12** instead of renumbering. The brief asks for the
  one-episode check as the last phase and a placeholder for the install phase. Phase 10
  (backups) stays where it is, so deploy's "runbook phases 5 and 6" and every phase anchor
  keep their meaning.
- **D3: AMAP's attachment limit is checked two ways.** The brief says the limit "must be at
  least `EARMARK_MAX_ATTACHMENT_MB`". That holds if AMAP counts decoded attachments. If it
  counts the whole message, base64 makes it about 1.37 times the cap (2.4), so the check
  asks for that too. Nothing in earmark changes.
- **D4: The layout gains a `.github/` line.** The brief's block names every top-level
  directory except it, and the completeness test (6.1) needs it. Top-level files
  (`README.md`, `pyproject.toml`, …) are not listed, as in the brief.
- **D5: The doc tests grow** (4.6, section 6): a layout checked against the repo and the
  status table, a planned-names scan that relaxes as steps 29–31 land, an allowlist-wording
  scan, and the placeholder scan over `CLAUDE.md`, the System section and this plan. The
  brief asks only that the system section is pinned.
- **D6: ARCHITECTURE's Re-scope copy gets PLAN's "Superseded" note** (2.1), so the two copies
  agree again. PLAN's Re-scope is not edited.
- **D7: The planner did not edit `docs/ARCHITECTURE.md`.** Its changes are the step's
  deliverable and are specified in 4.1 for the implementer. No code contract changes.
- **No dependency is added.**

## 9. Out of scope

- **Creating `contract/`, the plugin, the marketplace or the Shortcut file** (steps 29, 30,
  31), and moving the contract text out of ARCHITECTURE and PLAN (step 29).
- **The install commands and the receiving skill's behaviour** in the runbook: step 30 writes
  phase 11 and extends phase 12's check. Whether the runbook may then say "Claude Code" or
  `.claude-plugin` (2.2) is step 30's to settle.
- **`docs/shortcut.md`**: it stays the Shortcut's only doc; step 31 changes it.
- **`docs/PLAN.md`**: its status table is the orchestrator's, and its Re-scope copy needs no
  change.
- **The open operator decisions**: which PKA instances install the skill, and the optional
  session-start mention of unprocessed earmark mail.
- **Anything in code**, including a lower default for `EARMARK_MAX_ATTACHMENT_MB`, until
  AMAP's limit is known.
