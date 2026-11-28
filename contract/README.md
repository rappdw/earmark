# The mail seam and the /v1 API

earmark sends a transcript to a PKA instance as one email with two Markdown attachments.
This folder is that seam: the contract text, sample emails built by the real mail code, and
a pointer to the checker. The service sends, the agent mailbox layer (outside this repo)
delivers, and the receiving skill (step 30) checks and files. The request seam, from the
Shortcut to the service, has no folder here.

## Versions

| Version | Text | Samples | Status |
|---|---|---|---|
| `1` | `mail-v1.md` | `samples/v1/` (frozen) | `DRAFT`, still accepted |
| `2` | `mail-v2.md` | `samples/v2/` | `DRAFT`, what the service sends |

Version 2 (step 32) adds YouTube: `type: video`, `origin: youtube` and the `channel` key. It
is written as a list of changes to version 1. The service sends `contract: 2` for all mail,
and the receiving skill and the checker accept both versions. A revision is a new text and a
new samples folder. The old files stay, because mail sent under an earlier version can
still be waiting in an inbox. `docs/PLAN.md` and `docs/ARCHITECTURE.md` are updated in the
same commit. A test pins each text by hash, so an edit to one cannot pass quietly, and the v1
samples are frozen: they are pinned by hash, because the mail code no longer builds them.

## The checker

`plugins/earmark-pka/skills/earmark-receive/scripts/check_mail.py` is the only copy (a test
enforces it), and the plugin and skill around it are step 30's, and its receiver imports it. It reads the standard
library only, needs Python 3.9 or newer, and is run as `python3 -I`, and checks contract 1 and contract 2:

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

`samples/v2/README.md` and `samples/v1/README.md` say what each sample shows. `make samples`
regenerates the v2 samples from `tests/contract_samples.py`; review the diff before
committing. The v1 samples are never regenerated.

## The /v1 API

`api-v1.md` (step 36) is the text of the `/v1` API, which programs such as earshot use to
have audio transcribed with no mail and no push. It is the third seam. A test pins the text
between its markers by hash and checks every example response in it against the real app
(`tests/test_api_v1_contract.py`; the examples are built by `tests/api_v1_examples.py`). It
changes only additively; a breaking change is `/v2`, with its own file. It has no samples
folder or checker: the examples in the text are the samples.

Version 1.1 (step 37) added summaries: `POST /v1/summaries`, `GET /v1/jobs/{id}/summary`, a
`kind` field on the job, and the input caps. A transcript and instructions go to a model
server the operator runs on the service's host or its private network, never to a cloud
model. Section 13 of the text lists the revisions.
