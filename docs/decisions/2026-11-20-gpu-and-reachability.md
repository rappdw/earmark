# Decisions, 2026-11-20: GPU placement and API reachability

**Decision 1 superseded on 2026-11-23** by step 26: the worker now runs a GPU image with
CTranslate2 built for `sm_121` (`docs/decisions/2026-11-23-gpu-build-recipe.md`,
`docs/steps/26-gpu-image.md`), and the vLLM `asr` service and `gpu` profile are gone.
Decision 2 stands.

These were decided during step 16. The details are in `docs/steps/16-deploy-shortcut.md`
and `docs/deploy.md`.

| # | Decision | Choice | Deciding reason |
|---|----------|--------|-----------------|
| 1 | Which service gets the GPU | The compose `gpu` profile adds a vLLM Whisper `asr` service. The worker calls it through the `remote` ASR backend, and the earmark image stays CPU-only. | It is the only GPU path likely to work on the DGX Spark with today's CUDA 13 support, and it reuses the step 09 client, which is already tested. |
| 2 | How the phone reaches the API | A DNS-only Cloudflare record points to a private IP. Phones and Macs reach it over a WireGuard split tunnel with on-demand activation. A LAN reverse proxy terminates TLS with a DNS-01 certificate. Tailscale Serve is the alternative, and Cloudflare Tunnel is dropped. | The API stays off the public internet, the "never exposed publicly" rule stays absolute, and this is the setup the operator actually runs. |

## 1. GPU placement

**Problem.** The PyPI CTranslate2 wheel for aarch64 is CPU-only. Steps 08 and 16 both
found `get_cuda_device_count() == 0`. The Spark's GB10 needs CUDA 13, and upstream issue
OpenNMT/CTranslate2#1933 ("CUDA 13 support") is still open. So the worker's
faster-whisper backend cannot use the GPU with published wheels.

**Options considered.**

- **A. A vLLM ASR service gets the GPU (chosen).** It runs NVIDIA's arm64 CUDA 13
  container, `nvcr.io/nvidia/vllm`, pinned by digest, with raised audio limits. It is
  reached at `http://asr:8000/v1` with `EARMARK_ASR_BACKEND=remote`.
- **B. The worker gets the GPU through an in-image CTranslate2 built with CUDA.** This
  means a source build against unsupported upstream code, on a CUDA base image several
  GB in size. CI cannot build it, and the locked wheel would have to be replaced after
  `uv sync`.
- **C. No `gpu` profile.** This drops a brief criterion and leaves the step 09 work
  unused in the deployment.

**Caveat.** The `gpu` profile has never been built or run. It stays untested until it
runs on the Spark. The in-image CUDA build remains in `docs/deploy.md` only as an
unverified outline.

**Would revisit if** CTranslate2 ships CUDA 13 aarch64 wheels, which would make B cheap,
or if a local LLM needs all of the GPU memory.

## 2. Reachability

**Options considered.**

- **A. Cloudflare Tunnel, gated by Access (Service Auth) and the bearer token.** This
  puts the API on a public edge, and Cloudflare sees all the traffic.
- **B. Tailscale Serve only.**
- **C. A DNS-only Cloudflare record to a private IP, reached over WireGuard (chosen,
  proposed by the operator).**

**How C works.**

- An `A` record, for example `earmark.example.com`, points to the server's LAN or
  WireGuard IP. It must be **grey-cloud (DNS-only)**.
- WireGuard peers on the phone and the Mac use `AllowedIPs` limited to that address,
  which makes a split tunnel. They connect on demand on cellular and on non-home Wi-Fi.
  At home, the LAN routes directly.
- A reverse proxy on the LAN interface terminates TLS and forwards to earmark on
  `127.0.0.1:8787`.
- The certificate comes from Let's Encrypt through a DNS-01 challenge. That uses a
  Cloudflare API token with `Zone:Zone:Read` and `Zone:DNS:Edit`, both limited to one
  zone. Edit alone fails, because the DNS providers look up the zone id first. The
  token is held by the proxy (Caddy, in `/etc/caddy/cloudflare.env`), never by
  earmark.
- Nothing is reachable from the internet.

**Gotchas to document.**

- **DNS-rebinding protection.** Pi-hole, dnsmasq (`stop-dns-rebind`), Unifi, OPNsense
  and many routers drop public answers that point to private IPs, so the name silently
  fails to resolve. Allow-list the domain, for example
  `rebind-domain-ok=/example.com/`.
- **Public record.** The record reveals the internal IP to anyone who looks. This is a
  minor information leak.

**Consequence.** The architecture's rule that the API is never exposed publicly stays
absolute. The Shortcut sends only the bearer header.

**Would revisit if** the docs need to serve users who have neither WireGuard nor
Tailscale. In that case add option A back as a clearly less private section.
