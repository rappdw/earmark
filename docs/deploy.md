# Deploying earmark

How to run the API and the worker on one machine (a DGX Spark in the examples), and how
the iPhone and the Mac reach the API. The examples use the placeholders `spark` (the
host), `earmark.example.com` (the name of the API), `192.168.1.10` (the host's address on
the home network), and `tailnet-name`.

## 1. What is tested

- The container image is build-tested in CI (`.github/workflows/container.yml` on
  linux/arm64), not run-tested. CI builds it and runs only `earmark --version`, an import
  of the extras, and a user-id check inside it. The API and worker have never run in a
  container, because the build sandbox has no Docker.
- The `gpu` profile is untested: its vLLM service was never built or run.
- The systemd units and timer are examples, reviewed but never run. The compose command
  for the weekly digest (section 14) has not run either.
- The reachability setup is not verified by the earmark build. That covers the DNS-only
  Cloudflare record, the WireGuard split tunnel, Caddy with the DNS-01 challenge,
  Tailscale Serve, and ntfy self-hosting, all described from their documentation. Only the
  example `deploy/caddy/Caddyfile` was checked, with `caddy validate` and `caddy fmt`,
  using a Caddy build that has the Cloudflare module.

## 2. Overview

- The API and the worker run on one host and share one data directory.
- The vault folder is on the same host. Whatever you already use to sync the vault to the
  phone does that; earmark only writes files.
- **The API is never exposed publicly.** It listens on `127.0.0.1:8787`, and a reverse
  proxy on the host's LAN interface serves it over HTTPS as `earmark.example.com`. That
  name resolves, through public DNS, to a private address that is reachable only at home
  or over WireGuard.

```text
iPhone / Mac ──(home Wi-Fi, or WireGuard split tunnel)──► 192.168.1.10:443
  Caddy (TLS for earmark.example.com) ──► 127.0.0.1:8787 earmark API
Cloudflare DNS: earmark.example.com A 192.168.1.10 (DNS only, never proxied)
```

## 3. Requirements

- aarch64 or x86_64 Linux. The image builds for the host's architecture; CI checks
  arm64.
- Docker Engine with Compose v2.24 or later, or uv for a native install.
- `EARMARK_DATA_DIR` on a local filesystem. SQLite's write-ahead log needs shared memory,
  so no NFS or SMB. A Docker named volume is fine.
- A vault folder that exists.
- Disk: the image is about 1 GB, Whisper `large-v3` about 3 GB, and the embedding model
  65 MB. The `index` extra adds about 440 MB to the image.
- An Anthropic API key, or a local LLM ([docs/llm.md](llm.md)).

## 4. Install with Docker Compose

```bash
git clone <this repository> earmark && cd earmark
cp .env.example .env
chmod 600 .env
```

Edit `.env`:

- `EARMARK_API_TOKEN`: generate one with
  `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`.
- `EARMARK_LLM_API_KEY`: your Anthropic key (or point earmark at a local LLM, section 8).
- `VAULT_PATH`: the vault folder on this host.
- `APP_UID` and `APP_GID`: the numeric user and group of the vault's owner (`ls -ln` on the
  vault shows them, and `id -u` and `id -g` show yours).
- `EARMARK_TZ`: your time zone, such as `Europe/Berlin`. Without it, note dates use the
  container's zone, UTC.
- Optionally `EARMARK_OBSIDIAN_VAULT_NAME` and the ntfy settings (section 10).

Then build, check, and start:

```bash
docker compose build
docker compose run --rm worker doctor
docker compose up -d
docker compose logs -f worker
```

[`compose.yaml`](../compose.yaml) defines `api` (`earmark serve`) and `worker`
(`earmark worker`), which share one image, a named volume `data` at `/data`, and the
vault as a bind mount at `/vault`.

**Compose-only variables.** These are unprefixed, so earmark itself ignores them. Set them
in `.env`, which compose also reads.

| Variable | Default | Meaning |
|---|---|---|
| `VAULT_PATH` | none, required | The vault folder on the host, mounted at `/vault`. |
| `API_BIND` | `127.0.0.1:8787` | Host address and port the API is published on. Leave it at the default. |
| `APP_UID` | `1000` | User id the image runs as. Set it to the vault owner's id before building. |
| `APP_GID` | `1000` | Group id the image runs as. |
| `EXTRAS` | `transcribe index` | Optional extras installed in the image. |
| `ASR_GPU_MEMORY_UTILIZATION` | `0.2` | Share of GPU memory the `asr` service takes (`gpu` profile). |

Things to know:

- **What compose overrides.** It forces the in-container values of `EARMARK_DATA_DIR`,
  `EARMARK_VAULT_DIR`, `EARMARK_HOST`, `EARMARK_PORT`, and the two model directories
  (empty, so the defaults apply), whatever `.env` says. A `.env` shared with a native
  install keeps working.
- **Data and models live in the `data` volume**: the database, the episodes, the index,
  and the downloaded models (`/data/huggingface`, `/data/models/embed`). A restart does not
  download them again.
- **Ownership.** The image bakes the user id in at build time, so rebuild after changing
  `APP_UID`. A named volume takes its ownership from the image the first time it is used.
  A volume created under another id needs a one-time fix:
  `docker run --rm -v earmark_data:/data --user 0 --entrypoint chown earmark:local -R 1000:1000 /data`
  (with your ids). The vault is a bind mount, so its host ownership must let `APP_UID`
  write. Files earmark creates there belong to that user on the host.
- **Rootless Docker and `userns-remap` shift user ids.** This guide does not cover them.
- **Values in `.env` must not contain `$`.** Compose interpolates them.
- **The environment is exported.** Under compose, `env_file` puts every `.env` line into
  the container's environment. So, unlike a native run, an `ANTHROPIC_API_KEY` line in
  `.env` is seen by the Anthropic SDK. Prefer `EARMARK_LLM_API_KEY`.
- **`API_BIND` stays `127.0.0.1:8787`.** The reverse proxy (or `tailscale serve`) runs on
  the host and reaches the API there. Nothing in the default setup binds the API to a
  non-loopback host address: `EARMARK_HOST=0.0.0.0` exists only inside the container's own
  network namespace. Docker's published ports bypass ufw, so never set `API_BIND` to
  `0.0.0.0` or a LAN address. That would serve plain http to the whole LAN and skip the
  proxy's TLS.
- **Slimmer images.** `EXTRAS=index` with remote ASR, `EXTRAS=transcribe` with
  `EARMARK_INDEX_BACKEND=none`, or `EXTRAS=` for neither. Selecting a backend whose extra
  is missing makes the worker refuse to start, with a message that names the extra.

The image is CPU only, so GPU transcription uses the `asr` service (section 7). Its entrypoint is
`earmark` and its default command is `serve`. It runs as a non-root user, has no health check
of its own (the same image runs the worker; compose checks the API), and reads no `.env` file.

## 5. Install natively with systemd

1. Create a user `earmark` with `/srv/earmark` as its home, and put a checkout there.
2. Install uv system-wide, then, as that user:

   ```bash
   sudo -u earmark env UV_PYTHON_INSTALL_DIR=/srv/earmark/.python \
     uv sync --locked --no-dev --extra transcribe --extra index
   ```

3. Create `/srv/earmark/.env` (mode 0600, owned by `earmark`) with the settings from
   section 4, and `EARMARK_HOST=127.0.0.1` and `EARMARK_DATA_DIR=/srv/earmark/data`.
4. Copy the units from [`deploy/systemd/`](../deploy/systemd/earmark-api.service) to
   `/etc/systemd/system/`, and edit the vault path in
   [`earmark-worker.service`](../deploy/systemd/earmark-worker.service)'s
   `ReadWritePaths=`. Then:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now earmark-api earmark-worker
   ```

5. Check it. Run from `/srv/earmark`, so that `.env` is read:

   ```bash
   cd /srv/earmark && sudo -u earmark .venv/bin/earmark doctor
   ```

Run one worker per host.

## 6. Reaching the API from the iPhone and the Mac

**The API is never exposed publicly.** Earmark itself never terminates TLS. There are two
supported setups. Neither is verified by the earmark build.

### 6.1 A private DNS name, WireGuard, and Caddy (primary)

#### 6.1.1 The DNS record

- In Cloudflare, add `earmark.example.com` as an `A` record for the server's LAN address
  (`192.168.1.10`), with proxy status **DNS only (grey cloud)**.
- Proxying must stay off. Cloudflare's proxy cannot reach a private address, and it would
  put Cloudflare in the traffic path. With DNS only, Cloudflare answers DNS queries and
  never sees a request.
- Prefer the LAN address over the server's WireGuard interface address. The LAN address
  works at home without the tunnel, and through it.
- **Information leak.** The record publishes the internal address to anyone who looks the
  name up. That is a minor leak: it reveals the home subnet and the host's address, not a
  way in. Know it before you add the record.

#### 6.1.2 WireGuard on the phone and the Mac

- Use the WireGuard server your network already has (the router, OPNsense, Unifi, or one
  on the Spark). Setting one up is out of scope here.
- In the client's tunnel, set `AllowedIPs = 192.168.1.10/32`, or the home subnet such as
  `192.168.1.0/24`. That makes a **split tunnel**: only traffic to the server goes
  through it.
- Leave `DNS =` unset, so that the name resolves through the normal public resolver. If it
  points at the home resolver, the rebinding note in 6.1.6 applies there too.
- Turn on **on-demand** activation in the iOS and macOS WireGuard apps: activate on
  cellular, and on Wi-Fi except your home SSIDs.
- **On the home Wi-Fi the LAN routes directly**, so the tunnel stays down and nothing
  changes.

#### 6.1.3 The reverse proxy: Caddy on the LAN interface

Caddy needs one short site block, and it issues and renews the certificate by itself.

The stock Caddy build lacks the Cloudflare DNS module, so you need a build that has it
(`github.com/caddy-dns/cloudflare`):

- **Recommended, reproducible:**
  `xcaddy build v2.11.9 --with github.com/caddy-dns/cloudflare@v0.2.4` (needs Go).
- **Quick:** the download server,
  `https://caddyserver.com/api/download?os=linux&arch=arm64&p=github.com/caddy-dns/cloudflare`.
  It builds on demand, so it is not pinned.
- **In Docker:** the official image's builder pattern, `FROM caddy:2.11.9-builder`,
  `RUN xcaddy build --with github.com/caddy-dns/cloudflare@v0.2.4`, then copy
  `/usr/bin/caddy` into `caddy:2.11.9`. Run that container with `network_mode: host`, so it
  can bind the LAN address and reach `127.0.0.1`.

Check the build with `caddy list-modules | grep dns.providers.cloudflare`. Install the
binary as `/usr/bin/caddy` behind Caddy's own systemd unit, or use the Debian package's
documented `dpkg-divert` for custom builds.

Copy [`deploy/caddy/Caddyfile`](../deploy/caddy/Caddyfile) to `/etc/caddy/Caddyfile` and
replace the hostname and the `bind` address. `bind` must be the address the record points
to, never `0.0.0.0`. If that is a WireGuard interface address, the interface must be up
before Caddy starts. Port 80 is not used (`auto_https disable_redirects`).

Traefik works too. Its Cloudflare provider is built in (`CF_DNS_API_TOKEN`,
`dnsChallenge.provider: cloudflare`, an entrypoint on the LAN address, and a file-provider
route to `http://127.0.0.1:8787`), but it needs more configuration.

#### 6.1.4 The certificate: Let's Encrypt with DNS-01

- Create a Cloudflare API token with **Zone → DNS → Edit** and **Zone → Zone → Read**,
  and set Zone Resources to the one zone. Both Caddy's module and Traefik need the zone
  read to find the zone, so `Zone:DNS:Edit` alone is not enough.
- Put it in `/etc/caddy/cloudflare.env` as `CF_API_TOKEN=…` (mode 0600, owned by root),
  and load it with a `systemctl edit caddy` drop-in containing
  `EnvironmentFile=/etc/caddy/cloudflare.env`.
- The token belongs to the proxy only. Never put it in earmark's `.env`, which compose
  exports into earmark's containers.
- No inbound port is needed: Caddy proves control of the name by writing a TXT record.
- Troubleshooting: `Invalid request headers` means the token is unset or wrong.
  `expected 1 zone, got 0` and `timed out waiting for record to fully propagate` mean the
  proxy host's resolver overrides or caches the zone; the example's `resolvers 1.1.1.1`
  handles both.

#### 6.1.5 Why Caddy is not a compose service

The proxy is documented and shipped as an example file, not as a compose service:

- Compose's `env_file: .env` exports every line into the earmark containers. A Cloudflare
  token in the same file would land in the API and worker environments.
- The proxy is host infrastructure. It owns port 443 on the LAN address and often fronts
  other services. In Docker it would need `network_mode: host`, which removes most of the
  isolation a compose service brings.
- A stock image is not enough, because the Cloudflare module needs a custom build.
- One proxy setup then serves both the compose and the systemd install.

The cost is one more thing to install by hand, which this section walks through.

#### 6.1.6 DNS rebinding protection (a gotcha)

Many home resolvers silently drop a public answer that contains a private address, which is
exactly this record:

- Pi-hole with Unbound, per Pi-hole's Unbound guide (`private-address`);
- dnsmasq with `stop-dns-rebind`;
- OPNsense's Unbound rebind protection;
- many routers, Unifi among them.

How to tell:

- `dig +short earmark.example.com @1.1.1.1` prints `192.168.1.10`, but
  `dig earmark.example.com` through the home resolver returns no `A` record;
- dnsmasq logs "possible DNS-rebind attack detected";
- the Shortcut reports that the server cannot be found.

The fix is to allow-list the domain. Do not turn the protection off:

- dnsmasq (and Pi-hole v6, through `misc.dnsmasq_lines`): `rebind-domain-ok=/example.com/`;
- Unbound (Pi-hole's Unbound, and OPNsense's "Private Domains"): `private-domain:
  "example.com"`;
- routers: the "rebind protection" or "private domains" exception in their DNS settings.

A local DNS override for the name is an alternative. Caddy then still needs
`resolvers 1.1.1.1` (already in the example) for the challenge.

#### 6.1.7 Test

- From the Mac at home, then on cellular with the tunnel on:
  `curl https://earmark.example.com/healthz` prints `{"status":"ok"}`, with a valid
  certificate.
- From a host outside both, the name resolves but the connection fails. That is correct.
- The Shortcut URL is `https://earmark.example.com/ingest`
  ([docs/shortcut.md](shortcut.md)).

### 6.2 Tailscale Serve (the shorter alternative)

- Install Tailscale on the Spark, the iPhone, and the Mac, and turn on MagicDNS and HTTPS
  certificates in the admin console.
- Run `sudo tailscale serve --bg http://127.0.0.1:8787` and check it with
  `tailscale serve status`. The Shortcut URL is then
  `https://spark.tailnet-name.ts.net/ingest`.
- The Tailscale VPN must be on in the phone for the Shortcut to work.
- **Never use `tailscale funnel`.** It makes the service public.

## 7. Transcription on the DGX Spark

Three options.

### 7a. CPU, in the worker (the default)

`faster_whisper` with `large-v3` on the Spark's CPU is slow for long episodes. A smaller
`EARMARK_ASR_MODEL` (`distil-large-v3`, `large-v3-turbo`, `small`) is the stopgap. The PyPI
CTranslate2 wheel for aarch64 is CPU-only.

Hugging Face's `hf_xet` writes its own JSON logs under `$HF_HOME/xet/logs/`
(`/data/huggingface/xet/logs` in the container) and reports a download summary to the Hub.
`HF_HUB_DISABLE_TELEMETRY=1` (or `DO_NOT_TRACK=1`) and `HF_XET_LOG_DEST` (a directory ending
in `/`, or a file) are Hugging Face's settings for that; earmark has not tested them.

### 7b. The `gpu` profile (the recommended GPU path, untested)

- In `.env`:

  ```bash
  EARMARK_ASR_BACKEND=remote
  EARMARK_ASR_BASE_URL=http://asr:8000/v1
  EARMARK_ASR_MODEL=openai/whisper-large-v3-turbo
  EARMARK_ASR_API_KEY=<a random string>
  ```

  `EARMARK_ASR_MODEL` is also the model vLLM serves. Optionally set
  `ASR_GPU_MEMORY_UTILIZATION`; GPU memory is shared with any LLM you run.
- Then `docker compose --profile gpu up -d`. It needs the NVIDIA Container Toolkit, which
  DGX OS ships.
- The `asr` service is NVIDIA's vLLM container, pinned by digest to the `26.10-py3` tag.
  Bump it by hand. It publishes no port: only the worker reaches it, over the compose
  network.
- The audio limits are raised to 1024 MB, 14400 s, and 4 GiB, so episodes up to about four
  hours decode. Set `EARMARK_ASR_MAX_UPLOAD_MB=1024` so a larger file fails before upload.
- Until vLLM has loaded the model, the worker's first transcription fails transiently and
  is retried with backoff.
- The image ships an ffmpeg build. Whether it decodes M4A is unverified.
- For a server on another host, see [docs/asr.md](asr.md).

### 7c. faster-whisper on the GPU inside the worker (advanced, an unverified outline)

Nothing here was built. Upstream's CUDA 13 support issue (OpenNMT/CTranslate2#1933) is
open.

- Build CTranslate2 4.9.0 (the locked version) from source in an
  `nvidia/cuda:13.x-cudnn-devel-ubuntu24.04` image, with `-DWITH_CUDA=ON -DWITH_CUDNN=ON
  -DWITH_MKL=OFF -DOPENMP_RUNTIME=COMP -DCMAKE_CUDA_ARCHITECTURES=121`.
- Build its Python wheel for Python 3.13 with `CTRANSLATE2_ROOT` set.
- Install it over the locked wheel with `uv pip install --no-deps --reinstall`.
- Run on a CUDA 13 cuDNN runtime image, with `LD_LIBRARY_PATH` set.
- Run the venv's `earmark` directly, never `uv run`, which would sync the PyPI wheel back.
- Set `EARMARK_ASR_DEVICE=cuda`. The worker refuses to start if CTranslate2 sees no CUDA
  device. Verify with:

  ```python
  import ctranslate2

  print(ctranslate2.get_cuda_device_count(), ctranslate2.get_supported_compute_types("cuda"))
  ```

## 8. Analysis

- Claude is the default: set `EARMARK_LLM_API_KEY`, and optionally `EARMARK_LLM_MODEL` and
  `EARMARK_LLM_EFFORT`.
- A local server works too, as described in [docs/llm.md](llm.md). Note its warning:
  Ollama's OpenAI-compatible API silently truncates over-long prompts, so
  `OLLAMA_CONTEXT_LENGTH` must be at least `EARMARK_LLM_CONTEXT_TOKENS`.
- A local LLM is not part of `compose.yaml`. Run it as its own service and point
  `EARMARK_LLM_BASE_URL` at it. From inside compose, use the host's address, not
  `localhost`.

## 9. Search

- Search needs the `index` extra (about 440 MB).
- The embedding model (`BAAI/bge-small-en-v1.5`, 65 MB) downloads to
  `<EARMARK_DATA_DIR>/models/embed` (`/data/models/embed` in compose) on the worker's first
  index write, or when you run `earmark reindex`.
- Until then `GET /search` answers 503 and `earmark search` exits 1. The API never
  downloads models.
- Changing `EARMARK_EMBED_MODEL` needs `earmark reindex`.

## 10. Notifications (ntfy)

- **ntfy.sh.** Use a long random topic (the topic name is the password), at least 12
  characters, or reserve the topic and set `EARMARK_NTFY_TOKEN` (`tk_…`). Subscribe to the
  topic in the ntfy iOS app.
- **Self-hosted on the LAN.** Set `base-url` to the URL the phone uses (for example
  `https://ntfy.example.com` behind the same Caddy and DNS-only pattern, or a tailnet
  name), and set `upstream-base-url: "https://ntfy.sh"`, or iOS delivery can take hours.
  ntfy.sh then only relays a poll request with the message id; the phone fetches the
  message from your server, so it must reach it (home Wi-Fi, WireGuard, or the tailnet).
  Include its address in the tunnel's `AllowedIPs`.
- `EARMARK_OBSIDIAN_VAULT_NAME` adds an `obsidian://` link that opens the note.
- Earmark redacts the topic URL, and topics of 12 or more characters on their own, from
  logs and notifications.
- `earmark doctor --online` checks the server without publishing a message.

## 11. Fetching and DNS filtering

- Enclosure URLs usually start at a tracker (`dts.podtrac.com` and similar).
- A Pi-hole-style resolver that answers `0.0.0.0` for the tracker makes the download fail
  with a refused non-public address. The job error names the host and
  `EARMARK_ALLOW_PRIVATE_FETCH`.
- Allow-list the host in the resolver (preferred). Containers use the host's resolver. Or
  set `EARMARK_ALLOW_PRIVATE_FETCH=true`, which turns off the private-address guard for
  every feed and enclosure, so it is not recommended.
- The remote ASR, local LLM, and ntfy servers do not need it.

## 12. Stopping, restarts, and leases

- SIGTERM (`docker compose stop`, `systemctl stop`) releases the running job back to the
  queue. The worker gets 2 minutes under compose and 120 seconds under the unit, enough to
  reach its next interruption point.
- A SIGKILL or a crash leaves the job `running` until `EARMARK_JOB_LEASE_SECONDS` (900 s).
  Then it is requeued, and the attempt counts.

## 13. Checking the setup

`earmark doctor` checks the configuration, directories, database, and credentials.
`earmark doctor --online` also checks that the servers answer. Under compose:
`docker compose run --rm worker doctor --online`.

Exit code 0 means nothing failed (warnings are allowed), 1 means a check failed, and 2
means the settings did not load.

## 14. Weekly digest

`earmark digest` writes one note, `Digests/2026-W47.md`, with the research leads of every
episode first shared in that ISO week, grouped into themes and linked to the episode notes
and to their moments in the transcript notes. With no option it covers the last complete
week, so a run on Monday morning covers the week that just ended. It uses the same LLM
settings as the analysis and never runs in the worker, so it must be scheduled.

Run it by hand to catch up: `earmark digest --week 2026-W47`, or `earmark digest --since 7d`
(the last seven local days, written to `Digests/<first day> to <last day>.md`). A second run
with the same episodes and analyses makes no LLM call and changes nothing. `--refresh` asks
the model again. When ntfy is configured, a written or changed digest sends one
notification; `--no-notify` turns that off.

**Natively with systemd.** Copy
[`earmark-digest.service`](../deploy/systemd/earmark-digest.service) and
[`earmark-digest.timer`](../deploy/systemd/earmark-digest.timer) to `/etc/systemd/system/`,
edit the vault path in the service's `ReadWritePaths=`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now earmark-digest.timer
systemctl list-timers earmark-digest.timer
journalctl -u earmark-digest
```

The timer fires on Mondays at 07:00 host time and, with `Persistent=true`, runs once after
the host was off. A failed run is retried with `sudo systemctl start earmark-digest`.

**Under compose.** Compose has no scheduler. Run the command from a host cron line or a host
systemd timer, for example every Monday at 07:07:

```text
7 7 * * 1 cd /path/to/earmark && docker compose run --rm --no-deps worker digest
```

Keep `EARMARK_TZ` equal to the host's time zone (or set the zone in the timer's
`OnCalendar=`). Otherwise, shortly after Monday midnight, "the last complete week" can still
be the week before. `DATA_DIR/digests/` is a cache; delete it at any time and the next
digest asks the model again. The units and the compose command are examples: they have not
been run in the build sandbox.

## 15. Backups and upgrades

- Back up the vault, and the data directory's `episodes/` (transcripts and analyses;
  `entities rebuild` and `reindex` need them). `lancedb/`, `models/`, and `huggingface/`
  can be recreated. `digests/` is a cache of digest themes and need not be backed up.
- Upgrade with `git pull`, `docker compose build`, and `docker compose up -d` (or
  `uv sync --locked …` and `systemctl restart`). Database migrations run on start.
- Run `earmark entities rebuild` after changing `EARMARK_TZ`.

## 16. Troubleshooting

| Symptom | Likely cause |
|---|---|
| 401 from the Shortcut | The token in the Shortcut differs from `EARMARK_API_TOKEN`. |
| "Server cannot be found" | DNS rebinding protection on the resolver in use (6.1.6), or the name is not yet in public DNS. |
| Connection timeout away from home | WireGuard is not active (check the on-demand rules and `AllowedIPs`), or Tailscale is off. |
| A certificate error | Caddy has no certificate yet. Check `journalctl -u caddy` for the DNS-01 errors in 6.1.4. |
| Works on cellular, fails at home | The home resolver drops the record (6.1.6). |
| The worker keeps restarting | `docker compose logs worker`: usually a `ConfigError` naming the setting (no LLM key, vault missing). |
| Permission denied in the vault | `APP_UID` and `APP_GID` do not match the vault's owner (section 4). |
| "refused non-public address" | A resolver answered a tracker with `0.0.0.0` (section 11). |
| "You are sending unauthenticated requests to the HF Hub" in the worker log | Informational, shown once per process on a model download. Public models need no token, and earmark never reads one; `huggingface_hub` uses `HF_TOKEN` if you set it in the service environment. |
| `/search` answers 503 | The embedding model is not downloaded yet (section 9). |
