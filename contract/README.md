# The mail seam

earmark sends a transcript to a PKA instance as one email with two Markdown attachments.
This folder is that seam: the contract text, sample emails built by the real mail code, and
a pointer to the checker. The service sends, the agent mailbox layer (outside this repo)
delivers, and the receiving skill (step 30) checks and files. The request seam, from the
Shortcut to the service, has no folder here.

## Versions

| Version | Text | Samples | Status |
|---|---|---|---|
| `1` | `mail-v1.md` | `samples/v1/` | `DRAFT` |

A revision is a new text and a new samples folder, `mail-v2.md` and `samples/v2/`. The old
files stay, because mail sent under v1 can still be waiting in an inbox. `docs/PLAN.md` and
`docs/ARCHITECTURE.md` are updated in the same commit. A test pins the v1 text by hash, so
an edit to it cannot pass quietly.

## The checker

`plugins/earmark-pka/skills/earmark-receive/scripts/check_mail.py` is the only copy (a test
enforces it), and the plugin and skill around it are step 30's, and its receiver imports it. It reads the standard
library only, needs Python 3.9 or newer, and is run as `python3 -I`:

```text
python3 -I check_mail.py message [--json] PATH
python3 -I check_mail.py files [--json] --note PATH --note-name NAME
    --instructions PATH --instructions-name NAME [--note-media-type TYPE]
    [--instructions-media-type TYPE] [--subject-file PATH] [--body-file PATH]
python3 -I check_mail.py rules [--json]
```

`message` checks a whole `.eml`. `files` checks the parts a PKA's inbox tools return, with
the attachment names given separately; a rule whose input is not given is skipped and
listed. `rules` lists what is checked. The exit codes are 0 (every rule that ran passed),
1 (a rule failed), 2 (a prerequisite is missing, such as Python older than 3.9 or an
unreadable file) and 64 (a usage error).

## What the checker cannot check

- That paragraphs cover 30 to 60 seconds or one speaker turn: the note carries no segment
  ends or speakers.
- That the body holds only text earmark generates, beyond the three body rules.
- That the instructions are the requester's words verbatim.
- That `job_id` is stable across a resend and new on a re-share, and the de-duplication
  rules. These relate several messages. The service's tests check them on the samples, and
  the receiving skill applies them.

## Samples

`samples/v1/README.md` says what each sample shows. `make samples` regenerates them from
`tests/contract_samples.py`; review the diff before committing.
