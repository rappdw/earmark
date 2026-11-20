# Step 16: Deployment and Shortcut docs

Brief: `docs/PLAN.md`, step 16. Depends on steps 06–15, all done. This step adds the
container image, the compose file, systemd unit examples, `docs/deploy.md`,
`docs/shortcut.md`, and the `README.md`, plus tests that tie those documents to the code.
It also folds in one fix from the step 15 verifier: the bare ntfy topic joins the redaction
set.

The step can be planned as briefed. Three things cannot be checked in this sandbox, and the
docs say so:

- The image is never built here (no Docker). CI builds it on arm64.
- No container is ever started here, and CI starts none of the services.
- The `gpu` profile is never built or run anywhere.
- The reachability setup is never run: the WireGuard split tunnel, the DNS-only record,
  and Caddy with the DNS-01 challenge. Only the example Caddyfile was validated, with
  `caddy validate`.
- What the iOS share sheet hands a Shortcut needs a real iPhone, so it is not recorded
  (section 5.9).

**Decisions the user made (2026-11-20).**

- **GPU (deviation 1, accepted).** The compose `gpu` profile does not give the worker a
  GPU. Instead it starts a vLLM Whisper server, the `asr` service, on the Spark's GPU, and
  the worker calls it through the existing `remote` ASR backend. A CUDA build of
  CTranslate2 inside the earmark image is documented only as an untested outline. The
  reasons are in section 2.3.
- **Reachability (user's design).** The primary path has four parts:
  - a DNS-only (grey-cloud) Cloudflare `A` record pointing at the server's private
    address;
  - a WireGuard split tunnel on the phone and the Mac;
  - Caddy on the LAN interface, terminating TLS and forwarding to `127.0.0.1:8787`;
  - a Let's Encrypt certificate through the DNS-01 challenge, with a zone-scoped
    Cloudflare token that only the proxy holds.

  Tailscale Serve is the shorter alternative. Cloudflare Tunnel is dropped. The API is
  never exposed publicly. See sections 2.4, 4.4a, 4.7 §6, and 5.13.
- **Token permissions (correction to the brief).** The Cloudflare token needs **Zone:DNS:
  Edit and Zone:Zone:Read**, both limited to the one zone. `Zone:DNS:Edit` alone is not
  enough: both caddy-dns/cloudflare and Traefik's lego provider look up the zone id
  (section 2.4).

## 1. Outcome

- `Dockerfile` (one image, three stages, final stage `runtime`):
  - It is based on `python:3.13.18-slim-trixie` and `ghcr.io/astral-sh/uv:0.13.9`, both
    pinned by digest.
  - It installs the locked dependencies with `uv sync --locked --no-dev`, plus the extras
    named in the build argument `EXTRAS`. The default is `transcribe index`, so the default
    settings work.
  - It runs as a non-root `earmark` user whose UID and GID are build arguments, so the
    vault bind mount and the data volume have matching ownership.
  - Its entrypoint is `earmark`, with default command `serve`.
- `.dockerignore` allow-lists only what the build needs, so `.env`, `data/`, `.venv/`, and
  `.git/` never reach the build context.
- `compose.yaml` has three services:
  - `api` (`earmark serve`, published on `127.0.0.1:8787` by default, with a health check);
  - `worker` (`earmark worker`, `stop_grace_period: 2m`);
  - `asr`, only in the `gpu` profile: NVIDIA's arm64 CUDA 13 vLLM container serving
    Whisper, with the upload and decode limits raised.

  `api` and `worker` share a named `data` volume at `/data` and a vault bind mount at
  `/vault` (`VAULT_PATH`). Their settings come from `.env`, with in-container paths forced
  by `environment:`.
- `deploy/systemd/earmark-api.service` and `deploy/systemd/earmark-worker.service`: example
  units for a native install under `/srv/earmark`.
- `deploy/caddy/Caddyfile`: an example reverse-proxy config. It binds the LAN address only
  and serves `earmark.example.com` with a Let's Encrypt certificate from the DNS-01
  challenge (`dns cloudflare {env.CF_API_TOKEN}`), forwarding to `127.0.0.1:8787`. The
  proxy is documented, not a compose service (5.13).
- `.github/workflows/container.yml` ("Container image"):
  - it validates `compose.yaml` with `docker compose config`;
  - it builds the image on `ubuntu-24.04-arm` without pushing;
  - it runs three one-off checks in the built image: version, extras import, and non-root
    UID.

  `.github/dependabot.yml` gains the `docker` ecosystem, limited to patch and digest
  updates.
- `docs/deploy.md` covers:
  - compose first, then systemd;
  - reachability: first a DNS-only Cloudflare record for a private address, a WireGuard
    split tunnel, and Caddy with DNS-01; Tailscale Serve as the alternative;
  - the transcription options on the DGX Spark;
  - a local LLM (pointing to `docs/llm.md`);
  - search and the index extra;
  - ntfy, including self-hosting with `upstream-base-url`;
  - private fetches and DNS filtering;
  - shutdown, backups, upgrades, and `earmark doctor`;
  - what is and is not tested.
- `docs/shortcut.md`:
  - the Shortcut's actions in order;
  - the example request body and response;
  - error messages;
  - how to capture what the share sheet sends and add it as a test case.
- `README.md`:
  - what earmark does and what is built;
  - a quickstart on fake backends that runs verbatim in the sandbox;
  - links to the guides;
  - the commands;
  - a configuration reference that lists every `Settings` field;
  - development and CI.
- `Settings.secret_values()` also returns the bare ntfy topic when it is at least 12
  characters long. A job error that quotes only the topic is then redacted in `job.error`,
  in logs, and in a notification's `Reason:` line.
- Tests tie the documents to the code:
  - the README settings reference against `Settings`;
  - the Shortcut example body against `IngestRequest` and the live route;
  - the Shortcut action order;
  - the share-sheet fixture;
  - relative links;
  - the Dockerfile, `.dockerignore`, the compose file, and the units, all statically;
  - the new workflow under `tests/test_ci.py`'s rules.

## 2. Findings

Everything below was checked on 2026-11-20. Scratch files are in the planner's scratchpad,
and nothing was written outside `docs/`.

### 2.1 What earlier steps deferred to this one

Read from each plan's "Out of scope" section and its deployment notes:

| From | Deferred item | Where it lands |
|---|---|---|
| 01 | README, Docker, deployment docs | whole step |
| 01a | Container build in CI | `container.yml` |
| 03 §2.3 | `DATA_DIR` must be a local filesystem (WAL shared memory; no NFS/SMB; a Docker named volume or bind mount on the same host is fine) | deploy.md "Requirements" |
| 04 §2.4, §2.9, §5.13 | The Shortcut sends `Content-Type: application/json` via "Get Contents of URL" with a JSON body. The Shortcut passes the URL itself ("Get URLs from Input"), not text with a URL inside. What the share sheet hands a Shortcut is unverified. Tailscale binding, TLS. | shortcut.md, deploy.md |
| 05 §5.5 | The container stop timeout need not cover a transcription (the job is released). `SIGKILL` leaves the job `running` until `JOB_LEASE_SECONDS`. | compose `stop_grace_period`, units' `TimeoutStopSec`, deploy.md "Stopping" |
| 06 §2.3 | The container needs a CA bundle (httpcore2 loads OpenSSL's default paths) | Dockerfile installs `ca-certificates` |
| 06, risk 6 | `EARMARK_ALLOW_PRIVATE_FETCH` and Pi-hole-style blocking of trackers | deploy.md "Fetching" |
| 07 §2.5 | Paste a real iOS share into a test | 5.9: fixture plus procedure, left empty (no phone) |
| 08 §9 | CTranslate2 with CUDA for sm_121, the GPU profile, documented as untested | 2.3, deploy.md |
| 09 §9 | The ASR server and the GPU compose profile, documented as untested | compose `asr` service (`gpu` profile) |
| 11 §9 | Running a local LLM server in the container or compose file | deploy.md section only (deviation 4) |
| 14 §9 | Making `index.yml` required; the container image | README CI section (stays not required), image |
| 15 §9 | ntfy `upstream-base-url`, reserving the topic or using a token, Tailscale reachability for the phone, the README and Shortcut docs mentioning `earmark doctor` | deploy.md "Notifications", README, shortcut.md |
| 15 verifier | The bare ntfy topic is not in `Settings.secret_values()` | 5.10 |

### 2.2 Base images and tools (live)

- **`python:3.13.18-slim-trixie`.** Docker Hub lists it as updated on 2026-11-08. The
  index digest, from the registry `HEAD` with an OCI index `Accept` header, is
  `sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90`. The
  `linux/arm64/v8` manifest is `sha256:7e9b0661…`. `3.13-slim` and `3.13-slim-trixie`
  currently point to the same index. The Python patch level (3.13.18) equals the
  uv-managed CPython that CI uses (step 03 §2.1). Debian trixie's SQLite is 3.46.x, the
  same as the sandbox and above everything step 03 needs.
- **`ghcr.io/astral-sh/uv:0.13.9`.** Released 2026-11-17. This is the uv in the sandbox, and
  `setup-uv` allows `>=0.13.9,<0.14`. The index digest is
  `sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d`, with an arm64
  manifest. uv's Docker guide at tag 0.13.9
  (`docs/guides/integration/docker.md`) confirms:
  - `COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /uvx /bin/`;
  - `UV_COMPILE_BYTECODE=1`, `UV_LINK_MODE=copy`, `UV_PYTHON_DOWNLOADS=0`;
  - `RUN --mount=type=cache,target=/root/.cache/uv`;
  - `uv sync --locked --no-install-project`, then `uv sync --locked --no-editable` in a
    multi-stage build, copying `/app/.venv` into the final stage.
- **The build steps work.** Simulated in the sandbox with uv 0.13.9, in a scratch copy of
  `pyproject.toml uv.lock .python-version README.md` and then `src/`:
  - `UV_PROJECT_ENVIRONMENT=<venv> UV_PYTHON_DOWNLOADS=never uv sync --locked --no-dev
    --no-install-project` installed the core dependencies.
  - After copying `src`, `uv sync --locked --no-dev --no-editable --extra transcribe
    --extra index` installed `earmark==0.1.0` as a regular wheel, not editable.
  - From `/`, `<venv>/bin/earmark --version` printed `earmark 0.1.0`.
  - `<venv>/bin/python -c "import ctranslate2, faster_whisper, lancedb, fastembed"`
    succeeded in 1.1 s, with ctranslate2 4.9.0 and `get_cuda_device_count() == 0`.
  - The venv is 692 MB.
  - `README.md` must be in the build context, because `pyproject.toml` declares
    `readme = "README.md"` and `uv_build` reads it.
  - The venv's `bin/python` is a symlink to the interpreter that uv found. Both stages
    therefore use the same Python image, so `/usr/local/bin/python3.13` exists in the
    final stage.
- **System libraries.** `readelf -d` over every `.so` in ctranslate2, onnxruntime, av,
  lancedb, and pyarrow lists only `libc`, `libm`, `libpthread`, `libdl`, `librt`,
  `libgcc_s`, `libstdc++`, and `ld-linux-aarch64`. Everything else is vendored
  (`*.libs/`, including FFmpeg in `av.libs` and libgomp in `ctranslate2.libs`). Debian
  slim has all of these (apt itself needs libstdc++). No ffmpeg package is needed.
  `ca-certificates` is installed explicitly for step 06's CA bundle.
- **GitHub's `ubuntu-24.04-arm` image.** The runner-images readme
  `images/ubuntu/Ubuntu2404-Arm64-Readme.md` (image 20261116) lists Docker Client and
  Server 28.0.4, Docker Buildx 0.39.0, and Docker Compose 2.38.2. So the workflow needs no
  Docker setup action:
  - `docker build` uses BuildKit, whose built-in Dockerfile frontend supports
    `RUN --mount=type=cache`;
  - `docker compose config` can validate the compose file.

  Arm64 images moved from `actions/partner-runner-images` to `actions/runner-images` in
  2026. The label is unchanged.
- **Actions (GitHub API, releases/latest).** `actions/checkout` is v7.0.1, the existing
  pin. `astral-sh/setup-uv` v10.4.0 was released today, but the workflow does not use it.
  `docker/build-push-action` is v7.4.0 and `docker/setup-buildx-action` is v4.4.1.

  **Decision: no Docker actions.** Plain `docker build` and `docker run` steps need only
  the existing checkout pin. That adds no supply-chain surface and no pin churn. What it
  gives up is GHA layer caching: an uncached build downloads about 700 MB of wheels,
  which takes minutes, so this is acceptable for a check that is not required.
- **No `# syntax=` line in the Dockerfile.** That line makes BuildKit pull an unpinned
  `docker/dockerfile` frontend image from Docker Hub. The built-in frontend of Docker 28
  supports everything used here.

### 2.3 GPU transcription on the DGX Spark: decision

What exists:

- The PyPI CTranslate2 wheel for aarch64 is CPU-only (steps 01 and 08).
- Upstream `docs/installation.md` (master, read live) now has an "NVIDIA Jetson
  (aarch64 + CUDA)" source-build recipe:
  - `cmake .. -DWITH_CUDA=ON -DWITH_CUDNN=ON -DWITH_MKL=OFF -DOPENMP_RUNTIME=COMP`;
  - build the Python wheel with `CTRANSLATE2_ROOT`;
  - add the library path to `LD_LIBRARY_PATH` at run time.
- CTranslate2 v4.9.0's `CMakeLists.txt` (read live) uses first-class CMake CUDA, so the
  architecture is `-DCMAKE_CUDA_ARCHITECTURES=121`. **Dead end:** the old `CUDA_ARCH_LIST`
  option is gone; do not document it.
- The "CUDA 13 support" issue, OpenNMT/CTranslate2#1933, is still open (updated
  2026-11-15).
- `nvidia/cuda:13.x-cudnn-devel-ubuntu24.04` exists for arm64 (13.5.0 is from
  2026-11-05). Ubuntu 24.04 ships Python 3.12, so such an image would need uv-managed
  Python 3.13 for the venv.
- **NVIDIA's vLLM container** (`nvcr.io/nvidia/vllm`) can be pulled anonymously. Its
  token comes from `https://nvcr.io/proxy_auth`. The tags `26.10-py3` and `26.11-py3`
  both have `linux/arm64` manifests.
  - `26.10-py3` was created 2026-10-21. It carries vLLM 0.31.0, CUDA 13.4.3, and
    cuDNN 9.27, with entrypoint `/opt/nvidia/nvidia_entrypoint.sh` and no `CMD`. Its index
    digest is `sha256:0a0de9362545e3e062750bec4ad4f722422a04a81182b5258a7deda4325e9313`.
  - `26.11-py3` was created 2026-11-13 and is too new to pin.
  - The image's `PATH` starts with `/opt/ffmpeg-safe/bin`, so it ships an ffmpeg build.
    Step 09 left that open. Whether it decodes M4A is still not verified.
  - vLLM v0.31.0 `vllm/envs.py` (read live) still defines `VLLM_MAX_AUDIO_CLIP_FILESIZE_MB`
    (25), `VLLM_MAX_AUDIO_DECODE_DURATION_S` (600), and `VLLM_MAX_AUDIO_DECODE_BYTES`
    (268435456). It also reads the API key from `VLLM_API_KEY`, so the key does not need
    to go on the command line.

**Decision.** The earmark image has one target, CPU only. The compose `gpu` profile adds
the `asr` service: the pinned vLLM container serving Whisper, with a GPU reservation. The
worker reaches it as `http://asr:8000/v1` through `EARMARK_ASR_BACKEND=remote`.

- This is step 09's recommended path. It uses a container NVIDIA builds and supports for
  the Spark, and earmark's client for it is already tested against stubs.
- An in-image CUDA CTranslate2 would mean:
  - a from-source build against an open upstream CUDA 13 issue;
  - a CUDA and cuDNN runtime base (several GB);
  - uv-managed Python on Ubuntu;
  - replacing the locked PyPI wheel after `uv sync`.

  None of it can be built on the free runner (14 GB disk) or here.
- `docs/deploy.md` keeps a short, clearly unverified outline of that build for operators
  who want it (section 5.6).

### 2.4 Reachability (live documentation and a local `caddy validate`)

**Primary path: the user's design.** Checked 2026-11-20.

- **The token needs Zone:Zone:Read as well as Zone:DNS:Edit.**
  - **caddy-dns/cloudflare** (README at master, and tag `v0.2.4`, which is the master
    commit `a8737d0` of 2026-03-23). It is the Caddy module `dns.providers.cloudflare`.
    The recommended single API token needs "`Zone.Zone:Read` and `Zone.DNS:Edit`
    permissions for the domain(s)". The separate zone and DNS token form is deprecated.
    The Caddyfile form is `tls { dns cloudflare {env.CF_API_TOKEN} }`.
  - **Traefik's lego provider** (`go-acme/lego` `providers/dns/cloudflare/cloudflare.toml`)
    says the same: an API token with "Zone / Zone / Read" and "Zone / DNS / Edit" as
    `CF_DNS_API_TOKEN`, or a split `CF_ZONE_API_TOKEN`. It needs the zone read to resolve
    the domain to a zone id.
  - **So the brief's "scoped to Zone:DNS:Edit" is corrected** to: DNS Edit plus Zone Read,
    with Zone Resources set to that one zone. That is still least privilege for this job.
- **Caddy's README troubleshooting.**
  - `timed out waiting for record to fully propagate` means a resolver cached an earlier
    answer.
  - `expected 1 zone, got 0` means the domain is resolved by `/etc/hosts` or a local DNS
    server.
  - Both are fixed with `resolvers 1.1.1.1` in the `tls` block. That matters here, because
    a home resolver often overrides or filters exactly this zone. An invalid or missing
    token shows as `Invalid request headers`.
- **Getting a Caddy build with the module.** Caddy releases are v2.11.9 (2026-11-05) and
  v2.11.8.
  - **Download server.** `https://caddyserver.com/api/download?os=linux&arch=arm64&p=github.com/caddy-dns/cloudflare`
    returned 200 with `caddy_linux_arm64_custom`. In its own scratch directory it reported
    `v2.11.9` and `list-modules` included `dns.providers.cloudflare`. It is convenient but
    built on demand, so not reproducible.
  - **xcaddy (reproducible).** `xcaddy build v2.11.9 --with
    github.com/caddy-dns/cloudflare@v0.2.4` (needs Go).
  - **Docker.** The official image's builder pattern: `FROM caddy:2.11.9-builder` (arm64
    present), `RUN xcaddy build --with github.com/caddy-dns/cloudflare@v0.2.4`, then
    `COPY --from=builder /usr/bin/caddy` into `caddy:2.11.9`.
  - **The Debian package** with a diverted binary, which Caddy's docs describe for custom
    builds. Not tried.
- **The example Caddyfile is valid and has the intended shape.** It is section 4.4a. With
  that build, `caddy validate --adapter caddyfile` said "Valid configuration", with a
  syntactically plausible dummy token. The module rejects obviously bad tokens at
  provision time ("API token 'dummy' appears invalid"), so a verifier must use a
  40-character dummy. `caddy fmt` produced no diff. `caddy adapt` showed:
  - `listen: ["192.168.1.10:443"]`;
  - a host matcher for `earmark.example.com`;
  - `reverse_proxy` to `127.0.0.1:8787`;
  - `automatic_https: {disable_redirects: true}`, so nothing listens on port 80, which
    DNS-01 does not need.

  No certificate was requested and nothing listened.
- **Decision: Caddy, not Traefik.**
  - Caddy needs one site block of about 15 lines. Issuance, renewal, and OCSP are
    automatic, and a single static binary runs under systemd on the host.
  - Traefik ships the Cloudflare provider built in, so it needs no custom build. For one
    route to a host port, though, it needs a static config (entrypoint bound to the LAN
    address, a `certificatesResolvers` block with `dnsChallenge.provider: cloudflare`) plus
    a dynamic file-provider router and service. That is more to get wrong, for no gain
    here.
  - Caddy's one cost is the custom build. A pinned xcaddy build answers it, and deploy.md
    gives that command.
  - deploy.md mentions Traefik in two sentences (`CF_DNS_API_TOKEN`,
    `dnsChallenge.provider: cloudflare`) for operators who already run it.
- **DNS rebinding protection silently drops public answers with private addresses**
  (checked live):
  - **dnsmasq.** `--stop-dns-rebind` "Reject[s] (and log[s]) addresses from upstream
    nameservers which are in the private ranges". The exemption is
    `--rebind-domain-ok=/example.com/`, or `rebind-domain-ok=/example.com/` in a config
    file.
  - **Pi-hole.** FTL generates `bogus-priv` (reverse lookups only), not `stop-dns-rebind`,
    so Pi-hole alone does not drop the answer. But Pi-hole's own Unbound guide
    (`pi-hole/docs`, `guides/dns/unbound.md`) sets `private-address: 192.168.0.0/16`,
    `10.0.0.0/8`, `172.16.0.0/12`, and so on, which removes such answers. The exemption is
    `private-domain: "example.com"` in Unbound. Extra dnsmasq lines go in Pi-hole v6's
    `misc.dnsmasq_lines` (FTL `config.c`), set in `pihole.toml` or with
    `pihole-FTL --config`, not through the web API.
  - **OPNsense** (docs `manual/unbound.rst`). Unbound has "Rebind Protection networks",
    active when the DNS rebind check is on, and "Private Domains": "domains … allowed to
    contain private addresses".
  - **Unifi and other routers.** Not checked. deploy.md says to look for a rebind
    protection or private-domain setting in the gateway's DNS options.
  - **The symptom.**
    - `dig +short earmark.example.com @1.1.1.1` returns the private address, while
      `dig earmark.example.com` through the local resolver returns no `A` record
      (NOERROR with an empty answer, or NXDOMAIN or REFUSED, depending on the resolver).
    - dnsmasq logs "possible DNS-rebind attack detected".
    - The Shortcut fails with a "server could not be found" style error, not an HTTP
      error.
- **WireGuard.** `AllowedIPs` decides what goes into the tunnel. A `/32` for the server's
  LAN address, or the home subnet, keeps every other byte off the tunnel (split tunnel).
  The iOS and macOS WireGuard apps have on-demand rules: activate on cellular, and on
  Wi-Fi except named SSIDs. Not verified by the earmark build. Nothing was run.

**Alternative: Tailscale Serve.**

- **Tailscale Serve** (`tailscale.com/kb/1242/tailscale-serve`). It runs as
  `tailscale serve [--bg] [--https=<port>] <target>`. HTTPS on 443 is the default. The
  target can be a port, `localhost:3000`, or a full URL, and `tailscale serve status` and
  `reset` exist. So `tailscale serve --bg http://127.0.0.1:8787` gives
  `https://<machine>.<tailnet>.ts.net/` with a valid certificate, reachable only on the
  tailnet. It needs MagicDNS and HTTPS certificates enabled for the tailnet; the CLI points
  to the admin page when they are off. `tailscale funnel` would make it public and must
  not be used.
- **Dropped: Cloudflare Tunnel.** The user rejected it, because it puts Cloudflare in the
  traffic path and gives the API a public edge. Neither deploy.md nor shortcut.md mentions
  `CF-Access-*` headers.

### 2.5 ntfy (live: `binwiederhier/ntfy` `docs/config.md`)

- Section "iOS instant notifications":
  - With `upstream-base-url: "https://ntfy.sh"`, the self-hosted server forwards a
    `poll_request` to ntfy.sh. It carries only the message id and the SHA-256 of the topic
    URL.
  - The iOS app then polls the self-hosted server for the content, so the phone must reach
    that server (the tailnet).
  - Without it, delivery "can take hours".
  - `upstream-access-token` is optional.
  - The environment variable is `NTFY_UPSTREAM_BASE_URL`.
- Access tokens are `tk_…`, sent as `Authorization: Bearer`. earmark already sends
  `EARMARK_NTFY_TOKEN` that way (step 15).

### 2.6 Shortcuts (Apple's Shortcuts User Guide; Apple's site rate-limited some fetches)

- "Request your first API" (`apd58d46713f`) says "Get Contents of URL" has a method under
  "Show More" (GET, POST, PUT, PATCH, DELETE). POST adds a **Request Body**, which can be
  JSON, Form, or File.
- The share-sheet pages (`apd350ce757a`, input types `apd7644168e1`) say:
  - a shortcut run from the share sheet receives the host app's content as its input;
  - the accepted input types decide where it appears (only URLs means it appears in
    Safari but not Photos);
  - the input is passed to the first action as "Shortcut Input".
- Apple Community threads (iOS 16 era) report:
  - "network connection was lost" when a header row is empty or malformed;
  - a crash editing JSON keys on iOS 16.2.

  shortcut.md's troubleshooting mentions the first.
- **Not verified, and not verifiable here:**
  - the exact action titles on today's iOS;
  - whether "Ask for Input" accepts an empty answer;
  - what Apple Podcasts' share sheet hands the Shortcut (URL vs. text with a URL, the
    query parameters, "From Start" vs. "From [time]").

  Step 04 §2.9 recorded the URL shapes from Apple's own API and page metadata.
  `parse_apple_url` handles all of them, and the Shortcut's "Get URLs from Input" plus
  "Get Item from List" covers a text share. The docs say which parts are unverified.

### 2.7 The README quickstart runs as written (sandbox, 2026-11-20)

The draft in section 4.6 was run with `bash` from the repository root. It:

- printed the doctor summary (`all checks passed, 8 warnings`);
- returned `{"status":"ok"}` once the server was up;
- returned `{"job_id":…,"status":"queued","created":true}` for the ingest;
- printed `job … done` from `worker --once` and listed the job in `earmark jobs`;
- listed `…/vault/Earmark/Podcasts/<key>.md`;
- stopped the background server cleanly with `kill %1` then `wait`: `uv run` forwards
  SIGTERM, and uvicorn logged "Finished server process". Without the `wait`, the script
  can end while the server is still shutting down, and a `pgrep` run straight after it
  still sees the server.

Findings that shape it:

- The healthz wait uses `curl -s --retry 20 --retry-connrefused --retry-delay 1` instead
  of a `sleep` loop, which agent harnesses may block. With `-sS`, curl printed one
  "Failed to connect" line before the server was up, so the step uses `-s`.
- Everything lives in `mktemp -d`, so the quickstart leaves nothing in the repository.
  Exported variables win over any `.env`.
- `uv sync` without extras is correct here: every backend is fake.

### 2.8 Markdown and the gate

`ruff format --check .` (in `make check`) also formats the Python code blocks in Markdown
files.

- 145 `.py` and 28 `.md` files give "173 files already formatted".
- A scratch `t.md` with ```` ```python ```` / `x=1` failed, while a `bash` block next to it
  was not touched.

So any `python` fence in the new docs must be ruff-clean, and `make check` enforces it.
The plan avoids Python fences except one short one-liner in deploy.md (5.6). Shell, JSON,
YAML, INI, and Dockerfile fences are not checked.

### 2.9 Bare-topic redaction: current code

- `Settings.secret_values()` returns only `SecretStr` values, so it has the full
  `NTFY_URL` but not the topic.
- `doctor.report.doctor_secrets` already appends the topic (any length).
- `NtfyClient` adds the topic to its own error-detail secrets.
- `NtfyOptions.secrets` (used by `compose`), the runner's `describe_error`, and the log
  formatter all use `secret_values()`, so a `job.error` that contains only the topic
  survives into `job.error`, the logs, and the notification's `Reason:` line.
- `log.redact` replaces substrings. Redacting a short topic such as `earmark` would turn
  every logger name (`earmark.pipeline.worker`), every "run `earmark reindex`" hint, and
  so on into `**********`. Existing tests use topics of 14 or more characters
  (`sentinel-topic`, `sentinel-topic-91x`) and also `https://ntfy.sh/topic` (5) in
  `test_ntfy.py`. See 5.10 for the rule chosen.

## 3. Files

| File | Change |
|---|---|
| `Dockerfile` | New. Stages `uv`, `build`, `runtime` (section 4.1). |
| `.dockerignore` | New. Allow-list (4.2). |
| `compose.yaml` | New. `api`, `worker`, `asr` (profile `gpu`); volumes `data`, `asr-models` (4.3). |
| `deploy/systemd/earmark-api.service` | New. Example unit (4.4). |
| `deploy/systemd/earmark-worker.service` | New. Example unit (4.4). |
| `deploy/caddy/Caddyfile` | New. Example reverse proxy on the LAN address, DNS-01 via Cloudflare (4.4a). |
| `.github/workflows/container.yml` | New. "Container image" (4.5). |
| `.github/dependabot.yml` | Add the `docker` ecosystem (4.5). |
| `README.md` | Replace the placeholder (4.6). |
| `docs/deploy.md` | New (4.7). |
| `docs/shortcut.md` | New (4.8). |
| `tests/fixtures/share_sheet.json` | New. Recorded share-sheet captures; starts with an empty `shares` list (4.9). |
| `.env.example` | Add a commented "Docker Compose only" section: `VAULT_PATH`, `API_BIND`, `APP_UID`, `APP_GID`, `EXTRAS`, `ASR_GPU_MEMORY_UTILIZATION`. No `EARMARK_*` line changes. |
| `src/earmark/config.py` | `secret_values()` adds the bare ntfy topic (≥ `NTFY_TOPIC_SECRET_MIN_CHARS` = 12). New constant (4.10). |
| `src/earmark/doctor/report.py` | Optional simplification: `doctor_secrets` keeps appending the topic for any length; its docstring mentions that `secret_values()` already covers long topics. No behaviour change. |
| `tests/doc_helpers.py` | New. Markdown extraction helpers (4.11). |
| `tests/test_doc_helpers.py` | New. Tests of the helpers. |
| `tests/test_docs.py` | New. README, shortcut.md, deploy.md, and share-fixture tests (section 6). |
| `tests/test_deploy.py` | New. Static checks of the Dockerfile, `.dockerignore`, `compose.yaml`, and the units. |
| `tests/test_ci.py` | Container workflow tests; the Dependabot test accepts `docker`. |
| `tests/test_config.py` | Bare-topic tests. |
| `tests/test_ntfy.py` | The `Reason:` line redacts a bare topic coming from `Settings`. |
| `tests/test_runner.py` | `describe_error` with `secret_values()` redacts a bare topic. |
| `docs/ARCHITECTURE.md` | Already edited by this plan (section 8). |

Nothing else under `src/` changes.

## 4. Interfaces

### 4.1 `Dockerfile`

The required shape is below. Comments may be reworded; the tested properties are in
section 6.

```dockerfile
# earmark: one image for the API (`earmark serve`) and the worker (`earmark worker`).
# Built and checked by .github/workflows/container.yml on linux/arm64; never run in the
# build sandbox. See docs/deploy.md.

FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv

FROM python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Optional extras, space-separated: "transcribe index" (default), "index", "transcribe", "".
ARG EXTRAS="transcribe index"
COPY pyproject.toml uv.lock .python-version README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project \
      $(for e in $EXTRAS; do printf -- '--extra %s ' "$e"; done)
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable \
      $(for e in $EXTRAS; do printf -- '--extra %s ' "$e"; done)

FROM python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS runtime
# The user that owns /data and writes the vault. Match the vault owner on the host.
ARG UID=1000
ARG GID=1000
RUN test "$UID" -ne 0 && test "$GID" -ne 0 \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid "$GID" --non-unique earmark \
 && useradd --uid "$UID" --gid "$GID" --non-unique --create-home \
      --home-dir /home/earmark --shell /usr/sbin/nologin earmark \
 && install -d -o earmark -g earmark -m 0750 /data /vault
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    EARMARK_HOST=0.0.0.0 \
    EARMARK_DATA_DIR=/data \
    EARMARK_VAULT_DIR=/vault \
    HF_HOME=/data/huggingface
WORKDIR /home/earmark
USER earmark:earmark
EXPOSE 8787
ENTRYPOINT ["earmark"]
CMD ["serve"]
```

Rules:

- **Pinned images.** Every external `FROM` is `name:tag@sha256:<64 hex>`. Write the
  digests in `FROM` lines literally, never through `ARG`, so Dependabot's `docker`
  ecosystem can update them. The runtime and build stages use the identical Python
  reference (venv symlink, 2.2).
- **No health check in the image.** There is no `HEALTHCHECK`, because the same image runs
  the worker. The API's health check lives in compose.
- **No `VOLUME`.** Compose declares the mounts. A `VOLUME` would create anonymous volumes
  on every `docker run`.
- **No `.env` in the image.** Settings read `.env` from the working directory; the
  `WORKDIR` (`/home/earmark`) has none, and the build context cannot contain one (4.2).
  Settings come from the environment that compose's `env_file` provides.
- **`EARMARK_HOST=0.0.0.0` only inside the container.** The host port mapping decides
  exposure (4.3).
- **`HF_HOME=/data/huggingface`.** With `ASR_MODEL_DIR` unset, faster-whisper's models land
  in `/data/huggingface/hub`, on the volume, so a restart does not download 3 GB again.
  The embedding model goes to `<DATA_DIR>/models/embed` = `/data/models/embed` (step 14).

### 4.2 `.dockerignore`

```
# Allow-list: only what the image build reads. Keeps .env, data/, .venv/, .git/ out.
*
!pyproject.toml
!uv.lock
!.python-version
!README.md
!src/
**/__pycache__
**/*.pyc
```

### 4.3 `compose.yaml`

```yaml
# docker compose up -d            the API and the worker
# docker compose --profile gpu up -d   also a vLLM Whisper server on the GPU (untested)
# Settings come from .env (copy .env.example). See docs/deploy.md.
name: earmark

x-earmark: &earmark
  image: earmark:local
  build:
    context: .
    args:
      EXTRAS: ${EXTRAS:-transcribe index}
      UID: ${APP_UID:-1000}
      GID: ${APP_GID:-1000}
  env_file: .env
  environment:
    # In-container paths win over host paths that .env may hold for a native install.
    EARMARK_DATA_DIR: /data
    EARMARK_VAULT_DIR: /vault
    EARMARK_HOST: 0.0.0.0
    EARMARK_PORT: "8787"
    # Empty means unset: models go to the volume (HF_HOME and DATA_DIR defaults).
    EARMARK_ASR_MODEL_DIR: ""
    EARMARK_EMBED_MODEL_DIR: ""
  volumes:
    - data:/data
    - ${VAULT_PATH:?set VAULT_PATH in .env to the vault folder on this host}:/vault
  init: true
  restart: unless-stopped
  security_opt:
    - no-new-privileges:true
  cap_drop:
    - ALL

services:
  api:
    <<: *earmark
    command: ["serve"]
    ports:
      - ${API_BIND:-127.0.0.1:8787}:8787
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3)
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s

  worker:
    <<: *earmark
    command: ["worker"]
    # SIGTERM releases the running job back to the queue; this only covers that.
    stop_grace_period: 2m

  asr:
    profiles: ["gpu"]
    image: nvcr.io/nvidia/vllm:26.10-py3@sha256:0a0de9362545e3e062750bec4ad4f722422a04a81182b5258a7deda4325e9313
    command:
      - vllm
      - serve
      - ${EARMARK_ASR_MODEL:-openai/whisper-large-v3-turbo}
      - --host
      - 0.0.0.0
      - --port
      - "8000"
      - --gpu-memory-utilization
      - ${ASR_GPU_MEMORY_UTILIZATION:-0.2}
    environment:
      VLLM_API_KEY: ${EARMARK_ASR_API_KEY:-}
      VLLM_MAX_AUDIO_CLIP_FILESIZE_MB: "1024"
      VLLM_MAX_AUDIO_DECODE_DURATION_S: "14400"
      VLLM_MAX_AUDIO_DECODE_BYTES: "4294967296"
      HF_HOME: /models
    volumes:
      - asr-models:/models
    shm_size: 8gb
    restart: unless-stopped
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]

volumes:
  data:
  asr-models:
```

Notes the implementer must keep:

- **`asr` publishes no port.** Only the worker reaches it, over the compose network, at
  `http://asr:8000/v1`.
- **`${EARMARK_ASR_API_KEY:-}` uses `:-`, not `:?`.** Compose interpolates the whole file
  before it filters profiles, so `:?` would break `docker compose up` for everyone not
  using `gpu`.
- **`VAULT_PATH` uses `:?`.** Nothing works without it.
- **Compose-only variables.** `VAULT_PATH`, `API_BIND`, `APP_UID`, `APP_GID`, `EXTRAS`,
  and `ASR_GPU_MEMORY_UTILIZATION` are unprefixed. Settings ignore them (unprefixed keys
  are ignored, `test_unprefixed_vars_ignored`), and deploy.md documents them in one table.
- **Merge keys.** `<<: *earmark` is in the Compose spec, and PyYAML's `safe_load` resolves
  it, so tests see merged services.
- **The shared image.** Both services carry the same `image:` and `build:`. Compose may
  build twice; the second build is a cache hit.

### 4.4 systemd units (`deploy/systemd/`)

`earmark-api.service`:

```ini
# Example unit for a native install under /srv/earmark (see docs/deploy.md).
# Settings: /srv/earmark/.env (mode 0600, owned by earmark), read by earmark itself.
[Unit]
Description=earmark ingest API
Wants=network-online.target
After=network-online.target

[Service]
Type=exec
User=earmark
Group=earmark
WorkingDirectory=/srv/earmark
Environment=HF_HOME=/srv/earmark/data/huggingface
ExecStart=/srv/earmark/.venv/bin/earmark serve
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/srv/earmark/data

[Install]
WantedBy=multi-user.target
```

`earmark-worker.service` is the same except for these lines:

- `Description=earmark worker`;
- `ExecStart=/srv/earmark/.venv/bin/earmark worker`;
- `TimeoutStopSec=120`;
- a `ReadWritePaths=` line for the vault, shown as
  `ReadWritePaths=/srv/earmark/data /home/you/Obsidian/Vault` with a comment saying to
  replace the second path.

`WorkingDirectory` is where `.env` is read from (Settings read `.env` from the working
directory). The API needs write access to `data/` because it enqueues into SQLite. Both
units use `KillSignal=SIGTERM`, which is earmark's shutdown path (step 05).

### 4.4a `deploy/caddy/Caddyfile`

Use exactly this content. It was validated and fmt-checked in 2.4; only the comments may
change, and `caddy fmt` must still produce no diff (tabs for indentation).

```
# Example: TLS for earmark on the LAN interface only, certificate from Let's Encrypt
# through the DNS-01 challenge. Needs a Caddy build with github.com/caddy-dns/cloudflare.
{
	# DNS-01 needs no port 80; do not listen on it at all.
	auto_https disable_redirects
}

earmark.example.com {
	# The LAN (or WireGuard) address the DNS record points to. Never 0.0.0.0.
	bind 192.168.1.10
	tls {
		dns cloudflare {env.CF_API_TOKEN}
		# Check the challenge record on public DNS, not a local resolver that may
		# override or filter the zone.
		resolvers 1.1.1.1
	}
	reverse_proxy 127.0.0.1:8787
}
```

- **The token is only an environment placeholder.** It is set in the proxy's own
  environment: for Caddy's systemd unit, a drop-in with
  `EnvironmentFile=/etc/caddy/cloudflare.env`, mode 0600, owned by root. It is never in
  earmark's `.env`, `Settings`, `compose.yaml`, or `.env.example`.
- **The upstream is the loopback port.** That is what compose publishes by default
  (`API_BIND=127.0.0.1:8787`) and what the native unit binds (`EARMARK_HOST=127.0.0.1`).

### 4.5 `.github/workflows/container.yml` and Dependabot

```yaml
# Builds the container image on linux/arm64 without pushing it, then runs three one-off
# checks inside it. It never starts the API or the worker, and does not build the gpu
# profile's server. Not a required check. See docs/deploy.md.
name: Container image

on:
  push:
    branches: [main, impl]
  pull_request:

permissions:
  contents: read

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  image:
    name: docker build (linux/arm64)
    runs-on: ubuntu-24.04-arm
    timeout-minutes: 30
    steps:
      - name: Check out
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - name: Validate compose.yaml
        run: |
          cp .env.example .env
          printf 'VAULT_PATH=%s\n' "$RUNNER_TEMP/vault" >> .env
          docker compose config --quiet
          docker compose --profile gpu config --quiet
      - name: Build the image
        run: docker build --tag earmark:ci .
      - name: Check the image
        run: |
          docker run --rm earmark:ci --version
          docker run --rm --entrypoint python earmark:ci -c "import ctranslate2, faster_whisper, fastembed, lancedb"
          test "$(docker run --rm --entrypoint id earmark:ci -u)" = 1000
          docker image ls earmark:ci
```

- No `env:` keys and no Docker actions. The only `uses:` is the checkout pin shared with
  `ci.yml`.
- The words "secrets", `docker push`, `docker login`, and `--push` must not appear (the
  existing `test_only_llm_workflow_references_secrets` already scans every workflow for
  `\bsecrets\b`, comments included).

Dependabot gets a third entry:

```yaml
  - package-ecosystem: docker
    directory: /
    target-branch: impl
    schedule:
      interval: weekly
    cooldown:
      default-days: 7
    ignore:
      # Python 3.14 and uv 0.14 need project changes (requires-python, uv_build bound).
      - dependency-name: "*"
        update-types: ["version-update:semver-major", "version-update:semver-minor"]
    groups:
      images:
        patterns: ["*"]
```

The vLLM tag in `compose.yaml` is bumped by hand. It is not watched, because the
`docker-compose` ecosystem would also pick up `earmark:local`.

### 4.6 `README.md`

Sections, in this order:

1. **Title and one paragraph.** What earmark does (share an Apple Podcasts episode from
   the iPhone, get a Markdown research note in the Obsidian vault), self-hosted, Python
   3.13.
2. **How it works.**
   - The flow diagram from `CLAUDE.md` (`POST /ingest ─► jobs table … notify`).
   - One line per stage naming the built backend: `apple` resolver, `http` fetch,
     `faster_whisper` / `remote` ASR, `anthropic` / `openai_compat` analysis, `vault`
     store with entity notes, `lancedb` index, `ntfy` notifier.
   - The vault layout (`Earmark/Podcasts/<Show>/…`, `transcripts/`, `Entities/…`,
     `Backups/`, `aliases.yaml`), with a link to `docs/ARCHITECTURE.md` "Data layout".
   - Do **not** list `Digests/`, `reanalyze`, `/episodes`, or `earmark digest`: steps 17
     and 18 are not built. One line says "Planned: re-running a prompt on a stored
     episode, and a weekly digest (`docs/PLAN.md`)".
3. **Quickstart (fake backends).** It is fenced in `bash` between the markers below and
   must stay exactly runnable in the sandbox. Use this text, already run as described
   in 2.7:

   ````markdown
   <!-- quickstart:begin -->
   ```bash
   uv sync
   uv run earmark --version

   demo=$(mktemp -d)
   mkdir "$demo/vault"
   export EARMARK_API_TOKEN=demo-token
   export EARMARK_DATA_DIR="$demo/data" EARMARK_VAULT_DIR="$demo/vault"
   export EARMARK_RESOLVER_BACKEND=fake EARMARK_FETCH_BACKEND=fake EARMARK_ASR_BACKEND=fake \
     EARMARK_LLM_PROVIDER=fake EARMARK_STORE_BACKEND=fake EARMARK_INDEX_BACKEND=fake \
     EARMARK_NOTIFY_BACKEND=fake
   uv run earmark doctor

   uv run earmark serve &
   curl -s --retry 20 --retry-connrefused --retry-delay 1 http://127.0.0.1:8787/healthz
   curl -s http://127.0.0.1:8787/ingest \
     -H "Authorization: Bearer $EARMARK_API_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"url": "https://podcasts.apple.com/us/podcast/id1200361736?i=1000802907981", "prompt": "Which papers are mentioned?"}'
   uv run earmark worker --once
   uv run earmark jobs
   cat "$demo"/vault/Earmark/Podcasts/*.md
   kill %1
   wait
   ```
   <!-- quickstart:end -->
   ````

   After the block, explain the output in prose:
   - doctor passes with warnings for the fakes and the short token;
   - `{"status":"ok"}`, then `{"job_id": …, "status": "queued", "created": true}`;
   - `job <id> done`;
   - the fake note.

   Mention that the episode URL is never fetched (the resolver is fake), and that `kill %1`
   stops the server. Then a short "Real backends" paragraph pointing to `docs/deploy.md`
   (`uv sync --extra transcribe --extra index`, a vault, `EARMARK_LLM_API_KEY`).
4. **Guides.** Links to `docs/deploy.md`, `docs/shortcut.md`, `docs/asr.md`,
   `docs/llm.md`, `docs/ARCHITECTURE.md`, and `docs/PLAN.md`.
5. **Commands.** One table row each:
   - `serve`, `worker [--once]`, `submit <url> [--prompt]`, `jobs [--status] [--limit]`;
   - `resolve <url>`, `transcribe <file> [--json]`, `analyze <key> [--prompt] [--json]`;
   - `entities rebuild [--prune]`;
   - `search QUERY [--limit] [--kind] [--episode] [--json]`, `reindex [--episode KEY]`;
   - `doctor [--online]`, `config`, `--version`.

   Check each flag against `uv run earmark <cmd> --help` and list only flags that exist.
   Also the HTTP API (`POST /ingest`, `GET /jobs/{id}`, `GET /jobs`, `GET /search`,
   `GET|HEAD /healthz`), linking to ARCHITECTURE "HTTP API".
6. **Configuration reference.**
   - Settings come from `EARMARK_*` environment variables or `.env` in the working
     directory; real environment variables win, and an empty value means unset. Copy
     `.env.example`.
   - Then, between `<!-- settings:begin -->` and `<!-- settings:end -->`, one or more
     Markdown tables, grouped under `###` headings: API, storage, backends, transcription,
     remote ASR, analysis, index, notifications, fetching, worker, misc.
   - Each table has the columns `| Variable | Default | Meaning |`.
   - **Exactly one variable per row**, written as `` `EARMARK_<NAME>` `` and nothing else
     in the first cell. Every `Settings` field appears exactly once.
   - Defaults: as in `.env.example`, or `unset` (secrets are always `unset`).
   - Meanings: condensed from ARCHITECTURE's configuration table. Name the related guide
     where one exists (`docs/asr.md`, `docs/llm.md`, `docs/deploy.md`).
   - Secrets are marked "(secret)": `API_TOKEN`, `ASR_API_KEY`, `LLM_API_KEY`, `NTFY_URL`,
     `NTFY_TOKEN`.
   - After the tables, the Anthropic credential note (`EARMARK_LLM_API_KEY` vs.
     `ANTHROPIC_API_KEY`, and `.env` not exported), and that compose-only variables are in
     `docs/deploy.md`.
7. **Development.**
   - `uv sync`, `make check` (ruff, ruff format including Markdown Python blocks, mypy
     strict, pytest), and the opt-in markers.
   - CI workflows:
     - CI (`make check (linux/arm64)`, the only required check on `main`);
     - Index tests;
     - Live tests (`network.yml`: weekly and manual; jobs `network` and `slow`);
     - LLM test (`llm.yml`, manual only, environment `llm`);
     - Container image (`container.yml`).
   - Branch flow: steps land on `impl` and `main` is fast-forwarded after green CI.

Python fences: none needed in the README. If one is added, it must be ruff-clean (2.8).

### 4.7 `docs/deploy.md`

Headings and required content. The prose style follows `docs/asr.md`: placeholders
`spark`, `earmark.example.com`, `192.168.1.10` (the server's LAN address), and
`tailnet-name`, with "not verified by the earmark build" where it applies.

1. **What is tested.** A short box at the top. These sentences are required in substance;
   the wording may be adjusted:
   - "The container image is build-tested in CI (`.github/workflows/container.yml` on
     linux/arm64), not run-tested. CI builds it and runs only `earmark --version`, an
     import of the extras, and a user-id check inside it. The API and worker have never
     run in a container, because the build sandbox has no Docker."
   - "The `gpu` profile is untested: its vLLM service was never built or run."
   - "The systemd units are examples, reviewed but never run."
   - "The reachability setup is not verified by the earmark build. That covers the
     DNS-only Cloudflare record, the WireGuard split tunnel, Caddy with the DNS-01
     challenge, Tailscale Serve, and ntfy self-hosting, all described from their
     documentation. Only the example `deploy/caddy/Caddyfile` was checked, with
     `caddy validate` and `caddy fmt`, using a Caddy build that has the Cloudflare
     module."
2. **Overview.**
   - API and worker on one host (the DGX Spark), sharing one data directory.
   - The vault folder on the same host, synced to the phone by whatever the user already
     uses (earmark only writes files).
   - The API is never exposed publicly. It listens on `127.0.0.1:8787`, and a reverse
     proxy on the LAN interface serves it over HTTPS as `earmark.example.com`. That name
     resolves, through public DNS, to a private address only reachable at home or over
     WireGuard.
   - A diagram in a text fence:

     ```text
     iPhone / Mac ──(home Wi-Fi, or WireGuard split tunnel)──► 192.168.1.10:443
       Caddy (TLS for earmark.example.com) ──► 127.0.0.1:8787 earmark API
     Cloudflare DNS: earmark.example.com A 192.168.1.10 (DNS only, never proxied)
     ```
3. **Requirements.**
   - aarch64 or x86_64 Linux. The image builds for the host architecture; CI checks arm64.
   - Docker Engine with Compose v2.24 or later, or uv for a native install.
   - `DATA_DIR` on a local filesystem (SQLite WAL, so no NFS or SMB; a named volume is
     fine).
   - A vault folder that exists.
   - Disk:
     - image about 1 GB;
     - Whisper `large-v3` about 3 GB;
     - embedding model 65 MB;
     - the index extra adds about 440 MB to the image (step 14).
   - An Anthropic API key, or a local LLM (`docs/llm.md`).
4. **Install with Docker Compose.**
   - `git clone`, `cp .env.example .env`, `chmod 600 .env`.
   - Edit `.env`:
     - `EARMARK_API_TOKEN` (generate with
       `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`);
     - `EARMARK_LLM_API_KEY`;
     - `VAULT_PATH`;
     - `APP_UID`/`APP_GID` = `id -u`/`id -g` of the vault's owner (check with `ls -ln`);
     - `EARMARK_TZ`;
     - optionally `EARMARK_OBSIDIAN_VAULT_NAME` and ntfy.
   - `docker compose build`, then `docker compose run --rm worker doctor`, then
     `docker compose up -d`, then `docker compose logs -f worker`.
   - Explain:
     - **The compose-only variable table.** It must name every unprefixed `${…}` in
       `compose.yaml`: `VAULT_PATH`, `API_BIND`, `APP_UID`, `APP_GID`, `EXTRAS`,
       `ASR_GPU_MEMORY_UTILIZATION`.
     - **What compose overrides.** It forces the in-container values `EARMARK_DATA_DIR`,
       `VAULT_DIR`, `HOST`, `PORT`, and the empty model dirs, whatever `.env` says.
     - **Data and models live in the `data` volume.**
     - **Ownership.** The image bakes the UID at build time, so rebuild after changing
       it. A named volume takes its ownership from the image the first time; a volume
       created with another UID needs a `chown`, and the doc shows the one-line
       `docker run --rm -v earmark_data:/data … chown -R` command.
     - **Rootless Docker and `userns-remap` shift UIDs.** Not covered.
     - **Values in `.env` must not contain `$`.** Compose interpolates them.
     - **The environment is exported.** Under compose, `env_file` exports every `.env`
       line into the container's environment. So, unlike a native run, an
       `ANTHROPIC_API_KEY` line in `.env` is seen by the SDK. Prefer
       `EARMARK_LLM_API_KEY`.
     - **`API_BIND` stays `127.0.0.1:8787` (the default).** The reverse proxy (or
       `tailscale serve`) runs on the host and reaches the API there. Nothing in the
       default setup binds the API to a non-loopback host address: `EARMARK_HOST=0.0.0.0`
       exists only inside the container's own network namespace. Docker's published
       ports bypass ufw, so never set `API_BIND` to `0.0.0.0` or a LAN address. That
       would serve plain http to the whole LAN and skip the proxy's TLS.
     - **Slimmer images.** `EXTRAS=index` with remote ASR, `EXTRAS=transcribe` with
       `EARMARK_INDEX_BACKEND=none`, or `EXTRAS=` for both. Selecting a backend whose
       extra is missing makes the worker refuse to start with a message naming the extra.
5. **Install natively with systemd.**
   - A dedicated `earmark` user with `/srv/earmark` as its home, and a checkout there.
   - Install uv system-wide.
   - `sudo -u earmark env UV_PYTHON_INSTALL_DIR=/srv/earmark/.python uv sync --locked
     --no-dev --extra transcribe --extra index`.
   - `.env` in `/srv/earmark` (mode 0600), with `EARMARK_HOST=127.0.0.1` and
     `EARMARK_DATA_DIR=/srv/earmark/data`.
   - Copy the units from `deploy/systemd/`, edit the vault path in
     `earmark-worker.service`'s `ReadWritePaths=`, then `systemctl daemon-reload` and
     `systemctl enable --now earmark-api earmark-worker`.
   - `sudo -u earmark /srv/earmark/.venv/bin/earmark doctor` (run from `/srv/earmark` so
     `.env` is read).
   - Run one worker per host.
6. **Reaching the API from the iPhone and the Mac.**
   - Start with the rule: **the API is never exposed publicly.** Earmark itself never
     terminates TLS. There are two supported setups, and both are "not verified by the
     earmark build".
   - **6.1 Private DNS name, WireGuard, and Caddy (primary).** Subsections in this order:
     1. **The DNS record.**
        - In Cloudflare, add `earmark.example.com` as an `A` record for the server's LAN
          address (`192.168.1.10`), with proxy status **DNS only (grey cloud)**.
        - Proxying must stay off. Cloudflare's proxy cannot reach a private address, and
          it would put Cloudflare in the traffic path. With DNS only, Cloudflare answers
          DNS queries and never sees a request.
        - Prefer the LAN address over the server's WireGuard interface address. The LAN
          address works at home without the tunnel, and through it.
        - **Information leak.** The record publishes the internal address to anyone who
          looks it up. That is a minor leak (it reveals the home subnet and the host's
          address, not a way in), and the reader should know it.
     2. **WireGuard on the phone and the Mac.**
        - Use the WireGuard server the network already has (router, OPNsense, Unifi, or
          one on the Spark); setting one up is out of scope.
        - In the client's tunnel, `AllowedIPs = 192.168.1.10/32`, or the home subnet such
          as `192.168.1.0/24`. That makes a **split tunnel**: only traffic to the server
          goes through it.
        - Leave `DNS =` unset, so the name resolves through the normal (public) resolver.
          If it points at the home resolver, the rebinding note below applies there too.
        - Turn on **on-demand** in the iOS and macOS WireGuard apps: activate on cellular,
          and on Wi-Fi except the home SSIDs.
        - **On the home Wi-Fi the LAN routes directly**, so the tunnel stays down and
          nothing changes.
     3. **The reverse proxy: Caddy on the LAN interface.**
        - Why Caddy, in two sentences (2.4): one short site block, automatic issuance and
          renewal.
        - Getting a build with `caddy-dns/cloudflare` (the stock build lacks it):
          - recommended, reproducible:
            `xcaddy build v2.11.9 --with github.com/caddy-dns/cloudflare@v0.2.4`;
          - quick: the download server URL from 2.4 (unpinned, built on demand);
          - in Docker: the `caddy:2.11.9-builder` pattern, run with
            `network_mode: host` so it can bind the LAN address and reach `127.0.0.1`.
        - Check with `caddy list-modules | grep dns.providers.cloudflare`.
        - Install the binary as `/usr/bin/caddy` behind Caddy's own systemd unit, or with
          the Debian package's documented `dpkg-divert` for custom builds.
        - Copy `deploy/caddy/Caddyfile` to `/etc/caddy/Caddyfile` and replace the
          hostname and the `bind` address. `bind` must be the address the record points
          to, never `0.0.0.0`. If that is a WireGuard interface address, the interface
          must be up before Caddy starts.
        - Port 80 is not used (`auto_https disable_redirects`).
        - Traefik works too (its Cloudflare provider is built in: `CF_DNS_API_TOKEN`,
          `dnsChallenge.provider: cloudflare`, an entrypoint on the LAN address, a file
          provider route to `http://127.0.0.1:8787`), but it needs more configuration.
     4. **The certificate: Let's Encrypt with DNS-01.**
        - Create a Cloudflare API token with **Zone → DNS → Edit** and
          **Zone → Zone → Read**, and Zone Resources set to the one zone. Both Caddy's
          module and Traefik need the zone read to find the zone.
        - Put it in `/etc/caddy/cloudflare.env` as `CF_API_TOKEN=…` (mode 0600, root),
          loaded with a `systemctl edit caddy` drop-in `EnvironmentFile=`.
        - The token belongs to the proxy only: never in earmark's `.env`, which compose
          exports into earmark's containers.
        - No inbound port is needed: Caddy proves control by writing a TXT record.
        - Troubleshooting: `Invalid request headers` (token unset or wrong),
          `expected 1 zone, got 0` and `timed out waiting for record to fully propagate`
          (the proxy host's resolver overrides or caches the zone; the example's
          `resolvers 1.1.1.1` handles both).
     5. **The proxy decision.** Say why Caddy is documented and shipped as an example file,
        not as a compose service (5.13).
     6. **DNS rebinding protection (gotcha).** Many home resolvers silently drop a public
        answer that contains a private address, which is exactly this record:
        - Pi-hole with Unbound per Pi-hole's guide (`private-address`);
        - dnsmasq with `stop-dns-rebind`;
        - OPNsense's Unbound rebind protection;
        - many routers, Unifi among them.

        How to tell:
        - `dig +short earmark.example.com @1.1.1.1` prints `192.168.1.10`, but
          `dig earmark.example.com` through the home resolver returns no `A` record;
        - dnsmasq logs "possible DNS-rebind attack detected";
        - the Shortcut reports that the server cannot be found.

        Fix: allow-list the domain, never turn the protection off:
        - dnsmasq (and Pi-hole v6 through `misc.dnsmasq_lines`):
          `rebind-domain-ok=/example.com/`;
        - Unbound (Pi-hole's Unbound, OPNsense's "Private Domains"):
          `private-domain: "example.com"`;
        - routers: the "rebind protection" or "private domains" exception in their DNS
          settings.

        A local DNS override for the name is an alternative. Then Caddy needs
        `resolvers 1.1.1.1` (already in the example) for the challenge.
     7. **Test.**
        - From the Mac at home, then on cellular with the tunnel on:
          `curl https://earmark.example.com/healthz` prints `{"status":"ok"}` with a valid
          certificate.
        - From a host outside both, the name resolves but the connection fails. That is
          correct.
        - The Shortcut URL is `https://earmark.example.com/ingest`.
   - **6.2 Tailscale Serve (shorter alternative, for those without WireGuard).**
     - Install Tailscale on the Spark, the iPhone, and the Mac, and turn on MagicDNS and
       HTTPS certificates in the admin console.
     - Run `sudo tailscale serve --bg http://127.0.0.1:8787` and check it with
       `tailscale serve status`. The Shortcut URL is then
       `https://spark.tailnet-name.ts.net/ingest`.
     - The Tailscale VPN must be on in the phone for the Shortcut to work.
     - **Never use `tailscale funnel`**, which makes the service public.
7. **Transcription on the DGX Spark.** Three options:
   - **a. CPU, in the worker (default).** `faster_whisper` with `large-v3` on the Spark's
     CPU is slow for long episodes. A smaller `EARMARK_ASR_MODEL` (`distil-large-v3`,
     `large-v3-turbo`, `small`) is the stopgap. The PyPI CTranslate2 wheel for aarch64 is
     CPU-only.
   - **b. The `gpu` profile (recommended GPU path, untested).**
     - In `.env`: `EARMARK_ASR_BACKEND=remote`, `EARMARK_ASR_BASE_URL=http://asr:8000/v1`,
       `EARMARK_ASR_MODEL=openai/whisper-large-v3-turbo` (vLLM serves the same variable),
       and `EARMARK_ASR_API_KEY=<random>`. Optionally `ASR_GPU_MEMORY_UTILIZATION`; GPU
       memory is shared with any LLM.
     - Then `docker compose --profile gpu up -d`. It needs the NVIDIA Container Toolkit,
       which DGX OS ships.
     - The limits are raised to 1024 MB, 14400 s, and 4 GiB, so episodes up to about four
       hours decode. Set `EARMARK_ASR_MAX_UPLOAD_MB=1024` so a larger file fails before
       upload.
     - Until vLLM has loaded the model, the worker's first transcription fails
       transiently and is retried with backoff.
     - The image ships an ffmpeg build (2.3); M4A decoding is unverified.
     - The tag is pinned to `26.10-py3` by digest; bump it by hand.
     - For a server on another host, see `docs/asr.md`.
   - **c. faster-whisper on the GPU inside the worker (advanced, unverified outline).**
     - Build CTranslate2 4.9.0 (the locked version) from source in an
       `nvidia/cuda:13.x-cudnn-devel-ubuntu24.04` image, with `-DWITH_CUDA=ON
       -DWITH_CUDNN=ON -DWITH_MKL=OFF -DOPENMP_RUNTIME=COMP -DCMAKE_CUDA_ARCHITECTURES=121`.
     - Build its Python wheel for Python 3.13 with `CTRANSLATE2_ROOT` set.
     - Install it over the locked wheel with `uv pip install --no-deps --reinstall`.
     - Run on a CUDA 13 cuDNN runtime image with `LD_LIBRARY_PATH` set.
     - Run the venv's `earmark` directly, never `uv run`, which would re-sync the PyPI
       wheel.
     - Set `EARMARK_ASR_DEVICE=cuda`. The worker refuses to start if CTranslate2 sees no
       CUDA device. Verify with the Python one-liner in 5.6.
     - Upstream's CUDA 13 support issue is open (#1933). Nothing here was built.
8. **Analysis.**
   - Claude by default: `EARMARK_LLM_API_KEY`; `EARMARK_LLM_MODEL`, `EARMARK_LLM_EFFORT`.
   - A local server per `docs/llm.md`, including the warning that Ollama's
     OpenAI-compatible API truncates over-long prompts silently, so `OLLAMA_CONTEXT_LENGTH`
     must be at least `EARMARK_LLM_CONTEXT_TOKENS`.
   - A local LLM is not part of `compose.yaml`. Run it as its own service and point
     `EARMARK_LLM_BASE_URL` at it; from inside compose, use the host's address, not
     `localhost`.
9. **Search.**
   - The index extra (about 440 MB).
   - The embedding model (`BAAI/bge-small-en-v1.5`, 65 MB) downloads to
     `<DATA_DIR>/models/embed` (`/data/models/embed` in compose) on the worker's first
     index write, or with `earmark reindex`.
   - Until then `GET /search` answers 503 and `earmark search` exits 1.
   - The API never downloads models.
   - Changing `EARMARK_EMBED_MODEL` needs `earmark reindex`.
10. **Notifications (ntfy).**
    - **ntfy.sh.** Use a long random topic (the topic name is the password), or reserve
      the topic and set `EARMARK_NTFY_TOKEN` (`tk_…`). Subscribe to the topic in the ntfy
      iOS app.
    - **Self-hosted on the LAN.** Set `base-url` to the URL the phone uses (for example
      `https://ntfy.example.com` behind the same Caddy and grey-cloud pattern, or a tailnet
      name), and `upstream-base-url: "https://ntfy.sh"`, or iOS delivery can take hours.
      ntfy.sh then only relays a poll request with the message id; the phone fetches the
      message from your server, so it must reach it (home Wi-Fi, WireGuard, or the
      tailnet). Include its address in the tunnel's `AllowedIPs`.
    - `EARMARK_OBSIDIAN_VAULT_NAME` adds an `obsidian://` link that opens the note.
    - earmark redacts the topic URL, and topics of 12 or more characters on their own,
      from logs and notifications.
    - `earmark doctor --online` checks the server without publishing.
11. **Fetching and DNS filtering.**
    - Enclosure URLs usually start at a tracker (`dts.podtrac.com` and similar).
    - A Pi-hole-style resolver that answers `0.0.0.0` makes the download fail with a
      refused non-public address. The job error names the host and
      `EARMARK_ALLOW_PRIVATE_FETCH`.
    - Allow-list the host in the resolver (preferred); containers use the host's resolver.
      Or set `EARMARK_ALLOW_PRIVATE_FETCH=true`, which turns off the private-address guard
      for every feed and enclosure, so it is not recommended.
    - The remote ASR, local LLM, and ntfy servers do not need it.
12. **Stopping, restarts, and leases.**
    - SIGTERM (compose stop, systemctl stop) releases the running job back to the queue.
      The worker gets 2 minutes (compose) or 120 s (unit), enough to reach the next
      interruption point.
    - A SIGKILL or crash leaves the job `running` until `EARMARK_JOB_LEASE_SECONDS`
      (900 s); then it is requeued and the attempt counts.
13. **Checking the setup.**
    - `earmark doctor` and `earmark doctor --online` (under compose:
      `docker compose run --rm worker doctor --online`).
    - What exit codes 0, 1, and 2 mean.
14. **Backups and upgrades.**
    - Back up the vault and the data directory's `episodes/` (transcripts and analyses;
      `entities rebuild` and `reindex` need them). `lancedb/`, `models/`, and
      `huggingface/` can be recreated.
    - Upgrade with `git pull`, then `docker compose build`, then `docker compose up -d`
      (or `uv sync --locked …` and `systemctl restart`). Migrations run on start.
    - Run `earmark entities rebuild` after changing `EARMARK_TZ`.
15. **Troubleshooting.** A table:
    - 401 from the Shortcut: token.
    - "Server cannot be found": DNS rebinding protection on the resolver in use
      (section 6.1.6), or the name not yet in public DNS.
    - Connection timeout away from home: WireGuard not active (check on-demand rules and
      `AllowedIPs`), or Tailscale off.
    - A certificate error: Caddy has no certificate yet. Check `journalctl -u caddy` for
      the DNS-01 errors in 6.1.4.
    - Works on cellular, fails at home: the home resolver drops the record (6.1.6).
    - The worker restarting: `docker compose logs worker`, usually a `ConfigError` naming
      the setting (no LLM key, vault missing).
    - Permission denied in the vault: UID.
    - "refused non-public address": section 11.
    - `/search` 503: model not downloaded yet.

### 4.8 `docs/shortcut.md`

1. **Before you start.** You need three things:
   - the API reachable from the phone over HTTPS (deploy.md section 6: the private name
     with WireGuard, or Tailscale Serve), checked first with Safari on the phone opening
     `https://earmark.example.com/healthz`;
   - the token;
   - the Shortcuts app.
2. **Build the Shortcut.**
   - Name it "Earmark". In its details (ⓘ) turn on **Show in Share Sheet**.
   - The numbered list goes between `<!-- shortcut-actions:begin -->` and
     `<!-- shortcut-actions:end -->`. Each item starts with the action name in bold,
     exactly these six, in this order:
     1. **Receive input from Share Sheet**: the "Receive … input from Share Sheet" row
        at the top. Accept **URLs** and **Text**, so the shortcut appears whether
        Podcasts shares a link or text containing one. If there is no input: Stop and
        Respond. This accepts the shared URL.
     2. **Get URLs from Input**: input is Shortcut Input.
     3. **Get Item from List**: First Item, from URLs. A share can contain more than one
        URL; the API takes exactly one.
     4. **Ask for Input**: Text, prompt "Question for this episode (optional)". Leaving it
        blank gives the default analysis. This is the optional prompt; whether Shortcuts
        accepts an empty answer is unverified (2.6).
     5. **Get Contents of URL**: URL `https://earmark.example.com/ingest` (or
        `https://spark.tailnet-name.ts.net/ingest` with Tailscale Serve); under
        Show More: Method **POST**, Headers `Authorization` = `Bearer <your token>`,
        Request Body **JSON** with Text fields `url` = Item from List and `prompt` =
        Provided Input. This posts the JSON with the bearer header. `Authorization` is the
        only header to add. Shortcuts sends `Content-Type: application/json` for a JSON
        body. No header row may be left empty.
     6. **Show Result**: Contents of URL. This shows the response, which is the API's
        JSON (`job_id`, or `detail` on error).
   - Optional variations, outside the markers:
     - "Show Notification" instead of "Show Result";
     - adding `timestamp` (Format Date, ISO 8601) is not needed.
   - A warning: the token is stored in the Shortcut, so do not share the Shortcut;
     rotate `EARMARK_API_TOKEN` if you do.
3. **The request.**
   - The example body is a `json` fence directly after `<!-- ingest-request-example -->`:

     ```json
     {
       "url": "https://podcasts.apple.com/us/podcast/the-daily/id1200361736?i=1000802907981",
       "prompt": "Which studies are cited, and who ran them?"
     }
     ```

   - Then a `json` fence directly after `<!-- ingest-response-example -->`, the 202 body:

     ```json
     {"job_id": "3f0c9a6e2b8d4c1fa7e5d2b9c8a1f0e3", "status": "queued", "created": true}
     ```

   - Explain `created: false`: a repeat share while the first job is active returns the
     same job.
   - The `curl` equivalent for testing from a laptop (with `--form-string`-free
     `-d @body.json`).
4. **Errors the Shortcut can show.** These come from the API and are shown as is:
   - 401 (missing or wrong token);
   - 422 messages (not an Apple Podcasts link, a show link without an episode,
     `unknown field …`);
   - 413;
   - 503 (store busy, retry).

   Copy the exact texts from `src/earmark/resolve/apple.py` `MESSAGES` and
   `api/app.py::describe_validation_errors`, the same strings the tests use. Also:
   - "server cannot be found": the home resolver drops the private answer (deploy.md 6.1.6);
   - a connection timeout away from home: the WireGuard tunnel is not active (or
     Tailscale is off);
   - "The network connection was lost": an empty header row.
5. **After you share.**
   - The job runs in the background, and a notification arrives if ntfy is set up
     (deploy.md section 10).
   - `GET /jobs/{id}` with the same header shows progress.
   - A failure notification carries the reason.
   - `earmark doctor` for a broken setup.
6. **What the share sheet sends (unverified).** See 5.9 for the content and procedure.

### 4.9 `tests/fixtures/share_sheet.json`

```json
{
  "about": "Real Apple Podcasts share-sheet captures from an iPhone, added by hand. See docs/shortcut.md, 'What the share sheet sends'. Never add an invented entry.",
  "shares": []
}
```

Each entry, when one is added, has these keys:

```json
{
  "captured_on": "2026-12-01",
  "ios": "26.2",
  "share_option": "From Start",
  "shortcut_input": "<exactly what Show Result displayed for Shortcut Input>",
  "url": "<what Get URLs from Input + Get Item from List produced>",
  "canonical": "https://podcasts.apple.com/<cc>/podcast/id<show>?i=<episode>"
}
```

### 4.10 `Settings.secret_values()`

```python
NTFY_TOPIC_SECRET_MIN_CHARS: Final = 12


def secret_values(self) -> list[str]:
    """Every set secret, plus the bare ntfy topic when it is at least 12 characters."""
```

The list order stays the field order, with the topic appended last. Topic extraction:

- the validated `NTFY_URL` (already without a trailing `/`);
- take `urlsplit(url).path.rpartition("/")[2]`;
- append it only if `len(topic) >= NTFY_TOPIC_SECRET_MIN_CHARS` and it is not already in
  the list.

`config.py` must not import `notify.ntfy`, which imports httpx2.

### 4.11 `tests/doc_helpers.py`

```python
ROOT: Final[Path]


def read_doc(relative: str) -> str: ...


def between_markers(text: str, name: str) -> str:
    """Text between '<!-- {name}:begin -->' and '<!-- {name}:end -->'.

    AssertionError unless each marker occurs exactly once, begin before end.
    """


def after_marker(text: str, name: str) -> str:
    """Text after the single '<!-- {name} -->' marker; AssertionError otherwise."""


@dataclass(frozen=True)
class Fence:
    info: str  # the info string, stripped ("json", "bash", "")
    body: str  # the lines between the fences, without the common indentation
    line: int  # 1-based line of the opening fence


def fences(text: str) -> list[Fence]:
    """Fenced code blocks (``` or ~~~, length >= 3, optionally indented, as inside list
    items); a block closes at a line of the same character, at least as long."""


def first_fence(text: str, info: str) -> Fence:
    """The first fence in text; AssertionError unless its info string's first word is
    info."""


def table_variables(text: str) -> list[str]:
    """Every `EARMARK_*` name in a table row's first cell, in order, from rows shaped
    '| `EARMARK_X` | <default> | <meaning> |'. Header and separator rows are skipped.

    AssertionError for a row whose first cell holds a backticked EARMARK_ name plus
    anything else (e.g. two variables in one row), or whose other cells are empty.
    """


def relative_links(text: str) -> list[str]:
    """Targets of inline Markdown links and images that are not URLs (no scheme), not
    mailto:, and not pure '#anchors', with '#…' stripped. Links inside fences are
    ignored."""
```

Use `re` only; no Markdown library.

## 5. Behaviour and edge cases

### 5.1 One image, extras by build argument

- The default `EXTRAS="transcribe index"` matches the default settings
  (`ASR_BACKEND=faster_whisper`, `INDEX_BACKEND=lancedb`), so an unmodified `.env` gives a
  worker that starts, given a vault and an LLM key.
- An unknown extra name fails `uv sync` at build time, which is acceptable.
- The extras are installed through `--extra` flags built by a shell loop.
- `EXTRAS=""` gives a core-only image.

### 5.2 Ownership

- `/data` and `/vault` are created in the image, owned by `earmark:earmark` (UID/GID from
  build arguments, default 1000:1000, with 0 refused at build time).
- An empty named volume mounted at `/data` is initialised from the image directory,
  ownership included.
- The vault is a bind mount, so its host ownership must allow `APP_UID` to write. The doc
  tells the user to set `APP_UID`/`APP_GID` to the vault owner's ids before building.
- Files earmark creates in the vault are owned by that UID on the host, so they belong to
  the host user.
- `--non-unique` lets a GID that already exists in the image (`users`, 100) still work.

### 5.3 The environment inside the containers

- `env_file: .env` passes every line. `environment:` wins over `env_file`, which is
  Compose's documented precedence.
- The forced values are `EARMARK_DATA_DIR`, `EARMARK_VAULT_DIR`, `EARMARK_HOST`,
  `EARMARK_PORT`, and empty `EARMARK_ASR_MODEL_DIR`/`EARMARK_EMBED_MODEL_DIR`. Empty means
  unset (`env_ignore_empty`), so the defaults on the volume apply.
- `EARMARK_TZ` is passed through. Without it, note dates use the container's zone, UTC.
  deploy.md says to set it.
- The image also sets `EARMARK_DATA_DIR`, `VAULT_DIR`, and `HOST`, so a plain
  `docker run` is consistent too.

### 5.4 Ports and exposure

- **Compose.** The default is `127.0.0.1:8787:8787`. Docker's published port on the host's
  loopback is reachable by host processes, so Caddy (or `tailscale serve`) on the host
  proxies to `127.0.0.1:8787`.
- **Native.** The unit leaves `EARMARK_HOST` at its default, `127.0.0.1`.
- **Where `0.0.0.0` appears.** It is the in-container `EARMARK_HOST`, and only that. No
  file in this step binds the API, or anything else, to `0.0.0.0` on the host.
  - `API_BIND` is documented as "leave at the default".
  - The Caddyfile binds the LAN address, and only port 443.
- **The other services.** The worker publishes nothing, and neither does `asr`.

### 5.5 Signals and stopping

- `init: true` puts docker-init at PID 1, which forwards SIGTERM to `earmark`.
- The worker's handler releases the job (step 05).
- `stop_grace_period: 2m` (worker) and the default 10 s (API) apply.
- The units use `KillSignal=SIGTERM` with `TimeoutStopSec` of 120 (worker) and 30 (API).

### 5.6 The one Python snippet in deploy.md

Option c's check, ruff-clean:

```python
import ctranslate2

print(ctranslate2.get_cuda_device_count(), ctranslate2.get_supported_compute_types("cuda"))
```

Keep it on lines of at most 100 characters (the project's line length). `make check`
formats it.

### 5.7 The settings reference test

- It compares the set of `EARMARK_*` names in the README's settings block with
  `{env_var(f) for f in Settings.model_fields}`, and fails on duplicates, missing
  entries, or extra entries. The failure message lists the missing and extra names.
- Names in backticks outside table first cells (prose, the meaning column) are ignored,
  so the meaning text can mention other variables.
- A row with two variables (`` `EARMARK_HOST`, `EARMARK_PORT` ``) is rejected by
  `table_variables`, so a grouped row cannot satisfy the test.

### 5.8 The Shortcut body tests

- **Extraction.** The body is the first fence after `<!-- ingest-request-example -->`, and
  its info string must be `json`.
- **Validation.**
  - `IngestRequest.model_validate_json(body)` succeeds.
  - The JSON keys are exactly `{"url", "prompt"}`, the two fields the Shortcut builds.
  - The parsed `prompt` is not `None`.
  - `parse_apple_url(url)` succeeds.
- **Through the route.** Posting the same body with `TestClient(create_app(...))` and the
  test token returns 202. The response's key set equals the keys of the
  `<!-- ingest-response-example -->` fence's JSON, and the job is `queued` in the store
  with the canonical URL.
- So a schema change (a renamed field, a new required field) breaks the doc test.

### 5.9 The share sheet: how the doc handles what cannot be checked

docs/shortcut.md section 6 says, in substance:

- **Not captured yet.** The earmark build had no iPhone, so the exact input the Podcasts
  share sheet gives a Shortcut has not been captured. These forms come from Apple's lookup
  API and episode pages, and earmark accepts all of them:
  - `https://podcasts.apple.com/<cc>/podcast/<slug>/id<show>?i=<episode>`, with or
    without the slug and extra parameters such as `uo=4` or `&r=<seconds>`;
  - the legacy `itunes.apple.com` host.
- **Not accepted.** A show link without `?i=` is refused with a message saying so.
  `music.apple.com` and `embed.podcasts.apple.com` are not accepted.
- **Capturing a share.**
  1. Temporarily add **Show Result** (Shortcut Input) as the first action.
  2. Share an episode from Podcasts with "From Start", then again with "From [time]".
  3. Record the text shown, and the URL step 3 of the Shortcut produces.
- **Adding it as a test case.**
  1. Add an entry to `tests/fixtures/share_sheet.json` with the keys from 4.9, copied
     exactly.
  2. Run `uv run pytest tests/test_docs.py -k share_sheet`.
  3. If it fails, the shape is new: report it, or extend `parse_apple_url` with a test.
- Never add an entry that was not captured on a phone.

The test `test_recorded_share_sheet_captures` checks every entry with
`check_share_capture(entry) -> list[str]` (a helper in `test_docs.py` that returns
problems):

- the required keys are present and are strings;
- `parse_apple_url(entry["url"]).url == entry["canonical"]`;
- `entry["url"]` occurs in `entry["shortcut_input"]` (the Shortcut extracted it from the
  input);
- the entry posts to `/ingest` with 202.

With the empty list it checks nothing, so two more tests make the checker itself
falsifiable (section 6, S3 and S4). The fixture's `about` text is asserted to say that
entries come from a phone.

### 5.10 Bare-topic redaction

**Rule.** `secret_values()` adds the topic when it has at least 12 characters.

- Shorter topics are not redacted on their own; the full topic URL always is.
- A topic under 12 characters protects little on a public server, because it is
  guessable. It is not secret on a self-hosted server with access control.
- Masking a short common word (`earmark`, `podcasts`) everywhere would corrupt logs,
  `job.error`, notification text, and paths (`Earmark/…`).
- `doctor_secrets` keeps adding the topic at any length. Doctor output is short and
  one-off, so over-redaction is harmless there.

**Effects.** Every caller of `secret_values()` gains the topic: log formatters (`cli.py`),
`Pipeline` (`describe_error` for job error and warning text), and `NtfyOptions.secrets`
(`compose`'s title, message, and `Reason:` line).

**Docs.** deploy.md and the `.env.example` comment say: pick a long random topic, at least
12 characters; earmark masks it in logs and notifications.

**Dead end.** Do not redact by word boundary. `earmark.pipeline` still matches, because
`.` is a boundary.

### 5.11 Workflow rules

The new workflow follows `tests/test_ci.py`:

- it passes actionlint;
- every `uses:` is pinned to a full SHA with a `# vX.Y.Z` comment;
- it has a top-level `permissions` block;
- it has no `pull_request_target` and no `secrets`.

`docker build` without `--push` and without a registry login cannot push. Nothing in the
job has write permission.

### 5.13 The reverse proxy is documented, not a compose service

`deploy/caddy/Caddyfile` is an example file and deploy.md documents installing Caddy on
the host. There is no `proxy` service in `compose.yaml`, because:

- **The token would leak into earmark's containers.** Compose's `env_file: .env` exports
  every line into the earmark containers (5.3). A Cloudflare token in the same `.env`
  would land in the API and worker environments, and the user's rule is that the token
  lives only with the proxy. A separate env file would work, but it is easy to get wrong.
- **The proxy is host infrastructure.** It owns port 443 on the LAN address and often
  fronts other services (ntfy, for one). It has to bind a host address, so in Docker it
  would need `network_mode: host`, which removes most of the isolation a compose service
  would bring.
- **A stock image is not enough.** The `caddy-dns/cloudflare` module needs a custom build,
  which would be a second untested image in CI.
- **It serves both installs.** One proxy setup works the same for compose and for systemd.

The cost is one more thing to install by hand, which deploy.md walks through.

### 5.12 Links

`README.md`, `docs/deploy.md`, and `docs/shortcut.md` use relative links to repo files
(`docs/asr.md`, `../compose.yaml`, `../deploy/systemd/earmark-worker.service`). Every one
must resolve to an existing path relative to the linking file.

## 6. Tests

The default suite only: no network or Docker. All new tests are cheap.

### Brief acceptance criteria

| # | Criterion | Proof |
|---|---|---|
| A1 | The README quickstart runs verbatim in the sandbox with fake backends | The **verifier executes it** (section 7). Supporting tests in `tests/test_docs.py`: `test_quickstart_block_present` (one `bash` fence between the quickstart markers); `test_quickstart_uses_fake_backends` (every `Settings` field whose name ends in `_backend`, plus `llm_provider`, is set to `fake` in the block, so a new backend setting cannot silently make the quickstart real); `test_quickstart_ingest_body_valid` (the `-d '…'` JSON validates as `IngestRequest` and its URL passes `parse_apple_url`); `test_quickstart_runs_no_network_command` (no `git clone`, `--extra`, or non-loopback URL other than the Apple episode URL inside the JSON). |
| A2 | The example request body in docs/shortcut.md validates against `IngestRequest`, extracted from the doc | `test_shortcut_request_example_validates` and `test_shortcut_request_example_accepted_by_api` (5.8); `test_shortcut_response_example_matches_api` |
| A3 | docs/shortcut.md lists each action in order: accept URLs from the share sheet, ask for an optional prompt, POST JSON with the bearer header, show the response | `test_shortcut_actions_in_order`: the bold names between the action markers equal exactly `["Receive input from Share Sheet", "Get URLs from Input", "Get Item from List", "Ask for Input", "Get Contents of URL", "Show Result"]`. `test_shortcut_post_action_details`: the "Get Contents of URL" item mentions `POST`, `Authorization`, `Bearer`, `JSON`, `url`, and `prompt`, and the "Ask for Input" item says the prompt is optional. |
| A4 | The README's configuration reference covers every `Settings` field, enforced by a test | `test_readme_settings_reference_covers_every_field` (5.7); `test_readme_settings_reference_marks_secrets` (rows of the five `SecretStr` fields say "secret" and have default `unset`) |
| A5 | CI builds the image on an arm64 runner without pushing; static review; deploy.md says build-tested but not run-tested, and the GPU profile is untested | `tests/test_ci.py`: `test_container_workflow_triggers` (push main/impl, pull_request), `test_container_workflow_job` (arm64 runner, timeout int 1–30, steps `[checkout, run, run, run]`, checkout pin and `persist-credentials: false` equal to CI's), `test_container_workflow_builds_without_push` (a `docker build` command present; no `push`, `login`, `--push`, `buildx … --push`, or registry name in any `run`), `test_container_workflow_checks_image` (runs `--version`, imports `ctranslate2`, `faster_whisper`, `fastembed`, `lancedb`, and checks `id -u` against 1000), `test_container_workflow_validates_compose`, `test_container_workflow_read_only` (`permissions == {"contents": "read"}`, no job permissions, no `env`), `test_container_workflow_concurrency`. The parametrized actionlint, pinning, permissions, and `pull_request_target` tests pick the new file up automatically. Docs: `test_deploy_doc_states_test_status`: deploy.md's text before its second `## ` heading, lower-cased with backticks removed, contains "build-tested in ci", "not run-tested", and "gpu profile is untested". The verifier also reviews the files statically. |

### Criteria added by this plan

| # | Criterion | Proof |
|---|---|---|
| D1 | Base images are pinned by digest; Python matches `.python-version`; uv matches CI's and `uv_build`'s minor | `tests/test_deploy.py`: `test_dockerfile_images_pinned_by_digest` (every `FROM` that is not a stage name matches `^[^\s@]+:[^\s@]+@sha256:[0-9a-f]{64}$`); `test_dockerfile_python_matches_project` (the python tag starts with `.python-version` + `.`, and both Python `FROM`s are identical); `test_dockerfile_uv_matches_build_backend` (the uv tag's `major.minor` equals the `uv_build` bound's in `pyproject.toml` and the `version:` range in `ci.yml`'s setup-uv step) |
| D2 | The image installs locked dependencies only, without dev tools, and the default extras exist | `test_dockerfile_uv_sync_locked_no_dev` (every `uv sync` has `--locked` and `--no-dev`; the last one has `--no-editable`); `test_dockerfile_default_extras_exist` (the words of `ARG EXTRAS=` are keys of `[project.optional-dependencies]`) |
| D3 | The image runs as non-root and starts `earmark` | `test_dockerfile_runtime_user_not_root` (the last stage is `runtime`; its last `USER` is `earmark…`, never `root` or `0`; the `test "$UID" -ne 0` guard is present); `test_dockerfile_entrypoint` (`ENTRYPOINT ["earmark"]`, `CMD ["serve"]`); `test_dockerfile_no_volume_or_healthcheck` |
| D4 | Nothing secret or local reaches the build context | `test_dockerignore_is_allow_list` (first rule `*`; the re-included set equals `{pyproject.toml, uv.lock, .python-version, README.md, src/}`); `test_dockerfile_copies_only_allowed_paths` (every `COPY` without `--from` copies from that set) |
| D5 | Compose wires the API and worker as described | `test_compose_services` (`{api, worker, asr}`; `api.command == ["serve"]`, `worker.command == ["worker"]`; same `image` and `build`); `test_compose_api_publishes_loopback_by_default` (one port, `${API_BIND:-127.0.0.1:8787}:8787`); `test_compose_worker_and_asr_publish_nothing`; `test_compose_shared_data_and_vault` (both have `data:/data` and a `${VAULT_PATH:?…}:/vault` bind); `test_compose_forces_container_paths` (environment values as in 4.3); `test_compose_worker_stop_grace` (≥ 60 s); `test_compose_hardening` (`no-new-privileges:true`, `cap_drop: [ALL]`, `init: true`, no `privileged`, `network_mode`, `pid`, or `ipc: host`) |
| D6 | The `gpu` profile is isolated and pinned | `test_compose_gpu_profile` (`asr.profiles == ["gpu"]`; image pinned by digest; an nvidia GPU device reservation; `VLLM_API_KEY` from `${EARMARK_ASR_API_KEY:-}` (`:-`, not `:?`); the three `VLLM_MAX_AUDIO_*` raised above their defaults; no other service has `profiles`) |
| D7 | Compose and docs agree on variables | `test_compose_earmark_variables_are_settings` (every `EARMARK_*` key in any `environment:` and every `${EARMARK_…}` interpolation is `env_var(field)` of a real field); `test_compose_only_variables_documented` (every unprefixed `${NAME…}` in `compose.yaml` appears in a table row of docs/deploy.md and as a commented `# NAME=` line in `.env.example`) |
| D8 | The units are coherent | `test_systemd_units`: for both files, `ExecStart` ends in ` serve` / ` worker` and starts with `/srv/earmark/.venv/bin/earmark`; `User=earmark`; `KillSignal=SIGTERM`; `Restart=on-failure`; `NoNewPrivileges=yes`; `ProtectSystem=strict`; `ReadWritePaths` includes `/srv/earmark/data`; `TimeoutStopSec` worker ≥ 60; `WorkingDirectory=/srv/earmark`. Parse with a small line parser (`key=value` under `[Section]`, repeated keys collected into lists, `#` comments skipped), not `configparser`, which rejects repeated keys |
| D9 | The bare ntfy topic is redacted wherever `secret_values()` is used | `tests/test_config.py`: `test_secret_values_include_bare_ntfy_topic` (`https://ntfy.sh/sentinel-topic-5` gives both the URL and `sentinel-topic-5`, each once, the topic last); `test_secret_values_skip_short_ntfy_topic` (`https://ntfy.example/earmark` gives only the URL); `test_secret_values_topic_with_path_prefix` (`https://host/ntfy/sentinel-topic-77` gives `sentinel-topic-77`). `tests/test_ntfy.py`: `test_compose_reason_redacts_bare_topic_from_settings` (Settings with `NTFY_URL=https://ntfy.test/sentinel-topic-404x`, a failed job with `error="HTTP 403 for sentinel-topic-404x"`, composed with `NtfyOptions.from_settings(settings).secrets`; the message contains `Reason: HTTP 403 for **********` and not the topic). `tests/test_runner.py`: `test_describe_error_redacts_bare_ntfy_topic` (`describe_error(PermanentError("bad topic sentinel-topic-404x"), settings.secret_values())` has no topic). Each would fail if `secret_values()` lost the topic. |
| D10 | Docs link only to files that exist | `test_doc_relative_links_resolve` over README.md, docs/deploy.md, docs/shortcut.md |
| D11 | Share-sheet captures, once added, are checked; none are invented | `test_recorded_share_sheet_captures` (S1: every entry passes `check_share_capture`); `test_share_sheet_fixture_shape` (S2: keys `about` and `shares`, `shares` a list, `about` mentions a phone and "never"); `test_share_capture_checker_accepts_known_shape` (S3: a synthetic entry, built in the test from the step 04 slugged URL with `uo=4` inside a title-plus-URL text, labelled synthetic in a comment and never written to the fixture, has no problems); `test_share_capture_checker_reports_problems` (S4: a wrong `canonical`, a `url` not in `shortcut_input`, a missing key, and a show link without `?i=` each produce a problem) |
| D12 | Dependabot watches the Dockerfile's images, patch and digest only | `tests/test_ci.py::test_dependabot_config` now expects `Counter(["uv", "github-actions", "docker"])` and the same directory, branch, and schedule rules; `test_dependabot_docker_ignores_minor_and_major` |
| D14 | The example Caddyfile terminates TLS on a private address only, uses DNS-01 through Cloudflare, forwards to the loopback API, and holds no token | `tests/test_deploy.py::test_caddyfile_example` (plain text checks, no Caddy needed):<br>- exactly one site block, whose address is a hostname with no scheme or port;<br>- a `bind` line whose address is an IPv4 address in a private range (`ipaddress.ip_address(x).is_private`) and is not `0.0.0.0`;<br>- `dns cloudflare {env.CF_API_TOKEN}` inside a `tls` block;<br>- `reverse_proxy 127.0.0.1:8787`, matching the default `API_BIND` port in `compose.yaml`;<br>- `auto_https disable_redirects`;<br>- no `http://` site address.<br>`test_proxy_token_stays_out_of_earmark`: no `CF_` or `CLOUDFLARE` token appears in `.env.example`, `compose.yaml`, the `Dockerfile`, `deploy/systemd/*.service`, or any file under `src/earmark/`. |
| D15 | Reachability docs match the user's design: no tunnel, no Access headers, nothing public | `tests/test_docs.py::test_no_cloudflare_tunnel_or_access_headers`: `CF-Access`, `cloudflared`, and "Cloudflare Tunnel" (case-insensitive; a WireGuard "split tunnel" is fine) appear in none of README.md, docs/deploy.md, docs/shortcut.md. `test_shortcut_sends_only_bearer_header`: the "Get Contents of URL" item names `Authorization` as its one header. `test_deploy_doc_reachability`: deploy.md mentions "DNS only", `AllowedIPs`, `rebind-domain-ok`, `private-domain`, `Zone:Read` or "Zone → Zone → Read", and "never" next to `tailscale funnel`. These are coarse phrase checks, so the verifier still reads section 6. |
| D13 | The doc helpers are correct | `tests/test_doc_helpers.py`: missing or duplicate markers raise; fences with `~~~`, longer closing fences, indented fences in list items, a fence-looking line inside another fence; a two-variable table row raises; links inside fences are ignored; `#anchor` and `https://` links are skipped |

### Fixtures

- `tests/fixtures/share_sheet.json` (4.9), committed with `"shares": []`.
- No other new fixture. The tests read the real docs and deployment files.
- The API tests reuse the pattern from `tests/test_api.py`: `Settings(_env_file=None,
  api_token=SecretStr(TOKEN), data_dir=tmp_path / "data")`, a `JobStore` over
  `open_database`, and `TestClient(create_app(settings, store=store), headers=AUTH)`.
  Import those pieces; do not import fixtures from another test module.

## 7. Verification

```bash
cd /home/sandy/dev/earmark
uv sync
make check
uv run pytest -q tests/test_docs.py tests/test_doc_helpers.py tests/test_deploy.py \
  tests/test_ci.py tests/test_config.py tests/test_ntfy.py tests/test_runner.py
uv run ruff format --check README.md docs/
```

**The README quickstart, verbatim.** From the repository root, copy the lines between the
`bash` fence inside the quickstart markers into a script and run it with `bash`. Do not
retype them.

```bash
S=<scratchpad>
awk '/<!-- quickstart:begin -->/{f=1;next} /<!-- quickstart:end -->/{f=0} f' README.md \
  | sed '1d;$d' > "$S/quickstart.sh"     # drop the ```bash and ``` lines
cat "$S/quickstart.sh"                    # check it is the block, unchanged
bash "$S/quickstart.sh"
```

Expect, in order:

1. `earmark 0.1.0`;
2. a doctor report ending `doctor: all checks passed, 8 warnings`;
3. `{"status":"ok"}`;
4. `{"job_id":"…","status":"queued","created":true}`;
5. `job … done`;
6. one `done` line from `earmark jobs`;
7. the fake note's Markdown;
8. the server's shutdown log.

Then check that nothing is left behind:

- `pgrep -af "earmark serve"` shows no leftover server;
- `git status --short` shows no files from the quickstart.

Port 8787 must be free first.

**Static review.** There is no Docker, so read these and check them against sections 4.1
to 4.5:

- `Dockerfile`, `.dockerignore`, `compose.yaml`;
- `deploy/caddy/Caddyfile`, which must be byte-for-byte section 4.4a apart from comments;
- docs/deploy.md section 6 and docs/shortcut.md against the user's reachability design
  (deviation 3);
- `deploy/systemd/*.service`;
- `.github/workflows/container.yml`.

Also check the digests against section 2.2. They can be checked live with the registry
`HEAD` requests from 2.2 if network is available, for example:

```bash
T=$(curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull" | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')
curl -sI -H "Authorization: Bearer $T" -H "Accept: application/vnd.oci.image.index.v1+json" \
  https://registry-1.docker.io/v2/library/python/manifests/3.13.18-slim-trixie | grep -i docker-content-digest
```

**The Caddyfile, optional, needs network.** Download a Caddy build with the module into an
empty scratch directory and validate the committed example. The token is a dummy of a
plausible shape, and no certificate is requested.

```bash
mkdir -p "$S/caddy-bin" "$S/caddy-home"
curl -sSL -o "$S/caddy-bin/caddy" \
  "https://caddyserver.com/api/download?os=linux&arch=arm64&p=github.com/caddy-dns/cloudflare"
chmod +x "$S/caddy-bin/caddy"
"$S/caddy-bin/caddy" list-modules | grep dns.providers.cloudflare
CF_API_TOKEN=$(python3 -c 'print("A1b2" * 10)') HOME="$S/caddy-home" \
  XDG_DATA_HOME="$S/caddy-home" XDG_CONFIG_HOME="$S/caddy-home" \
  "$S/caddy-bin/caddy" validate --config deploy/caddy/Caddyfile --adapter caddyfile
# Valid configuration
"$S/caddy-bin/caddy" fmt deploy/caddy/Caddyfile | diff - deploy/caddy/Caddyfile && echo fmt-clean
```

**Redaction smoke.**

```bash
EARMARK_NTFY_URL=https://ntfy.sh/sentinel-topic-404x uv run python -c \
  'from earmark.config import load_settings; print(load_settings(env_file=None).secret_values())'
# both the URL and 'sentinel-topic-404x'
EARMARK_NTFY_URL=https://ntfy.sh/earmark uv run python -c \
  'from earmark.config import load_settings; print(load_settings(env_file=None).secret_values())'
# only the URL
```

**Cannot be verified here:**

- building the image, which CI does on the first push;
- running any container;
- the `gpu` profile and vLLM;
- the CTranslate2 CUDA outline;
- the systemd units under systemd;
- the WireGuard split tunnel and on-demand rules;
- the DNS-only record and resolver rebinding behaviour;
- Caddy obtaining a certificate through DNS-01, and Tailscale Serve;
- a self-hosted ntfy with `upstream-base-url`;
- the Shortcut on an iPhone and what the share sheet sends.

## 8. Deviations

1. **The `gpu` profile runs the ASR server, not a GPU worker.** The brief says "GPU profile
   for the worker". Instead, the worker talks to a vLLM Whisper service in the `gpu`
   profile through the `remote` backend, and the in-image CUDA CTranslate2 build is
   documented as an unverified outline (2.3). The aarch64 CTranslate2 wheel is CPU-only, a
   CUDA 13 source build is upstream-unsupported (#1933 open), and it could not be built in
   CI or here. vLLM in NVIDIA's arm64 container is step 09's recommended GPU path.
   **Accepted by the user on 2026-11-20.**
2. **CI runs one-off commands in the built image.** The brief says the image is
   build-tested, not run-tested. CI also runs `--version`, an extras import, and `id -u`
   inside it, which catches a broken entrypoint, a missing extra, or a root user. It still
   never runs the API or the worker, and deploy.md says exactly that.
3. **Reachability follows the user's design, not the brief's "Tailscale first, Cloudflare
   Tunnel as the alternative".** Decided by the user on 2026-11-20.
   - **The primary path.** A DNS-only Cloudflare `A` record pointing at a private address,
     a WireGuard split tunnel with on-demand activation, Caddy on the LAN address
     terminating TLS for `127.0.0.1:8787`, and a Let's Encrypt certificate through DNS-01.
   - **Alternative and removals.** Tailscale Serve is the alternative. Cloudflare Tunnel,
     Access, and the `CF-Access-*` headers are dropped. The API is never exposed publicly,
     and the ARCHITECTURE rule stays absolute.
   - **Choices within it.**
     - Caddy over Traefik (2.4).
     - The proxy is an example file and documentation, not a compose service (5.13).
     - The Cloudflare token needs Zone:Zone:Read in addition to the brief's
       Zone:DNS:Edit, both limited to the one zone. Caddy's module and lego both look up
       the zone id (2.4).
4. **No local LLM service in `compose.yaml`.** Step 11 deferred "running a local LLM server
   in the container or compose file" to this step. The choice of model, context, and GPU
   memory is the operator's, and `docs/llm.md` already covers the servers. deploy.md
   explains how to point earmark at one.
5. **Bare-topic redaction has a 12-character minimum** (5.10). The verifier's note asked
   for the topic in the redaction set. Shorter topics are not redacted on their own,
   because masking a common word would corrupt logs and messages. `doctor_secrets` is
   unchanged.
6. **Dependabot gains the `docker` ecosystem**, limited to patch and digest updates, and
   `test_dependabot_config` changes accordingly.
7. **Shortcut action list.** Six actions, not four. "Get URLs from Input" and "Get Item
   from List" are added between the share-sheet input and the prompt, so a text share or
   several URLs still give the API exactly one URL (step 04 §5.13 rejects whitespace
   inside the URL). The brief's four appear in the brief's order.
8. **ARCHITECTURE changes** (already made by this plan):
   - the Reachability decision row: the rule "never exposed publicly" is kept absolute,
     and the row describes the private DNS name, WireGuard, and Caddy path, plus Tailscale
     Serve;
   - a new "Deployment" section (image, compose, units, the `gpu` profile, CI);
   - the module layout's note on top-level deployment files;
   - the Security "Secrets" bullet (the bare topic);
   - the Testing section (the container workflow, Dependabot `docker`, the doc-driven
     tests);
   - Known risks 1 and 4 updated.

## 9. Out of scope

- Publishing the image to a registry, multi-arch builds, and signing or SBOMs.
- Building or testing the `gpu` profile, and a CUDA CTranslate2 image target.
- A local LLM service in compose.
- Running the services in CI (a compose up with fakes would be a natural follow-up).
- Making `container.yml` or `index.yml` a required check.
- Reanalyze (step 17) and digests, including their scheduling notes in deploy.md
  (step 18).
- A Shortcut that polls `GET /jobs/{id}`, and a status page (backlog).
- Any change to `IngestRequest`, `parse_apple_url`, or the API.
- Recording a real share-sheet capture, which needs an iPhone.
- Setting up the WireGuard server, a reverse-proxy compose service or image, and a
  Traefik example file.
