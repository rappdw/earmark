#!/usr/bin/env bash
# Copy an existing TLS certificate (full chain + key) to the Spark for Caddy.
#
# Runs on the Mac (bash 3.2 and LibreSSL are fine). Checks the files locally, copies
# them over ssh into a private temporary directory, installs them as
#   /etc/caddy/certs/fullchain.pem  (root:caddy 0644)
#   /etc/caddy/certs/privkey.pem    (root:caddy 0640)
# then validates the Caddyfile and reloads Caddy. sudo on the Spark asks for a password
# if it needs one. The key is never printed.
#
# The chain file is the server's certificate, then each intermediate, in that order.
# The key must be unencrypted. --dry-run stops after the local checks and never calls ssh.
#
# Usage:
#   deploy/caddy/install-cert.sh [--dry-run] <ssh-host> <fullchain.pem> <private.key>
#   deploy/caddy/install-cert.sh -h | --help
# Example:
#   deploy/caddy/install-cert.sh spark ~/ssl/example.com.fullchain.pem ~/ssl/example.com.key
#
# The Caddyfile's site block then uses:
#   tls /etc/caddy/certs/fullchain.pem /etc/caddy/certs/privkey.pem
set -euo pipefail

CERT_DIR=/etc/caddy/certs
CADDYFILE=/etc/caddy/Caddyfile

# The header comment, without its "# ": from line 2 to the line above the set line.
print_usage() {
    sed -n '2,/^set -euo pipefail$/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
}

die() {
    echo "install-cert: $*" >&2
    exit 1
}

case "${1:-}" in
    -h | --help)
        print_usage
        exit 0
        ;;
esac

dry_run=0
if [ "${1:-}" = "--dry-run" ]; then
    dry_run=1
    shift
fi
if [ "$#" -ne 3 ]; then
    print_usage >&2
    exit 64
fi
host=$1
chain=$2
key=$3

case "$host" in
    '' | -*) die "not an ssh host: $host" ;;
esac
[ -f "$chain" ] || die "no such file: $chain"
[ -f "$key" ] || die "no such file: $key"
command -v openssl >/dev/null || die "openssl not found"

# --- local checks: nothing here prints the key -------------------------------------

# The chain file is installed world-readable, so a key in it must never get that far.
if grep -q 'PRIVATE KEY' "$chain"; then
    die "$chain contains a private key; give the certificates and the key as two files"
fi

openssl x509 -in "$chain" -noout >/dev/null 2>&1 || die "$chain is not a PEM certificate"
# -passin pass: and no stdin: an encrypted key fails here instead of prompting.
openssl pkey -in "$key" -noout -passin pass: </dev/null >/dev/null 2>&1 \
    || die "$key is not a readable unencrypted PEM private key"

cert_pub=$(openssl x509 -in "$chain" -noout -pubkey)
key_pub=$(openssl pkey -in "$key" -pubout -passin pass: </dev/null)
[ "$cert_pub" = "$key_pub" ] \
    || die "the key does not match the first certificate in $chain (the server's certificate comes first)"

# Every certificate in the chain, with its subject and issuer, from one pipeline.
names=$(openssl crl2pkcs7 -nocrl -certfile "$chain" 2>/dev/null \
    | openssl pkcs7 -print_certs -noout 2>/dev/null) \
    || die "$chain has a certificate that does not parse"
count=$(printf '%s\n' "$names" | grep -c '^subject=' || true)
[ "$count" -ge 2 ] \
    || die "$chain has $count certificate(s); it needs the intermediates too (iOS rejects a bare leaf)"

# Certificate i must have been issued by certificate i+1. The last one is never compared
# with a following certificate, so a root at the end passes. Read all input: no early exit.
broken=$(printf '%s\n' "$names" | awk '
    /^subject=/ { n++; subject[n] = substr($0, 9) }
    /^issuer=/ { issuer[n] = substr($0, 8) }
    END { for (i = 1; i < n; i++) if (issuer[i] != subject[i + 1]) { print i; break } }')
if [ -n "$broken" ]; then
    next=$((broken + 1))
    die "certificate $broken in $chain was not issued by certificate $next; the order is the server's certificate, then each intermediate"
fi

openssl x509 -in "$chain" -noout -checkend 0 >/dev/null || die "the certificate has expired"

# The SAN line follows its header. Read all input: no early exit in a pipeline.
sans=$(openssl x509 -in "$chain" -noout -text | awk '
    found == 1 { sub(/^ +/, ""); print; found = 2 }
    /X509v3 Subject Alternative Name/ { if (!found) found = 1 }')
[ -n "$sans" ] || die "the certificate has no Subject Alternative Name (iOS ignores the common name)"

echo "certificate:"
openssl x509 -in "$chain" -noout -subject -issuer -serial -enddate | sed 's/^/  /'
echo "  names: $sans"
echo "  chain: $count certificates, in order; key matches"
if ! openssl x509 -in "$chain" -noout -checkend $((30 * 24 * 3600)) >/dev/null; then
    echo "  WARNING: expires within 30 days" >&2
fi

if [ "$dry_run" -eq 1 ]; then
    echo "dry run: checks passed, nothing copied to $host"
    exit 0
fi

# --- copy and install --------------------------------------------------------------

remote_tmp=$(ssh "$host" 'umask 077 && mktemp -d')
# The answer is pasted into quoted remote commands, rm -rf among them: trust only a plain
# path under /tmp or /var/tmp. An odd answer is not removed either.
case "$remote_tmp" in
    *[!A-Za-z0-9._/-]* | */../* | */..) die "unexpected temporary directory on $host: $remote_tmp" ;;
    /tmp/?* | /var/tmp/?*) ;;
    *) die "unexpected temporary directory on $host: $remote_tmp" ;;
esac
cleanup() {
    if ! ssh "$host" "rm -rf '$remote_tmp'" >/dev/null 2>&1; then
        echo "install-cert: WARNING: could not remove $remote_tmp on $host; remove it by hand" >&2
    fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

echo "copying to $host ..."
scp -q -- "$chain" "$host:$remote_tmp/fullchain.pem"
scp -q -- "$key" "$host:$remote_tmp/privkey.pem"

echo "installing on $host (sudo) ..."
ssh -t "$host" "
set -e
getent group caddy >/dev/null || { echo 'install-cert: no caddy group on this host; install Caddy first' >&2; exit 1; }
sudo install -d -m 750 -o root -g caddy '$CERT_DIR'
sudo install -m 644 -o root -g caddy '$remote_tmp/fullchain.pem' '$CERT_DIR/fullchain.pem'
sudo install -m 640 -o root -g caddy '$remote_tmp/privkey.pem' '$CERT_DIR/privkey.pem'
rm -rf '$remote_tmp'
sudo -u caddy test -r '$CERT_DIR/privkey.pem' || { echo 'install-cert: the caddy user cannot read $CERT_DIR/privkey.pem' >&2; exit 1; }
if [ ! -f '$CADDYFILE' ]; then
    echo 'no $CADDYFILE yet: copy deploy/caddy/Caddyfile.own-cert there, edit it, then run this again'
    exit 0
fi
grep -qF '$CERT_DIR/fullchain.pem' '$CADDYFILE' || echo 'install-cert: WARNING: $CADDYFILE does not use $CERT_DIR/fullchain.pem' >&2
sudo caddy validate --adapter caddyfile --config '$CADDYFILE' >/dev/null || { echo 'install-cert: $CADDYFILE does not validate; caddy was not reloaded' >&2; exit 1; }
echo 'Caddyfile valid'
if systemctl is-active --quiet caddy; then
    systemctl show -p ExecReload --value caddy | grep -q -- --force || echo 'install-cert: WARNING: this caddy unit reloads without --force, which keeps the old certificate; run sudo systemctl restart caddy' >&2
    sudo systemctl reload caddy
    echo 'caddy reloaded'
else
    echo 'caddy is not running; start it with: sudo systemctl start caddy'
fi
"
echo "done. From the Mac: curl https://<your earmark name>[:port]/healthz"
