# Deploying earmark

How to run the API and the worker on one machine (a DGX Spark in the examples), and how
the iPhone and the Mac reach the API. The examples use the placeholders `spark` (the
host), `earmark.example.com` (the name of the API), `192.168.1.10` (the host's address on
the home network), `example.org` (your mail domain), `pka-personal@example.net` and
`pka-work@example.net` (the two destinations), and `tailnet-name`.

For a first setup on a DGX Spark, [docs/runbook-spark.md](runbook-spark.md) walks
through this guide in order, with a check after each phase.

## 1. What is tested

- The container image is build-tested in CI (`.github/workflows/container.yml` on
  linux/arm64), not run-tested. CI builds it and runs only `earmark --version`, an import
  of the extras, and a user-id check inside it. The API and worker have never run in a
  container, because the build sandbox has no Docker.
- YouTube downloads are not tested here. The tests run yt-dlp stubbed, and the weekly `slow`
  job runs the real library offline. A real download needs YouTube's answer to your server's
  address, so only runbook phase 4 checks one (section 18).
- The GPU image is untested: CI only runs `docker build --check` on `Dockerfile.gpu`, and it
  has never been built or run. The Spark smoke test in section 7 is the only real check.
- The systemd units are examples, reviewed but never run.
- Email delivery is tested only against an in-process SMTP stub. TLS against your server,
  DMARC at the recipient, and AMAP delivering the mail to a PKA are operator checks
  (runbook phases 5 and 6). The receiving skill is built (step 30) but has never run in a PKA, and the whole
  system has never run (runbook phase 12).
- The Shortcut has never run on a phone.
- The reachability setup is not verified by the earmark build. That covers the DNS-only
  Cloudflare record, the WireGuard split tunnel, Caddy with the DNS-01 challenge,
  Tailscale Serve, and ntfy self-hosting, all described from their documentation. The
  example `deploy/caddy/Caddyfile` was checked with `caddy validate` and `caddy fmt`,
  using a Caddy build that has the Cloudflare module. `deploy/caddy/Caddyfile.own-cert` was
  checked the same way with Caddy 2.6.2 and 2.11.9, and `deploy/caddy/install-cert.sh`'s
  checks are tested against generated certificates, with ssh, sudo and Caddy stubbed. The
  own-certificate route (6.1.8) has run once, on the operator's Spark.

## 2. Overview

- The API and the worker run on one host and share one data volume.
- The worker sends mail through your provider's submission server, and pushes through
  ntfy.
- **The API is never exposed publicly.** It listens on `127.0.0.1:8787`, and a reverse
  proxy on the host's LAN interface serves it over HTTPS as `earmark.example.com`. That
  name resolves, through public DNS, to a private address that is reachable only at home
  or over WireGuard.

```text
iPhone / Mac ──(home Wi-Fi, or WireGuard split tunnel)──► 192.168.1.10:443
  Caddy (TLS for earmark.example.com) ──► 127.0.0.1:8787 earmark API
Cloudflare DNS: earmark.example.com A 192.168.1.10 (DNS only, never proxied)
worker ──(SMTP submission, TLS)──► smtp.example.org ──► each PKA's address ──(AMAP)──► the PKA
```

With a certificate you already have, Caddy may listen on another port, such as 8443 (6.1.8).

- AMAP, outside this repo, applies the sender allowlist and DMARC, and hands the mail to the
  PKA (section 8.5).

## 3. Requirements

- aarch64 or x86_64 Linux. The image builds for the host's architecture; CI checks
  arm64.
- Docker Engine with Compose 2.30 or later for the GPU worker (`gpus: all`), or uv for a
  native, CPU-only install.
- `EARMARK_DATA_DIR` on a local filesystem. SQLite's write-ahead log needs shared memory,
  so no NFS or SMB. A Docker named volume is fine.
- A sending mailbox on a domain you control (section 8).
- Disk:
  - the CPU image is about 1 GB;
  - the GPU build pulls a CUDA devel base of about 6 GB compressed;
  - the runtime base is about 3 GB compressed;
  - Whisper `large-v3` is about 3 GB.

  The total build footprint was not measured.

## 4. Install with Docker Compose

```bash
git clone <this repository> earmark && cd earmark
cp .env.example .env
chmod 600 .env
```

Edit `.env`:

- `EARMARK_API_TOKEN`: generate one with
  `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`.
- `EARMARK_ALLOWED_RECIPIENTS`: who may receive transcripts (section 9).
- The `EARMARK_SMTP_*` settings: the sending mailbox (section 8).
- Optionally the ntfy settings (section 10).
- `APP_UID` and `APP_GID` are only needed if 1000 is unsuitable.

Then build, check, and start:

```bash
docker compose build
docker compose run --rm worker doctor
docker compose up -d
docker compose logs -f worker
```

[`compose.yaml`](../compose.yaml) defines `api` (`earmark serve`, the CPU image
`earmark:local`) and `worker` (`earmark worker`, the GPU image `earmark:gpu`, `gpus: all`,
section 7). Both share the named volume `data` at `/data`.

**Compose-only variables.** These are unprefixed, so earmark itself ignores them. Set them
in `.env`, which compose also reads.

| Variable | Default | Meaning |
|---|---|---|
| `API_BIND` | `127.0.0.1:8787` | Host address and port the API is published on. Leave it at the default. |
| `APP_UID` | `1000` | User id both images run as; the data volume belongs to it. |
| `APP_GID` | `1000` | Group id both images run as. |
| `EXTRAS` | `transcribe youtube` | Optional extras in the CPU image (`api`). `serve` needs none, but a CPU-only worker (7.2) needs `transcribe`, and YouTube links (section 18) need `youtube`. |
| `HF_HUB_DISABLE_TELEMETRY` | `1` | Hugging Face telemetry in the containers; `0` turns it back on (section 17). Read by Hugging Face's libraries, not by earmark. |

Things to know:

- **What compose overrides.** `EARMARK_DATA_DIR`, `EARMARK_HOST`, `EARMARK_PORT` and
  `EARMARK_ASR_MODEL_DIR` (empty, so the default applies) and `EARMARK_API_URL` (empty, so
  commands run in a container are never API clients) for both services, whatever
  `.env` says. For the worker, also `EARMARK_ASR_DEVICE=cuda` (forced) and
  `EARMARK_ASR_COMPUTE_TYPE` (default `float16`). A `.env` shared with a native install
  keeps working.
- **The data volume** holds the database, the transcript cache, `uploads/`, `downloads/`,
  `outbox/` (the fake mailer only) and `huggingface/` (the Whisper models). A restart does
  not download the models again.
- **Ownership.** The image bakes the user id in at build time, so rebuild after changing
  `APP_UID`. A named volume takes its ownership from the image the first time it is used.
  A volume created under another id needs a one-time fix:
  `docker run --rm -v earmark_data:/data --user 0 --entrypoint chown earmark:local -R 1000:1000 /data`
  (with your ids).
- **Rootless Docker and `userns-remap` shift user ids.** This guide does not cover them.
- **Values in `.env` must not contain `$`.** Compose interpolates them. **Single-quote a
  value that has spaces**, such as an app password.
- **The environment is exported.** Under compose, `env_file` puts every `.env` line into
  both containers' environments, the SMTP password included. The Cloudflare token must
  never be in this file.
- **`API_BIND` stays `127.0.0.1:8787`.** The reverse proxy (or `tailscale serve`) runs on
  the host and reaches the API there. Nothing in the default setup binds the API to a
  non-loopback host address: `EARMARK_HOST=0.0.0.0` exists only inside the container's own
  network namespace. Docker's published ports bypass ufw, so never set `API_BIND` to
  `0.0.0.0` or a LAN address. That would serve plain http to the whole LAN and skip the
  proxy's TLS.

Both images run as the non-root user `earmark`, have no health check of their own (compose
checks the API), and contain no `.env` file; compose passes the settings in.

## 5. Install natively with systemd

1. Create a user `earmark` with `/srv/earmark` as its home, and put a checkout there.
2. Install uv system-wide, then, as that user:

   ```bash
   sudo -u earmark env UV_PYTHON_INSTALL_DIR=/srv/earmark/.python \
     uv sync --locked --no-dev --extra transcribe --extra youtube
   ```

   The `youtube` extra brings yt-dlp and the Deno runtime it needs (section 18). A worker
   without it refuses to start, naming the extra: add it, or set
   `EARMARK_YOUTUBE_BACKEND=fake` to run without YouTube.

3. Create `/srv/earmark/.env` (mode 0600, owned by `earmark`) with the settings from
   section 4, and `EARMARK_HOST=127.0.0.1` and `EARMARK_DATA_DIR=/srv/earmark/data`.
4. Copy the units from [`deploy/systemd/`](../deploy/systemd/earmark-api.service) and
   [`earmark-worker.service`](../deploy/systemd/earmark-worker.service) to
   `/etc/systemd/system/`. Then:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now earmark-api earmark-worker
   ```

   The units set `HF_HUB_DISABLE_TELEMETRY=1` (Hugging Face telemetry off); section 17 has
   the details.

5. Check it. Run from `/srv/earmark`, so that `.env` is read:

   ```bash
   cd /srv/earmark && sudo -u earmark .venv/bin/earmark doctor
   ```

Run one worker per host.

A native worker runs on the CPU, because the PyPI CTranslate2 wheel for linux/aarch64 has
no CUDA. Set `EARMARK_ASR_DEVICE=cpu` and a smaller `EARMARK_ASR_MODEL` such as `small`, or
use the container for the GPU.

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

With a certificate you already have, stock Caddy is enough and the build steps below do not
apply: see 6.1.8 for the Caddyfile and the install.

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

This is the Let's Encrypt route. With a certificate you already have, skip it: see 6.1.8.

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
- The proxy is host infrastructure. It owns port 443 on the LAN address, or another port if something else holds 443 (6.1.8), and often fronts
  other services. In Docker it would need `network_mode: host`, which removes most of the
  isolation a compose service brings.
- For the Let's Encrypt route a stock image is not enough, because the Cloudflare module
  needs a custom build.
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
  If Caddy listens on another port (6.1.8), add it to every URL here, for example
  `https://earmark.example.com:8443/healthz`.
- From a host outside both, the name resolves but the connection fails. That is correct.
- The Shortcut URL is `https://earmark.example.com/transcriptions`
  ([docs/shortcut.md](shortcut.md)).

#### 6.1.8 With a certificate you already have

If you already have a certificate for the name, such as a commercial wildcard for
`*.example.com`, Caddy can serve it instead of getting one from Let's Encrypt. The DNS record
(6.1.1), WireGuard (6.1.2) and the rebinding note (6.1.6) stay the same; 6.1.4 does not
apply.

- **Stock Caddy is enough.** No DNS module, no custom build and no Cloudflare token: the
  distribution's package (`sudo apt install caddy`) or Caddy's own package both work.
- **The chain must include the intermediates.** The file Caddy serves is the server's
  certificate followed by each intermediate, in that order. iOS rejects a bare leaf, and
  clients such as `curl` never fetch a missing intermediate. Your CA's download usually
  has a "full chain" or "bundle" file; otherwise append the CA's intermediate file to the
  certificate.
- **The Caddyfile** is [`deploy/caddy/Caddyfile.own-cert`](../deploy/caddy/Caddyfile.own-cert).
  Copy it to `/etc/caddy/Caddyfile` and change the name, the `bind` address and the port. Its
  `tls` line names the two installed files.
- **Install the certificate with the script**, from a checkout on the Mac (`spark` is the
  Spark's ssh host):

  ```bash
  deploy/caddy/install-cert.sh --dry-run spark ~/ssl/example.com.fullchain.pem ~/ssl/example.com.key
  deploy/caddy/install-cert.sh spark ~/ssl/example.com.fullchain.pem ~/ssl/example.com.key
  ```

  [`deploy/caddy/install-cert.sh`](../deploy/caddy/install-cert.sh) checks locally that every
  certificate parses, that the key matches the first one, that the chain has the intermediates
  in order, and that the certificate has a Subject Alternative Name and has not expired. It
  warns within 30 days of expiry. `--dry-run` stops there. Otherwise it copies both files over
  ssh into a private temporary directory on the server, installs them as
  `/etc/caddy/certs/fullchain.pem` (root:caddy 0644) and `/etc/caddy/certs/privkey.pem`
  (root:caddy 0640), removes the copies, validates `/etc/caddy/Caddyfile`, and reloads Caddy
  if it is running. sudo on the server asks for your password. The script never prints the
  key. It needs bash and openssl (the Mac's own are fine), and an unencrypted key.
- **Renewal is manual.** Nothing renews this certificate. Before the date the script prints,
  get the new files from your CA and run the script again: it replaces both files and
  reloads Caddy. The packaged unit reloads with `caddy reload --force`, which reads the new
  files. A reload without `--force` keeps the old certificate, and the script warns if the
  unit's reload lacks it.
- **When 443 is taken.** If another proxy on the host already listens on 443 (check with
  `sudo ss -ltnp 'sport = :443'`), give Caddy another port in the site address, such as
  `earmark.example.com:8443`, as the example does. Every URL then carries the port:
  - the Shortcut's, `https://earmark.example.com:8443/transcriptions`;
  - every `curl` in these docs and in the runbook;
  - `EARMARK_API_URL` on the Mac (section 19.4).

  The example keeps `auto_https disable_redirects`, so Caddy needs no port 80, which the
  other proxy may hold as well. If the host runs a firewall, allow the port from the LAN
  and the WireGuard subnet only.

### 6.2 Tailscale Serve (the shorter alternative)

- Install Tailscale on the Spark, the iPhone, and the Mac, and turn on MagicDNS and HTTPS
  certificates in the admin console.
- Run `sudo tailscale serve --bg http://127.0.0.1:8787` and check it with
  `tailscale serve status`. The Shortcut URL is then
  `https://spark.tailnet-name.ts.net/transcriptions`.
- The Tailscale VPN must be on in the phone for the Shortcut to work.
- **Never use `tailscale funnel`.** It makes the service public.

### 6.3 Uploads through the proxy

The Shortcut uploads audio files, which can be large. From Caddy's documentation
(2026-11-23; none of it was run here):

- Caddy's defaults set no body size limit and no timeout for reading the request body or
  writing the response, and its reverse proxy streams the request body to earmark. Neither
  example Caddyfile adds anything that limits uploads.
- The limit is `EARMARK_MAX_UPLOAD_MB` (default 1024 MiB). earmark answers 413 above it.
- The API answers only after the whole file has arrived, so a large voice memo on
  cellular takes as long as the upload does. How long the Shortcut waits is not
  documented.
- A Caddy `request_body { max_size … }` lower than the earmark cap would turn big uploads
  into proxy errors. Do not add one, or keep it at or above `EARMARK_MAX_UPLOAD_MB`.
- Tailscale Serve's limits are not verified.

Runbook phase 7 checks an upload through the proxy.

## 7. Transcription on the DGX Spark

### 7.1 The GPU image

`Dockerfile.gpu` builds the worker's image: CUDA 12.9.1 on Ubuntu 24.04, a uv-managed
Python 3.13.18, and CTranslate2 compiled from source with CUDA for `sm_121` (the GB10),
swapped into the venv over the CPU-only PyPI wheel. `compose.yaml` runs `worker` from it
with `gpus: all`, forces `EARMARK_ASR_DEVICE=cuda`, and defaults
`EARMARK_ASR_COMPUTE_TYPE` to `float16`. `api` stays on the CPU image.

The host needs:

- the NVIDIA driver;
- the NVIDIA Container Toolkit;
- Docker Compose 2.30 or later, for `gpus: all`;
- `nvidia-smi --query-gpu=compute_cap --format=csv,noheader` printing `12.1`.

Notes:

- The build takes a long time. The CUDA devel base is about 6 GB to download, and the build
  time was not measured.
- `EARMARK_ASR_BATCH_SIZE` (0, off, by default) turns on faster-whisper's batched pipeline.
  Try 8 or 16 on a GPU. It holds the features of the whole file in memory, about 550 MB
  for 3 hours of speech, on top of the decoded audio. Changing it changes the transcript
  cache key.
- Leave `EARMARK_ASR_COMPUTE_TYPE` out of `.env` to get `float16`. A line there overrides it.
- `docker compose run --rm worker doctor` checks the CUDA device and `float16` in the
  worker. The `api` container uses the CPU image.
- `hf_xet` writes its own logs under `$HF_HOME/xet/logs/`, and Hugging Face telemetry is off
  by default (section 17).
- Record the build time and the image size; runbook phase 3 asks for them.

### 7.2 Without a GPU

- **Remote.** The `remote` backend sends the audio to a GPU server elsewhere
  ([docs/asr.md](asr.md)).
- **The CPU image.** For a worker on a host without a GPU, create `compose.override.yaml`
  next to `compose.yaml`. Compose reads it automatically.

<!-- cpu-worker-override -->
```yaml
services:
  worker:
    image: earmark:local
    build:
      dockerfile: Dockerfile
    gpus: !reset null
    environment:
      EARMARK_ASR_DEVICE: cpu
      EARMARK_ASR_COMPUTE_TYPE: int8
```

The worker then uses the CPU image (set `EXTRAS` to include `transcribe` and `youtube`,
which is the default) and `int8` on the CPU. Use a smaller `EARMARK_ASR_MODEL` on a CPU, such as `small`
or `distil-large-v3`. Without the override, a worker with `EARMARK_ASR_DEVICE=cuda` and no
GPU refuses to start.

**Check:** `docker compose config` shows the worker with `image: earmark:local` and no
`gpus`.

This has not been run. `!reset` needs a recent Compose, and the worker's GPU setup already
needs 2.30.

### 7.3 The Spark smoke test

Run this in the checkout on the Spark, with a `.env` from `.env.example`. It is the only
real check of the GPU path. Until it has passed, nothing here says the GPU image builds,
sees the GB10, or transcribes.

<!-- gpu-smoke:begin -->
```bash
# 0. The host
nvidia-smi --query-gpu=name,compute_cap,driver_version --format=csv,noheader  # NVIDIA GB10, 12.1, ...
docker compose version                                                      # 2.30 or later

# 1. Build. The log shows "-- CUDA architectures: 121". It takes a long time (unmeasured).
docker compose build worker

# 2. The CUDA build is the one in the venv, and the image has no uv that could undo it
docker compose run --rm --no-deps --entrypoint python worker -c \
  "import ctranslate2; print(ctranslate2.__version__, ctranslate2.__file__); print(ctranslate2.get_cuda_device_count(), sorted(ctranslate2.get_supported_compute_types('cuda')))"
#    expect: 4.9.0 /app/.venv/lib/python3.13/site-packages/ctranslate2/__init__.py
#            1 [..., 'float16', ...]
docker compose run --rm --no-deps --entrypoint sh worker -c 'command -v uv || echo "no uv"'
#    expect: no uv

# 3. Doctor
docker compose run --rm worker doctor
#    expect: asr ok "faster_whisper, model large-v3, device cuda, compute float16"
#            asr cuda ok "1 CUDA device, float16 supported"

# 4. A short public clip on cuda (JFK, 1961, public domain; the slow tests' clip)
curl -fsSLo jfk.flac https://raw.githubusercontent.com/SYSTRAN/faster-whisper/65882eee9f5cdbeeb2d877f1131d48cf241b327d/tests/data/jfk.flac
echo "63a4b1e4c1dc655ac70961ffbf518acd249df237e5a0152faae9a4a836949715  jfk.flac" | sha256sum -c
docker compose run --rm --no-deps -v "$PWD/jfk.flac:/tmp/jfk.flac:ro" worker transcribe /tmp/jfk.flac
#    expect on stderr: loaded Whisper model 'large-v3' in ... s on cuda (float16)
#    then the text: "And so my fellow Americans, ask not what your country can do for you ..."
#    optional: add -e EARMARK_ASR_BATCH_SIZE=8 and compare the "x real time" figure
```
<!-- gpu-smoke:end -->

Step 2's device count proves only that CUDA and the device are visible. Step 4 proves the
build, because a wrong architecture fails there. Record the result, the build time and the
image size (`docker image ls earmark:gpu`). The runbook's phase 3 asks for them.

### 7.4 If the smoke test fails

| Symptom | Remedy |
|---|---|
| The CTranslate2 compile or the bindings build fails, or the build-time check fails | `docker compose build --build-arg CT2_VERSION=v4.6.0 worker`, then step 2 again, expecting `4.6.0`. 4.6.0 builds the common architectures and the GB10 JIT-compiles the PTX on the first load, which is slow. `CUDA_CACHE_PATH=/data/cuda-cache` in `.env` keeps the JIT cache on the volume. Report the error: 4.9.0 is the first native-CMake release. |
| `get_cuda_device_count()` prints 0, or the worker exits with "sees no CUDA device" | A host problem. Run `docker compose run --rm --no-deps --entrypoint nvidia-smi worker`. Check the NVIDIA Container Toolkit, and "nvml error: driver not loaded" after a kernel update. |
| "no kernel image is available for execution on the device" at load or transcription | Wrong architecture. Check the build log's `CUDA architectures` line and the `CUDA_ARCH` build argument, or fall back to `v4.6.0`. |
| `float16` missing from the list, or doctor's `asr cuda` fails | Report it. Set `EARMARK_ASR_COMPUTE_TYPE` to a listed type meanwhile. |
| "Error 804: forward compatibility was attempted on non supported HW" | The base's `cuda-compat-12-9` is being used. Add `apt-get purge -y cuda-compat-12-9` to the runtime stage, as upstream CTranslate2's Dockerfile does, and report it. |
| Compose rejects `gpus` | Compose older than 2.30. Upgrade the Docker Compose plugin. |

## 8. Email delivery

### 8.1 What earmark sends

- One message per job, to the one `to` of the request, from `EARMARK_SMTP_FROM`.
- Two Markdown attachments: the transcript note (`YYYY-MM-DD-<slug>.md`) and the
  instructions (`YYYY-MM-DD-<slug>.instructions.md`).
- A body that earmark generates. It names both files and the job id.
- The subject is `[earmark] <title>`.
- Delivery is at-least-once. A resend keeps the `Message-ID` and the job id, and the PKA
  ignores a duplicate.

The mail contract is in "Delivering" in [docs/ARCHITECTURE.md](ARCHITECTURE.md), drafted by
pka-skills and owned by this repo. It is
**DRAFT** until you approve the receiving procedure, which is now the receiving skill (step 30).

### 8.2 A sending mailbox on your domain

- Create `earmark@example.org` at a provider that offers SMTP submission (587 with
  STARTTLS, or 465 with TLS) and per-app passwords.
- Use a dedicated mailbox, for nothing but earmark: AMAP's router allowlists this sender for
  each PKA address, and the receiving skill's trust in the instructions rests on no one else
  sending from it.

### 8.3 SPF, DKIM and DMARC

AMAP's router makes attachments readable only for an allowlisted sender whose mail passes
DMARC. The provider supplies the exact values. The records have this shape:

- SPF: `example.org TXT "v=spf1 include:<provider's SPF domain> -all"`. Keep one SPF
  record per domain, merged with any existing one.
- DKIM: the provider's records at `<selector>._domainkey.example.org`, usually CNAMEs.
- DMARC: `_dmarc.example.org TXT "v=DMARC1; p=quarantine; rua=mailto:<reports address>"`.
  Use `p=none` only while testing, and move to `quarantine` or `reject` once a test
  passes.

earmark uses `EARMARK_SMTP_FROM` as both the `From` header and the envelope sender. DMARC
passes when the provider signs with a DKIM key for the From domain (DKIM alignment). SPF
alignment depends on whether the provider uses your domain as the envelope sender, and many
rewrite it, so rely on DKIM. This is general knowledge about email, not something the
earmark build tested.

**Check:** `dig +short TXT example.org`, `dig +short TXT _dmarc.example.org` and
`dig +short CNAME <selector>._domainkey.example.org` show the records. Then, after a real
send (runbook phase 5), the received message's `Authentication-Results` header shows
`dkim=pass` with `header.d=example.org` and `dmarc=pass`.

### 8.4 The app password and the settings

```bash
EARMARK_MAIL_BACKEND=smtp
EARMARK_SMTP_HOST=smtp.example.org
EARMARK_SMTP_SECURITY=starttls
EARMARK_SMTP_USERNAME=earmark@example.org
EARMARK_SMTP_PASSWORD='<app password>'
EARMARK_SMTP_FROM=earmark@example.org
```

- `EARMARK_SMTP_PORT` defaults to 587 for `starttls` and 465 for `tls`.
- `EARMARK_SMTP_TIMEOUT_S` defaults to 60 and is at most 90.
- `EARMARK_MAX_ATTACHMENT_MB` defaults to 10 MiB, for the two attachments together. A
  three-hour transcript is about 300 KB. Keep it at or below AMAP's attachment limit for the
  PKA addresses, which is not known yet (runbook phase 6 finds it). If AMAP limits the whole
  message, base64 makes the message about 1.37 times the attachments.
- The password is printable ASCII, and spaces are allowed: single-quote it in `.env`.
- TLS is always verified, AUTH happens only after TLS, and there is no plaintext mode. A
  server without STARTTLS fails delivery.

**Check:** `docker compose run --rm worker doctor` shows `ok mail smtp to
smtp.example.org:587 (starttls), from @example.org, timeout 60 s`. `doctor --online` shows
`ok smtp smtp.example.org:587: TLSv1.3, login accepted, nothing sent` (the TLS version is
the server's). Doctor never sends mail.

### 8.5 The PKA side

- How mail reaches a PKA: earmark sends plain email to the PKA's address; AMAP, outside this
  repo, applies the allowlist and DMARC and provides it to the PKA through AMMP; the PKA
  reads it with its `inbox` tools after a doorbell in its session. See
  [ARCHITECTURE.md "System"](ARCHITECTURE.md#system-three-parts-and-three-seams).
- **The sender allowlist is AMAP router configuration.** Add `earmark@example.org` to the
  AMAP router's allowlist for each PKA's address. You set it; it is not something to ask a
  PKA for. earmark cannot see whether it was done: SMTP's 250 reply is all it learns.
- **AMAP's attachment limit** must be at least `EARMARK_MAX_ATTACHMENT_MB` (8.4).
- **The test** is the PKA's `inbox` tools showing both attachments with disposition
  `clean` (runbook phase 6). Filing needs the receiving skill (runbook phase 11).

### 8.6 Reading a message without sending

- `EARMARK_MAIL_BACKEND=fake` writes each message to `data/outbox/<job_id>.eml` (mode
  0600) and sends nothing.
- Under compose, copy them out while `api` runs:
  `docker compose cp api:/data/outbox ./outbox`.
- Read them with the Python reader in the [README quickstart](../README.md#quickstart-fake-backends),
  run with `python3` on the host.

### 8.7 A transcript without the email

`GET /jobs/<job_id>/transcript` serves a job's transcript from the cache, behind the same
bearer token. `?format=` picks one of three:

- `note` (the default) is the transcript note, rendered by the same code as the mailed
  attachment. It is that attachment, byte for byte, while the episode's cache is unchanged
  (the exceptions are under "HTTP API" in [ARCHITECTURE](ARCHITECTURE.md)).
- `text` is `transcript.txt`.
- `json` is the segments, with the job's own id and without the ASR model.

From the Mac, or any machine that reaches the API:

```bash
curl -fsS -OJ -H "Authorization: Bearer $EARMARK_API_TOKEN" \
  "https://earmark.example.com/jobs/<job_id>/transcript"
curl -fsS -H "Authorization: Bearer $EARMARK_API_TOKEN" \
  "https://earmark.example.com/jobs/<job_id>/transcript?format=text" -o transcript.txt
```

`-OJ` saves the note under its own file name. On the Spark, inside the container:

```bash
docker compose exec -T api earmark transcript <job_id> > note.md
```

Keep `-T`: without it compose allocates a terminal, which turns line endings into CRLF and
makes the command replace control characters.

The answers:

- 409: the job has no transcript yet, or it failed or was skipped before it had one.
- 410: the episode's cache was deleted; share it again.
- A job that failed on `EARMARK_MAX_ATTACHMENT_MB` still has its transcript here.

## 9. Recipients: EARMARK_ALLOWED_RECIPIENTS

- Every request names one `to`, and earmark never defaults it.
- The setting is a comma-separated list of exact addresses. The local part is
  case-sensitive and the domain is not, so `Pka-Work@example.net` is a different address.
- Unset refuses every request with `no recipients are allowed yet: set
  EARMARK_ALLOWED_RECIPIENTS on the server`, and doctor FAILs.

<!-- allowlist-example -->
```bash
EARMARK_ALLOWED_RECIPIENTS=pka-personal@example.net,pka-work@example.net
```

**`all`.** It accepts any single, syntactically valid address (ASCII, at most 254
characters). It is your written acceptance of a risk: anyone holding the API token can
have earmark mail anyone from your domain, which costs your domain's reputation, and can
instruct both PKAs. `serve` logs a WARNING at startup, and doctor warns. There is no
send-rate cap (backlog). `all` cannot be mixed with addresses.

**Changing it.** Recreate both containers (`docker compose up -d`): the API checks `to` at
request time, and the worker checks again at delivery. A job queued for an address you
removed fails with "the destination is no longer allowed by EARMARK_ALLOWED_RECIPIENTS;
nothing was sent".

Logs and pushes show addresses masked (`p…@example.net`). `GET /jobs` shows them in full to
token holders.

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
- A push says:
  - `Sent: <title>` with `To: <masked address>` and the duration;
  - `Failed: <title>` with the stage and the reason, and "The email may already have been
    delivered." when that applies;
  - "Resent after an interruption." after a retry;
  - never a URL path, the instructions, a full address or a feed guid.
- Earmark redacts the topic URL, and topics of 12 or more characters on their own, from
  logs and notifications.
- `earmark doctor --online` checks the server without publishing a message.

## 11. Fetching and DNS filtering

- Direct audio URLs from requests go through the same guard as enclosures.
- Enclosure URLs usually start at a tracker (`dts.podtrac.com` and similar).
- A Pi-hole-style resolver that answers `0.0.0.0` for the tracker makes the download fail
  with a refused non-public address. The job error names the host and
  `EARMARK_ALLOW_PRIVATE_FETCH`.
- Allow-list the host in the resolver (preferred). Containers use the host's resolver. Or
  set `EARMARK_ALLOW_PRIVATE_FETCH=true`, which turns off the private-address guard for
  every feed and enclosure, so it is not recommended.
- The remote ASR, SMTP and ntfy servers do not need it.

## 12. Stopping, restarts, and leases

- SIGTERM (`docker compose stop`, `systemctl stop`) releases the running job back to the
  queue. The worker gets 2 minutes under compose and 120 seconds under the unit, enough to
  reach its next interruption point. A delivery in progress finishes first, within
  `EARMARK_SMTP_TIMEOUT_S`, and both grace periods cover it.
- A SIGKILL or a crash leaves the job `running` until `EARMARK_JOB_LEASE_SECONDS` (900 s).
  Then it is requeued, and the attempt counts.

## 13. Checking the setup

`earmark doctor` checks the configuration, directories, database, credentials, and the
`recipients`, `mail` and `asr cuda` lines. It never sends mail.
`earmark doctor --online` also checks that the servers answer, including the `smtp` login. Under compose:
`docker compose run --rm worker doctor --online`.

Exit code 0 means nothing failed (warnings are allowed), 1 means a check failed, and 2
means the settings did not load.

## 14. Upgrading from v0.1.0

<!-- upgrade-v0.1.0:begin -->
**What changed.** earmark no longer analyses, writes a vault, indexes or digests. It
refuses a v0.1.0 database and does not migrate it. **The vault is no longer touched:** your
notes stay as they are, and nothing reads or writes the vault.

**Compose:**

```bash
docker compose down --remove-orphans
git pull && docker compose build
docker compose run --rm --no-deps --entrypoint sh api -c '
  cd /data && mkdir -p v0.1.0 &&
  for f in earmark.db earmark.db-wal earmark.db-shm; do [ ! -e "$f" ] || mv "$f" v0.1.0/; done &&
  rm -rf episodes/*/analyses lancedb models/embed digests vault.lock index.lock &&
  ls -A /data /data/v0.1.0'
docker volume rm earmark_asr-models 2>/dev/null || true
```

- The old database moves aside with its `-wal` and `-shm` files, into `v0.1.0/`.
- `analyses/`, the `lancedb/` index, the `models/embed/` embedding model, `digests/` and the
  lock files are deleted.
- `huggingface/` (the Whisper models) stays.
- The `asr-models` volume came from the old `gpu` profile.

Transcripts cached by v0.1.0 carry no ASR profile and are never reused. Delete `episodes/`
too if you do not want their text.

**Native:** the same under `/srv/earmark/data`, with `sudo -u earmark sh -c '…'`, plus:

- `sudo systemctl disable --now earmark-digest.timer`;
- delete `/etc/systemd/system/earmark-digest.service` and `.timer`;
- copy the two new units;
- `sudo systemctl daemon-reload`.

**`.env`.** Delete these lines. They are ignored, but some now mislead:

- `EARMARK_LLM_*` and `ANTHROPIC_API_KEY`;
- `EARMARK_VAULT_DIR`, `EARMARK_VAULT_SUBDIR` and `EARMARK_OBSIDIAN_VAULT_NAME`;
- `EARMARK_STORE_BACKEND`, `EARMARK_INDEX_BACKEND`, `EARMARK_EMBED_MODEL` and
  `EARMARK_EMBED_MODEL_DIR`;
- `EARMARK_TZ`, `VAULT_PATH`, `COMPOSE_PROFILES` and `ASR_GPU_MEMORY_UTILIZATION`;
- the vLLM lines: `EARMARK_ASR_BACKEND=remote`, `EARMARK_ASR_BASE_URL=http://asr:8000/v1`,
  `EARMARK_ASR_MODEL=openai/…`, `EARMARK_ASR_API_KEY` and `EARMARK_ASR_MAX_UPLOAD_MB`. They
  would send the worker to a server that no longer exists.

Then add the new settings (sections 8 and 9).

**Check:** `docker compose run --rm worker doctor` no longer shows `FAIL database … was
created by earmark 0.1.0`. The `database` line reads "not created yet" until the first
start. This `grep` prints nothing:

```bash
grep -nE '^(EARMARK_(LLM|VAULT|OBSIDIAN|STORE|INDEX|EMBED|TZ)|ANTHROPIC|VAULT_PATH|COMPOSE_PROFILES|ASR_GPU)' .env
```
<!-- upgrade-v0.1.0:end -->

## 15. Backups and upgrades

- **Back up `.env`** (it holds secrets) somewhere safe.
- earmark holds no system of record: the notes live with the PKAs. The data volume holds
  the job history and a transcript cache, which can be recreated by transcribing again.
- **Upgrade** with `git pull`, `docker compose build` (the GPU image rebuilds CTranslate2,
  so it takes long), and `docker compose up -d`. Migrations run on start. After a rebuild,
  repeat smoke steps 2 and 4 (section 7.3).
- **The database moves to schema 5** the first time this version starts (the `/v1` columns,
  after schema 4's `youtube` source). An older image then refuses the database as newer than
  it supports, so back up `earmark.db` (and its `-wal` and `-shm` files, with earmark
  stopped) before upgrading. A
  native install that skips `--extra youtube` on upgrade must add it, or set
  `EARMARK_YOUTUBE_BACKEND=fake`: the worker refuses to start without the extra.

## 16. Troubleshooting

| Symptom | Likely cause |
|---|---|
| 401 from the Shortcut | The token in the Shortcut differs from `EARMARK_API_TOKEN`. |
| "Server cannot be found" | DNS rebinding protection on the resolver in use (6.1.6), or the name is not yet in public DNS. |
| Connection timeout away from home | WireGuard is not active (check the on-demand rules and `AllowedIPs`), or Tailscale is off. |
| A certificate error | Caddy has no certificate yet. Check `journalctl -u caddy` for the DNS-01 errors in 6.1.4. |
| Works on cellular, fails at home | The home resolver drops the record (6.1.6). |
| The worker keeps restarting | `docker compose logs worker`: usually a `ConfigError` naming the setting. |
| 422 `to is not an allowed recipient` | The address is not in `EARMARK_ALLOWED_RECIPIENTS` (section 9). |
| 422 `no recipients are allowed yet` | `EARMARK_ALLOWED_RECIPIENTS` is unset (section 9). |
| doctor `FAIL mail` naming `EARMARK_SMTP_*` | A missing setting (section 8.4). |
| job failed, naming `EARMARK_SMTP_USERNAME` and `EARMARK_SMTP_PASSWORD` | The login was refused: check the app password. |
| job failed, naming `EARMARK_SMTP_HOST` | The certificate was not verified. |
| job failed: the server does not offer STARTTLS | Use `EARMARK_SMTP_SECURITY=tls` if the provider uses port 465. |
| job failed, naming `EARMARK_MAX_ATTACHMENT_MB` | The attachments are over the cap. |
| the PKA gets the mail, but its attachments are not readable (not `clean`) | `earmark@example.org` is not on the AMAP router's allowlist for that address, or DMARC fails (the 8.3 check). |
| the PKA reads the mail but does not file it | The receiving skill is not installed there (runbook phase 11). |
| the skill stops at its preflight | `.pka/bin/pka-tools` is missing or older than 1.8.0: run `bootstrap upgrade` in that PKA. |
| the skill reports `invalid` with `note.hash` | The attachment's bytes changed on the way: check AMAP's attachment handling (runbook phase 6). |
| a PKA got the same message twice | Delivery is at-least-once; the PKA's dedupe drops it. |
| doctor or startup: `was created by earmark 0.1.0` | Section 14. |
| the worker exits "sees no CUDA device" | Section 7.4. |
| the worker exits naming the `youtube` extra | The extra is not installed: section 5, or `EARMARK_YOUTUBE_BACKEND=fake`. |
| a YouTube job failed: "out of date", "no audio-only stream" or "HTTP 403" | yt-dlp needs an upgrade: section 18.2. |
| a YouTube job failed: "prove it is not a bot" | YouTube checks the server's address; earmark does not sign in. Share the link again later (section 18.1). |
| "refused non-public address" | A resolver answered a tracker with `0.0.0.0` (section 11). |
| "You are sending unauthenticated requests to the HF Hub" in the worker log | Informational, shown once per process on a model download. Public models need no token, and earmark never reads one; `huggingface_hub` uses `HF_TOKEN` if you set it in the service environment. |

## 17. Hugging Face telemetry and logs

**Off by default.** Every shipped way of running earmark sets `HF_HUB_DISABLE_TELEMETRY=1`:
both images, the `api` and `worker` compose services, and the systemd services.

**What that stops.** `hf_xet`'s download summary to the Xet CAS server, and
`huggingface_hub`'s daily `/api/agent-harnesses` fetch and User-Agent extras. It was tested
with a real model download (hf_xet 1.8.0, huggingface_hub 1.33.0), outside a container.

**Turning it back on:**

- compose: `HF_HUB_DISABLE_TELEMETRY=0` in `.env`, then `docker compose up -d`;
- systemd: `sudo systemctl edit earmark-worker` (and `earmark-api`) with
  `[Service]` and `Environment=HF_HUB_DISABLE_TELEMETRY=0`, then restart. A line in
  `/srv/earmark/.env` has no effect, because earmark reads that file itself;
- `docker run`: `-e HF_HUB_DISABLE_TELEMETRY=0`.

It stays off while `DO_NOT_TRACK=1` or `DISABLE_TELEMETRY=1` is set in the environment.

**Commands run by hand** on a native install, such as `earmark transcribe` (which downloads the
Whisper model), do not get the unit's environment. Prefix them with
`env HF_HUB_DISABLE_TELEMETRY=1`. `docker compose run` gets it from compose.

**hf_xet logs.** They live in `$HF_HOME/xet/logs/` (`/data/huggingface/xet/logs` in the
container). hf_xet prunes them at 250 MB or 14 days. They hold hosts and request ids but no
URL paths. Earmark leaves them there. `HF_XET_LOG_DEST` (a directory ending in `/`, or a
file) moves them, and an empty value is ignored.

## 18. YouTube

A YouTube video link is a request like a podcast link (step 32). The worker has yt-dlp read
the video's title, channel and date, download an audio-only stream, and transcribe it; the
note is `type: video` (`contract: 2`).

### 18.1 What it needs, and what it refuses

- **The `youtube` extra** of the project, locked in `uv.lock` and installed by both images
  and by the native `uv sync` (sections 4 and 5): yt-dlp, `yt-dlp-ejs` (the scripts that
  solve YouTube's player challenges, pinned by yt-dlp to its own version), and Deno, the
  JavaScript runtime those scripts run in, as a PyPI wheel. Nothing is installed from `apt`
  or fetched at run time. The player's JavaScript runs in Deno with no file, network,
  environment or process permission.
- **No ffmpeg, no cookies, no sign-in.** earmark picks an audio-only stream and sends the file
  to the transcriber as it is.
- `EARMARK_YOUTUBE_BACKEND` is `ytdlp` (the default) or `fake`. `earmark doctor` shows the
  three versions and fails when the extra or Deno is missing. It makes no request.
- Deno keeps a small cache under `<data dir>/cache/deno/`. Downloads sit in
  `<data dir>/downloads/` while a job runs and are deleted when it ends. The size cap is
  `EARMARK_MAX_AUDIO_MB`; there is no duration cap.
- **Refused, with the reason in the failure and the push:** a live stream or a premiere that
  has not aired, a recording YouTube is still processing, private, removed, members-only and
  Premium videos, age-restricted videos, and videos that are not available in the server's
  country. A bot check fails the job and is not retried: earmark does not sign in to YouTube,
  and retrying from the same address adds requests. YouTube's "try again later" is retried
  after an hour.
- A link to a channel, a playlist or a search is refused at once with a 422.

### 18.2 Updating yt-dlp

YouTube changes break yt-dlp every few weeks. When a job fails with "out of date", "no
audio-only stream" or "HTTP 403", upgrade it and rebuild:

```bash
uv lock --upgrade-package yt-dlp --upgrade-package yt-dlp-ejs --upgrade-package deno
make check
git commit -am "Upgrade yt-dlp"
# on the Spark:
git pull
docker compose build
docker compose up -d
docker compose run --rm worker doctor
```

Upgrade the three together: yt-dlp pins the `yt-dlp-ejs` it needs. Dependabot proposes the
update weekly (its seven-day cooldown applies; run the lock command by hand when a failure
cannot wait). The weekly `slow` job runs the real library offline, so a broken upgrade shows
there, but only a share on the Spark proves a download.

### 18.3 What earmark fetches from YouTube

yt-dlp makes its own requests, so this is the one exception to "every third-party fetch goes
through `fetch.py`":

- yt-dlp gets one URL, `https://www.youtube.com/watch?v=<id>`, built by earmark from the
  video id; never the string the client sent. Only the `youtube` extractor is loaded: no
  generic extractor, no playlists, no plugins, no config files, no cookies kept, no
  environment proxies, no remote components.
- The stream must be `https` on a `googlevideo.com` host before anything is downloaded.
- yt-dlp's requests to YouTube's hosts do not pass the private-address guard or
  `EARMARK_ALLOW_PRIVATE_FETCH`. A resolver that points those names at a private address is
  not caught. The size cap is kept.
- Logs and pushes name the video by its id and title, never a URL; yt-dlp's debug output
  never prints.

## 19. The command line

`earmark jobs`, `earmark submit` and `earmark transcript` work in two modes. Local mode reads
and writes `EARMARK_DATA_DIR`. Client mode calls the running API, so they work from a machine
that has no data directory of its own.

### 19.1 In the containers

Commands run in a container read the service's own data in `/data`:

```bash
docker compose exec api earmark jobs
docker compose exec api earmark jobs <job_id>
docker compose exec -T api earmark transcript <job_id> > note.md
docker compose exec api earmark submit '<link>' --to <address>
```

Keep `-T` for `transcript`, as in 8.7. A local file is not visible inside the container, so
submit a file from the host in client mode (19.3). Compose sets `EARMARK_API_URL` to empty in
both containers, so a command run there is always local, whatever `.env` says.

### 19.2 Client mode

With `EARMARK_API_URL` set, `jobs`, `submit` and `transcript` call the API with
`EARMARK_API_TOKEN` and never touch `EARMARK_DATA_DIR`. The other commands stay local. Their
output and exit codes are the local ones. An error from the API is printed as
`earmark <command>: <the API's detail>`.

- **Transport.** The URL is `https://`, or `http://` to a loopback IP address such as
  `http://127.0.0.1:8787`. Anything else is refused when settings load, so the token never
  crosses a network unencrypted. `localhost` is a name, not an address, and is refused.
- **Certificates** are always verified, against the system trust store (the Mac's keychain).
  There is no way to turn that off. A certificate needs its intermediates, as 6.1 says.
- **The token** goes only in the `Authorization` header, and is never printed or logged.
- **Where to set it: in your shell, not in `.env`.** Compose passes `.env` into the
  containers (it blanks this one there), a native install's units read `.env` too, and an
  empty `EARMARK_API_URL=` on the command line does not override a value in `.env`.

### 19.3 On the Spark's host

Make a venv in the checkout with `uv sync`, then:

```bash
export EARMARK_API_URL=http://127.0.0.1:8787
uv run earmark jobs
```

The token comes from the checkout's `.env`, which compose reads too. An earlier
`earmark jobs` on the host may have created `./data/earmark.db`. Under compose that file is
not the service's data (that is in the `data` volume), and it can be deleted. On a native
install `./data` is the real data.

### 19.4 On the Mac

From a checkout, run `uv sync`, then:

```bash
export EARMARK_API_URL=https://earmark.example.com
export EARMARK_API_TOKEN='<the token from the Spark>'
uv run earmark jobs
uv run earmark submit ~/Downloads/memo.m4a --to pka-personal@example.net \
  --instructions "File this under ideas."
uv run earmark transcript <job_id> > note.md
```

Add the port to the URL if the proxy does not listen on 443. Keep the token out of shell
history and files others can read.

### 19.5 Checking it

`earmark doctor` shows a `client mode` line, and `earmark doctor --online` adds `api server`,
which asks `/healthz`. On the Mac the other doctor lines check the Mac's own settings and can
be ignored.

### 19.6 What the answers mean

| Answer | Meaning |
|---|---|
| `missing or invalid API token (check EARMARK_API_TOKEN)` | The token differs from the server's. |
| `job not found` | No such job (404), or an id that is not 32 hex digits. |
| 409, 410 | The job has no transcript yet, or its cache was deleted: as in 8.7. |
| 422, 413 on `submit` (exit 2) | The server refused the request: a recipient it does not allow, a bad field, or an upload over its limit. |
| `the certificate of <host> did not verify` | The server needs a certificate this machine trusts, with its intermediates. |
| `could not reach the API at <host>` | Wrong `EARMARK_API_URL`, or the service is not running. |
| `EARMARK_DATA_DIR (…) does not exist` (local mode) | Nothing was created. Set `EARMARK_API_URL`, or use `docker compose exec api earmark …`. |

## 20. The /v1 API for programs

`/v1` is for a program on your own hardware, such as earshot, that wants a transcript back
and nothing else: no mail, no push, no recipient. The program uploads audio, fetches the
transcript by job id, and deletes it when done. The text a client builds against is
[contract/api-v1.md](../contract/api-v1.md). Nothing leaves the Spark, except to a remote ASR
server if you chose one (`EARMARK_ASR_BACKEND=remote`), which receives the audio and the
`initial_prompt`. The only gate is the bearer token: `EARMARK_ALLOWED_RECIPIENTS` does not
apply, because nothing is sent.

### 20.1 Reaching it over an SSH tunnel

The API listens on loopback. From the machine that runs the program, tunnel to it, then use
`http://127.0.0.1:8787` with the token. The tunnel is encrypted, so the program needs no TLS
(`http://` to a loopback address is the one plain-HTTP form earmark accepts):

```bash
ssh -N -L 8787:127.0.0.1:8787 spark.example.com
```

Add your user name and `@` before the host if it differs from your local one.

The HTTPS name of section 6 works as well.

### 20.2 An example

```bash
export EARMARK_API_TOKEN='<the token from the Spark>'
curl -s -H "Authorization: Bearer $EARMARK_API_TOKEN" \
  -F "file=@meeting.flac;type=audio/flac" \
  --form-string "initial_prompt=Alice Johnson, Bob Smith, Kubernetes" \
  --form-string "language=en" \
  http://127.0.0.1:8787/v1/transcriptions
# {"job_id":"3f9c2a7e5b1d4c8e9a6f0b2d4e6a8c01","status":"queued"}
curl -s -H "Authorization: Bearer $EARMARK_API_TOKEN" \
  http://127.0.0.1:8787/v1/jobs/<job_id>
curl -s -H "Authorization: Bearer $EARMARK_API_TOKEN" \
  "http://127.0.0.1:8787/v1/jobs/<job_id>/transcript?format=text"
curl -s -X DELETE -H "Authorization: Bearer $EARMARK_API_TOKEN" \
  http://127.0.0.1:8787/v1/jobs/<job_id>
```

Use `--form-string` for `initial_prompt` and `language`, so a prompt that starts with `@` or
`<` is sent as text, and `-F file=@...` for the audio. Leave `language` out to have the
language detected. Poll the job until `status` is `done`.

### 20.3 The upload cap and the audio format

The cap is `EARMARK_MAX_UPLOAD_MB` (1024 MiB by default), and a larger upload is a 413.
Whisper resamples everything to 16 kHz mono, so send 16 kHz mono: a FLAC of speech is about
60 MB per hour, so a 4-hour meeting is about 250 MB, well under the default. The same
meeting at 48 kHz stereo is several times larger and passes the cap after about an hour and a
half.

### 20.4 Time and memory

A 4-hour file takes tens of minutes on the GPU and needs on the order of 12 GB of memory on
the worker; the Spark has far more. A job is not resumable: if the worker stops (a restart,
`docker compose stop`), the next run starts the file over. The worker's heartbeat keeps the
job's lease through a long transcription, so a long file is not requeued while it runs.

### 20.5 What the prompt reaches

Whisper conditions on the last 223 tokens of the `initial_prompt`; earmark caps it at 1000
characters. With the default sequential decoding the prompt conditions only the first
windows of a long recording, then the transcript's own text takes over. With
`EARMARK_ASR_BATCH_SIZE` set above 0 (your setting, off by default) the prompt is applied to
every chunk, which suits a long meeting with names to get right. Section 7 covers batching.

### 20.6 What DELETE removes, and what it does not

`DELETE /v1/jobs/<job_id>` unlinks the job's transcript files and any staged upload, and
clears the stored `initial_prompt`. It refuses a queued or running job (409; there is no
cancel) and can be repeated. The transcript is shared by every job with the same audio and
options, so deleting one deletes it for all the finished ones. What remains is the job's row:
id, status, times, language, duration and a hash of the audio. The database overwrites freed
space and truncates its log after a delete, but this is deletion, not shredding: filesystem
blocks and backups of `EARMARK_DATA_DIR` are outside earmark's reach, and a remote ASR server
keeps whatever it keeps.
