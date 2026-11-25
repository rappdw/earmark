# Step 35: Your own certificate

The brief is in `docs/PLAN.md`, "Step 35". It depends on step 34 only for ordering. It
touches no service code. It builds on step 16 (the Caddy example, `docs/deploy.md` section
6, `tests/test_deploy.py`'s Caddy checks) and on step 27/28 (the runbook and its tests in
`tests/test_docs.py`).

The step can be built as briefed. Two choices change what the operator sees, so they are
stated first (section 8 has the reasoning):

- **Decision P1 (product): the script gets stricter.** It now refuses three things that the
  version used on the Spark accepted. A chain file that also holds a private key would have
  been installed world-readable as `fullchain.pem`. A chain out of order, or the leaf
  repeated, passed the "two or more certificates" count. A certificate with no Subject
  Alternative Name made the script stop with no message. A correctly ordered commercial
  chain, the case the operator used, still passes, with or without the root at the end.
- **Decision P2 (product): a reload that would keep the old certificate gets a warning, not
  an automatic restart.** The packaged `caddy.service` reloads with
  `caddy reload --force`, which reads replaced certificate files. A plain `caddy reload`
  does not (both checked, section 2.4). If the unit's `ExecReload` lacks `--force`, the
  script warns and says to run `sudo systemctl restart caddy`. It does not restart Caddy
  itself.

Rules for the implementer that are not about the code itself:

- **Never run the script against a real host.** This machine has `ssh`, and `spark` may
  resolve through the operator's `~/.ssh/config`. Run it by hand only with `--dry-run`, or
  with the test stubs first on `PATH` (section 7). The tests use the host name
  `server.invalid`, which can never resolve. Never ssh to any host.
- **Do not run any `git stash` command.** The operator keeps local work in a stash.
- **Use a `TMPDIR` under `~/.cache/`**: `export TMPDIR=/home/sandy/.cache/earmark-step35-tmp
  && mkdir -p "$TMPDIR"`, and `rm -rf` it when done. `/tmp` is a small tmpfs. Install
  nothing into the repo `.venv`; a scratch venv or download goes under `TMPDIR`. Never run
  `pytest -m network`.
- **Run both gates:** `make check` and `GITHUB_ACTIONS=true make check`.
- **Placeholders only**, in every file this step writes: `earmark.example.com`,
  `example.com`/`.org`/`.net`, `192.168.1.10`, `127.0.0.1`, `spark`, port `8443`. No real
  domain, LAN address, certificate or key. Tests generate their certificates at run time.
  Nothing PEM-encoded is committed.
- **`ruff format --check` covers Python code blocks in Markdown.** Keep any
  ```` ```python ```` fence in `docs/` ruff-formatted, or use `bash`/`text`.
- **The removed-wording scan** (`tests/test_docs.py::REMOVED`) bans, among others,
  `\bclaude\b`, `vault`, `\bdigest`, `embedding` and `cloudflare tunnel` in `README.md`,
  `docs/deploy.md`, `docs/shortcut.md`, `docs/runbook-spark.md` and `docs/asr.md`.
- `deploy/caddy/install-cert.sh` is untracked now and already has mode 0755. Keep the
  executable bit: git must record it as `100755` (`git ls-files -s deploy/caddy`, after
  `git add`). The orchestrator commits. A test checks the bit, so CI catches a `100644`.

## 1. Outcome

- `deploy/caddy/install-cert.sh` is committed and reviewed. It keeps its behaviour: local
  openssl checks; with `--dry-run`, nothing more; otherwise it copies the files with scp into
  a remote `mktemp -d` that a trap removes, and runs one `ssh -t` session that installs them
  with sudo as `root:caddy` 0644 and 0640, validates the Caddyfile and reloads Caddy if it
  runs. It never prints the key. It also gains the fixes of section 5.1. It still runs under
  bash 3.2 with LibreSSL, which was checked in planning against real bash 3.2.57 and LibreSSL
  3.3.6.
- `deploy/caddy/Caddyfile.own-cert` is a second example: a `tls` line with the two
  installed paths, `bind 192.168.1.10`, the site address `earmark.example.com:8443`, a
  comment saying when to change the port, and no port 80. It validates on Caddy 2.6.2
  (Ubuntu 24.04's package) and 2.11.9 (upstream).
- `docs/deploy.md` gets section 6.1.8, "With a certificate you already have", and a pointer to
  it from 6.1.3 and 6.1.4. Runbook phase 7 offers route A (Let's Encrypt) and route B (your
  own certificate), with checks for both, and phases 7 and 9 say to add the port. The
  Shortcut guide names the port once. `docs/ARCHITECTURE.md` lists the two files and the
  route.
- New tests:
  - `tests/test_install_cert.py` runs the script against certificates generated in the
    test, with ssh and scp replaced by stubs, so nothing leaves the machine;
  - `tests/test_deploy.py` checks the new Caddyfile;
  - `tests/test_docs.py` checks the docs, and its placeholder scan now covers every file
    under `deploy/`, the step 35 plan included.

## 2. Findings

Each finding was checked under `TMPDIR=/home/sandy/.cache/earmark-plan35-tmp`, with scratch
copies only. Nothing was installed into the repo, and no host was contacted except package
downloads.

### 2.1 openssl here, in CI and on the Mac

- **This sandbox:** `/usr/bin/openssl`, OpenSSL 3.5.7 (Debian).
- **CI** (`.github/workflows/ci.yml`, `runs-on: ubuntu-24.04-arm`):
  - GitHub's Ubuntu 24.04 image readme (image 20261116.376.1) lists "OpenSSL
    3.0.13-0ubuntu3.16".
  - The arm64 partner image's page says its inventory is the same Ubuntu 24.04 readme.
  - On Ubuntu, `ca-certificates` depends on `openssl` (read from the package's control
    file), so every runner that verifies TLS has the command.
  - To test that exact build, I extracted Ubuntu noble's `openssl_3.0.13-0ubuntu3.17_arm64.deb`
    and `libssl3t64` into `TMPDIR` and ran them with `LD_LIBRARY_PATH`.
- **The Mac:** macOS ships LibreSSL as `/usr/bin/openssl`. LibreSSL 3.3.6 (macOS 12 to 15)
  was built from `cdn.openbsd.org` source into `TMPDIR`.
- **Result:** every command the script uses, and every command the test fixture uses (section
  6.1), behaves the same on 3.5.7, 3.0.13 and, for the script's commands, LibreSSL 3.3.6.
  Tables 2.2 and 2.3 have the cases. The tests therefore need **no skip**. openssl is present
  in both places the suite runs, and a missing command makes the fixture fail with a message
  rather than skip, so CI can never pass by skipping (section 5.6).
- Output formats differ, and the script prints them as they come. Nothing compares text
  between two binaries:
  - OpenSSL 3.5 prints `subject=CN=*.example.com`;
  - OpenSSL 3.0 prints `subject=CN = *.example.com`;
  - LibreSSL prints `subject= /CN=*.example.com`, and `pkcs7 -print_certs` prints
    `subject=/CN=…`.

  The order check compares one binary's `issuer=` with the same binary's `subject=`, so the
  differences cancel out.
- **LibreSSL 3.3.6 has no `x509 -ext`** ("unknown option -ext"). The SAN therefore stays
  parsed from `-text`, and a test pins the options the script may use (section 6.3).
- LibreSSL's `s_client` has `-servername` and `-showcerts`, which the runbook check uses.

### 2.2 The script as it stands (reviewed and run)

I ran the untracked script with `--dry-run` under bash 5.2 and a real bash 3.2.57, which was
built from GNU source in `TMPDIR` with bison from Ubuntu's package. Each run used each of the
three openssl builds. Stubs that log and fail were first on `PATH`, so no call could escape.

| Case | Result today | Verdict |
|---|---|---|
| leaf + intermediate, matching EC key | passes, prints subject, issuer, date, names | keep |
| RSA leaf, PKCS#1 `BEGIN RSA PRIVATE KEY` key | passes | keep (commercial CAs hand these out) |
| bare leaf | `… has 1 certificate(s); it needs the intermediates too (iOS rejects a bare leaf)` | keep |
| other key | `the key does not match the first certificate in …` | keep |
| expired leaf + intermediate | `the certificate has expired` | keep |
| leaf valid 10 days | passes, `WARNING: expires within 30 days` on stderr | keep |
| encrypted key, no terminal | `… is not a readable unencrypted PEM private key` | keep |
| **encrypted key, with a terminal** | **hangs** at `Enter pass phrase` (openssl opens `/dev/tty`, so `2>&1 >/dev/null` does not hide it); shown with `script -qec … /dev/null`, killed by `timeout` | fix: `-passin pass:` and `</dev/null` |
| **key + leaf + intermediate in one file as the chain** | **passes**, so it would install the key as `fullchain.pem`, mode 0644 | fix: refuse |
| **leaf + root (intermediate missing)** | **passes** (two certificates) | fix: order check |
| **leaf twice** | **passes** | fix: order check |
| **no Subject Alternative Name** | **exits 1 with no message**: under `pipefail` the `grep -A1` that finds no SAN fails the pipeline | fix: explicit check and message |
| second certificate corrupt | passes (only the first is parsed) | fix: parse every certificate |

No case called ssh or scp. The usage text and exit 64 work under bash 3.2.

The remote half was also reviewed:

- `remote_tmp` is pasted into single-quoted remote commands, `rm -rf` among them, and is only
  checked against `/tmp/*`. A login banner line, or a quote in the output, is not refused
  cleanly. Fix: allow only `[A-Za-z0-9._/-]`, no `..`, under `/tmp/` or `/var/tmp/`.
- The cleanup trap ignores a failed removal silently. Fix: warn, naming the directory.
- A host beginning with `-` would be read by ssh as an option. Fix: refuse it. File operands
  get `--` for scp. Checked: OpenSSH 10.0's scp accepts `--` and then reads `-weird.pem` as a
  file. macOS's scp is OpenSSH too.
- The remote script does not check that the `caddy` user can read the key it just installed,
  nor that the Caddyfile uses those files. Fix: `sudo -u caddy test -r`, and a warning from
  `grep`.

### 2.3 The fixed script, prototyped

A scratch copy with the fixes of section 5.1 was run under bash 3.2.57 and 5.2, with OpenSSL
3.5.7, OpenSSL 3.0.13 and LibreSSL 3.3.6 (2 × 3 runs per case). Every case gave the same
exit code and message in all six runs:

- the good chain, the RSA PKCS#1 key, and the 10-day leaf (with its warning) pass;
- the bare leaf, the mismatched key, the expired certificate, the encrypted key, the chain with
  the key inside, the leaf with the root, the leaf twice, the corrupt second certificate, and
  the missing SAN each fail with their message (section 4.2).

The full run used the stubs of section 4.4, which run the remote commands locally against a
fake root:

- it installs `fullchain.pem` 0644, `privkey.pem` 0640 and the directory 0750, and removes
  the temporary directory;
- `scp` failing, or no `caddy` group, still removes it;
- an odd `mktemp` answer (`/tmp/a'b`, `/home/x/tmp.1`) stops before any copy;
- validate failing exits 1 without a reload;
- a stopped Caddy gives the start hint, and a unit without `--force` gives the warning.

Other checks:

- `openssl crl2pkcs7 -nocrl -certfile F | openssl pkcs7 -print_certs -noout` lists `subject=`
  and `issuer=` for every certificate. It exits non-zero if any certificate is corrupt, on all
  three builds.
- `-passin pass:` makes an encrypted key fail at once, and changes nothing for a plain key, on
  all three builds.

### 2.4 Caddy (downloaded and run in `TMPDIR`)

- **The packages:**
  - Ubuntu 24.04 ships `caddy_2.6.2-6ubuntu0.24.04.3` (universe), and upstream's latest is
    `v2.11.9` (2026-11-05). I extracted both `.deb`s.
  - Both create the system user and group `caddy` (`groupadd --system caddy` in `postinst`),
    and both run `User=caddy`, `ExecStart=/usr/bin/caddy run --environ --config
    /etc/caddy/Caddyfile` and `ExecReload=/usr/bin/caddy reload --config /etc/caddy/Caddyfile
    --force`.
  - Both ship a default `/etc/caddy/Caddyfile` serving `:80`. If another proxy holds 80,
    Caddy fails to start after `apt install`, until the new Caddyfile is in place. That is
    harmless, and the script then says to start it.
- **The draft `Caddyfile.own-cert`** (section 4.3), with the `tls` paths pointed at generated
  files:
  - `caddy validate --adapter caddyfile` prints "Valid configuration" on 2.6.2 and 2.11.9,
    and `caddy fmt` leaves it unchanged on both;
  - `caddy adapt` shows one server listening on `192.168.1.10:8443` only, with
    `automatic_https: {disable_redirects: true}`;
  - the tls app has `load_files` and **no automation policy**, so Caddy never tries ACME for
    the name.
- **`caddy validate` loads the certificate files.**
  - A mismatched key fails with `tls: private key does not match public key`, and a missing
    file with `open …: no such file or directory`, on both versions.
  - So the script's remote `caddy validate` checks the installed pair as Caddy reads it.
    Running as root, it does not check that the caddy user can read the key, hence the extra
    `sudo -u caddy test -r`.
- **Reload and replaced files (renewal).**
  - Live, on 127.0.0.1 with an admin endpoint on loopback, both versions served certificate
    serial 03, and the files were replaced in place (new chain and new key, serial 09). `caddy
    reload --force` then served 09. A further replacement (serial 04) followed by a plain
    `caddy reload` kept serving 09: unchanged config, no reload.
  - So renewal works through `systemctl reload caddy` **because** the packaged unit passes
    `--force`. That is decision P2.
- **Port 80.** Without `auto_https disable_redirects`, 2.11.9 started a second server, named
  `remaining_auto_https_redirects`, on the bind address port 80. If another proxy holds 80
  (it holds 443, so it likely does), Caddy would fail to start. The example keeps
  `disable_redirects`.
- **The served chain.** Against that live server:
  - LibreSSL's `openssl s_client -connect … -servername earmark.example.com -showcerts
    </dev/null | grep -c 'BEGIN CERTIFICATE'` printed 2;
  - `s_client … | openssl x509 -noout -serial -enddate` printed the serial and date of the
    file installed last;
  - `curl --cacert root.pem` (the test root only) verified, so the intermediate was served.

  These are the runbook's route B checks.

### 2.5 How the existing tests see the new files

- **Placeholder scan.**
  - `test_docs_use_placeholders_only` scans `DOCS`, `PLANS` and `CLAUDE.md`. It does **not**
    scan anything under `deploy/` today, so the brief's "the existing placeholder scan covers
    the new files" needs a new parametrization (section 6.1, A1).
  - `_assert_placeholders` checks email domains, IPv4 addresses, `*.ts.net`, ntfy tokens and
    secret lines. It has **no host-name check**, so a real domain in a Caddyfile would pass.
    The new deploy-file test adds one.
  - Today `deploy/caddy/install-cert.sh` and `deploy/caddy/Caddyfile` pass
    `_assert_placeholders`.
  - A host-name regex over common TLDs found only `earmark.example.com`, `github.com` (in
    the existing Caddyfile's comment) and `example.com` (in the script's example). `.sh` must
    not be a TLD in that regex, or `install-cert.sh` matches.
  - The step 33 and 34 plans fail `_assert_placeholders`, on `demo-token` inside a longer line
    and on an address at a subdomain of `example.com`. That is why they are not in `PLANS`. Add only this plan.
- **Repo layout.**
  - `deploy/, Dockerfile, Dockerfile.gpu, compose.yaml` is one multi-path line, so
    `layout_entries` cannot give it children.
  - The layout in section 4.6 splits it, and was run through `layout_entries`. It gives
    `deploy/caddy/Caddyfile.own-cert` and `deploy/caddy/install-cert.sh`, each with step 35,
    plus `deploy/caddy/Caddyfile`, `deploy/systemd/`, `Dockerfile`, `Dockerfile.gpu` and
    `compose.yaml`.
  - `test_architecture_repo_layout_matches_repo` accepts a missing path only if its step is
    `todo`, and step 35 is `todo` until the orchestrator marks it.
  - `test_planned_paths_are_marked_planned` requires any paragraph naming a missing planned
    path to name its step. Once the files exist this no longer applies.
- **Runbook.**
  - `test_runbook_phases_end_in_checks`: every phase has exactly one `**Check:**`, a non-empty
    body before it, and no `### ` heading after it. So route B's checks go into phase 7's one
    Check list, as labelled bullets.
  - `_phases()` requires exactly phases 0–12 plus "Send back". No phase is added.
  - No test pins phase 7's or 9's wording today.
- **Deploy guide.**
  - `test_deploy_doc_reachability` needs `DNS only`, `AllowedIPs`, `rebind-domain-ok`,
    `private-domain`, `Zone:Read` (or `Zone → Zone → Read`), the funnel sentence, and
    `### 6.3 ` mentioning `EARMARK_MAX_UPLOAD_MB`. All of these stay.
  - `test_doc_anchors_resolve` and `test_doc_relative_links_resolve` check every
    `deploy.md#…` link and every relative link in the five docs.
  - The runbook already links to `deploy.md#61-…` and `#63-…`, and refers to "6.1.3" and
    "6.1.6" in text, so renumbering 6.1.5–6.1.7 would break references. The new subsection is
    appended as **6.1.8**. Its anchor, by `heading_anchors`, is
    `618-with-a-certificate-you-already-have`.
- **The Caddy tests.**
  - `tests/test_deploy.py::test_caddyfile_example` reads only `deploy/caddy/Caddyfile`, and
    stays as it is.
  - `test_systemd_dir_has_only_the_two_services` pins `deploy/systemd/` only.
  - `test_proxy_token_stays_out_of_earmark` does not read `deploy/caddy/`.
- **ruff rules on tests:**
  - `tests/**` ignores S101, S105, S106, S108 and S603, **not S607**, so subprocess calls use
    absolute paths from `shutil.which`;
  - T20 bans `print` (use `sys.stdout.write`);
  - PTH wants `Path.open`.

### 2.6 What the docs say today

- **`docs/deploy.md` §6.1:**
  - 6.1.1 is the Cloudflare DNS-only record;
  - 6.1.2 is WireGuard;
  - 6.1.3 is Caddy: it says "The stock Caddy build lacks the Cloudflare DNS module, so you need
    a build that has it", with xcaddy and the download server;
  - 6.1.4 is Let's Encrypt DNS-01 with the token;
  - 6.1.5 says why Caddy is not a compose service, including "A stock image is not enough,
    because the Cloudflare module needs a custom build", and "It owns port 443 on the LAN
    address";
  - 6.1.6 is DNS rebinding;
  - 6.1.7 is the test: `curl https://earmark.example.com/healthz`, and the Shortcut URL
    without a port.

  Elsewhere in the guide:

  - §1 says "Only the example `deploy/caddy/Caddyfile` was checked, with `caddy validate`
    and `caddy fmt`";
  - §2's diagram shows `192.168.1.10:443`;
  - §6.3 says "The example Caddyfile adds nothing that limits uploads";
  - §19.4 already says "Add the port to the URL if the proxy does not listen on 443".
- **Runbook phase 7:**
  - the intro names Caddy, the DNS-only record, and a link to 6.1;
  - step 1 is the DNS record;
  - step 2 is the Cloudflare token;
  - step 3 is the xcaddy build with `list-modules`;
  - step 4 is a bash block (`cp deploy/caddy/Caddyfile`, `cloudflare.env`, `systemctl edit`,
    `caddy validate`, `restart`, and `journalctl` to wait for the certificate);
  - step 5 is WireGuard;
  - the Check has two `dig` lines, `curl …/healthz` from the Mac, Safari on cellular, and the
    phase 4 upload pointed at `https://earmark.example.com/transcriptions`.

  None of it mentions a port.
- **Runbook phase 9:** build the Shortcut from shortcut.md, capture the share sheet, share an
  episode and a voice memo. It does not state the URL.
- **Runbook intro:**
  - list item 5 reads "A DNS-only Cloudflare name, served by Caddy on the LAN and reached
    over WireGuard";
  - "Your values" says `earmark.example.com` is "The API's name, in your Cloudflare zone".
- **`docs/shortcut.md` §1:** `https://earmark.example.com/healthz`, "replace it with your
  name". No port.
- **`docs/ARCHITECTURE.md`:**
  - the Decisions "Reachability" row and the "Reverse proxy" bullet describe DNS-01 only;
  - the module-layout fence lists `deploy/caddy/Caddyfile the example reverse proxy (step
    16)`;
  - "Open questions" item 4 says "Only the example Caddyfile was checked, with `caddy
    validate`".

### 2.7 Dead ends

- **Do not generate the expired certificate with `-days -1`, `-days 0` or `-not_after`.**
  OpenSSL 3.0 refuses a negative `-days`, and `-not_before`/`-not_after` exist only from
  OpenSSL 3.4, which CI does not have. `openssl ca -startdate … -enddate …` with a small config
  works on 3.0.13 and 3.5.7 (section 6.1).
- **Do not add `cryptography` to make certificates.** It is not in the lock, and openssl is
  present everywhere the tests run.
- **Do not use `openssl x509 -ext subjectAltName`.** LibreSSL 3.3.6 lacks it.
- **Do not end an awk early in a pipeline** (`awk '…{print; exit}'`, `head`, `grep -q` fed by
  openssl). Under `set -o pipefail`, openssl's SIGPIPE fails the pipeline, and `set -e` then
  ends the script silently. That is the bug the SAN line has today. Read all input, and use a
  flag variable.
- **Do not split the chain into temporary files** to check each certificate. The
  `crl2pkcs7 | pkcs7 -print_certs` pipeline does it in memory, on all three builds.
- **Do not count `BEGIN CERTIFICATE` with grep any more.** It counts a corrupt block, and it
  puts the literal `-----BEGIN` back into a file the key-material scan reads (section 6.1, A1).
- **Do not run the real `caddy` or `ssh` in tests.** The stubs of section 4.4 stand in for
  both.

## 3. Files

| File | Change |
|---|---|
| `deploy/caddy/install-cert.sh` | **Add** (now untracked), with the fixes of section 5.1. Mode 0755. |
| `deploy/caddy/Caddyfile.own-cert` | **New.** The example of section 4.3. |
| `tests/install_cert_stub.py` | **New.** One standalone stdlib script that stands in for `ssh`, `scp`, `sudo`, `install`, `getent`, `caddy` and `systemctl` (section 4.4). Not a test module; it is executed, not imported. |
| `tests/test_install_cert.py` | **New.** The script's tests (section 6). |
| `tests/test_deploy.py` | `test_caddyfile_own_cert_example`; module docstring unchanged. |
| `tests/test_docs.py` | The deploy-file placeholder test, the step 35 plan in `PLANS`, and the doc tests of section 6. |
| `docs/deploy.md` | §1 bullet, §2 diagram note, 6.1.3 and 6.1.4 pointers, 6.1.5 wording, 6.1.7 port, new 6.1.8, §6.3 wording (section 4.5). |
| `docs/runbook-spark.md` | Intro item 5, "Your values" rows, phase 7 rewritten with routes A and B, phase 9 URL (section 4.5). |
| `docs/shortcut.md` | One sentence on the port in §1 (section 4.5). |
| `docs/ARCHITECTURE.md` | Repo layout, module layout, Reachability row, "Reverse proxy" bullet, open question 4, "Settled by step 35" (section 4.6). |

`CLAUDE.md`, `README.md`, `docs/PLAN.md` (the orchestrator updates the status table),
`src/`, `compose.yaml`, `Dockerfile*`, `.github/` and `plugins/` do not change. No dependency
is added.

## 4. Interfaces

### 4.1 The script's command line

```text
deploy/caddy/install-cert.sh [--dry-run] <ssh-host> <fullchain.pem> <private.key>
deploy/caddy/install-cert.sh -h | --help
```

- `-h`/`--help` as the first argument prints the usage to stdout and exits 0.
- `--dry-run` is recognised only as the first argument.
- Anything other than exactly three remaining arguments prints the usage to **stderr** and
  exits 64.
- The usage is the header comment, lines 2 to the line above `set -euo pipefail`, with `# `
  removed. It must contain `Usage:`, the usage line above, `--dry-run`, the two installed
  paths and their modes, and the `tls /etc/caddy/certs/fullchain.pem
  /etc/caddy/certs/privkey.pem` line. Its example uses `spark` and `~/ssl/example.com.*`
  files.
- Constants stay as they are: `CERT_DIR=/etc/caddy/certs`, `CADDYFILE=/etc/caddy/Caddyfile`.
  Tests read `CERT_DIR=` from the script.
- **Exit codes:** 0 on success (dry run included); 64 for usage; 1 for every refusal and
  every failure. An `ssh`/`scp` failure exits with that command's status under `set -e`, and
  the trap still runs.

### 4.2 The script's messages (tests match these substrings)

Refusals go to stderr with the prefix `install-cert: ` and exit 1, in this order:

| # | Condition | Message |
|---|---|---|
| 1 | host empty or starting with `-` | `not an ssh host: <host>` |
| 2 | chain or key not a regular file | `no such file: <path>` |
| 3 | no `openssl` on PATH | `openssl not found` |
| 4 | the chain file contains `PRIVATE KEY` | `<chain> contains a private key; give the certificates and the key as two files` |
| 5 | first certificate does not parse | `<chain> is not a PEM certificate` |
| 6 | key does not parse with an empty passphrase | `<key> is not a readable unencrypted PEM private key` |
| 7 | key's public key ≠ first certificate's | `the key does not match the first certificate in <chain> (the server's certificate comes first)` |
| 8 | any certificate does not parse | `<chain> has a certificate that does not parse` |
| 9 | fewer than two certificates | `<chain> has <n> certificate(s); it needs the intermediates too (iOS rejects a bare leaf)` |
| 10 | certificate *i*'s issuer ≠ certificate *i+1*'s subject | `certificate <i> in <chain> was not issued by certificate <i+1>; the order is the server's certificate, then each intermediate` |
| 11 | `-checkend 0` fails | `the certificate has expired` |
| 12 | no SAN in the first certificate | `the certificate has no Subject Alternative Name (iOS ignores the common name)` |
| 13 | remote `mktemp -d` answer not safe (section 5.1) | `unexpected temporary directory on <host>: <answer>` |

On success the local part prints to stdout:

```text
certificate:
  subject=…          (openssl's own lines, indented two spaces: -subject -issuer -serial -enddate)
  issuer=…
  serial=…
  notAfter=…
  names: DNS:*.example.com, DNS:example.com
  chain: <n> certificates, in order; key matches
```

Within 30 days of expiry it also prints `  WARNING: expires within 30 days` to stderr. Then
it prints:

- with `--dry-run`: `dry run: checks passed, nothing copied to <host>`, exit 0;
- otherwise: `copying to <host> ...`, `installing on <host> (sudo) ...`, the remote lines
  below, and last `done. From the Mac: curl https://<your earmark name>[:port]/healthz`.

The remote script's messages. Errors go to stderr with the prefix `install-cert: `, and the
remote shell's non-zero exit ends the local script with status 1:

| Condition | Output | Exit |
|---|---|---|
| no `caddy` group | `install-cert: no caddy group on this host; install Caddy first` | 1, nothing installed |
| caddy user cannot read the key | `install-cert: the caddy user cannot read /etc/caddy/certs/privkey.pem` | 1 |
| no Caddyfile | `no /etc/caddy/Caddyfile yet: copy deploy/caddy/Caddyfile.own-cert there, edit it, then run this again` | 0 |
| Caddyfile does not name `/etc/caddy/certs/fullchain.pem` | `install-cert: WARNING: /etc/caddy/Caddyfile does not use /etc/caddy/certs/fullchain.pem` (then continues) | — |
| `caddy validate` fails | `install-cert: /etc/caddy/Caddyfile does not validate; caddy was not reloaded` | 1 |
| valid | `Caddyfile valid` | — |
| running, unit reload lacks `--force` | `install-cert: WARNING: this caddy unit reloads without --force, which keeps the old certificate; run sudo systemctl restart caddy` (then reloads) | — |
| running | `caddy reloaded` | 0 |
| not running | `caddy is not running; start it with: sudo systemctl start caddy` | 0 |

The cleanup failure warning (local, stderr) is `install-cert: WARNING: could not remove
<dir> on <host>; remove it by hand`.

### 4.3 `deploy/caddy/Caddyfile.own-cert`

Exactly this. It was validated and `caddy fmt`-clean on 2.6.2 and 2.11.9, with tabs as
Caddy formats them:

```text
# Example: TLS for earmark on the LAN interface only, with a certificate you already
# have (a commercial wildcard, say). Stock Caddy is enough: no DNS module, no API token.
# deploy/caddy/install-cert.sh installs the two files below; see docs/deploy.md 6.1.8.
{
	# No port 80: nothing redirects to HTTPS, and another proxy may hold port 80.
	auto_https disable_redirects
}

# :8443 because another proxy on this host already listens on 443. If nothing else uses
# 443, drop ":8443" (Caddy then serves 443), and leave the port out of every URL.
earmark.example.com:8443 {
	# The LAN (or WireGuard) address the DNS record points to. Never 0.0.0.0.
	bind 192.168.1.10
	# The full chain (the server's certificate, then each intermediate), then the key.
	tls /etc/caddy/certs/fullchain.pem /etc/caddy/certs/privkey.pem
	reverse_proxy 127.0.0.1:8787
}
```

The comment lines may be reworded, but must keep the facts that the tests and docs rely on:
when to change the port, no port 80, never `0.0.0.0`, and the script's name.

### 4.4 `tests/install_cert_stub.py`

A standalone script, stdlib only, typed for `mypy --strict`, ruff-clean (no `print`: use
`sys.stdout.write`). The test writes one wrapper per name into a `bin/` directory placed
first on `PATH`:

```bash
#!/bin/sh
exec '<sys.executable>' -I '<repo>/tests/install_cert_stub.py' ssh "$@"
```

The paths are quoted with `shlex.quote`. The first argument is the name it stands for. Its
environment:

| Variable | Meaning | Default |
|---|---|---|
| `STUB_ROOT` | the fake server's filesystem root (a test directory) | required |
| `STUB_LOG` | file to which each call appends one JSON line `[name, *args]` | required |
| `STUB_MKTEMP` | what the fake `mktemp -d` prints | `/tmp/tmp.EarmarkTest1` |
| `STUB_FAIL` | a name whose call exits 1 after logging (e.g. `scp`) | none |
| `STUB_GETENT_RC`, `STUB_VALIDATE_RC`, `STUB_ACTIVE_RC` | exit codes of `getent`, `caddy`, `systemctl is-active` | `0` |
| `STUB_EXEC_RELOAD` | what `systemctl show -p ExecReload --value caddy` prints | `{ argv[]=/usr/bin/caddy reload --config /etc/caddy/Caddyfile --force }` |

**Remote paths are mapped** into `STUB_ROOT` by one regex pass,
`(?<![\w/.])(/tmp/|/var/tmp/|/etc/caddy)` → `STUB_ROOT` + the match, applied to remote
commands and to scp targets only, never to local paths. Behaviour per name:

- **`ssh`.** The arguments are `[host, command]` or `["-t", host, command]`.
  - `umask 077 && mktemp -d` prints `STUB_MKTEMP` and a newline. It also creates the mapped
    directory, but only if `STUB_MKTEMP` matches `^/tmp/[A-Za-z0-9.]+$`.
  - Any other command runs as `[bash, "-c", mapped command]`, with `bash` from
    `shutil.which`, and returns that exit code.
  - A third shape exits 97.
- **`scp`** expects exactly `["-q", "--", source, "host:path"]`; anything else exits 97. It
  copies the source to the mapped path.
- **`sudo`** drops a leading `-u caddy`, then runs the rest with `subprocess.run` (resolving
  through the stub `PATH`) and returns its code.
- **`install`** handles `-d`, `-m MODE`, `-o USER`, `-g GROUP`, and creates or copies, then
  `chmod`s to the mode. It never `chown`s; the owners are visible in the log.
- **`getent`** returns `STUB_GETENT_RC`.
- **`caddy`** returns `STUB_VALIDATE_RC`.
- **`systemctl`:**
  - `is-active` returns `STUB_ACTIVE_RC`;
  - `show` prints `STUB_EXEC_RELOAD`;
  - anything else (`reload`) exits 0.
- **Any other name** exits 97.

`test -r` (through the sudo stub) and `grep`, `rm` and `[` (in the remote command) are the
real ones, working on mapped paths. That is the point: the remote script's quoting and logic
actually run.

### 4.5 Docs

**`docs/deploy.md`.**

- **§1, last bullet.** Keep the existing sentence about what is not verified, and replace
  "Only the example `deploy/caddy/Caddyfile` was checked, …" with:

  > The example `deploy/caddy/Caddyfile` was checked with `caddy validate` and `caddy fmt`,
  > using a Caddy build that has the Cloudflare module. `deploy/caddy/Caddyfile.own-cert` was
  > checked the same way with Caddy 2.6.2 and 2.11.9, and `deploy/caddy/install-cert.sh`'s
  > checks are tested against generated certificates, with ssh, sudo and Caddy stubbed. The
  > own-certificate route (6.1.8) has run once, on the operator's Spark.
- **§2.** Under the diagram, add one line: "With a certificate you already have, Caddy may
  listen on another port, such as 8443 (6.1.8)."
- **6.1.3.** Before "The stock Caddy build lacks…", add a paragraph: "With a certificate you
  already have, stock Caddy is enough and the build steps below do not apply: see 6.1.8 for
  the Caddyfile and the install." Keep the rest as it is.
- **6.1.4.** As its first line, add: "This is the Let's Encrypt route. With a certificate
  you already have, skip it: see 6.1.8."
- **6.1.5.** Change "A stock image is not enough, because the Cloudflare module needs a
  custom build." to "For the Let's Encrypt route a stock image is not enough, because the
  Cloudflare module needs a custom build." Keep "It owns port 443 on the LAN address" as is,
  but add ", or another port if something else holds 443 (6.1.8)".
- **6.1.7.** Add, after the first bullet: "If Caddy listens on another port (6.1.8), add it to
  every URL here, for example `https://earmark.example.com:8443/healthz`."
- **6.1.8 (new, after 6.1.7, before `### 6.2`).** The content below. The wording may be
  improved, but every needle of test D1 (section 6.2) must stay.

  ~~~~markdown
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
  ~~~~

- **§6.3.** Change "The example Caddyfile adds nothing that limits uploads." to "Neither
  example Caddyfile adds anything that limits uploads."

**`docs/runbook-spark.md`.**

- **Intro list, item 5:** "A DNS-only Cloudflare name, served by Caddy on the LAN with a Let's
  Encrypt certificate or one you already have, and reached over WireGuard."
- **"Your values":**
  - `earmark.example.com` becomes "The API's name, in your DNS zone.";
  - `example.com` becomes "That DNS zone (in Cloudflare for phase 7's route A).";
  - after the `example.com` row, add `| `8443` | The HTTPS port, only if something else on
    the Spark holds 443 (phase 7). | |`.
- **Phase 7.** It is rewritten as below. Each needle of test D2 must stay. The single
  `**Check:**` stays last, and no `###` heading is used.

  ~~~~markdown
  ## Phase 7: Reaching the API from the phone and the Mac

  The API stays on `127.0.0.1:8787`. Caddy serves it over HTTPS on the LAN address, and a
  DNS-only record points the name at that private address
  ([deploy.md section 6.1](deploy.md#61-a-private-dns-name-wireguard-and-caddy-primary)).
  The certificate comes one of two ways: **route A**, Let's Encrypt with a Cloudflare token
  (step 3), or **route B**, a certificate you already have, such as a commercial wildcard
  (step 4, [deploy.md section 6.1.8](deploy.md#618-with-a-certificate-you-already-have)).
  Do one of them.

  1. **DNS record.** In Cloudflare (or the DNS provider of your zone, for route B), add an
     `A` record `earmark.example.com` → `192.168.1.10`. In Cloudflare, set proxy status **DNS
     only (grey cloud)**. The record publishes your internal address, which is a minor leak.
  2. **The port.** On the Spark, `sudo ss -ltnp 'sport = :443'` shows whether something
     already listens on 443. If another program holds it (another project's proxy, say),
     use another port, such as 8443, in the Caddyfile's site address
     (`earmark.example.com:8443`). From then on, add it to every URL: in the checks below
     (`https://earmark.example.com:8443/healthz`), in the Shortcut (phase 9), and in
     `EARMARK_API_URL` on the Mac. Neither example Caddyfile uses port 80.
  3. **Route A: Let's Encrypt with a Cloudflare token.**
     - Create an API token with **Zone → DNS → Edit** and **Zone → Zone → Read**, and limit
       it to the one zone. Edit alone fails.
     - Build or download Caddy with the Cloudflare module, then confirm the module is there:

       ```bash
       xcaddy build v2.11.9 --with github.com/caddy-dns/cloudflare@v0.2.4
       ./caddy list-modules | grep dns.providers.cloudflare
       ```

       Install it as `/usr/bin/caddy` behind Caddy's own systemd unit (6.1.3).
     - Config and token:

       ```bash
       sudo cp deploy/caddy/Caddyfile /etc/caddy/Caddyfile   # then edit the name and bind
       sudo install -m 600 /dev/null /etc/caddy/cloudflare.env
       sudoedit /etc/caddy/cloudflare.env                    # CF_API_TOKEN=<token>
       sudo systemctl edit caddy        # [Service] EnvironmentFile=/etc/caddy/cloudflare.env
       sudo caddy validate --config /etc/caddy/Caddyfile
       sudo systemctl restart caddy && journalctl -u caddy -f   # wait for the certificate
       ```

  4. **Route B: a certificate you already have.** Stock Caddy, no token, and no module.
     - On the Spark, install Caddy from the distribution's packages and put the example in
       place:

       ```bash
       sudo apt install caddy
       sudo cp deploy/caddy/Caddyfile.own-cert /etc/caddy/Caddyfile   # then edit the name, bind and port
       ```

     - On the Mac, from a checkout, check the files, then install them (sudo asks for your
       password on the Spark):

       ```bash
       deploy/caddy/install-cert.sh --dry-run spark ~/ssl/example.com.fullchain.pem ~/ssl/example.com.key
       deploy/caddy/install-cert.sh spark ~/ssl/example.com.fullchain.pem ~/ssl/example.com.key
       ```

       If Caddy was not running, start it: `sudo systemctl start caddy`.
     - Write down the expiry date the script printed. Renewal is manual: before that date, run
       the same line with the new files.
  5. **WireGuard on the phone and the Mac.** Add the Spark to the existing tunnel:
     `AllowedIPs = 192.168.1.10/32` (a split tunnel), `DNS =` unset. Turn on on-demand
     activation for cellular and for Wi-Fi other than the home SSIDs.

  **Check:**

  - Add your port to each URL below if you chose one in step 2.
  - `dig +short earmark.example.com @1.1.1.1` prints `192.168.1.10`.
  - `dig earmark.example.com` through your home resolver gives the same answer. If it
    doesn't, DNS-rebinding protection is dropping it. Allow-list the domain (6.1.6), for
    example `rebind-domain-ok=/example.com/` for dnsmasq or Pi-hole.
  - On the Spark, `sudo ss -ltnp | grep caddy` shows `192.168.1.10:443` (or your port), and
    never `0.0.0.0` or `*`.
  - Route A: `journalctl -u caddy` shows the certificate obtained for `earmark.example.com`.
  - Route B:
    - the install printed `chain: 2 certificates, in order; key matches` (or more
      certificates), then `Caddyfile valid` and `caddy reloaded`;
    - `sudo ls -l /etc/caddy/certs` shows `privkey.pem` as `-rw-r-----` `root caddy`;
    - from the Mac,
      `openssl s_client -connect earmark.example.com:8443 -servername earmark.example.com -showcerts </dev/null 2>/dev/null | grep -c 'BEGIN CERTIFICATE'`
      prints 2 or more: the server sends the intermediates;
    - the same `openssl s_client` without `-showcerts`, piped to
      `openssl x509 -noout -serial -enddate`, shows the serial and date the script printed.
  - From the Mac at home, `curl https://earmark.example.com/healthz` (route B on port 8443:
    `curl https://earmark.example.com:8443/healthz`) prints `{"status":"ok"}` with a valid
    certificate.
  - On the phone, on cellular with Wi-Fi off, Safari opens
    `https://earmark.example.com/healthz`, with your port if you chose one.
  - An upload through the proxy from the Mac: the phase 4 upload `curl`, pointed at
    `https://earmark.example.com/transcriptions` (with your port), answers 202. See
    [deploy.md section 6.3](deploy.md#63-uploads-through-the-proxy).
  ~~~~

  Keep the literal `**Check:**` line exactly once (`test_runbook_phases_end_in_checks`).
- **Phase 9.** After the first paragraph, add: "The Shortcut's URL is
  `https://earmark.example.com/transcriptions`, or with the port from phase 7 if you chose
  one: `https://earmark.example.com:8443/transcriptions`." Add a first check bullet: "the
  Shortcut's URL has the port from phase 7, if you chose one, and Safari on the phone opens
  that host and port's `/healthz`;".

**`docs/shortcut.md` §1.** After "replace it with your name.", add: "If the proxy listens on a
port other than 443, include it: `https://earmark.example.com:8443`
([docs/deploy.md](deploy.md) section 6.1.8)."

### 4.6 `docs/ARCHITECTURE.md`

- **Repo layout fence** (after `<!-- repo-layout -->`). Replace the line
  `deploy/, Dockerfile, Dockerfile.gpu, compose.yaml   part 2: deployment` with these lines.
  They were checked with `layout_entries`; keep the two-space indents, and at least two
  spaces before each description:

  ```text
  deploy/                           part 2: deployment
    caddy/Caddyfile                   the example reverse proxy, Let's Encrypt through DNS-01
    caddy/Caddyfile.own-cert          the same with a certificate you already have (step 35)
    caddy/install-cert.sh             installs that certificate on the server, from the Mac (step 35)
    systemd/                          the units for a native install
  Dockerfile, Dockerfile.gpu, compose.yaml   part 2: the images and the services
  ```

- **Module layout fence.** After `deploy/caddy/Caddyfile the example reverse proxy (step 16)`,
  add: `deploy/caddy/Caddyfile.own-cert, install-cert.sh   the own-certificate example and its
  installer, run from the Mac (step 35)`. That fence is not parsed by any test.
- **Decisions, "Reachability" row.** Replace "with a Let's Encrypt certificate from the
  DNS-01 challenge; the Cloudflare token lives only with the proxy." with "with a Let's
  Encrypt certificate from the DNS-01 challenge (the Cloudflare token lives only with the
  proxy). Or the operator's own certificate, installed by `deploy/caddy/install-cert.sh`,
  with stock Caddy, on another port if 443 is taken (step 35)."
- **Deployment, "Reverse proxy" bullet.** After its sub-list and before "The token is never
  in earmark's `.env`…", add:

  > `deploy/caddy/Caddyfile.own-cert` (step 35) is the same proxy with a certificate the
  > operator already has: `tls /etc/caddy/certs/fullchain.pem /etc/caddy/certs/privkey.pem`,
  > stock Caddy, `auto_https disable_redirects`, and a port in the site address
  > (`earmark.example.com:8443`) for a host where another proxy holds 443.
  > `deploy/caddy/install-cert.sh` runs on the Mac (bash 3.2, LibreSSL) and does three things:
  >
  > - it checks the chain and key locally: every certificate parses, the key matches, the
  >   intermediates are in order, there is a SAN, the certificate has not expired, and it
  >   warns within 30 days;
  > - it copies them over ssh into a remote `mktemp -d` that a trap removes;
  > - it installs them with sudo as root:caddy 0644 and 0640, validates the Caddyfile, and
  >   reloads Caddy.
  >
  > Renewal is rerunning it; the packaged unit's `caddy reload --force` reads replaced files.
- **"Open questions", item 4.** Replace "Only the example Caddyfile was checked, with `caddy
  validate`." with "The example Caddyfiles were checked with `caddy validate`; the
  own-certificate route (step 35) has run once on the operator's Spark, and its script is
  tested only with ssh, sudo and Caddy stubbed."
- **"Settled questions".** Add after the step 34 bullet:

  > - **Settled by step 35** (see `docs/steps/35-own-certificate.md` section 8):
  >   - a certificate the operator already has is an alternative to DNS-01, not a
  >     replacement; both examples stay;
  >   - `deploy/caddy/install-cert.sh` is the only installer, and it runs from the Mac;
  >     renewal is rerunning it, by hand;
  >   - the script refuses a chain that holds a key, is out of order, or lacks a SAN, and it
  >     warns rather than restarts when the unit's reload lacks `--force`.

## 5. Behaviour and edge cases

### 5.1 The script's fixes, in order

These were prototyped and run (section 2.3). The snippets are the shapes that were checked.

1. **Arguments.**
   - `-h|--help` prints the usage and exits 0;
   - a wrong argument count prints the usage to stderr and exits 64;
   - a host that is empty or starts with `-` is refused;
   - scp gets `--` before its file operands.
2. **No key in the chain:** `if grep -q 'PRIVATE KEY' "$chain"; then die …; fi`. This runs
   before any openssl call on the chain. `grep -q` reads a file here, not a pipe, so it is
   safe under `pipefail`.
3. **The key never prompts.** Both `openssl pkey` calls get `-passin pass: </dev/null`. An
   encrypted key then fails message 6 at once, on all three builds.
4. **Every certificate, its count and its order**, from one pipeline:

   ```bash
   names=$(openssl crl2pkcs7 -nocrl -certfile "$chain" 2>/dev/null \
       | openssl pkcs7 -print_certs -noout 2>/dev/null) \
       || die "$chain has a certificate that does not parse"
   count=$(printf '%s\n' "$names" | grep -c '^subject=' || true)
   broken=$(printf '%s\n' "$names" | awk '
       /^subject=/ { n++; subject[n] = substr($0, 9) }
       /^issuer=/ { issuer[n] = substr($0, 8) }
       END { for (i = 1; i < n; i++) if (issuer[i] != subject[i + 1]) { print i; break } }')
   ```

   The count check (message 9) comes before the order check (message 10). A root at the end
   passes: its issuer is itself, and it is never compared with a following certificate. A
   cross-signed last certificate also passes, for the same reason. The `-checkend 0` check
   (message 11) applies to the first certificate only, so an expired cross-signed root at the
   end, which clients ignore, is not refused.
5. **The SAN, without ending a pipeline early:**

   ```bash
   sans=$(openssl x509 -in "$chain" -noout -text | awk '
       found == 1 { sub(/^ +/, ""); print; found = 2 }
       /X509v3 Subject Alternative Name/ { if (!found) found = 1 }')
   [ -n "$sans" ] || die "the certificate has no Subject Alternative Name (iOS ignores the common name)"
   ```

6. **What is printed:** add `-serial` to the `-subject -issuer -enddate` line, so the runbook
   can compare it with what the server sends. The chain line becomes `chain: <n> certificates,
   in order; key matches`. Nothing prints the key. `key_pub` is the public key, and it is only
   compared.
7. **The remote directory answer** is checked before anything is copied, and before the trap
   is set:

   ```bash
   case "$remote_tmp" in
       *[!A-Za-z0-9._/-]* | */../* | */..) die "unexpected temporary directory on $host: $remote_tmp" ;;
       /tmp/?* | /var/tmp/?*) ;;
       *) die "unexpected temporary directory on $host: $remote_tmp" ;;
   esac
   ```

   A newline (login-banner output) or a quote falls into the first pattern. An odd answer is
   not removed: it is not trusted enough to `rm -rf`.
8. **Cleanup:**
   - `trap cleanup EXIT` and then `trap 'exit 130' INT TERM HUP`, so Ctrl-C at the sudo
     prompt still removes the copies;
   - `cleanup` runs `ssh "$host" "rm -rf '$remote_tmp'"`, and prints the WARNING of section
     4.2 if that fails;
   - it runs after a successful install too, when the directory is already gone. That costs
     one extra ssh, and is kept because it is simple.
9. **The remote script** keeps its shape, as one double-quoted string with single-quoted paths,
   and `set -e`, in this order:
   1. `getent group caddy`;
   2. `install -d -m 750 -o root -g caddy` the directory, then the chain 644 and the key 640;
   3. `rm -rf` the temporary directory;
   4. `sudo -u caddy test -r` the key;
   5. stop with the "no Caddyfile yet" line, exit 0, if there is none;
   6. warn if the Caddyfile does not name `$CERT_DIR/fullchain.pem`;
   7. `sudo caddy validate --adapter caddyfile --config '$CADDYFILE' >/dev/null`, or the
      error, exit 1;
   8. `Caddyfile valid`;
   9. if active: warn unless `systemctl show -p ExecReload --value caddy | grep -q -- --force`,
      then `sudo systemctl reload caddy` and `caddy reloaded`. Otherwise the start hint.

   On the remote side there is no `pipefail`, so `grep -q` ending the `systemctl show` pipe
   early is harmless.
10. **Bash 3.2.**
    - No arrays (with `set -u`, `"${a[@]}"` of an empty array fails in bash < 4.4);
    - no `[[ ]]` features beyond 3.2;
    - no `declare -A`, `mapfile`, `readarray`, `${x,,}`, `${x^^}`, `|&`, `&>>`, `;;&`,
      `coproc`, `${x@Q}`, `$EPOCHSECONDS` or `local -n`;
    - for local commands, no `sed -i`, `grep -P`, `readlink -f` or `date -d` (the BSD tools on
      the Mac differ);
    - awk: POSIX only (`substr`, `sub`, arrays), no gawk functions.
11. **LibreSSL.** The local part uses only these:
    - `openssl x509` with `-in -noout -pubkey -subject -issuer -serial -enddate -text
      -checkend`;
    - `openssl pkey` with `-in -noout -pubout -passin`;
    - `openssl crl2pkcs7` with `-nocrl -certfile`;
    - `openssl pkcs7` with `-print_certs -noout`.

    All of these were run on LibreSSL 3.3.6.

### 5.2 Things that stay as they are

- The script installs the files as given. It does not rewrite or reorder them, and does not
  strip "Bag Attributes" text. Caddy's PEM reader skips text outside blocks.
- It does not check that the certificate's names cover the API's name, because it does not
  know that name. It prints the names for the operator to read.
- It installs the chain and the key in two `install` calls. If the second one fails, rerun the
  script; Caddy is not reloaded in between.
- `--dry-run` still needs only the two files and openssl. It never calls ssh, so the host need
  not exist.

### 5.3 The example Caddyfile

- It has a single site block whose address carries an explicit port. The tests take the
  port from it, and the docs must use the same one (`8443`).
- There is no `dns` line, no `{env.…}` placeholder, and no `CF_` variable.
- `auto_https disable_redirects` is required, not optional: without it Caddy binds port 80 on
  the bind address (section 2.4).

### 5.4 Docs

- 6.1.8 is appended, not inserted, so no existing anchor or "6.1.x" reference changes.
- Route B's checks share phase 7's single `**Check:**`, labelled "Route A:" and "Route B:".
- The port appears as a placeholder (`8443`) wherever a URL is shown with one. URLs without a
  port stay as they are, for the 443 case.

### 5.5 Key material

Nothing PEM-encoded is committed. The tests write their keys under `tmp_path` and generate
them anew each run. The deploy-file scan rejects `-----BEGIN` and any unbroken base64 run of 64
or more characters in a file under `deploy/` (A1).

### 5.6 openssl in the tests

The PKI fixture asserts `shutil.which("openssl")` with the message "the install-cert tests
need the openssl command". The suite fails rather than skips without it. This sandbox has
it, CI has it, and so does every macOS.

## 6. Tests

### 6.1 The fixture: certificates generated in the test

`tests/test_install_cert.py` has a module-scoped fixture `pki` (via `tmp_path_factory`). It
writes into a directory whose name contains a space, `"my certs"`, to test quoting. It runs
`openssl` with `cwd` set to that directory, and returns a small frozen dataclass of the paths.
These commands were run on OpenSSL 3.5.7 and 3.0.13. The configs are written by the fixture,
and the CNs and SANs use example names only.

```text
req.cnf:   [req] distinguished_name = dn / x509_extensions = ca_ext / prompt = no / [dn] /
           [ca_ext] basicConstraints = critical, CA:TRUE / keyUsage = critical, keyCertSign, cRLSign
ext.cnf:   [int_ext]  basicConstraints = critical, CA:TRUE, pathlen:0 / keyUsage = critical, keyCertSign, cRLSign
           [leaf_ext] basicConstraints = critical, CA:FALSE / keyUsage = critical, digitalSignature /
                      extendedKeyUsage = serverAuth / subjectAltName = DNS:*.example.com, DNS:example.com
           [nosan_ext] basicConstraints = critical, CA:FALSE / extendedKeyUsage = serverAuth
ca.cnf:    [ca] default_ca = test / [test] database = ca/index.txt / new_certs_dir = ca/new /
           serial = ca/serial / default_md = sha256 / policy = anything / copy_extensions = none /
           unique_subject = no / [anything] commonName = supplied
           (and: mkdir ca/new; empty ca/index.txt; "06" in ca/serial)

openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out {root,int,leaf,other}.key
openssl req -x509 -new -key root.key -subj "/CN=earmark test root" -days 3650 -config req.cnf -out root.pem
openssl req -config req.cnf -new -key int.key -subj "/CN=earmark test intermediate" -out int.csr
openssl x509 -req -in int.csr -CA root.pem -CAkey root.key -set_serial 2 -days 3650 -extfile ext.cnf -extensions int_ext -out int.pem
openssl req -config req.cnf -new -key leaf.key -subj "/CN=*.example.com" -out leaf.csr
openssl x509 -req -in leaf.csr -CA int.pem -CAkey int.key -set_serial 3 -days 365 -extfile ext.cnf -extensions leaf_ext -out leaf.pem
openssl x509 -req … -set_serial 4 -days 10  -extensions leaf_ext  -out soon.pem
openssl x509 -req … -set_serial 5 -days 365 -extensions nosan_ext -out nosan.pem
openssl ca -batch -config ca.cnf -cert int.pem -keyfile int.key -in leaf.csr \
    -startdate 20200101000000Z -enddate 20210101000000Z -extfile ext.cnf -extensions leaf_ext -notext -out expired.pem
openssl pkey -in leaf.key -aes256 -passout pass:secret -out leaf-enc.key
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out rsa.key
openssl rsa -in rsa.key -traditional -out rsa-trad.key          # "BEGIN RSA PRIVATE KEY"
openssl req -config req.cnf -new -key rsa.key -subj "/CN=*.example.com" -out rsa.csr
openssl x509 -req -in rsa.csr -CA int.pem -CAkey int.key -set_serial 9 -days 365 -extfile ext.cnf -extensions leaf_ext -out rsa-leaf.pem
```

Files are concatenated in Python:

- `fullchain = leaf + int`;
- `expired_chain = expired + int`;
- `soon_chain = soon + int`;
- `nosan_chain = nosan + int`;
- `rsa_chain = rsa-leaf + int`;
- `with_root = leaf + root` (intermediate missing);
- `twice = leaf + leaf`;
- `combined = leaf.key + leaf + int`;
- `corrupt = leaf + int with one base64 character of line 5 changed to '#'`.

Each `openssl` call uses `check=True` and `capture_output=True`, with the absolute `openssl`
path. That avoids S607.

**Running the script.** One helper,
`run_script(*args, stubs, env_extra=None) -> subprocess.CompletedProcess[str]`, runs
`[BASH, SCRIPT, *args]`:

- `BASH` comes from `shutil.which("bash")` and `SCRIPT` is `ROOT / "deploy/caddy/install-cert.sh"`;
- the call uses `text=True`, `capture_output=True`, `timeout=60` and `stdin=DEVNULL`;
- the env is `PATH` (the stub `bin/` first), `STUB_ROOT`, `STUB_LOG`, `HOME` (a test
  directory), `LC_ALL=C`, `TMPDIR` when set, and `env_extra`;
- the host argument is always `server.invalid`.

A `stubs` fixture (function-scoped) builds `bin/`, the root and the log, and has a `calls()`
method that reads the JSON lines.

### 6.2 Brief criteria

**"The docs tests cover the new subsection and the runbook alternative, and the existing
placeholder scan covers the new files: no real domain, address or key material."**

- `tests/test_docs.py::test_deploy_doc_own_certificate` (D1):
  - `section(text, "#### 6.1.8 ")` exists, with the heading `#### 6.1.8 With a certificate you
    already have`;
  - it contains each of `intermediates`, `iOS rejects a bare leaf`, `Stock Caddy`, `no
    Cloudflare token`, `install-cert.sh --dry-run`, `Caddyfile.own-cert`, `run the script
    again`, `--force`, `443`, `https://earmark.example.com:8443/transcriptions`, `curl`,
    `EARMARK_API_URL`, `disable_redirects`, `root:caddy 0640` and `root:caddy 0644`;
  - `section(text, "#### 6.1.3 ")` and `section(text, "#### 6.1.4 ")` each contain `6.1.8`.
- `tests/test_docs.py::test_runbook_phase_7_offers_own_certificate` (D2). Split
  `_phase(7)` at `**Check:**`:
  - the body has `route A`, `route B`, `deploy.md#618-with-a-certificate-you-already-have`,
    `Caddyfile.own-cert`, `install-cert.sh --dry-run`, `sudo apt install caddy`, `ss -ltnp`
    and `cloudflare.env` (route A kept);
  - the check has `Route A:`, `Route B:`, `s_client`, `-showcerts`, `BEGIN CERTIFICATE`,
    `-serial`, `privkey.pem`, `-rw-r-----`, `@1.1.1.1` and `in order; key matches`.
- `tests/test_docs.py::test_runbook_and_docs_name_the_port` (D3):
  - the port is read from `Caddyfile.own-cert`'s site address (regex
    `^earmark\.example\.com:(\d+) \{$`, multiline), and is `8443`;
  - `f"https://earmark.example.com:{port}/healthz"` is in phase 7;
  - `f"https://earmark.example.com:{port}/transcriptions"` is in phase 9 and in deploy 6.1.8;
  - `f"https://earmark.example.com:{port}"` is in `docs/shortcut.md`.
- `tests/test_docs.py::test_install_cert_usage_matches_docs` (D4):
  - for every line inside a `bash` fence of `DOCS` that contains `install-cert.sh`,
    `shlex.split` it. The words after the script are an optional `--dry-run` and then exactly
    three operands, none starting with `-`;
  - at least four such lines exist (two in deploy 6.1.8, two in phase 7);
  - the script's header contains the usage line of section 4.1 exactly.
- `tests/test_docs.py::test_deploy_files_use_placeholders_only` (A1), parametrized over
  every regular file under `deploy/` (sorted `rglob`, ids by relative path). Each file:
  - passes `_assert_placeholders`;
  - passes the new `_assert_example_hosts`: every match of
    `\b(?:[a-z0-9-]+\.)+(?:com|org|net|io|dev|app|cloud|me|co|uk|de|ai)\b` (case-insensitive;
    no `sh`) is `example.com`, `example.org`, `example.net`, a subdomain of one of them, or
    `github.com`;
  - contains no `-----BEGIN`, and nothing matching `[A-Za-z0-9+/]{64,}`.

  A companion `test_deploy_file_scan_catches_real_names` feeds `_assert_example_hosts` the
  strings `earmark.realhost.org` and `tls /x/real.example.co` and expects `AssertionError`. It
  also expects one for a `-----BEGIN PRIVATE KEY-----` line given to the key-material check,
  so the scan is shown to fail.
- `PLANS` gains `"docs/steps/35-own-certificate.md"`, so `test_docs_use_placeholders_only`
  scans this plan.

**"A test runs the script's local checks against certificates generated in the test (a
matching chain passes `--dry-run`; a bare leaf, a mismatched key and an expired certificate
each fail with their message), with no ssh and no network."** All are in
`tests/test_install_cert.py`. Every test asserts that the stub log is empty after a
`--dry-run`, which proves ssh and scp were never called. No socket is opened: pytest-socket
stays on, and the subprocesses only run openssl, which reads files.

- `test_dry_run_accepts_a_matching_chain`:
  - exit 0;
  - stdout has `names: DNS:*.example.com, DNS:example.com`, `serial=`, `notAfter=`,
    `chain: 2 certificates, in order; key matches` and `dry run: checks passed, nothing
    copied to server.invalid`;
  - stderr is empty, and no stub was called.
- `test_dry_run_refuses_a_bare_leaf`: `leaf.pem` with `leaf.key` exits 1, with `has 1
  certificate(s); it needs the intermediates too (iOS rejects a bare leaf)`.
- `test_dry_run_refuses_a_mismatched_key`: `fullchain` with `other.key` exits 1, with `the
  key does not match the first certificate in`.
- `test_dry_run_refuses_an_expired_certificate`: `expired_chain` with `leaf.key` exits 1,
  with `the certificate has expired`.

### 6.3 Criteria this plan adds (script)

- `test_dry_run_warns_within_30_days`: `soon_chain` exits 0, with `WARNING: expires within 30
  days` on stderr.
- `test_dry_run_accepts_a_traditional_rsa_key`: `rsa_chain` with `rsa-trad.key` exits 0.
- `test_dry_run_refusals`, parametrized with `(chain, key, message)` cases:
  - `combined` gives `contains a private key`;
  - `leaf-enc.key` gives `is not a readable unencrypted PEM private key`;
  - `with_root` gives `certificate 1 in … was not issued by certificate 2`;
  - `twice` gives the same;
  - `corrupt` gives `has a certificate that does not parse`;
  - `nosan_chain` gives `has no Subject Alternative Name`;
  - a missing path gives `no such file:`;
  - `leaf.key` as the chain gives `contains a private key`.

  Each exits 1, prints the message on stderr with the `install-cert: ` prefix, and calls no
  stub.
- `test_dry_run_refuses_an_option_as_host`: host `-oProxyCommand=x` exits 1 with `not an ssh
  host`, and calls no stub.
- `test_usage`: no arguments exits 64, with `Usage:` on stderr; `--help` exits 0, with `Usage:`
  and `--dry-run` on stdout and no `set -euo`.
- `test_full_run_installs_the_files` (Caddyfile present, written from `Caddyfile.own-cert` with
  `/etc/caddy` replaced by `STUB_ROOT/etc/caddy`; Caddy active). It exits 0, and stdout has
  `Caddyfile valid` and `caddy reloaded`. Then:
  - the modes are `STUB_ROOT/etc/caddy/certs/fullchain.pem` 0o644, `privkey.pem` 0o640, and
    the directory 0o750, and both files' bytes equal the inputs;
  - each `install` call in the log has `-o root -g caddy`;
  - there was one `caddy validate --adapter caddyfile --config …/etc/caddy/Caddyfile` call and
    one `systemctl reload caddy` call;
  - the calls start `ssh`, `scp`, `scp`, `ssh -t`, and the last call is the `ssh` `rm -rf`;
  - `STUB_ROOT/tmp/tmp.EarmarkTest1` does not exist afterwards.
- `test_full_run_never_prints_the_key`, on the same run:
  - no line of the key file's base64 body, and not `PRIVATE KEY`, appears in stdout, in
    stderr, or in any logged argument;
  - the key's path appears only in the second `scp` call.
- `test_full_run_without_caddyfile`: exit 0, with `Caddyfile yet` on stdout. The files are
  installed, and there is no `caddy` call.
- `test_full_run_with_caddy_stopped`: `STUB_ACTIVE_RC=3` exits 0, with `caddy is not
  running`, and no `reload` call.
- `test_full_run_validate_failure`: `STUB_VALIDATE_RC=1` exits non-zero, with `does not
  validate; caddy was not reloaded`. There is no `reload` call, and the temporary directory
  is gone.
- `test_full_run_warns_without_force`: `STUB_EXEC_RELOAD` without `--force` gives the
  `reloads without --force` warning on stderr, and still a `reload` call.
- `test_full_run_warns_when_caddyfile_ignores_the_files`: the Caddyfile is the DNS-01
  `deploy/caddy/Caddyfile`, mapped the same way, and `STUB_VALIDATE_RC=0`. The result is the
  `does not use` warning.
- `test_full_run_without_caddy_group`: `STUB_GETENT_RC=2` exits non-zero, with `no caddy
  group`. There is no `install` call, and the temporary directory is gone.
- `test_failed_copy_removes_the_remote_directory`: `STUB_FAIL=scp` exits non-zero. There is
  no `ssh -t` call, the last call is the `rm -rf` ssh, and the directory is gone.
- `test_unexpected_remote_directory_is_refused`, parametrized over `STUB_MKTEMP` in
  `["/home/op/tmp.1", "/tmp/a'b", "/tmp/", "/tmp/../etc", "banner\n/tmp/tmp.x", ""]`: each
  exits 1 with `unexpected temporary directory`, and the log holds exactly one call (the
  `mktemp` ssh), with no `scp` and no `rm`.

  For `""`: `$(…)` gives an empty string, which the `*)` branch refuses.
- **Static checks:**
  - `test_script_is_executable_bash`:
    - the shebang is `#!/usr/bin/env bash`;
    - `os.access(SCRIPT, os.X_OK)`;
    - `set -euo pipefail` is on a line of its own;
    - `[BASH, "-n", SCRIPT]` exits 0.
  - `test_script_avoids_newer_bash` scans the whole file. It allows no match of `declare -A`,
    `local -n`, `mapfile`, `readarray`, `\$\{\w+(,,|\^\^|,|\^)\}`, `\|&`, `&>>`, `;;&`,
    `coproc`, `\$\{\w+@`, `EPOCH(SECONDS|REALTIME)`, `wait -n`, `\w+=\(`, `\[@\]`,
    `sed -i`, `grep -P`, `readlink -f`, `date -d`, `gensub`, `strftime` or `asort`.
  - `test_script_uses_libressl_options`:
    - for each line of the script before the line that starts `ssh -t "$host"`, each `openssl
      <sub>` invocation's `-word` tokens, up to the next `|`, `)`, `>` or `<`, must be in the
      allowed set of section 5.1, item 11;
    - all four subcommands must appear;
    - `-ext` must not appear anywhere.
  - `test_every_pkey_call_sets_passin`: every line with `openssl pkey` also has
    `-passin pass:`.
  - `test_script_calls_ssh_by_name`: there is no `/bin/ssh`, `/bin/scp`, `/usr/bin/ssh` or
    `/usr/bin/scp`, so the stubs on `PATH` always win.

### 6.4 Criteria this plan adds (Caddyfile and docs)

- `tests/test_deploy.py::test_caddyfile_own_cert_example`. Parse it like
  `test_caddyfile_example` (non-comment lines, stripped):
  - there is exactly one site block, whose address matches
    `earmark\.example\.com:(\d+)`, with a port in 1024–65535 other than 8787;
  - `bind` is a private IPv4 address, not `0.0.0.0`;
  - `tls {CERT_DIR}/fullchain.pem {CERT_DIR}/privkey.pem` is a code line, where `CERT_DIR`
    is read from the script's `^CERT_DIR=(\S+)$` line;
  - `reverse_proxy 127.0.0.1:8787` and `auto_https disable_redirects` are code lines;
  - there is no `dns ` line, and the file has no `{env.` and no `CF_`;
  - a comment line mentions `443`, so the port's reason is stated;
  - `http://` does not appear.
- `tests/test_docs.py::test_architecture_repo_layout_matches_repo` (unchanged) passes with
  the new entries. Its `brief` set gains `"deploy/caddy/Caddyfile.own-cert"` and
  `"deploy/caddy/install-cert.sh"`. That is the brief's "The repo layout test passes with the
  new files listed".
- `tests/test_docs.py::test_architecture_names_own_certificate`:
  - the Reachability row (`_table` over Decisions, or a `section` read) contains
    `install-cert.sh` and `step 35`;
  - `"Settled by step 35"` is in the architecture.

### 6.5 "`make check` passes"

`make check` and `GITHUB_ACTIONS=true make check`. The new tests add a few seconds. The RSA
key is the slowest openssl call, and the fixture is module-scoped, so it runs once.

## 7. Verification

```bash
cd /home/sandy/dev/earmark
export TMPDIR=/home/sandy/.cache/earmark-step35-verify && mkdir -p "$TMPDIR"
make check
GITHUB_ACTIONS=true make check
uv run pytest tests/test_install_cert.py tests/test_deploy.py tests/test_docs.py -q
git status --short          # the files of section 3 only
git ls-files -s deploy/caddy  # after the orchestrator's git add: install-cert.sh is 100755
```

**Smoke: the dry run, by hand, with generated certificates.** This contacts no host:
`--dry-run` never calls ssh, and the stubs are first on `PATH` anyway, so an accidental call
would be logged, not sent.

```bash
d=$(mktemp -d) && cd "$d"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout ca.key -out ca.pem \
  -subj "/CN=smoke root" -days 30 -addext basicConstraints=critical,CA:TRUE
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout leaf.key -out leaf.csr \
  -subj "/CN=earmark.example.com"
printf 'subjectAltName=DNS:earmark.example.com\n' > san.cnf
openssl x509 -req -in leaf.csr -CA ca.pem -CAkey ca.key -set_serial 7 -days 20 -extfile san.cnf -out leaf.pem
cat leaf.pem ca.pem > fullchain.pem
mkdir stub && for n in ssh scp; do printf '#!/bin/sh\necho "STUB %s $*" >> %s/stub.log; exit 97\n' $n "$d" > stub/$n; chmod +x stub/$n; done
PATH="$d/stub:$PATH" /home/sandy/dev/earmark/deploy/caddy/install-cert.sh --dry-run server.invalid fullchain.pem leaf.key; echo "exit $?"
PATH="$d/stub:$PATH" /home/sandy/dev/earmark/deploy/caddy/install-cert.sh --dry-run server.invalid leaf.pem leaf.key; echo "exit $?"
PATH="$d/stub:$PATH" /home/sandy/dev/earmark/deploy/caddy/install-cert.sh --dry-run server.invalid fullchain.pem ca.key; echo "exit $?"
test ! -e stub.log && echo "no ssh or scp was called"
cd / && rm -rf "$d"
```

Expected:

- the first run exits 0, prints `names: DNS:earmark.example.com`, `chain: 2 certificates, in
  order; key matches` and the dry-run line, and prints the 30-day WARNING (the leaf is valid
  for 20 days);
- the second exits 1 with the bare-leaf message, and the third with the key-mismatch message;
- the last line prints "no ssh or scp was called".

Here the chain is leaf plus root, and the order check passes: the leaf's issuer is the root's
subject.

**Optional, if the verifier has network: validate the example on the Caddy versions named.**

```bash
mkdir -p "$TMPDIR/caddy" && cd "$TMPDIR/caddy"
curl -sSfLO https://github.com/caddyserver/caddy/releases/download/v2.11.9/caddy_2.11.9_linux_arm64.deb
dpkg-deb -x caddy_2.11.9_linux_arm64.deb root
# reuse fullchain.pem and leaf.key from a smoke dir kept for this purpose
sed "s#/etc/caddy/certs/fullchain.pem#$PWD/fullchain.pem#; s#/etc/caddy/certs/privkey.pem#$PWD/leaf.key#" \
  /home/sandy/dev/earmark/deploy/caddy/Caddyfile.own-cert > Caddyfile.test
HOME=$PWD ./root/usr/bin/caddy validate --adapter caddyfile --config Caddyfile.test   # Valid configuration
HOME=$PWD ./root/usr/bin/caddy fmt /home/sandy/dev/earmark/deploy/caddy/Caddyfile.own-cert \
  | diff - /home/sandy/dev/earmark/deploy/caddy/Caddyfile.own-cert && echo "fmt clean"
```

For 2.6.2, use `https://ports.ubuntu.com/ubuntu-ports/pool/universe/c/caddy/caddy_2.6.2-6ubuntu0.24.04.3_arm64.deb`.

**Not verifiable here, and to be said so:**

- the real remote half (ssh, sudo, the real `install`, Caddy under systemd);
- the phone's handling of the chain.

The operator ran the remote half once on the Spark. Bash 3.2 and LibreSSL were run in
planning only (section 2). CI runs bash 5 with OpenSSL 3.0, and the static tests of section
6.3 guard the rest.

Finally, `rm -rf "$TMPDIR"`.

## 8. Deviations and decisions

### 8.1 P1: stricter checks (product; the brief says "may fix it, keeping that behaviour")

The brief lists four local checks, and all of them stay. This plan adds four refusals:

- a key inside the chain file. It is a security fix: that file is installed 0644;
- every certificate must parse;
- the chain must be in order, each certificate issued by the next. Otherwise a leaf plus a
  stray root, or the leaf twice, passes the count without the intermediates the brief asks
  for;
- the leaf must have a SAN. Today a SAN-less certificate ends the script with no message.

The alternative was to warn instead of refusing. It was not taken: each case makes a server
that iOS rejects, or exposes the key, and the operator only learns of it from the phone. A
correctly ordered commercial chain passes, with or without the root at the end.

### 8.2 P2: reload without `--force` warns (product)

Both packaged units reload with `--force` (section 2.4), so the warning should never fire on
the operator's Caddy. Restarting automatically would drop connections for every site that
Caddy fronts. The operator's Caddy fronts only earmark, but the script cannot know that. A
warning naming the command keeps the operator in charge.

### 8.3 Engineering choices

- **D1. 6.1.8, not a renumbered 6.1.5.** Appending keeps every anchor and every "6.1.x"
  reference stable (section 2.5). 6.1.3 and 6.1.4 point to it.
- **D2. The remote half is tested with stubs** that run the real remote commands against a
  fake root. That goes beyond the brief, which asks for the local checks only. It is the
  only way to test the quoting of the remote script, the trap, and the modes without a host.
  No real ssh, sudo or Caddy runs.
- **D3. Bash 3.2 and LibreSSL are guarded statically in CI**, by banned constructs and
  allowed openssl options, and were run for real in planning. Building them in CI would cost
  minutes per run, for a script that changes rarely.
- **D4. The placeholder scan is extended to `deploy/`**, with a host-name and key-material
  check, because `_assert_placeholders` checks neither host names nor PEM blocks
  (section 2.5). The brief assumed the existing scan already covered the new files; it did
  not.
- **D5. The generated chain uses EC keys, plus one RSA PKCS#1 case.** EC is fast; the RSA case
  covers the key format commercial CAs often hand out.
- **D6. Docs beyond the brief's list:** one sentence in `docs/shortcut.md` (the brief says the
  port "appears in the Shortcut's URL"), the runbook's intro and "Your values", and
  `ARCHITECTURE.md`'s Reachability row, Reverse proxy bullet and open question 4, which
  would otherwise describe DNS-01 as the only route.

### 8.4 Dependencies

None. openssl and bash are already on every machine the suite runs on. `cryptography` was
considered for generating certificates and rejected (section 2.7).

### 8.5 `docs/ARCHITECTURE.md`

This plan does not edit it. The implementer makes the changes of section 4.6, which describe
a new route and two files, not a changed contract.

## 9. Out of scope

- Automatic renewal, reminders, or a timer. Renewal stays manual, as the brief says.
- Checking that the certificate's names cover the API's name. The script cannot know the
  name.
- Running the script anywhere but the Mac, or against Caddy in Docker. The packaged Caddy
  with systemd is the route described.
- Traefik, Tailscale Serve, or any change to the Let's Encrypt route beyond the pointers.
- A `doctor` check of the certificate, and anything in `src/`.
- Step 31 (the Shortcut file), which will carry the URL with the port when it is built.
