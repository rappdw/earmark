"""deploy/caddy/install-cert.sh, run against certificates generated in the test.

ssh, scp, sudo, install, getent, caddy and systemctl are stubs (tests/install_cert_stub.py)
that run the script's remote commands against a fake root, so nothing leaves the machine.
The host is always server.invalid, which can never resolve.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.doc_helpers import ROOT

SCRIPT = ROOT / "deploy" / "caddy" / "install-cert.sh"
STUB = ROOT / "tests" / "install_cert_stub.py"
OWN_CERT_CADDYFILE = ROOT / "deploy" / "caddy" / "Caddyfile.own-cert"
DNS_CADDYFILE = ROOT / "deploy" / "caddy" / "Caddyfile"
BASH = shutil.which("bash") or "bash"
HOST = "server.invalid"
STUB_NAMES = ("ssh", "scp", "sudo", "install", "getent", "caddy", "systemctl")
MKTEMP = "/tmp/tmp.EarmarkTest1"

REQ_CNF = """\
[req]
distinguished_name = dn
x509_extensions = ca_ext
prompt = no
[dn]
CN = unused
[ca_ext]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
"""
EXT_CNF = """\
[int_ext]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
[leaf_ext]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = DNS:*.example.com, DNS:example.com
[nosan_ext]
basicConstraints = critical, CA:FALSE
extendedKeyUsage = serverAuth
"""
CA_CNF = """\
[ca]
default_ca = test
[test]
database = ca/index.txt
new_certs_dir = ca/new
serial = ca/serial
default_md = sha256
policy = anything
copy_extensions = none
unique_subject = no
[anything]
commonName = supplied
"""


@dataclass(frozen=True)
class Pki:
    directory: Path
    leaf: Path
    leaf_key: Path
    other_key: Path
    encrypted_key: Path
    rsa_key: Path
    fullchain: Path
    expired_chain: Path
    soon_chain: Path
    nosan_chain: Path
    rsa_chain: Path
    with_root: Path
    twice: Path
    combined: Path
    corrupt: Path


def _openssl(directory: Path, *args: str) -> None:
    openssl = shutil.which("openssl")
    assert openssl, "the install-cert tests need the openssl command"
    subprocess.run([openssl, *args], cwd=directory, check=True, capture_output=True)


def _concat(target: Path, *parts: Path) -> Path:
    target.write_bytes(b"".join(part.read_bytes() for part in parts))
    return target


def _sign(d: Path, csr: str, ca: str, serial: int, days: int, ext: str, out: str) -> None:
    _openssl(
        d,
        *("x509", "-req", "-in", csr, "-CA", f"{ca}.pem", "-CAkey", f"{ca}.key"),
        *("-set_serial", str(serial), "-days", str(days)),
        *("-extfile", "ext.cnf", "-extensions", ext, "-out", out),
    )


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Pki:
    d = tmp_path_factory.mktemp("pki") / "my certs"  # a space, to test quoting
    d.mkdir()
    (d / "req.cnf").write_text(REQ_CNF)
    (d / "ext.cnf").write_text(EXT_CNF)
    (d / "ca.cnf").write_text(CA_CNF)
    (d / "ca" / "new").mkdir(parents=True)
    (d / "ca" / "index.txt").write_text("")
    (d / "ca" / "serial").write_text("06\n")

    for name in ("root", "int", "leaf", "other"):
        _openssl(
            d,
            *("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256"),
            *("-out", f"{name}.key"),
        )
    _openssl(
        d,
        *("req", "-x509", "-new", "-key", "root.key", "-subj", "/CN=earmark test root"),
        *("-days", "3650", "-config", "req.cnf", "-out", "root.pem"),
    )
    _openssl(
        d,
        *("req", "-config", "req.cnf", "-new", "-key", "int.key"),
        *("-subj", "/CN=earmark test intermediate", "-out", "int.csr"),
    )
    _sign(d, "int.csr", "root", 2, 3650, "int_ext", "int.pem")
    _openssl(
        d,
        *("req", "-config", "req.cnf", "-new", "-key", "leaf.key"),
        *("-subj", "/CN=*.example.com", "-out", "leaf.csr"),
    )
    _sign(d, "leaf.csr", "int", 3, 365, "leaf_ext", "leaf.pem")
    _sign(d, "leaf.csr", "int", 4, 10, "leaf_ext", "soon.pem")
    _sign(d, "leaf.csr", "int", 5, 365, "nosan_ext", "nosan.pem")
    # An expired certificate: openssl x509 cannot make one (no negative -days before 3.4).
    _openssl(
        d,
        *("ca", "-batch", "-config", "ca.cnf", "-cert", "int.pem", "-keyfile", "int.key"),
        *("-in", "leaf.csr", "-startdate", "20200101000000Z", "-enddate", "20210101000000Z"),
        *("-extfile", "ext.cnf", "-extensions", "leaf_ext", "-notext", "-out", "expired.pem"),
    )
    _openssl(
        d, "pkey", "-in", "leaf.key", "-aes256", "-passout", "pass:secret", "-out", "leaf-enc.key"
    )
    _openssl(
        d,
        *("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", "rsa.key"),
    )
    # "BEGIN RSA PRIVATE KEY": the format commercial CAs often hand out.
    _openssl(d, "rsa", "-in", "rsa.key", "-traditional", "-out", "rsa-trad.key")
    _openssl(
        d,
        *("req", "-config", "req.cnf", "-new", "-key", "rsa.key"),
        *("-subj", "/CN=*.example.com", "-out", "rsa.csr"),
    )
    _sign(d, "rsa.csr", "int", 9, 365, "leaf_ext", "rsa-leaf.pem")

    leaf, int_ = d / "leaf.pem", d / "int.pem"
    corrupt = _concat(d / "corrupt.pem", leaf, int_)
    lines = corrupt.read_text().splitlines()
    first_int_line = lines.index("-----BEGIN CERTIFICATE-----", 1)
    target = first_int_line + 4  # a line of base64 in the second certificate
    lines[target] = "#" + lines[target][1:]
    corrupt.write_text("\n".join(lines) + "\n")
    return Pki(
        directory=d,
        leaf=leaf,
        leaf_key=d / "leaf.key",
        other_key=d / "other.key",
        encrypted_key=d / "leaf-enc.key",
        rsa_key=d / "rsa-trad.key",
        fullchain=_concat(d / "fullchain.pem", leaf, int_),
        expired_chain=_concat(d / "expired-chain.pem", d / "expired.pem", int_),
        soon_chain=_concat(d / "soon-chain.pem", d / "soon.pem", int_),
        nosan_chain=_concat(d / "nosan-chain.pem", d / "nosan.pem", int_),
        rsa_chain=_concat(d / "rsa-chain.pem", d / "rsa-leaf.pem", int_),
        with_root=_concat(d / "with-root.pem", leaf, d / "root.pem"),
        twice=_concat(d / "twice.pem", leaf, leaf),
        combined=_concat(d / "combined.pem", d / "leaf.key", leaf, int_),
        corrupt=corrupt,
    )


# --- running the script with the stubs -----------------------------------------------------


@dataclass
class Stubs:
    bin: Path
    root: Path
    log: Path
    home: Path

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def top_level(self) -> list[list[str]]:
        """The ssh and scp calls the script made itself, not the ones inside the remote shell."""
        return [c for c in self.calls() if c[0] in ("ssh", "scp")]

    def named(self, name: str) -> list[list[str]]:
        return [c for c in self.calls() if c[0] == name]


@pytest.fixture
def stubs(tmp_path: Path) -> Stubs:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in STUB_NAMES:
        wrapper = bin_dir / name
        wrapper.write_text(
            "#!/bin/sh\n"
            f'exec {shlex.quote(sys.executable)} -I {shlex.quote(str(STUB))} {name} "$@"\n'
        )
        wrapper.chmod(0o755)
    root = tmp_path / "root"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    log = tmp_path / "stub.log"
    log.write_text("")
    return Stubs(bin=bin_dir, root=root, log=log, home=home)


def run_script(
    *args: str, stubs: Stubs, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{stubs.bin}{os.pathsep}{os.environ['PATH']}",
        "STUB_ROOT": str(stubs.root),
        "STUB_LOG": str(stubs.log),
        "HOME": str(stubs.home),
        "LC_ALL": "C",
    }
    if "TMPDIR" in os.environ:
        env["TMPDIR"] = os.environ["TMPDIR"]
    env.update(env_extra or {})
    return subprocess.run(
        [BASH, str(SCRIPT), *args],
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        check=False,
    )


def dry(chain: Path, key: Path, stubs: Stubs, host: str = HOST) -> subprocess.CompletedProcess[str]:
    return run_script("--dry-run", host, str(chain), str(key), stubs=stubs)


# --- the local checks (--dry-run) ------------------------------------------------------------


def test_dry_run_accepts_a_matching_chain(pki: Pki, stubs: Stubs) -> None:
    result = dry(pki.fullchain, pki.leaf_key, stubs)
    assert result.returncode == 0, result.stderr
    for needle in (
        "names: DNS:*.example.com, DNS:example.com",
        "serial=",
        "notAfter=",
        "chain: 2 certificates, in order; key matches",
        "dry run: checks passed, nothing copied to server.invalid",
    ):
        assert needle in result.stdout, needle
    assert result.stderr == ""
    assert stubs.calls() == []


def test_dry_run_refuses_a_bare_leaf(pki: Pki, stubs: Stubs) -> None:
    result = dry(pki.leaf, pki.leaf_key, stubs)
    assert result.returncode == 1
    assert (
        "has 1 certificate(s); it needs the intermediates too (iOS rejects a bare leaf)"
        in result.stderr
    )
    assert stubs.calls() == []


def test_dry_run_refuses_a_mismatched_key(pki: Pki, stubs: Stubs) -> None:
    result = dry(pki.fullchain, pki.other_key, stubs)
    assert result.returncode == 1
    assert "the key does not match the first certificate in" in result.stderr
    assert stubs.calls() == []


def test_dry_run_refuses_an_expired_certificate(pki: Pki, stubs: Stubs) -> None:
    result = dry(pki.expired_chain, pki.leaf_key, stubs)
    assert result.returncode == 1
    assert "the certificate has expired" in result.stderr
    assert stubs.calls() == []


def test_dry_run_warns_within_30_days(pki: Pki, stubs: Stubs) -> None:
    result = dry(pki.soon_chain, pki.leaf_key, stubs)
    assert result.returncode == 0, result.stderr
    assert "WARNING: expires within 30 days" in result.stderr
    assert stubs.calls() == []


def test_dry_run_accepts_a_traditional_rsa_key(pki: Pki, stubs: Stubs) -> None:
    assert "BEGIN RSA PRIVATE KEY" in pki.rsa_key.read_text()
    result = dry(pki.rsa_chain, pki.rsa_key, stubs)
    assert result.returncode == 0, result.stderr
    assert "key matches" in result.stdout


@pytest.mark.parametrize(
    ("chain_name", "key_name", "message"),
    [
        ("combined", "leaf_key", "contains a private key"),
        ("fullchain", "encrypted_key", "is not a readable unencrypted PEM private key"),
        ("with_root", "leaf_key", "certificate 1 in "),
        ("twice", "leaf_key", "was not issued by certificate 2"),
        ("corrupt", "leaf_key", "has a certificate that does not parse"),
        ("nosan_chain", "leaf_key", "has no Subject Alternative Name"),
        ("missing", "leaf_key", "no such file:"),
        ("leaf_key", "leaf_key", "contains a private key"),
    ],
)
def test_dry_run_refusals(
    pki: Pki, stubs: Stubs, chain_name: str, key_name: str, message: str
) -> None:
    chain = pki.directory / "missing.pem" if chain_name == "missing" else getattr(pki, chain_name)
    result = dry(chain, getattr(pki, key_name), stubs)
    assert result.returncode == 1, result.stdout
    assert "install-cert: " in result.stderr
    assert message in result.stderr
    assert stubs.calls() == []


def test_dry_run_order_message_names_both_certificates(pki: Pki, stubs: Stubs) -> None:
    result = dry(pki.with_root, pki.leaf_key, stubs)
    assert (
        "certificate 1 in "
        f"{pki.with_root} was not issued by certificate 2; the order is the server's "
        "certificate, then each intermediate"
    ) in result.stderr


def test_dry_run_refuses_an_option_as_host(pki: Pki, stubs: Stubs) -> None:
    result = dry(pki.fullchain, pki.leaf_key, stubs, host="-oProxyCommand=x")
    assert result.returncode == 1
    assert "not an ssh host" in result.stderr
    assert stubs.calls() == []


def test_usage(stubs: Stubs) -> None:
    bad = run_script(stubs=stubs)
    assert bad.returncode == 64
    assert "Usage:" in bad.stderr
    helped = run_script("--help", stubs=stubs)
    assert helped.returncode == 0
    assert "Usage:" in helped.stdout
    assert "--dry-run" in helped.stdout
    assert "set -euo" not in helped.stdout
    assert "tls /etc/caddy/certs/fullchain.pem /etc/caddy/certs/privkey.pem" in helped.stdout
    assert stubs.calls() == []


# --- the whole run, with the remote half against a fake root ---------------------------------


def _own_cert_caddyfile(stubs: Stubs, source: Path = OWN_CERT_CADDYFILE) -> Path:
    target = stubs.root / "etc" / "caddy" / "Caddyfile"
    target.parent.mkdir(parents=True)
    target.write_text(source.read_text().replace("/etc/caddy", f"{stubs.root}/etc/caddy"))
    return target


def full(
    pki: Pki, stubs: Stubs, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {"STUB_MKTEMP": MKTEMP}
    env.update(env_extra or {})
    return run_script(HOST, str(pki.fullchain), str(pki.leaf_key), stubs=stubs, env_extra=env)


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_full_run_installs_the_files(pki: Pki, stubs: Stubs) -> None:
    caddyfile = _own_cert_caddyfile(stubs)
    result = full(pki, stubs)
    assert result.returncode == 0, result.stderr
    assert "Caddyfile valid" in result.stdout
    assert "caddy reloaded" in result.stdout
    assert result.stderr == ""
    certs = stubs.root / "etc" / "caddy" / "certs"
    assert _mode(certs) == 0o750
    assert _mode(certs / "fullchain.pem") == 0o644
    assert _mode(certs / "privkey.pem") == 0o640
    assert (certs / "fullchain.pem").read_bytes() == pki.fullchain.read_bytes()
    assert (certs / "privkey.pem").read_bytes() == pki.leaf_key.read_bytes()
    installs = stubs.named("install")
    assert len(installs) == 3
    for call in installs:
        assert call[call.index("-o") + 1] == "root"
        assert call[call.index("-g") + 1] == "caddy"
    validate = stubs.named("caddy")
    assert validate == [["caddy", "validate", "--adapter", "caddyfile", "--config", str(caddyfile)]]
    reloads = [c for c in stubs.named("systemctl") if c[1:] == ["reload", "caddy"]]
    assert len(reloads) == 1
    top = stubs.top_level()
    assert [c[0] for c in top[:3]] == ["ssh", "scp", "scp"]
    assert top[3][:2] == ["ssh", "-t"]
    assert top[-1][0] == "ssh"
    assert top[-1][-1] == f"rm -rf '{MKTEMP}'"
    assert not (stubs.root / "tmp" / "tmp.EarmarkTest1").exists()


def test_full_run_never_prints_the_key(pki: Pki, stubs: Stubs) -> None:
    _own_cert_caddyfile(stubs)
    result = full(pki, stubs)
    assert result.returncode == 0, result.stderr
    body = [
        line
        for line in pki.leaf_key.read_text().splitlines()
        if line and not line.startswith("-----")
    ]
    assert body
    logged = "\n".join(" ".join(c) for c in stubs.calls())
    for text in (result.stdout, result.stderr, logged):
        assert "PRIVATE KEY" not in text
        for line in body:
            assert line not in text
    key_calls = [c for c in stubs.calls() if any(str(pki.leaf_key) in a for a in c)]
    assert len(key_calls) == 1
    assert key_calls[0][0] == "scp"
    assert key_calls[0] == stubs.named("scp")[1]


def test_full_run_without_caddyfile(pki: Pki, stubs: Stubs) -> None:
    result = full(pki, stubs)
    assert result.returncode == 0, result.stderr
    assert "Caddyfile yet" in result.stdout
    assert (stubs.root / "etc" / "caddy" / "certs" / "fullchain.pem").exists()
    assert stubs.named("caddy") == []


def test_full_run_with_caddy_stopped(pki: Pki, stubs: Stubs) -> None:
    _own_cert_caddyfile(stubs)
    result = full(pki, stubs, {"STUB_ACTIVE_RC": "3"})
    assert result.returncode == 0, result.stderr
    assert "caddy is not running; start it with: sudo systemctl start caddy" in result.stdout
    assert [c for c in stubs.named("systemctl") if "reload" in c] == []


def test_full_run_validate_failure(pki: Pki, stubs: Stubs) -> None:
    _own_cert_caddyfile(stubs)
    result = full(pki, stubs, {"STUB_VALIDATE_RC": "1"})
    assert result.returncode != 0
    assert "does not validate; caddy was not reloaded" in result.stderr
    assert [c for c in stubs.named("systemctl") if "reload" in c] == []
    assert not (stubs.root / "tmp" / "tmp.EarmarkTest1").exists()


def test_full_run_warns_without_force(pki: Pki, stubs: Stubs) -> None:
    _own_cert_caddyfile(stubs)
    result = full(
        pki,
        stubs,
        {"STUB_EXEC_RELOAD": "{ argv[]=/usr/bin/caddy reload --config /etc/caddy/Caddyfile }"},
    )
    assert result.returncode == 0, result.stderr
    assert "reloads without --force" in result.stderr
    assert "sudo systemctl restart caddy" in result.stderr
    assert [c for c in stubs.named("systemctl") if c[1:] == ["reload", "caddy"]]
    assert "caddy reloaded" in result.stdout


def test_full_run_warns_when_caddyfile_ignores_the_files(pki: Pki, stubs: Stubs) -> None:
    _own_cert_caddyfile(stubs, DNS_CADDYFILE)
    result = full(pki, stubs)
    assert result.returncode == 0, result.stderr
    assert "does not use" in result.stderr
    assert "fullchain.pem" in result.stderr


def test_full_run_without_caddy_group(pki: Pki, stubs: Stubs) -> None:
    result = full(pki, stubs, {"STUB_GETENT_RC": "2"})
    assert result.returncode != 0
    assert "no caddy group" in result.stderr
    assert stubs.named("install") == []
    assert not (stubs.root / "tmp" / "tmp.EarmarkTest1").exists()


def test_failed_copy_removes_the_remote_directory(pki: Pki, stubs: Stubs) -> None:
    result = full(pki, stubs, {"STUB_FAIL": "scp"})
    assert result.returncode != 0
    assert not [c for c in stubs.top_level() if c[:2] == ["ssh", "-t"]]
    assert stubs.top_level()[-1][-1] == f"rm -rf '{MKTEMP}'"
    assert not (stubs.root / "tmp" / "tmp.EarmarkTest1").exists()


@pytest.mark.parametrize(
    "answer",
    ["/home/op/tmp.1", "/tmp/a'b", "/tmp/", "/tmp/../etc", "banner\n/tmp/tmp.x", ""],
)
def test_unexpected_remote_directory_is_refused(pki: Pki, stubs: Stubs, answer: str) -> None:
    result = full(pki, stubs, {"STUB_MKTEMP": answer})
    assert result.returncode == 1
    assert "unexpected temporary directory" in result.stderr
    calls = stubs.calls()
    assert len(calls) == 1
    assert calls[0][0] == "ssh"


# --- the script as a file ----------------------------------------------------------------------


def test_script_is_executable_bash() -> None:
    text = SCRIPT.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert os.access(SCRIPT, os.X_OK)
    assert "\nset -euo pipefail\n" in text
    assert subprocess.run([BASH, "-n", str(SCRIPT)], check=False).returncode == 0


NEWER_BASH = [
    r"declare -A",
    r"local -n",
    r"mapfile",
    r"readarray",
    r"\$\{\w+(,,|\^\^|,|\^)\}",
    r"\|&",
    r"&>>",
    r";;&",
    r"coproc",
    r"\$\{\w+@",
    r"EPOCH(SECONDS|REALTIME)",
    r"wait -n",
    r"\w+=\(",
    r"\[@\]",
    r"sed -i",
    r"grep -P",
    r"readlink -f",
    r"date -d",
    r"gensub",
    r"strftime",
    r"asort",
]


@pytest.mark.parametrize("pattern", NEWER_BASH)
def test_script_avoids_newer_bash(pattern: str) -> None:
    assert re.search(pattern, SCRIPT.read_text()) is None


LIBRESSL_OPTIONS = {
    "x509": {
        "-in",
        "-noout",
        "-pubkey",
        "-subject",
        "-issuer",
        "-serial",
        "-enddate",
        "-text",
        "-checkend",
    },
    "pkey": {"-in", "-noout", "-pubout", "-passin"},
    "crl2pkcs7": {"-nocrl", "-certfile"},
    "pkcs7": {"-print_certs", "-noout"},
}


def test_script_uses_libressl_options() -> None:
    local_part = SCRIPT.read_text().split('ssh -t "$host"')[0]
    seen: set[str] = set()
    for line in local_part.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for match in re.finditer(r"openssl (x509|pkey|crl2pkcs7|pkcs7)\b([^|)<>]*)", line):
            sub = match[1]
            seen.add(sub)
            options = set(re.findall(r"(?<!\S)-\w+", match[2]))
            assert options <= LIBRESSL_OPTIONS[sub], (sub, options - LIBRESSL_OPTIONS[sub])
    assert seen == set(LIBRESSL_OPTIONS)
    assert re.search(r"-ext\b", SCRIPT.read_text()) is None


def test_every_pkey_call_sets_passin() -> None:
    lines = [x for x in SCRIPT.read_text().splitlines() if "openssl pkey" in x]
    assert len(lines) == 2
    for line in lines:
        assert "-passin pass:" in line


def test_script_calls_ssh_by_name() -> None:
    text = SCRIPT.read_text()
    for path in ("/bin/ssh", "/bin/scp", "/usr/bin/ssh", "/usr/bin/scp"):
        assert path not in text
