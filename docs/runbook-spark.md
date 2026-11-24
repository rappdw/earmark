# Runbook: setting up earmark on the DGX Spark

This runbook takes the setup in the order of the choices already made:

1. Docker Compose on the Spark, with the GPU image for the worker.
2. The GPU smoke test, then a real transcription into the fake outbox.
3. A sending mailbox, and a real delivery to your own address.
4. The PKA recipients.
5. A DNS-only Cloudflare name, served by Caddy on the LAN and reached over WireGuard.
6. ntfy notifications, then the iOS Shortcut.
7. The receiving skill on each PKA (step 30, not built yet), then one episode through the
   whole system.

Each phase ends with a **Check**. Stop at the first check that fails: later phases add
layers on top, and debugging is much easier one layer at a time.
[docs/deploy.md](deploy.md) is the reference behind every step; this runbook links to its
sections rather than repeating them.

**What has never run.** None of this has been run on real hardware yet. The build sandbox
had no Docker, GPU, iPhone, mail server or network of yours. The pipeline itself was run
end to end on fake backends, and the GPU image has only been linted. Treat every phase as a
first run, and note what differs from what is written here. The receiving skill and the
Shortcut file are not built yet (steps 30 and 31), and the whole system has never run:
phase 12 is its first run.

## Your values

Fill these in once. The commands below use the placeholders on the left.

| Placeholder | Meaning | Yours |
|---|---|---|
| `spark` | The Spark's hostname. | |
| `192.168.1.10` | The Spark's LAN address. A DHCP reservation keeps it fixed. | |
| `earmark.example.com` | The API's name, in your Cloudflare zone. | |
| `example.com` | That Cloudflare zone. | |
| `example.org` | Your mail domain. | |
| `earmark@example.org` | The sending mailbox. | |
| `smtp.example.org` | The provider's submission host. | |
| `you@example.org` | Your own inbox, for test mail. | |
| `pka-personal@example.net`, `pka-work@example.net` | The two PKA addresses. | |
| `/path/to/earmark` | The checkout on the Spark. | |

## Phase 0: Before you start

- [ ] Shell access to the Spark with `sudo`.
- [ ] The GPU is visible:

  ```bash
  nvidia-smi --query-gpu=name,compute_cap,driver_version --format=csv,noheader
  ```

- [ ] The NVIDIA Container Toolkit:

  ```bash
  nvidia-ctk --version
  docker info | grep -i nvidia
  ```

- [ ] Docker Compose 2.30 or later:

  ```bash
  docker compose version
  ```

- [ ] Free disk space, about 30 GB, because the build footprint was not measured:

  ```bash
  df -h /var/lib/docker
  ```

- [ ] A domain you control with DNS access, and a mail provider with SMTP submission and
  app passwords.
- [ ] The two PKA addresses, and access to the AMAP router's configuration for them: you
  add a sender to their allowlist, and read their attachment limit.
- [ ] A session on each PKA instance, to see what arrives.
- [ ] Access to the Cloudflare zone, and an existing WireGuard server on your network.
- [ ] The ntfy app on the phone.

**Check:** each command prints what is listed: the GB10 and `12.1` from `nvidia-smi`, a
version from `nvidia-ctk`, an `nvidia` runtime in `docker info`, Compose 2.30 or later, and
about 30 GB free. These ticks replace any earlier phase-0 list.

## Phase 1: Upgrading from v0.1.0

Skip this on a new install.

<!-- upgrade-v0.1.0:begin -->
earmark 0.1.0 analysed episodes into a vault. The current service does not, and it refuses
a v0.1.0 database. [docs/deploy.md](deploy.md#14-upgrading-from-v010) section 14 has the
commands. In short:

- Stop the stack, pull, and rebuild.
- Move the old database aside with its `-wal` and `-shm` files, into `v0.1.0/` in the data
  volume.
- Delete `analyses/`, the index (`lancedb/`) and the embedding model (`models/embed/`),
  plus the digests and lock files.
- **The vault is no longer touched.** Your notes stay as they are.
- Clean the `.env`: remove the `EARMARK_LLM_*`, vault, store, index, embed and
  time-zone settings, `COMPOSE_PROFILES`, and the vLLM lines.
<!-- upgrade-v0.1.0:end -->

**Check:** as deploy section 14: `docker compose run --rm worker doctor` no longer shows
`FAIL database … was created by earmark 0.1.0`, and the `grep` in that section prints
nothing.

## Phase 2: Get the code and write `.env`

```bash
git clone <this repository's URL> earmark && cd earmark
git checkout main
cp .env.example .env
chmod 600 .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'   # the API token
```

Edit `.env`. These are the settings for the first run:

```bash
EARMARK_API_TOKEN=<the token printed above>
EARMARK_ALLOWED_RECIPIENTS=you@example.org
EARMARK_MAIL_BACKEND=fake
```

- `you@example.org` is your own inbox, for now.
- `fake` writes messages to the outbox until phase 5.
- Notifications stay off while `EARMARK_NTFY_URL` is unset.
- The worker defaults to `large-v3` on `cuda` with `float16`.

Rules for this file:

- No value may contain `$`, because Compose interpolates it. Single-quote a value that has
  spaces.
- The Cloudflare token must **never** go in this file. Compose exports every line into
  earmark's containers.
- Leave `API_BIND` and `EARMARK_ALLOW_PRIVATE_FETCH` at their defaults.

**Check:** `ls -l .env` shows `-rw-------`, and `docker compose config --quiet` prints
nothing.

## Phase 3: Build the images and run the Spark smoke test

```bash
docker compose build api
```

Then run [the Spark smoke test](deploy.md#73-the-spark-smoke-test), steps 0 to 4, as
written. It is the only real check of the GPU image (`Dockerfile.gpu`). If a step fails, use
[section 7.4](deploy.md#74-if-the-smoke-test-fails). Then:

```bash
docker compose run --rm worker doctor
docker compose run --rm worker doctor --online
```

**Check:**

- step 2 prints `4.9.0`, a path under `/app/.venv/`, `1` and a list with `float16`;
- `no uv`;
- doctor shows `asr cuda ok "1 CUDA device, float16 supported"`, `recipients ok 1
  allowed`, and `mail warn` (the fake). It exits 0;
- step 4 logs `loaded Whisper model 'large-v3' in … s on cuda (float16)` and prints the JFK
  text;
- `doctor --online` shows the Apple lookup ok.

Write down the build time, the output of `docker image ls earmark:gpu`, and the "x real
time" figure with and without `-e EARMARK_ASR_BATCH_SIZE=8`.

## Phase 4: Start, then transcribe one episode and one upload

```bash
docker compose up -d
docker compose ps
curl -s http://127.0.0.1:8787/healthz
TOKEN=$(grep '^EARMARK_API_TOKEN=' .env | cut -d= -f2-)
```

A 13-minute Up First episode keeps it quick. The delimiter is unquoted, so the shell fills
in `timestamp`:

```bash
cat > body.json <<EOF
{
  "url": "https://podcasts.apple.com/us/podcast/id1222114325?i=1000803102998",
  "to": "you@example.org",
  "instructions": "What are the main policy disputes, and who is on each side?",
  "timestamp": "$(date -Iseconds)"
}
EOF
curl -s http://127.0.0.1:8787/transcriptions \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d @body.json
curl -s http://127.0.0.1:8787/transcriptions \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@jfk.flac" \
  --form-string to=you@example.org \
  --form-string instructions="File this under history." \
  --form-string timestamp="$(date -Iseconds)"
docker compose logs -f worker      # wait for two "done" lines, then Ctrl-C
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8787/jobs
docker compose cp api:/data/outbox ./outbox
```

Then read the messages with the Python reader in the
[README quickstart](../README.md#quickstart-fake-backends), over `./outbox/*.eml`, with
`python3`.

**Check:**

- both jobs are `done`, with `delivery_state` `sent` (the fake's);
- the worker log shows the transcription on `cuda` and how many times real time it ran;
- each message has the two attachments, and `transcript_sha256 ok`;
- the podcast note has `origin: "apple_podcasts"`, a `feed:` or `apple:` guid, and real
  text in `[hh:mm:ss]` paragraphs;
- the upload note has `origin: "upload"`, a `sha256:` guid and the JFK text;
- `shared_at` carries the Spark's offset.

## Phase 5: Email: the sending mailbox and a real delivery

1. Set up the mailbox, its SPF, DKIM and DMARC DNS records and the app password: deploy sections
   [8.2 to 8.4](deploy.md#84-the-app-password-and-the-settings).
2. In `.env`, replace the `EARMARK_MAIL_BACKEND=fake` line with the 8.4 lines
   (`EARMARK_MAIL_BACKEND=smtp` and the `EARMARK_SMTP_*` settings).
3. Recreate the containers and check the login:

   ```bash
   docker compose up -d
   docker compose run --rm worker doctor --online
   ```

4. Send a real delivery. Re-send phase 4's `body.json` with new `instructions` (edit the
   file) using the same JSON `curl`. The transcript is cached, so the job is quick, and the
   new instructions make it a new job.

**Check:** a message reaches your own address, and you open it:

- doctor shows `mail ok` and `smtp ok … login accepted, nothing sent`;
- within a minute a message reaches `you@example.org` with the subject `[earmark] <the
  episode title>`;
- its body names both attachments (`….md` as the transcript note, and `….instructions.md`)
  and the job id;
- both attachments open, on the phone and on the Mac. The note shows the transcript, and
  the instructions file shows your new instructions;
- `GET /jobs/<job id>` shows `delivery_state` `sent` with `sent_at`;
- the original message's `Authentication-Results` header shows `dkim=pass` with
  `header.d=example.org`, and `dmarc=pass`. If DMARC fails, fix it before phase 6: AMAP's
  router does not make the attachments of mail that fails DMARC readable.

## Phase 6: The PKA recipients and AMAP

1. For each PKA address, add `earmark@example.org` to the AMAP router's sender allowlist
   for that address. This is AMAP router configuration, which you set. It is not something
   to ask a PKA for, and earmark cannot see it: SMTP's reply is all it learns
   ([deploy.md section 8.5](deploy.md#85-the-pka-side)).
2. Find AMAP's attachment limit for each PKA address, and whether it counts the attachments
   or the whole message. Compare it with `EARMARK_MAX_ATTACHMENT_MB` (default 10 MiB, the
   two attachments together): a limit on the attachments must be at least the cap; a limit
   on the whole message must be at least about 1.37 times the cap (about 14 MiB at the
   default), because base64 adds about a third. If it is lower, set
   `EARMARK_MAX_ATTACHMENT_MB` to a whole number of MiB that fits.
3. Set `EARMARK_ALLOWED_RECIPIENTS=pka-personal@example.net,pka-work@example.net` in
   `.env` (keep `you@example.org` for test mail), and recreate the containers with
   `docker compose up -d`.
4. Send one transcription to each PKA, with instructions such as "File this; no analysis".

`all` is the other setting: it lets anyone holding the API token have earmark mail any
address from your domain. Read [deploy.md section 9](deploy.md#9-recipients-earmark_allowed_recipients)
before using it.

The mail contract is DRAFT until you approve the receiving procedure, which is the
receiving skill (step 30).

**Check:**

- doctor shows `recipients ok 2 allowed: p…@example.net, p…@example.net` (or 3);
- a request to `stranger@example.com` gets 422 `to is not an allowed recipient
  (EARMARK_ALLOWED_RECIPIENTS)`;
- you have written down AMAP's attachment limit for each address and how it counts, and it
  is at least `EARMARK_MAX_ATTACHMENT_MB` (or about 1.37 times it, for a whole-message
  limit);
- in each PKA's session, the message arrives (the doorbell rings, or it is there at the
  next session), and the `inbox` tools show both attachments with disposition `clean`.

Filing is not checked here: it needs the receiving skill (phases 11 and 12).

## Phase 7: Reaching the API from the phone and the Mac

The API stays on `127.0.0.1:8787`. Caddy serves it over HTTPS on the LAN address, and a
DNS-only Cloudflare record points the name at that private address
([deploy.md section 6.1](deploy.md#61-a-private-dns-name-wireguard-and-caddy-primary)).

1. **DNS record.** In Cloudflare, add an `A` record `earmark.example.com` →
   `192.168.1.10`, with proxy status **DNS only (grey cloud)**. It publishes your internal
   address, which is a minor leak.
2. **Cloudflare token.** Create an API token with **Zone → DNS → Edit** and
   **Zone → Zone → Read**, and limit it to the one zone. Edit alone fails.
3. **Caddy with the Cloudflare module.** Build or download it, then confirm the module is
   there:

   ```bash
   xcaddy build v2.11.9 --with github.com/caddy-dns/cloudflare@v0.2.4
   ./caddy list-modules | grep dns.providers.cloudflare
   ```

   Install it as `/usr/bin/caddy` behind Caddy's own systemd unit (6.1.3).
4. **Config and token.**

   ```bash
   sudo cp deploy/caddy/Caddyfile /etc/caddy/Caddyfile   # then edit the name and bind
   sudo install -m 600 /dev/null /etc/caddy/cloudflare.env
   sudoedit /etc/caddy/cloudflare.env                    # CF_API_TOKEN=<token>
   sudo systemctl edit caddy        # [Service] EnvironmentFile=/etc/caddy/cloudflare.env
   sudo caddy validate --config /etc/caddy/Caddyfile
   sudo systemctl restart caddy && journalctl -u caddy -f   # wait for the certificate
   ```

5. **WireGuard on the phone and the Mac.** Add the Spark to the existing tunnel:
   `AllowedIPs = 192.168.1.10/32` (a split tunnel), `DNS =` unset. Turn on on-demand
   activation for cellular and for Wi-Fi other than the home SSIDs.

**Check:**

- `dig +short earmark.example.com @1.1.1.1` prints `192.168.1.10`.
- `dig earmark.example.com` through your home resolver gives the same answer. If it
  doesn't, DNS-rebinding protection is dropping it. Allow-list the domain (6.1.6), for
  example `rebind-domain-ok=/example.com/` for dnsmasq or Pi-hole.
- From the Mac at home, `curl https://earmark.example.com/healthz` prints
  `{"status":"ok"}` with a valid certificate.
- On the phone, on cellular with Wi-Fi off, Safari opens
  `https://earmark.example.com/healthz`.
- An upload through the proxy from the Mac: the phase 4 upload `curl`, pointed at
  `https://earmark.example.com/transcriptions`, answers 202. See
  [deploy.md section 6.3](deploy.md#63-uploads-through-the-proxy).

## Phase 8: Notifications (ntfy)

1. Pick a long random topic of at least 12 characters, for example from the token command,
   and subscribe to it in the ntfy iOS app.
2. In `.env`, set `EARMARK_NTFY_URL=https://ntfy.example.com/<topic>`, or your ntfy.sh
   topic URL. If you self-host ntfy, set `upstream-base-url: "https://ntfy.sh"` in its
   config (deploy section 10).
3. Run `docker compose up -d` to recreate the containers with the new `.env`, then
   `docker compose run --rm worker doctor --online`.

**Check:**

- `doctor --online` shows ntfy ok. It never sends a test message;
- a re-share brings a `Sent: <title>` push with `To: p…@example.net` and the duration;
- a request for `https://example.com/not-audio.mp3` (to an allowed address) fails, and a
  `Failed:` push names the host and the reason. Nothing is mailed.

## Phase 9: The Shortcut

Build it from [docs/shortcut.md](shortcut.md) section 2. First capture the share sheet
(shortcut.md section 6), for a podcast link and for a file. Then share an episode and
choose Personal, and share a voice memo or an audio file from Files and choose Work.

**Check:**

- the Shortcut shows a `job_id` each time;
- the pushes arrive;
- each PKA gets its message;
- the note's `shared_at` ends in your phone's offset, such as `-08:00`, not `+00:00`: this
  proves `timestamp` is sent;
- a second share of the same episode with new instructions makes a new job, and the worker
  log shows no new transcription;
- an identical second share within 10 minutes answers `"created": false`.

## Phase 10: Backups, upgrades and stopping

- Back up and upgrade as in deploy sections 12 and 15.
- Keep `.env` in a password manager.
- Upgrade with `git pull && docker compose build && docker compose up -d`, then repeat smoke
  steps 2 and 4.
- `docker compose stop` releases a running job back to the queue. Nothing is lost.

**Check:** after `docker compose stop` and `docker compose up -d`, `docker compose ps`
shows `api` healthy, and `GET /jobs` still lists the earlier jobs.

## Phase 11: Install the receiving skill on each PKA (step 30)

Not written yet. The receiving skill, `earmark-receive` in the `earmark-pka` plugin from
this repository's plugin marketplace, is not built: step 30 builds it and writes this
phase. It installs at project scope, on the PKA instances you choose, never at user scope.

**Check:** None yet. Until step 30 fills in this phase there is nothing to check, and
phase 12 cannot pass.

## Phase 12: The whole system: one episode, end to end

This is the system's definition of done
([ARCHITECTURE.md](ARCHITECTURE.md#definition-of-done)). Every earlier phase checks one
part; this checks them together. It cannot pass before phase 11 exists (step 30).

1. On the phone, in Apple Podcasts, share one episode to the Earmark Shortcut and choose a
   PKA whose instance has the receiving skill.
2. As instructions, give something inside the receiving skill's narrow scope, for example
   "File this under podcasts/economics and tag it economics." (a destination folder and a
   tag).
3. Wait for the `Sent:` push, then let that PKA's session handle the mail (the doorbell, or
   the next session).

**Check:**

- the Shortcut shows a `job_id`, and the push says `Sent: <title>` with
  `To: p…@example.net`;
- the worker log shows the episode transcribed on `cuda`, or the cached transcript reused
  if it was shared before;
- the PKA reads the message with the `inbox` tools and handles it through the receiving
  skill, not by hand;
- the note is filed in the PKA's workspace, with `source: "earmark"` and the same `guid` as
  the attachment, and the PKA lists the changes it made and commits nothing;
- the instructions were acted on: the note is in the folder you named and carries the tag;
- step 30 adds the receiving skill's own evidence to this check. Until then, write down
  what the PKA showed.

## Send back

These close the remaining gaps:

1. **The share-sheet captures.** For links, a fixture entry in
   `tests/fixtures/share_sheet.json`. For files, what `Get Type` showed, and the file's name.
2. **The phase 3 figures:** the build time, the image size, and the times real time with
   and without batching.
3. **The `Authentication-Results` line from phase 5,** with the addresses removed.
4. **Anything that differed from this runbook,** with `docker compose logs worker --since
   1h` (secrets and URL paths are already scrubbed from it) and the doctor output.
5. **AMAP's attachment limit** for each PKA address, and how it counts (phase 6).
6. **The phase 12 outcome:** pass, or the first thing that failed.
