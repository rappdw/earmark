"""Static checks of the container image, compose file, systemd units and Caddy example.

Nothing here builds or runs a container: the sandbox has no Docker. CI builds the image.
"""

import ipaddress
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from earmark.config import SMTP_TIMEOUT_MAX_S, WORKER_STOP_MARGIN_S, Settings, env_var
from tests.doc_helpers import ROOT, read_doc
from tests.dockerfile_helpers import (
    PINNED_IMAGE,
    external_froms,
    froms,
    instructions,
    runtime_stage_lines,
)

DOCKERFILE = (ROOT / "Dockerfile").read_text()
ALLOWED_CONTEXT = {"pyproject.toml", "uv.lock", ".python-version", "README.md", "src/"}
UNITS = {"api": "earmark-api.service", "worker": "earmark-worker.service"}


def _instructions(name: str) -> list[str]:
    return instructions(DOCKERFILE, name)


def _froms() -> list[tuple[str, str | None]]:
    return froms(DOCKERFILE)


def _external_froms() -> list[str]:
    return external_froms(DOCKERFILE)


def _pyproject() -> dict[str, Any]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


# --- Dockerfile ---------------------------------------------------------------------------


def test_dockerfile_images_pinned_by_digest() -> None:
    images = _external_froms()
    assert len(images) == 3
    for image in images:
        assert PINNED_IMAGE.match(image), image


def test_dockerfile_python_matches_project() -> None:
    python = [i for i, _ in _froms() if i.startswith("python:")]
    assert len(python) == 2
    assert python[0] == python[1]
    version = (ROOT / ".python-version").read_text().strip()
    assert python[0].startswith(f"python:{version}.")
    assert [s for _, s in _froms()] == ["uv", "build", "runtime"]


def test_dockerfile_uv_matches_build_backend() -> None:
    [uv_image] = [i for i in _external_froms() if "astral-sh/uv" in i]
    tag = uv_image.split(":", 1)[1].split("@", 1)[0]
    minor = ".".join(tag.split(".")[:2])
    requires = _pyproject()["build-system"]["requires"]
    [bound] = [r for r in requires if r.startswith("uv_build")]
    assert f">={minor}." in bound or f">={minor}," in bound
    assert f"<{minor.split('.')[0]}.{int(minor.split('.')[1]) + 1}.0" in bound
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert f">={minor}." in ci
    assert f"<{minor.split('.')[0]}.{int(minor.split('.')[1]) + 1}" in ci


def test_dockerfile_uv_sync_locked_no_dev() -> None:
    syncs = [line for line in _instructions("RUN") if "uv sync" in line]
    assert len(syncs) == 2
    for sync in syncs:
        assert "--locked" in sync
        assert "--no-dev" in sync
    assert "--no-editable" in syncs[-1]


def test_dockerfile_default_extras_exist() -> None:
    [arg] = [line for line in _instructions("ARG") if line.startswith("ARG EXTRAS=")]
    default = arg.split("=", 1)[1].strip('"')
    extras = _pyproject()["project"]["optional-dependencies"]
    assert default == "transcribe youtube"
    assert set(default.split()) <= set(extras)


DOCKERFILES = {
    "Dockerfile": DOCKERFILE,
    "Dockerfile.gpu": (ROOT / "Dockerfile.gpu").read_text(),
}
BOTH = pytest.mark.parametrize("text", list(DOCKERFILES.values()), ids=list(DOCKERFILES))


@BOTH
def test_dockerfile_runtime_user_not_root(text: str) -> None:
    assert froms(text)[-1][1] == "runtime"
    users = instructions(text, "USER")
    assert users
    last = users[-1].split()[1]
    assert last.startswith("earmark")
    assert last not in {"root", "0"}
    assert 'test "$UID" -ne 0' in text
    assert 'test "$GID" -ne 0' in text


@BOTH
def test_dockerfile_entrypoint(text: str) -> None:
    assert instructions(text, "ENTRYPOINT") == ['ENTRYPOINT ["earmark"]']
    assert instructions(text, "CMD") == ['CMD ["serve"]']


@BOTH
def test_dockerfile_no_volume_or_healthcheck(text: str) -> None:
    assert instructions(text, "VOLUME") == []
    assert instructions(text, "HEALTHCHECK") == []


@BOTH
def test_dockerfile_installs_ca_certificates(text: str) -> None:
    assert "ca-certificates" in text


@BOTH
def test_dockerfile_environment(text: str) -> None:
    runtime = runtime_stage_lines(text)
    env = " ".join(line for line in runtime if line.split()[0].upper() == "ENV")
    for expected in (
        "EARMARK_HOST=0.0.0.0",
        "EARMARK_DATA_DIR=/data",
        "HF_HOME=/data/huggingface",
    ):
        assert expected in env
    assert set(re.findall(r"\b(EARMARK_[A-Z_]+)=", env)) == {"EARMARK_HOST", "EARMARK_DATA_DIR"}


@BOTH
def test_dockerfile_runtime_dirs(text: str) -> None:
    runs = [line for line in runtime_stage_lines(text) if line.split()[0].upper() == "RUN"]
    [run] = [line for line in runs if "install -d" in line]
    assert run.endswith("install -d -o earmark -g earmark -m 0750 /data")


@BOTH
def test_dockerfile_disables_hf_telemetry(text: str) -> None:
    env = " ".join(line for line in runtime_stage_lines(text) if line.split()[0].upper() == "ENV")
    assert "HF_HUB_DISABLE_TELEMETRY=1" in env.split()


# --- .dockerignore ------------------------------------------------------------------------


def _dockerignore_rules() -> list[str]:
    lines = (ROOT / ".dockerignore").read_text().splitlines()
    return [x.strip() for x in lines if x.strip() and not x.startswith("#")]


def test_dockerignore_is_allow_list() -> None:
    rules = _dockerignore_rules()
    assert rules[0] == "*"
    assert {r[1:] for r in rules if r.startswith("!")} == ALLOWED_CONTEXT


@BOTH
def test_dockerfile_copies_only_allowed_paths(text: str) -> None:
    copies = [c for c in instructions(text, "COPY") if "--from" not in c]
    assert copies
    for line in copies:
        *sources, destination = line.split()[1:]
        assert destination
        for source in sources:
            normalised = source.removeprefix("./")
            allowed = ALLOWED_CONTEXT | {s.rstrip("/") for s in ALLOWED_CONTEXT}
            assert normalised in allowed, source


# --- compose.yaml -------------------------------------------------------------------------

COMPOSE_TEXT = (ROOT / "compose.yaml").read_text()
COMPOSE: dict[str, Any] = yaml.safe_load(COMPOSE_TEXT)
SERVICES: dict[str, dict[str, Any]] = COMPOSE["services"]


def test_compose_services() -> None:
    assert set(SERVICES) == {"api", "worker"}
    api, worker = SERVICES["api"], SERVICES["worker"]
    assert api["command"] == ["serve"]
    assert worker["command"] == ["worker"]
    assert api["image"] == "earmark:local"
    assert worker["image"] == "earmark:gpu"
    assert api["build"]["context"] == "." == worker["build"]["context"]
    assert "dockerfile" not in api["build"]
    assert worker["build"]["dockerfile"] == "Dockerfile.gpu"
    assert (ROOT / "Dockerfile.gpu").is_file()
    assert set(api["build"]["args"]) == {"EXTRAS", "UID", "GID"}
    assert set(worker["build"]["args"]) == {"UID", "GID"}
    for arg in ("UID", "GID"):
        assert api["build"]["args"][arg] == worker["build"]["args"][arg]


def test_compose_api_publishes_loopback_by_default() -> None:
    assert SERVICES["api"]["ports"] == ["${API_BIND:-127.0.0.1:8787}:8787"]


def test_compose_worker_publishes_nothing() -> None:
    assert "ports" not in SERVICES["worker"]
    assert "0.0.0.0:" not in COMPOSE_TEXT


def test_compose_shared_data() -> None:
    for name in ("api", "worker"):
        assert SERVICES[name]["volumes"] == ["data:/data"]
    assert set(COMPOSE["volumes"]) == {"data"}


def test_compose_builds_transcribe_extra() -> None:
    assert SERVICES["api"]["build"]["args"]["EXTRAS"] == "${EXTRAS:-transcribe youtube}"
    assert "EXTRAS" not in SERVICES["worker"]["build"]["args"]  # Dockerfile.gpu always has it


def test_compose_forces_container_paths() -> None:
    shared = {
        "EARMARK_DATA_DIR",
        "EARMARK_HOST",
        "EARMARK_PORT",
        "EARMARK_ASR_MODEL_DIR",
        "EARMARK_API_URL",
        "HF_HUB_DISABLE_TELEMETRY",
    }
    worker_only = {"EARMARK_ASR_DEVICE", "EARMARK_ASR_COMPUTE_TYPE"}
    for name in ("api", "worker"):
        environment = SERVICES[name]["environment"]
        assert set(environment) == shared | (worker_only if name == "worker" else set())
        assert environment["EARMARK_DATA_DIR"] == "/data"
        assert environment["EARMARK_HOST"] == "0.0.0.0"  # noqa: S104 - the in-container bind
        assert environment["EARMARK_PORT"] == "8787"
        assert environment["EARMARK_ASR_MODEL_DIR"] == ""
        assert environment["EARMARK_API_URL"] == ""
        assert SERVICES[name]["env_file"] == ".env"
    worker_env = SERVICES["worker"]["environment"]
    assert worker_env["EARMARK_ASR_DEVICE"] == "cuda"
    assert worker_env["EARMARK_ASR_COMPUTE_TYPE"] == "${EARMARK_ASR_COMPUTE_TYPE:-float16}"


def test_compose_worker_stop_grace() -> None:
    grace = SERVICES["worker"]["stop_grace_period"]
    match = re.fullmatch(r"(\d+)(s|m)", grace)
    assert match
    seconds = int(match[1]) * (60 if match[2] == "m" else 1)
    assert seconds >= 60


def test_smtp_timeout_below_worker_stop_grace() -> None:
    """A SIGTERM during delivery waits for the SMTP session, so the grace must cover it."""
    needed = SMTP_TIMEOUT_MAX_S + WORKER_STOP_MARGIN_S
    match = re.fullmatch(r"(\d+)(s|m)", SERVICES["worker"]["stop_grace_period"])
    assert match
    assert int(match[1]) * (60 if match[2] == "m" else 1) >= needed
    unit = _parse_unit(ROOT / "deploy" / "systemd" / UNITS["worker"])
    [timeout] = unit["Service"]["TimeoutStopSec"]
    assert int(timeout) >= needed
    Settings(_env_file=None, smtp_timeout_s=SMTP_TIMEOUT_MAX_S)
    with pytest.raises(ValueError, match="smtp_timeout_s"):
        Settings(_env_file=None, smtp_timeout_s=SMTP_TIMEOUT_MAX_S + 1)


def test_compose_hardening() -> None:
    for name in ("api", "worker"):
        service = SERVICES[name]
        assert service["init"] is True
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["cap_drop"] == ["ALL"]
    for name, service in SERVICES.items():
        assert not service.get("privileged"), name
        assert "network_mode" not in service, name
        assert "pid" not in service, name
        assert service.get("ipc") != "host", name


def test_compose_worker_gpu() -> None:
    assert SERVICES["worker"]["gpus"] == "all"
    assert "gpus" not in SERVICES["api"]
    for name, service in SERVICES.items():
        for key in ("profiles", "deploy", "runtime"):
            assert key not in service, (name, key)
    assert "asr" not in SERVICES
    lowered = COMPOSE_TEXT.lower()
    for banned in ("vllm", "asr-models", "profiles"):
        assert banned not in lowered, banned


def test_compose_earmark_variables_are_settings() -> None:
    known = {env_var(f) for f in Settings.model_fields}
    used = set(re.findall(r"\$\{(EARMARK_[A-Z0-9_]+)", COMPOSE_TEXT))
    for service in SERVICES.values():
        environment = service.get("environment", {})
        used |= {k for k in environment if k.startswith("EARMARK_")}
    assert used
    assert used <= known, used - known


def _compose_only_variables() -> set[str]:
    names = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", COMPOSE_TEXT))
    return {n for n in names if not n.startswith("EARMARK_")}


def test_compose_only_variables_documented() -> None:
    names = _compose_only_variables()
    assert names == {
        "API_BIND",
        "APP_UID",
        "APP_GID",
        "EXTRAS",
        "HF_HUB_DISABLE_TELEMETRY",
    }
    deploy = read_doc("docs/deploy.md")
    example = (ROOT / ".env.example").read_text()
    for name in names:
        assert re.search(rf"^\| `{name}` \|", deploy, re.MULTILINE), name
        assert re.search(rf"^# {name}=", example, re.MULTILINE), name


@pytest.mark.parametrize("name", ["api", "worker"])
def test_compose_disables_hf_telemetry(name: str) -> None:
    value = SERVICES[name]["environment"]["HF_HUB_DISABLE_TELEMETRY"]
    assert value == "${HF_HUB_DISABLE_TELEMETRY:-1}"
    assert value.startswith("${HF_HUB_DISABLE_TELEMETRY:")  # interpolated, so .env can override


# --- systemd units ------------------------------------------------------------------------


def _parse_unit(path: Path) -> dict[str, dict[str, list[str]]]:
    sections: dict[str, dict[str, list[str]]] = {}
    current = ""
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, {})
            continue
        key, _, value = line.partition("=")
        sections[current].setdefault(key, []).append(value)
    return sections


@pytest.mark.parametrize(("role", "filename"), sorted(UNITS.items()))
def test_systemd_units(role: str, filename: str) -> None:
    unit = _parse_unit(ROOT / "deploy" / "systemd" / filename)
    service = unit["Service"]
    [exec_start] = service["ExecStart"]
    assert exec_start.startswith("/srv/earmark/.venv/bin/earmark ")
    assert exec_start.endswith(f" {'serve' if role == 'api' else 'worker'}")
    assert service["User"] == ["earmark"]
    assert service["KillSignal"] == ["SIGTERM"]
    assert service["Restart"] == ["on-failure"]
    assert service["NoNewPrivileges"] == ["yes"]
    assert service["ProtectSystem"] == ["strict"]
    assert service["WorkingDirectory"] == ["/srv/earmark"]
    write_paths = " ".join(service["ReadWritePaths"]).split()
    assert write_paths == ["/srv/earmark/data"]
    [timeout] = service["TimeoutStopSec"]
    if role == "worker":
        assert int(timeout) >= 60
    assert unit["Install"]["WantedBy"] == ["multi-user.target"]


# --- Caddy example ------------------------------------------------------------------------

CADDYFILE = (ROOT / "deploy" / "caddy" / "Caddyfile").read_text()


def _caddy_code() -> list[str]:
    return [
        x.strip() for x in CADDYFILE.splitlines() if x.strip() and not x.strip().startswith("#")
    ]


def test_caddyfile_example() -> None:
    code = _caddy_code()
    site_blocks = [x for x in CADDYFILE.splitlines() if x.endswith(" {") and not x.startswith("\t")]
    assert len(site_blocks) == 1
    address = site_blocks[0][:-2].strip()
    assert re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}", address), address
    [bind] = [x.split()[1] for x in code if x.startswith("bind ")]
    parsed = ipaddress.ip_address(bind)
    assert parsed.version == 4
    assert parsed.is_private
    assert str(parsed) != "0.0.0.0"  # noqa: S104 - checking the example never binds everything
    assert "dns cloudflare {env.CF_API_TOKEN}" in code
    tls_start = code.index("tls {")
    assert code.index("dns cloudflare {env.CF_API_TOKEN}") > tls_start
    assert "reverse_proxy 127.0.0.1:8787" in code
    assert "auto_https disable_redirects" in code
    assert "http://" not in CADDYFILE.replace("https://", "")
    compose_bind = COMPOSE["services"]["api"]["ports"][0]
    assert "127.0.0.1:8787" in compose_bind


def test_caddyfile_own_cert_example() -> None:
    text = (ROOT / "deploy" / "caddy" / "Caddyfile.own-cert").read_text()
    cert_dir = re.search(
        r"^CERT_DIR=(\S+)$", (ROOT / "deploy" / "caddy" / "install-cert.sh").read_text(), re.M
    )
    assert cert_dir
    lines = text.splitlines()
    code = [x.strip() for x in lines if x.strip() and not x.strip().startswith("#")]
    site_blocks = [x for x in lines if x.endswith(" {") and not x.startswith("\t")]
    assert len(site_blocks) == 1
    address = re.fullmatch(r"earmark\.example\.com:(\d+) \{", site_blocks[0])
    assert address
    assert 1024 <= int(address[1]) <= 65535
    assert int(address[1]) != 8787
    [bind] = [x.split()[1] for x in code if x.startswith("bind ")]
    parsed = ipaddress.ip_address(bind)
    assert parsed.version == 4
    assert parsed.is_private
    assert str(parsed) != "0.0.0.0"  # noqa: S104 - checking the example never binds everything
    assert f"tls {cert_dir[1]}/fullchain.pem {cert_dir[1]}/privkey.pem" in code
    assert "reverse_proxy 127.0.0.1:8787" in code
    assert "auto_https disable_redirects" in code
    assert not any(x.startswith("dns ") for x in code)
    assert "{env." not in text
    assert "CF_" not in text
    assert any(x.startswith("#") and "443" in x for x in (y.strip() for y in lines))
    assert "http://" not in text.replace("https://", "")


def test_proxy_token_stays_out_of_earmark() -> None:
    files = [
        ROOT / ".env.example",
        ROOT / "compose.yaml",
        ROOT / "Dockerfile",
        ROOT / "Dockerfile.gpu",
        *sorted((ROOT / "deploy" / "systemd").glob("*.service")),
        *sorted((ROOT / "src" / "earmark").rglob("*.py")),
    ]
    for path in files:
        text = path.read_text()
        assert re.search(r"\bCF_[A-Z_]*TOKEN|CLOUDFLARE", text, re.IGNORECASE) is None, path


# --- Hugging Face telemetry (step 21) -----------------------------------------------------

SERVICE_FILES = sorted((ROOT / "deploy" / "systemd").glob("*.service"))


def test_systemd_service_files_are_the_known_two() -> None:
    assert {p.name for p in SERVICE_FILES} == {"earmark-api.service", "earmark-worker.service"}


def test_systemd_dir_has_only_the_two_services() -> None:
    assert {p.name for p in (ROOT / "deploy" / "systemd").iterdir()} == {
        "earmark-api.service",
        "earmark-worker.service",
    }


@pytest.mark.parametrize("path", SERVICE_FILES, ids=lambda p: p.name)
def test_systemd_services_disable_hf_telemetry(path: Path) -> None:
    service = _parse_unit(path)["Service"]
    assert "HF_HUB_DISABLE_TELEMETRY=1" in " ".join(service.get("Environment", [])).split()


def test_deploy_files_leave_xet_logs_alone() -> None:
    files = [
        ROOT / "Dockerfile",
        ROOT / "Dockerfile.gpu",
        ROOT / "compose.yaml",
        *sorted((ROOT / "deploy" / "systemd").glob("*")),
    ]
    for path in files:
        text = path.read_text()
        for name in ("HF_XET_LOG_DEST", "RUST_LOG"):
            assert name not in text, (path.name, name)


# --- the GPU image (step 26) --------------------------------------------------------------


def test_env_example_leaves_compute_type_to_compose() -> None:
    example = (ROOT / ".env.example").read_text()
    assert re.search(r"^EARMARK_ASR_COMPUTE_TYPE=", example, re.MULTILINE) is None
    assert "ASR_GPU_MEMORY_UTILIZATION" not in example


def _uv_run_without_no_sync(command: str) -> bool:
    return bool(re.search(r"\buv\s+run\b", command)) and "--no-sync" not in command


def test_deploy_runtime_never_syncs() -> None:
    commands: list[str] = []
    for text in DOCKERFILES.values():
        for name in ("ENTRYPOINT", "CMD"):
            commands += instructions(text, name)
    for service in SERVICES.values():
        for key in ("command", "entrypoint", "healthcheck"):
            if key in service:
                commands.append(str(service[key]))
    for path in SERVICE_FILES:
        commands += _parse_unit(path)["Service"]["ExecStart"]
    assert commands
    for command in commands:
        assert not re.search(r"\buv\s+sync\b", command), command
        assert not _uv_run_without_no_sync(command), command
