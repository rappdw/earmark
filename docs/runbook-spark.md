# Runbook: setting up earmark on the DGX Spark

This runbook takes the setup in the order of the choices already made:

1. Docker Compose on the Spark.
2. Claude for analysis.
3. GPU transcription through the `asr` (vLLM) service.
4. A DNS-only Cloudflare name, served by Caddy on the LAN and reached over WireGuard.
5. ntfy notifications, then the iOS Shortcut.

Each phase ends with a **Check**. Stop at the first check that fails: later phases add
layers on top, and debugging is much easier one layer at a time.
[docs/deploy.md](deploy.md) is the reference behind every step; this runbook links to
its sections rather than repeating them.

**What has never run.** None of this has been run on real hardware yet. The build
sandbox had no Docker, GPU, iPhone or network of yours. The pipeline itself was dry-run
end to end on CPU, with a real Apple link, a real download and real transcription. Treat
every phase as a first run, and note what differs from what is written here.

## Your values

Fill these in once. The commands below use the placeholders on the left.

| Placeholder | Meaning | Yours |
|---|---|---|
| `spark` | The Spark's hostname. | |
| `192.168.1.10` | The Spark's LAN address. A DHCP reservation keeps it fixed. | |
| `earmark.example.com` | The API's name, in your Cloudflare zone. | |
| `example.com` | That Cloudflare zone. | |
| `/path/to/vault` | The Obsidian vault folder on the Spark. | |
| `<vault name>` | The vault's name as Obsidian on the phone shows it. | |
| `<your zone>` | Your time zone, for example `America/Denver`. | |

## Phase 0: Before you start

- [ ] Shell access to the Spark with `sudo`.
- [ ] Docker and Compose v2.24 or later:

  ```bash
  docker compose version
  ```

- [ ] The NVIDIA container runtime, which DGX OS ships:

  ```bash
  nvidia-smi
  docker info | grep -i nvidia
  ```

- [ ] Free disk space. The earmark image is about 1 GB. NVIDIA's vLLM image is several GB
  more, and the Whisper weights about 3 GB more. Check with `df -h /var/lib/docker`.
- [ ] The vault folder exists on the Spark, and something you already use keeps it in
  sync with the phone. Earmark only writes files into it.
- [ ] An Anthropic API key.
- [ ] Access to the Cloudflare zone, and an existing WireGuard server on your network.

## Phase 1: Get the code and write `.env`

```bash
git clone https://github.com/rappdw/earmark.git earmark && cd earmark
git checkout main
cp .env.example .env
chmod 600 .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'   # the API token
ls -ln /path/to/vault                                           # uid and gid for APP_UID / APP_GID
```

Edit `.env`. These are the settings for the first run.

Transcription starts on the CPU with a small model, so that everything except the GPU is
proven first. Notifications stay off until phase 5. They are off simply because
`EARMARK_NTFY_URL` is unset.

```bash
EARMARK_API_TOKEN=<the token printed above>
EARMARK_LLM_API_KEY=<your Anthropic key>
EARMARK_TZ=<your zone>
EARMARK_OBSIDIAN_VAULT_NAME=<vault name>
EARMARK_ASR_MODEL=small
VAULT_PATH=/path/to/vault
APP_UID=<uid from ls -ln>
APP_GID=<gid from ls -ln>
```

Rules for this file:

- No value may contain `$`, because Compose interpolates it.
- The Cloudflare token must **never** go in this file. Compose exports every line into
  earmark's containers.
- Leave `API_BIND` and `EARMARK_ALLOW_PRIVATE_FETCH` at their defaults.

**Check:** `ls -l .env` shows `-rw-------`, and `docker compose config --quiet` prints
nothing. That second command proves `VAULT_PATH` is set and the file parses.

## Phase 2: Build, check, start

```bash
docker compose build
docker compose run --rm worker doctor
docker compose run --rm worker doctor --online
docker compose up -d
docker compose ps
curl -s http://127.0.0.1:8787/healthz
```

**Check:**

- `doctor` exits 0. Warnings are fine. On a first run, expect "… is not downloaded yet"
  for both the Whisper model and the embedding model.
- `doctor --online` reports Apple, Anthropic and the model as ok.
- `docker compose ps` shows `api` as healthy.
- `curl` prints `{"status":"ok"}`.

If `doctor` fails, its message names the setting to fix. A permission error on the vault
means `APP_UID` and `APP_GID` don't match the vault's owner, and changing them needs
`docker compose build` again ([deploy.md section 4](deploy.md#4-install-with-docker-compose)).

## Phase 3: One real episode, from the Spark itself

This is the same flow as the sandbox dry run, now with real Claude analysis. A
13-minute Up First episode keeps it quick. The analysis should cost well under a dollar.

```bash
TOKEN=$(grep '^EARMARK_API_TOKEN=' .env | cut -d= -f2-)
curl -s http://127.0.0.1:8787/ingest \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"url": "https://podcasts.apple.com/us/podcast/id1222114325?i=1000803102998",
       "prompt": "What are the main policy disputes, and who is on each side?"}'
docker compose logs -f worker      # wait for "job … done", then Ctrl-C
docker compose run --rm worker jobs
docker compose run --rm worker search "government funding"
```

**Check:**

- The job is `done` with no warnings.
- `/path/to/vault/Earmark/Podcasts/Up First from NPR/` holds the episode note and a
  `transcripts/` note.
- The analysis is real, not the fake text, and names real people and claims with
  `[HH:MM:SS]` times.
- Entity notes exist under `Earmark/Entities/`.
- `search` returns hits. The first index write downloads the 65 MB embedding model.
- Open the note in Obsidian on the Mac, and check that its links resolve.

Write down how long transcription took on the CPU with `small`, from the worker log line
"transcribed … (Nx real time)". It is the baseline for phase 4.

## Phase 4: GPU transcription (the `asr` service)

The image is CPU-only, because the aarch64 CTranslate2 wheel has no CUDA. The GPU path is
NVIDIA's vLLM container serving Whisper, which the worker calls over the compose network
([deploy.md section 7b](deploy.md#7b-the-gpu-profile-the-recommended-gpu-path-untested)).
This profile has never run anywhere.

Change these lines in `.env`. **`EARMARK_ASR_MODEL` must change too.** The `asr` service
serves whatever it names, and `small` (or the `.env.example` default, `large-v3`) is not a
model id vLLM can load.

```bash
EARMARK_ASR_BACKEND=remote
EARMARK_ASR_BASE_URL=http://asr:8000/v1
EARMARK_ASR_MODEL=openai/whisper-large-v3-turbo
EARMARK_ASR_API_KEY=<another random string from the token command>
EARMARK_ASR_MAX_UPLOAD_MB=1024
COMPOSE_PROFILES=gpu
```

`COMPOSE_PROFILES=gpu` makes plain `docker compose up -d` include `asr` from now on. Then:

```bash
docker compose up -d
docker compose logs -f asr      # wait until vLLM reports the model loaded and the server up
nvidia-smi                      # a vllm process holds GPU memory
docker compose run --rm worker doctor --online
```

Then share a **different** episode, because the first one's transcript is cached and
would not be transcribed again. Use a long one, around an hour, to see the difference:
the same `curl` as in phase 3 with another `?i=` id, then `docker compose logs -f worker`.

**Check:**

- `doctor --online` reports the ASR server ok.
- The job is `done`, and the transcript note says `model: "remote:openai/whisper-large-v3-turbo"`.
- Names are spelled noticeably better than with `small`.
- Compare the time with phase 3. A first transcription can fail transiently while vLLM
  is still loading; the worker retries it with backoff.

If it doesn't work, check these, in order:

- `docker compose logs asr`.
- The GPU reservation: `docker compose config | grep -A6 devices`.
- Whether vLLM refuses the audio format. Whether its ffmpeg decodes M4A is unverified.

To go back to CPU, restore the phase 1 ASR lines, remove `COMPOSE_PROFILES`, and run
`docker compose up -d --remove-orphans`.

Optional: vLLM has its own usage statistics, separate from Hugging Face's. Its
documentation describes `VLLM_NO_USAGE_STATS=1` or `DO_NOT_TRACK=1` to turn them off.
Neither is set in `compose.yaml` or verified here. Ask if you want it added.

## Phase 5: Reaching the API from the phone and the Mac

The API stays on `127.0.0.1:8787`. Caddy serves it over HTTPS on the LAN address, and a
DNS-only Cloudflare record points the name at that private address
([deploy.md section 6.1](deploy.md#61-a-private-dns-name-wireguard-and-caddy-primary)).

1. **DNS record.** In Cloudflare, add an `A` record `earmark.example.com` →
   `192.168.1.10`, with proxy status **DNS only (grey cloud)**. It publishes your
   internal address, which is a minor leak.
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

## Phase 6: Notifications (ntfy)

1. Pick a long random topic of at least 12 characters, for example from the token
   command, and subscribe to it in the ntfy iOS app.
2. In `.env`, set `EARMARK_NTFY_URL=https://ntfy.sh/<topic>`. If you self-host ntfy, set
   `upstream-base-url: "https://ntfy.sh"` in its config (section 10).
3. Run `docker compose up -d` to recreate the containers with the new `.env`, then
   `docker compose run --rm worker doctor --online`.

**Check:** `doctor --online` reports ntfy ok. It never sends a test message. Share any
episode again with a new question using the phase 3 `curl`. A "Note ready" notification
arrives, and tapping it opens the note in Obsidian.

## Phase 7: The Shortcut

Build it from [docs/shortcut.md](shortcut.md) section 2, using the URL
`https://earmark.example.com/ingest` and `Authorization: Bearer <token>`.

**First, capture what the share sheet really sends** (shortcut.md section 6). The build
could never check this:

1. Temporarily add **Show Result** (Shortcut Input) as the first action.
2. Share an episode with "From Start", then again with "From [time]".
3. Write down exactly what is shown, then remove that action.

Then share an episode with a question.

**Check:**

- The Shortcut shows a `job_id`.
- The notification arrives.
- The note is in the vault and synced to the phone.
- A second share of the same episode with a new question adds a section without
  downloading again.

## Phase 8: The weekly digest

Compose has no scheduler, so add a host cron line, adjusting the path. It runs every
Monday at 07:07:

```text
7 7 * * 1 cd /home/you/earmark && docker compose run --rm --no-deps worker digest
```

**Check:** `docker compose run --rm --no-deps worker digest --since 7d` writes
`Earmark/Digests/<first day> to <last day>.md`, with themes linking to the episodes from
phases 3 to 7. Keep `EARMARK_TZ` equal to the host's time zone (section 14).

## Phase 9: Backups and upgrades

- **Back up:** the vault, and the data volume's `episodes/` folder, which holds the
  transcripts and analyses. Find the volume with
  `docker volume inspect earmark_data --format '{{ .Mountpoint }}'`.
- **Upgrade:** run `git pull && docker compose build && docker compose up -d`.
  Migrations run on start.
- **Stop:** `docker compose stop` releases a running job back to the queue. Nothing is
  lost.

## Send back

These close the remaining gaps:

1. **The share-sheet capture from phase 7.** It becomes a test case in
   `tests/fixtures/share_sheet.json`.
2. **Phase 4 timings.** CPU `small` against GPU `large-v3-turbo`, with the episode
   lengths.
3. **Anything that differed from this runbook**, with `docker compose logs worker --since
   1h` (secrets and URL paths are already scrubbed from it) and the `doctor --online`
   output.
4. **Whether vLLM accepted every episode's audio format**, especially M4A.

Optional: to run the live Claude tests in CI, create a GitHub environment named `llm`
with an `ANTHROPIC_API_KEY` secret, then run the "LLM test" workflow by hand from the
Actions tab.
