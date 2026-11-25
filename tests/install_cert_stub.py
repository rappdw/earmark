"""Stands in for ssh, scp, sudo, install, getent, caddy and systemctl in install-cert tests.

Not a test module: tests/test_install_cert.py writes one wrapper per name that runs this
file with the name as its first argument. Remote paths (/tmp, /var/tmp, /etc/caddy) are
mapped into STUB_ROOT, so the remote half of deploy/caddy/install-cert.sh runs for real
against a fake root. Nothing here opens a socket.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

UNEXPECTED = 97
REMOTE_PATH = re.compile(r"(?<![\w/.])(/tmp/|/var/tmp/|/etc/caddy)")
MKTEMP_COMMAND = "umask 077 && mktemp -d"
SAFE_MKTEMP = re.compile(r"/tmp/[A-Za-z0-9.]+")
DEFAULT_MKTEMP = "/tmp/tmp.EarmarkTest1"
DEFAULT_EXEC_RELOAD = "{ argv[]=/usr/bin/caddy reload --config /etc/caddy/Caddyfile --force }"


def _root() -> str:
    return os.environ["STUB_ROOT"]


def _map(text: str) -> str:
    root = _root()
    return REMOTE_PATH.sub(lambda m: root + m.group(1), text)


def _rc(variable: str) -> int:
    return int(os.environ.get(variable, "0"))


def _ssh(args: list[str]) -> int:
    if len(args) == 3 and args[0] == "-t":
        command = args[2]
    elif len(args) == 2:
        command = args[1]
    else:
        return UNEXPECTED
    if command == MKTEMP_COMMAND:
        answer = os.environ.get("STUB_MKTEMP", DEFAULT_MKTEMP)
        if SAFE_MKTEMP.fullmatch(answer):
            Path(_map(answer)).mkdir(parents=True, mode=0o700, exist_ok=True)
        sys.stdout.write(answer + "\n")
        return 0
    bash = shutil.which("bash")
    assert bash is not None
    return subprocess.run([bash, "-c", _map(command)], check=False).returncode


def _scp(args: list[str]) -> int:
    if len(args) != 4 or args[:2] != ["-q", "--"]:
        return UNEXPECTED
    _, _, remote = args[3].partition(":")
    target = Path(_map(remote))
    shutil.copyfile(args[2], target)
    return 0


def _sudo(args: list[str]) -> int:
    if args[:2] == ["-u", "caddy"]:
        args = args[2:]
    if not args:
        return UNEXPECTED
    return subprocess.run(args, check=False).returncode


def _install(args: list[str]) -> int:
    make_dir = False
    mode = 0o755
    paths: list[str] = []
    rest = iter(args)
    for arg in rest:
        if arg == "-d":
            make_dir = True
        elif arg == "-m":
            mode = int(next(rest), 8)
        elif arg in ("-o", "-g"):
            next(rest)
        else:
            paths.append(arg)
    if make_dir:
        for path in paths:
            Path(path).mkdir(parents=True, exist_ok=True)
            Path(path).chmod(mode)
        return 0
    source, target = paths
    shutil.copyfile(source, target)
    Path(target).chmod(mode)
    return 0


def _systemctl(args: list[str]) -> int:
    if args and args[0] == "is-active":
        return _rc("STUB_ACTIVE_RC")
    if args and args[0] == "show":
        sys.stdout.write(os.environ.get("STUB_EXEC_RELOAD", DEFAULT_EXEC_RELOAD) + "\n")
        return 0
    return 0


def main(argv: list[str]) -> int:
    name, args = argv[0], argv[1:]
    with Path(os.environ["STUB_LOG"]).open("a") as log:
        log.write(json.dumps([name, *args]) + "\n")
    if os.environ.get("STUB_FAIL") == name:
        return 1
    if name == "ssh":
        return _ssh(args)
    if name == "scp":
        return _scp(args)
    if name == "sudo":
        return _sudo(args)
    if name == "install":
        return _install(args)
    if name == "getent":
        return _rc("STUB_GETENT_RC")
    if name == "caddy":
        return _rc("STUB_VALIDATE_RC")
    if name == "systemctl":
        return _systemctl(args)
    return UNEXPECTED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
